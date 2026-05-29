// app/components/VoiceCallModal.tsx
// 改进版：WAV无损 + 置信度 + 严格VAD + 沉默检测（含静音状态）
import axios from 'axios';
import { Audio } from 'expo-av';
import * as FileSystem from 'expo-file-system/legacy';
import React, { useEffect, useRef, useState } from 'react';
import {
  Dimensions,
  Modal,
  Platform,
  ScrollView,
  StyleSheet,
  Text,
  TouchableOpacity,
  View,
} from 'react-native';
import { Message } from '../app/(tabs)/chat';
import { C, SERVER_URL, nowTime } from '../constants/theme';

const { width } = Dimensions.get('window');

// ───── VAD 参数 ─────
const SPEECH_THRESHOLD     = -32;
const SPEECH_MIN_MS        = 500;
const SILENCE_TRIGGER_MS   = 2200;   // ★ 从1800→2200，给更长停顿避免截断
const POLL_INTERVAL_MS     = 120;
const MIN_AUDIO_SIZE       = 10000;  // ★ WAV格式更大，提到10KB
const CONSECUTIVE_FRAMES   = 3;

// ───── 沉默主动开口 ─────
const IDLE_CHECK_INTERVAL  = 5000;
const IDLE_FIRST_MS        = 25000;
const IDLE_SECOND_MS       = 50000;
const IDLE_THIRD_MS        = 90000;
const MAX_PROACTIVE_TIMES  = 4;

interface Props {
  userId: string;
  onClose: () => void;
  onAddMessages: (msgs: Message[]) => void;
}

interface CallMsg {
  id: string;
  role: 'user' | 'gojo';
  jp?: string;
  zh: string;
  time: string;
}

type Phase =
  | 'connecting'
  | 'listening'
  | 'speaking'
  | 'processing'
  | 'responding'
  | 'paused'
  | 'ended';

export default function VoiceCallModal({ userId, onClose, onAddMessages }: Props) {
  const [phase, setPhase]         = useState<Phase>('connecting');
  const [callMsgs, setCallMsgs]   = useState<CallMsg[]>([]);
  const [subtitle, setSubtitle]   = useState('');
  const [duration, setDuration]   = useState(0);
  const [isSpeaker, setIsSpeaker] = useState(true);
  const [dbLevel, setDbLevel]     = useState(-160);
  const [debugText, setDebugText] = useState('');

  const recordingRef    = useRef<Audio.Recording | null>(null);
  const currentSoundRef = useRef<Audio.Sound | null>(null);
  const pollTimerRef    = useRef<ReturnType<typeof setInterval> | null>(null);
  const callTimerRef    = useRef<ReturnType<typeof setInterval> | null>(null);
  const idleTimerRef    = useRef<ReturnType<typeof setInterval> | null>(null);
  const scrollRef       = useRef<ScrollView>(null);
  const activeRef       = useRef(true);

  const phaseRef             = useRef<Phase>('connecting');
  const speechStartRef       = useRef<number | null>(null);
  const silenceStartRef      = useRef<number | null>(null);
  const consecutiveSpeechRef = useRef(0);
  const speechConfirmedRef   = useRef(false);

  const lastActiveTimeRef = useRef<number>(Date.now());
  const proactiveCountRef = useRef(0);

  const setPhaseSync = (p: Phase) => {
    phaseRef.current = p;
    setPhase(p);
  };

  useEffect(() => {
    callTimerRef.current = setInterval(() => setDuration(d => d + 1), 1000);
    return () => { if (callTimerRef.current) clearInterval(callTimerRef.current); };
  }, []);

  useEffect(() => {
    const init = async () => {
      try {
        const { status } = await Audio.requestPermissionsAsync();
        if (status !== 'granted') {
          alert('需要麦克风权限才能使用语音通话');
          onClose();
          return;
        }
        await new Promise(r => setTimeout(r, 800));
        setPhaseSync('listening');
        lastActiveTimeRef.current = Date.now();
        await startRecording();
        startIdleDetection();
      } catch (e) {
        console.warn('通话初始化失败', e);
        setDebugText(`⚠️ 初始化失败：${e}`);
      }
    };
    init();
    return () => { cleanup(); };
  }, []);

  useEffect(() => {
    Audio.setAudioModeAsync({
      allowsRecordingIOS: true, playsInSilentModeIOS: true,
      staysActiveInBackground: false, shouldDuckAndroid: true,
      playThroughEarpieceAndroid: !isSpeaker,
    }).catch(() => {});
  }, [isSpeaker]);

  const cleanup = async () => {
    activeRef.current = false;
    stopPolling();
    stopIdleDetection();
    try { await recordingRef.current?.stopAndUnloadAsync(); } catch {}
    try { await currentSoundRef.current?.unloadAsync(); } catch {}
    recordingRef.current = null;
    currentSoundRef.current = null;
    if (callTimerRef.current) clearInterval(callTimerRef.current);
    await Audio.setAudioModeAsync({
      allowsRecordingIOS: false, playsInSilentModeIOS: true,
      staysActiveInBackground: false, shouldDuckAndroid: true, playThroughEarpieceAndroid: false,
    }).catch(() => {});
  };

  // ───── 沉默检测（★ 支持 listening + paused 两种状态）─────
  const startIdleDetection = () => {
    stopIdleDetection();
    lastActiveTimeRef.current = Date.now();
    proactiveCountRef.current = 0;

    idleTimerRef.current = setInterval(async () => {
      if (!activeRef.current) return;

      // ★ 关键：paused（静音）状态也检测沉默
      const currentPhase = phaseRef.current;
      if (currentPhase !== 'listening' && currentPhase !== 'paused') return;

      const silenceMs = Date.now() - lastActiveTimeRef.current;
      const count = proactiveCountRef.current;
      if (count >= MAX_PROACTIVE_TIMES) return;

      let shouldTrigger = false;
      let mode: 'idle' | 'missed' = 'idle';

      if (count === 0 && silenceMs >= IDLE_FIRST_MS) {
        shouldTrigger = true; mode = 'idle';
      } else if (count === 1 && silenceMs >= IDLE_SECOND_MS) {
        shouldTrigger = true; mode = 'idle';
      } else if (count === 2 && silenceMs >= IDLE_THIRD_MS) {
        shouldTrigger = true; mode = 'missed';
      } else if (count >= 3 && silenceMs >= IDLE_THIRD_MS + 60000 * (count - 2)) {
        shouldTrigger = true; mode = 'missed';
      }

      if (shouldTrigger) {
        proactiveCountRef.current = count + 1;
        await triggerProactiveMessage(mode, Math.floor(silenceMs / 1000));
      }
    }, IDLE_CHECK_INTERVAL);
  };

  const stopIdleDetection = () => {
    if (idleTimerRef.current) {
      clearInterval(idleTimerRef.current);
      idleTimerRef.current = null;
    }
  };

  // ★ 主动消息：处理 paused 状态下的触发
  const triggerProactiveMessage = async (mode: 'idle' | 'missed', silenceSeconds: number) => {
    if (!activeRef.current) return;

    const wasPaused = phaseRef.current === 'paused';

    try {
      // 只在非 paused 状态才停录音（paused 时没有录音对象）
      if (!wasPaused) {
        stopPolling();
        try { await recordingRef.current?.stopAndUnloadAsync(); } catch {}
        recordingRef.current = null;
      }

      setPhaseSync('responding');

      const res = await axios.post(`${SERVER_URL}/chat/voice/proactive`, {
        user_id: userId, mode, silence_seconds: silenceSeconds,
      });
      if (!activeRef.current) return;

      const segments = res.data?.messages || [];
      for (const seg of segments) {
        if (!activeRef.current) break;
        const gojoMsg: CallMsg = {
          id: `idle_${Date.now()}`, role: 'gojo',
          jp: seg.jp, zh: seg.zh, time: nowTime(),
        };
        setCallMsgs(prev => [...prev, gojoMsg]);
        setSubtitle(seg.zh || seg.jp || '');
        scrollRef.current?.scrollToEnd({ animated: true });

        if (seg.audio_b64 && seg.audio_b64.length > 100) {
          await playAudio(seg.audio_b64);
        }
      }
    } catch (e) {
      console.warn('proactive error', e);
    } finally {
      setSubtitle('');
      lastActiveTimeRef.current = Date.now();  // ★ 悟说完话刷新时间
      if (activeRef.current) {
        // ★ 恢复到之前的状态
        if (wasPaused) {
          setPhaseSync('paused');
        } else {
          await resumeListening();
        }
      }
    }
  };

  // ───── 开始录音（★ WAV 无损格式，提高识别准确度）─────
  const startRecording = async () => {
    if (!activeRef.current) return;
    try {
      await Audio.setAudioModeAsync({
        allowsRecordingIOS: true, playsInSilentModeIOS: true,
        staysActiveInBackground: false, shouldDuckAndroid: false, playThroughEarpieceAndroid: false,
      });
      const { recording } = await Audio.Recording.createAsync({
        android: {
          extension: '.wav',                                // ★ WAV 无损
          outputFormat: Audio.AndroidOutputFormat.DEFAULT,
          audioEncoder: Audio.AndroidAudioEncoder.DEFAULT,
          sampleRate: 16000,
          numberOfChannels: 1,
          bitRate: 256000,                                  // ★ 提高码率
        },
        ios: {
          extension: '.wav',                                // ★ WAV 无损
          audioQuality: Audio.IOSAudioQuality.HIGH,         // ★ 高质量
          sampleRate: 16000,
          numberOfChannels: 1,
          bitRate: 256000,
          linearPCMBitDepth: 16,
          linearPCMIsBigEndian: false,
          linearPCMIsFloat: false,
        },
        web: {},
        isMeteringEnabled: true,
      });
      recordingRef.current = recording;
      speechStartRef.current = null;
      silenceStartRef.current = null;
      consecutiveSpeechRef.current = 0;
      speechConfirmedRef.current = false;
      startPolling();
    } catch (e) {
      console.warn('startRecording failed', e);
      setDebugText(`❌ 录音启动失败：${e}`);
      setTimeout(() => setDebugText(''), 4000);
    }
  };

  // ───── VAD 轮询（连续帧确认）─────
  const startPolling = () => {
    stopPolling();
    pollTimerRef.current = setInterval(async () => {
      if (!activeRef.current || !recordingRef.current) return;
      const currentPhase = phaseRef.current;
      if (currentPhase !== 'listening' && currentPhase !== 'speaking') return;

      try {
        const status = await recordingRef.current.getStatusAsync();
        if (!status.isRecording) return;

        const db = status.metering ?? -160;
        setDbLevel(db);
        const now = Date.now();
        const isTalking = db > SPEECH_THRESHOLD;

        if (isTalking) {
          consecutiveSpeechRef.current++;
          if (consecutiveSpeechRef.current >= CONSECUTIVE_FRAMES) {
            silenceStartRef.current = null;
            if (!speechConfirmedRef.current) {
              speechConfirmedRef.current = true;
              speechStartRef.current = now;
              setPhaseSync('speaking');
            }
          }
        } else {
          consecutiveSpeechRef.current = 0;
          if (speechConfirmedRef.current && currentPhase === 'speaking') {
            if (!silenceStartRef.current) silenceStartRef.current = now;
            const silenceDuration = now - silenceStartRef.current;
            const speechDuration = speechStartRef.current ? now - speechStartRef.current : 0;

            if (silenceDuration >= SILENCE_TRIGGER_MS && speechDuration >= SPEECH_MIN_MS) {
              stopPolling();
              setPhaseSync('processing');
              speechConfirmedRef.current = false;
              consecutiveSpeechRef.current = 0;
              await processRecording();
            }
          }
        }
      } catch {}
    }, POLL_INTERVAL_MS);
  };

  const stopPolling = () => {
    if (pollTimerRef.current) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  };

  // ───── 处理录音 ─────
  const processRecording = async () => {
    if (!activeRef.current) return;
    const rec = recordingRef.current;
    recordingRef.current = null;

    if (!rec) {
      setDebugText('❌ rec=null');
      setTimeout(() => setDebugText(''), 3000);
      await resumeListening();
      return;
    }

    try {
      let uri: string | null = null;
      try {
        await rec.stopAndUnloadAsync();
        uri = rec.getURI();
      } catch (stopErr) {
        setDebugText(`❌ stop：${stopErr}`);
        setTimeout(() => setDebugText(''), 4000);
        await resumeListening();
        return;
      }

      if (!uri) {
        setDebugText('❌ URI 为空');
        setTimeout(() => setDebugText(''), 3000);
        await resumeListening();
        return;
      }

      const info = await FileSystem.getInfoAsync(uri);
      const fileSize = info.exists && 'size' in info ? info.size : 0;

      if (fileSize < MIN_AUDIO_SIZE) {
        setDebugText(`⚠️ 音频太小(${fileSize}B)`);
        setTimeout(() => setDebugText(''), 1500);
        await resumeListening();
        return;
      }

      setDebugText(`📁 ${Math.round(fileSize / 1000)}KB 发送中...`);

      let base64 = '';
      try {
        base64 = await FileSystem.readAsStringAsync(uri, {
          encoding: FileSystem.EncodingType.Base64,
        });
      } catch (readErr) {
        setDebugText(`❌ 读取失败：${readErr}`);
        setTimeout(() => setDebugText(''), 3000);
        await resumeListening();
        return;
      }

      let sttRes: any;
      try {
        sttRes = await axios.post(`${SERVER_URL}/transcribe`, {
          audio_base64: base64, user_id: userId,
        }, { timeout: 30000 });
      } catch (netErr: any) {
        setDebugText(`❌ 网络：${netErr?.message}`);
        setTimeout(() => setDebugText(''), 4000);
        if (activeRef.current) await resumeListening();
        return;
      }

      const text = sttRes.data?.text?.trim();
      const filtered = sttRes.data?.filtered;
      const lowConfidence = sttRes.data?.low_confidence;

      if (filtered) {
        setDebugText(`⚠️ 过滤 (${sttRes.data?.reason || ''})`);
        setTimeout(() => setDebugText(''), 1500);
        if (activeRef.current) await resumeListening();
        return;
      }

      if (!text || text.length < 2) {
        setDebugText('⚠️ 未识别');
        setTimeout(() => setDebugText(''), 1500);
        if (activeRef.current) await resumeListening();
        return;
      }

      // ★ 低置信度标记（仍发送，但显示提示）
      if (lowConfidence) {
        setDebugText(`🤔 可能听错：${text}`);
      } else {
        setDebugText(`✅ ${text}`);
      }

      // 真正识别到了——刷新沉默计时
      lastActiveTimeRef.current = Date.now();
      proactiveCountRef.current = 0;

      const userMsg: CallMsg = {
        id: Date.now().toString(), role: 'user', zh: text, time: nowTime(),
      };
      setCallMsgs(prev => [...prev, userMsg]);
      scrollRef.current?.scrollToEnd({ animated: true });

      await sendToGojo(text);

    } catch (e: any) {
      setDebugText(`❌ ${e?.message || e}`);
      setTimeout(() => setDebugText(''), 4000);
      if (activeRef.current) await resumeListening();
    }
  };

  const sendToGojo = async (text: string) => {
    if (!activeRef.current) return;
    try {
      const res = await axios.post(`${SERVER_URL}/chat/voice_text`, { text, user_id: userId });
      if (!activeRef.current) return;

      setPhaseSync('responding');

      const segments = Array.isArray(res.data?.messages)
        ? res.data.messages
        : res.data?.jp
          ? [{ jp: res.data.jp, zh: res.data.zh || '', audio_b64: res.data.audio_b64 || '' }]
          : [];

      for (let i = 0; i < segments.length; i++) {
        if (!activeRef.current) break;
        const seg = segments[i];
        const gojoMsg: CallMsg = {
          id: `g_${Date.now()}_${i}`, role: 'gojo',
          jp: seg.jp, zh: seg.zh, time: nowTime(),
        };
        setCallMsgs(prev => [...prev, gojoMsg]);
        setSubtitle(seg.zh || seg.jp || '');
        scrollRef.current?.scrollToEnd({ animated: true });

        if (seg.audio_b64 && seg.audio_b64.length > 100) {
          await playAudio(seg.audio_b64);
        } else {
          await sleep(800);
        }
      }
      lastActiveTimeRef.current = Date.now();
    } catch (e) {
      console.warn('sendToGojo error', e);
    } finally {
      setSubtitle('');
      setDebugText('');
      if (activeRef.current) await resumeListening();
    }
  };

  const resumeListening = async () => {
    if (!activeRef.current) return;
    setPhaseSync('listening');
    await Audio.setAudioModeAsync({
      allowsRecordingIOS: true, playsInSilentModeIOS: true,
      staysActiveInBackground: false, shouldDuckAndroid: false, playThroughEarpieceAndroid: false,
    }).catch(() => {});
    await startRecording();
  };

  const playAudio = (b64: string): Promise<void> =>
    new Promise(async (resolve) => {
      try {
        if (currentSoundRef.current) {
          await currentSoundRef.current.unloadAsync();
          currentSoundRef.current = null;
        }
        await Audio.setAudioModeAsync({
          allowsRecordingIOS: false, playsInSilentModeIOS: true,
          staysActiveInBackground: false, shouldDuckAndroid: true,
          playThroughEarpieceAndroid: !isSpeaker,
        });
        const { sound } = await Audio.Sound.createAsync(
          { uri: `data:audio/mp3;base64,${b64}` },
          { shouldPlay: true, volume: 1.0 }
        );
        currentSoundRef.current = sound;
        sound.setOnPlaybackStatusUpdate(status => {
          if (status.isLoaded && status.didJustFinish) {
            sound.unloadAsync().catch(() => {});
            if (currentSoundRef.current === sound) currentSoundRef.current = null;
            resolve();
          }
        });
      } catch { resolve(); }
    });

  const togglePause = async () => {
    if (phase === 'paused') {
      setPhaseSync('listening');
      lastActiveTimeRef.current = Date.now();
      await startRecording();
    } else if (phase === 'listening' || phase === 'speaking') {
      stopPolling();
      try { await recordingRef.current?.stopAndUnloadAsync(); } catch {}
      recordingRef.current = null;
      setPhaseSync('paused');
    }
  };

  const hangUp = async () => {
    activeRef.current = false;
    stopPolling();
    stopIdleDetection();
    try { await recordingRef.current?.stopAndUnloadAsync(); } catch {}
    try { await currentSoundRef.current?.unloadAsync(); } catch {}
    recordingRef.current = null;
    currentSoundRef.current = null;
    if (callTimerRef.current) clearInterval(callTimerRef.current);

    setPhaseSync('ended');
    await sleep(2000);

    await Audio.setAudioModeAsync({
      allowsRecordingIOS: false, playsInSilentModeIOS: true,
      staysActiveInBackground: false, shouldDuckAndroid: true, playThroughEarpieceAndroid: false,
    }).catch(() => {});

    if (callMsgs.length > 0) {
      const divider: Message = {
        id: `div_${Date.now()}`, role: 'gojo',
        text: `── 通话记录（${formatDuration(duration)}）──`, time: nowTime(),
      };
      const chatMsgs: Message[] = callMsgs.map(m => ({
        id: m.id, role: m.role,
        text: m.role === 'gojo' ? (m.jp || m.zh) : m.zh,
        subtitle: m.role === 'gojo' ? m.zh : undefined,
        time: m.time,
      }));
      onAddMessages([divider, ...chatMsgs]);
    }
    onClose();
  };

  const formatDuration = (s: number) =>
    `${String(Math.floor(s/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`;

  const volumeBars = () => {
    const normalized = Math.max(0, Math.min(1, (dbLevel + 60) / 60));
    const filled = Math.round(normalized * 5);
    return Array.from({ length: 5 }, (_, i) => i < filled);
  };

  const phaseLabel: Record<Phase, string> = {
    connecting: '正在接通...',
    listening:  '聆听中',
    speaking:   '检测到说话...',
    processing: '识别中...',
    responding: '',
    paused:     '已暂停',
    ended:      '',
  };

  return (
    <Modal visible animationType="slide" statusBarTranslucent>
      <View style={s.screen}>

        {phase === 'ended' && (
          <View style={s.endedOverlay}>
            <Text style={s.endedIcon}>📵</Text>
            <Text style={s.endedTitle}>通话已结束</Text>
            <Text style={s.endedDuration}>通话时长 {formatDuration(duration)}</Text>
            <Text style={s.endedHint}>对话已保存到聊天记录</Text>
          </View>
        )}

        <View style={s.topBar}>
          <Text style={s.timer}>{formatDuration(duration)}</Text>
        </View>

        <View style={s.avatarArea}>
          <View style={[
            s.avatarRing,
            phase === 'speaking' && s.ringSpeak,
            phase === 'responding' && s.ringRespond,
          ]}>
            <View style={s.avatar}>
              <Text style={s.avatarText}>悟</Text>
            </View>
          </View>
          <Text style={s.name}>五条悟</Text>
          <Text style={s.phaseLabel}>{phaseLabel[phase]}</Text>

          {(phase === 'listening' || phase === 'speaking') && (
            <View style={s.volRow}>
              {volumeBars().map((filled, i) => (
                <View key={i} style={[s.volBar, filled && s.volBarFilled]} />
              ))}
            </View>
          )}

          {debugText !== '' && (
            <View style={s.debugBox}>
              <Text style={s.debugText}>{debugText}</Text>
            </View>
          )}
        </View>

        {phase === 'responding' && subtitle !== '' && (
          <View style={s.subtitleBox}>
            <Text style={s.subtitleText}>{subtitle}</Text>
          </View>
        )}

        <ScrollView
          ref={scrollRef}
          style={s.msgList}
          contentContainerStyle={{ padding: 16 }}
          onContentSizeChange={() => scrollRef.current?.scrollToEnd({ animated: true })}
        >
          {callMsgs.length === 0 && (
            <Text style={s.hint}>直接说话，五条悟会自动听到并回复你</Text>
          )}
          {callMsgs.map(msg => (
            <View key={msg.id} style={[
              s.callMsg,
              msg.role === 'user' ? s.msgUser : s.msgGojo,
            ]}>
              <Text style={[s.msgText, msg.role === 'user' && { color: '#fff' }]}>
                {msg.role === 'gojo' && msg.jp ? msg.jp : msg.zh}
              </Text>
              {msg.role === 'gojo' && msg.zh && msg.jp && msg.zh !== msg.jp && (
                <Text style={s.msgSub}>{msg.zh}</Text>
              )}
            </View>
          ))}
        </ScrollView>

        <View style={s.controls}>
          <TouchableOpacity
            style={[s.ctrlBtn, isSpeaker && s.ctrlActive]}
            onPress={() => setIsSpeaker(v => !v)}
          >
            <Text style={s.ctrlIcon}>{isSpeaker ? '🔊' : '🔈'}</Text>
            <Text style={s.ctrlLabel}>{isSpeaker ? '扬声器' : '听筒'}</Text>
          </TouchableOpacity>

          <TouchableOpacity style={s.hangupBtn} onPress={hangUp} disabled={phase === 'ended'}>
            <Text style={s.hangupIcon}>📵</Text>
          </TouchableOpacity>

          <TouchableOpacity
            style={[s.ctrlBtn, phase === 'paused' && s.ctrlActive]}
            onPress={togglePause}
            disabled={phase === 'processing' || phase === 'responding' || phase === 'connecting' || phase === 'ended'}
          >
            <Text style={s.ctrlIcon}>{phase === 'paused' ? '🎙' : '🔇'}</Text>
            <Text style={s.ctrlLabel}>{phase === 'paused' ? '继续' : '静音'}</Text>
          </TouchableOpacity>
        </View>

      </View>
    </Modal>
  );
}

function sleep(ms: number) {
  return new Promise<void>(r => setTimeout(r, ms));
}

const s = StyleSheet.create({
  screen:      { flex: 1, backgroundColor: '#080e1a', alignItems: 'center', paddingTop: Platform.OS === 'ios' ? 50 : 30, paddingBottom: 20 },
  topBar:      { width: '100%', alignItems: 'center', paddingVertical: 6 },
  timer:       { color: '#64748b', fontSize: 15, letterSpacing: 3 },

  avatarArea:  { alignItems: 'center', marginTop: 20, marginBottom: 10 },
  avatarRing:  { width: 130, height: 130, borderRadius: 65, borderWidth: 2, borderColor: C.accent + '40', alignItems: 'center', justifyContent: 'center', marginBottom: 14 },
  ringSpeak:   { borderColor: '#22c55e', shadowColor: '#22c55e', shadowOffset: { width:0, height:0 }, shadowOpacity: 0.7, shadowRadius: 16, elevation: 10 },
  ringRespond: { borderColor: C.accent, shadowColor: C.accent, shadowOffset: { width:0, height:0 }, shadowOpacity: 0.8, shadowRadius: 16, elevation: 10 },
  avatar:      { width: 110, height: 110, borderRadius: 55, backgroundColor: C.accentDim, alignItems: 'center', justifyContent: 'center', borderWidth: 2, borderColor: C.accent },
  avatarText:  { color: '#fff', fontSize: 46, fontWeight: '700' },
  name:        { color: '#fff', fontSize: 20, fontWeight: '600', marginBottom: 6 },
  phaseLabel:  { color: '#64748b', fontSize: 13, marginBottom: 10 },

  volRow:      { flexDirection: 'row', gap: 5, marginTop: 4 },
  volBar:      { width: 6, height: 18, borderRadius: 3, backgroundColor: '#1e293b' },
  volBarFilled:{ backgroundColor: '#22c55e' },

  debugBox:    { marginTop: 10, backgroundColor: 'rgba(255,255,255,0.08)', borderRadius: 10, paddingHorizontal: 14, paddingVertical: 6, maxWidth: width - 60 },
  debugText:   { color: '#94a3b8', fontSize: 12, textAlign: 'center' },

  subtitleBox: { marginHorizontal: 24, marginBottom: 8, backgroundColor: 'rgba(255,255,255,0.07)', borderRadius: 14, padding: 14, maxWidth: width - 48 },
  subtitleText:{ color: '#e2e8f0', fontSize: 14, textAlign: 'center', lineHeight: 22 },

  msgList:     { flex: 1, width: '100%' },
  hint:        { color: '#334155', fontSize: 13, textAlign: 'center', marginTop: 24, lineHeight: 22 },
  callMsg:     { marginBottom: 10, maxWidth: width * 0.72, borderRadius: 14, padding: 11 },
  msgUser:     { alignSelf: 'flex-end', backgroundColor: C.accent },
  msgGojo:     { alignSelf: 'flex-start', backgroundColor: 'rgba(255,255,255,0.07)', borderLeftWidth: 2, borderLeftColor: C.accent },
  msgText:     { color: '#e2e8f0', fontSize: 14, lineHeight: 21 },
  msgSub:      { color: '#94a3b8', fontSize: 12, marginTop: 4, fontStyle: 'italic' },

  controls:    { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-around', width: '100%', paddingHorizontal: 24, paddingVertical: 16 },
  ctrlBtn:     { alignItems: 'center', padding: 10, borderRadius: 16, width: 72 },
  ctrlActive:  { backgroundColor: 'rgba(255,255,255,0.12)' },
  ctrlIcon:    { fontSize: 26, marginBottom: 4 },
  ctrlLabel:   { color: '#64748b', fontSize: 11 },
  hangupBtn:   { width: 66, height: 66, borderRadius: 33, backgroundColor: '#ef4444', alignItems: 'center', justifyContent: 'center', shadowColor: '#ef4444', shadowOffset: { width:0, height:4 }, shadowOpacity: 0.4, shadowRadius: 8, elevation: 8 },
  hangupIcon:  { fontSize: 28 },

  endedOverlay:  { position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, backgroundColor: '#080e1a', alignItems: 'center', justifyContent: 'center', zIndex: 99 },
  endedIcon:     { fontSize: 70, marginBottom: 24, opacity: 0.7 },
  endedTitle:    { color: '#fff', fontSize: 26, fontWeight: '700', marginBottom: 12, letterSpacing: 1 },
  endedDuration: { color: '#94a3b8', fontSize: 15, marginBottom: 8 },
  endedHint:     { color: '#475569', fontSize: 12, marginTop: 20 },
});