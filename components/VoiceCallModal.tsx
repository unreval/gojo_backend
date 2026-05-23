// app/components/VoiceCallModal.tsx
// 全自动语音通话：说话自动识别，静音后自动发送，Gojo 语音回复
import axios from 'axios';
import { Audio } from 'expo-av';
import * as FileSystem from 'expo-file-system';
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

// ── VAD 参数（可根据环境调整）──
const SPEECH_THRESHOLD   = -40;   // dBFS，高于此认为在说话
const SPEECH_MIN_MS      = 400;   // 至少说这么久才算有效语音
const SILENCE_TRIGGER_MS = 1500;  // 说完后静音这么久就触发识别
const POLL_INTERVAL_MS   = 120;   // 检测频率

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
  | 'connecting'   // 通话接通中
  | 'listening'    // 聆听中（等用户说话）
  | 'speaking'     // 检测到用户在说话
  | 'processing'   // 识别 + Gojo 思考
  | 'responding'   // Gojo 正在说话
  | 'paused'       // 手动暂停
  | 'ended';       // 通话结束

export default function VoiceCallModal({ userId, onClose, onAddMessages }: Props) {
  const [phase, setPhase]         = useState<Phase>('connecting');
  const [callMsgs, setCallMsgs]   = useState<CallMsg[]>([]);
  const [subtitle, setSubtitle]   = useState('');
  const [duration, setDuration]   = useState(0);
  const [isSpeaker, setIsSpeaker] = useState(true);
  const [dbLevel, setDbLevel]     = useState(-160); // 用于显示音量条

  const recordingRef    = useRef<Audio.Recording | null>(null);
  const currentSoundRef = useRef<Audio.Sound | null>(null);
  const pollTimerRef    = useRef<ReturnType<typeof setInterval> | null>(null);
  const callTimerRef    = useRef<ReturnType<typeof setInterval> | null>(null);
  const scrollRef       = useRef<ScrollView>(null);
  const activeRef       = useRef(true);

  const [debugText, setDebugText] = useState(''); // 临时显示识别结果

  const phaseRef          = useRef<Phase>('connecting');
  const speechStartRef    = useRef<number | null>(null);
  const silenceStartRef   = useRef<number | null>(null);

  const setPhaseSync = (p: Phase) => {
    phaseRef.current = p;
    setPhase(p);
  };

  // ── 计时器 ──
  useEffect(() => {
    callTimerRef.current = setInterval(() => setDuration(d => d + 1), 1000);
    return () => { if (callTimerRef.current) clearInterval(callTimerRef.current); };
  }, []);

  // ── 启动通话 ──
  useEffect(() => {
    const init = async () => {
      try {
        const { status } = await Audio.requestPermissionsAsync();
        if (status !== 'granted') {
          alert('需要麦克风权限才能使用语音通话');
          onClose();
          return;
        }
        // 短暂延迟模拟"接通"
        await new Promise(r => setTimeout(r, 800));
        setPhaseSync('listening');
        await startRecording();
      } catch (e) {
        console.warn('通话初始化失败', e);
      }
    };
    init();
    return () => { cleanup(); };
  }, []);

  // ── 扬声器设置 ──
  useEffect(() => {
    Audio.setAudioModeAsync({
      allowsRecordingIOS: true,
      playsInSilentModeIOS: true,
      staysActiveInBackground: false,
      shouldDuckAndroid: true,
      playThroughEarpieceAndroid: !isSpeaker,
    }).catch(() => {});
  }, [isSpeaker]);

  const cleanup = async () => {
    activeRef.current = false;
    stopPolling();
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

  // ── 开始录音 ──
  const startRecording = async () => {
    if (!activeRef.current) return;
    try {
      await Audio.setAudioModeAsync({
        allowsRecordingIOS: true, playsInSilentModeIOS: true,
        staysActiveInBackground: false, shouldDuckAndroid: false, playThroughEarpieceAndroid: false,
      });
      const { recording } = await Audio.Recording.createAsync({
        android: {
          extension: '.m4a',
          outputFormat: Audio.AndroidOutputFormat.MPEG_4,
          audioEncoder: Audio.AndroidAudioEncoder.AAC,
          sampleRate: 16000,
          numberOfChannels: 1,
          bitRate: 48000,
        },
        ios: {
          extension: '.m4a',
          audioQuality: Audio.IOSAudioQuality.MEDIUM,
          sampleRate: 16000,
          numberOfChannels: 1,
          bitRate: 48000,
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
      startPolling();
    } catch (e) {
      console.warn('startRecording failed', e);
    }
  };

  // ── 开始轮询音量（VAD 核心）──
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
          // 用户在说话
          silenceStartRef.current = null;
          if (currentPhase === 'listening') {
            speechStartRef.current = now;
            setPhaseSync('speaking');
          }
        } else {
          // 静音
          if (currentPhase === 'speaking') {
            if (!silenceStartRef.current) {
              silenceStartRef.current = now;
            }
            const silenceDuration = now - silenceStartRef.current;
            const speechDuration = speechStartRef.current ? now - speechStartRef.current : 0;

            if (silenceDuration >= SILENCE_TRIGGER_MS && speechDuration >= SPEECH_MIN_MS) {
              // 用户说完了，处理录音
              stopPolling();
              setPhaseSync('processing');
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

  // ── 处理录音（识别 + 回复）──
  const processRecording = async () => {
    if (!activeRef.current) return;

    const rec = recordingRef.current;
    recordingRef.current = null;

    try {
      if (!rec) { await resumeListening(); return; }

      await rec.stopAndUnloadAsync();
      const uri = rec.getURI();
      if (!uri) { setDebugText('❌ 录音文件为空'); await resumeListening(); return; }

      // 检查文件大小
      const info = await FileSystem.getInfoAsync(uri);
      const fileSize = info.exists && 'size' in info ? info.size : 0;
      setDebugText(`📁 文件大小：${fileSize} bytes，识别中...`);

      if (fileSize < 2000) {
        // 文件太小，可能只是背景噪音
        setDebugText('⚠️ 录音太短，重新聆听');
        await resumeListening();
        return;
      }

      // 读取音频转 base64
      const base64 = await FileSystem.readAsStringAsync(uri, {
        encoding: FileSystem.EncodingType.Base64,
      });

      // 发给 Groq 识别
      const sttRes = await axios.post(`${SERVER_URL}/transcribe`, {
        audio_base64: base64,
        user_id: userId,
      });

      const text = sttRes.data?.text?.trim();
      const errMsg = sttRes.data?.error || '';

      if (errMsg) {
        setDebugText(`❌ 错误：${errMsg}`);
        setTimeout(() => setDebugText(''), 3000);
        if (activeRef.current) await resumeListening();
        return;
      }

      if (!text || text.length < 2) {
        setDebugText('⚠️ 未识别到内容，重试');
        setTimeout(() => setDebugText(''), 2000);
        if (activeRef.current) await resumeListening();
        return;
      }

      setDebugText(`✅ 识别：${text}`);

      // 添加用户消息
      const userMsg: CallMsg = {
        id: Date.now().toString(), role: 'user', zh: text, time: nowTime(),
      };
      setCallMsgs(prev => [...prev, userMsg]);
      scrollRef.current?.scrollToEnd({ animated: true });

      // 发给 Gojo
      await sendToGojo(text);

    } catch (e: any) {
      setDebugText(`❌ 请求失败：${e?.message || e}`);
      setTimeout(() => setDebugText(''), 3000);
      console.warn('processRecording error', e);
      if (activeRef.current) await resumeListening();
    }
  };

  // ── 发给 Gojo ──
  const sendToGojo = async (text: string) => {
    if (!activeRef.current) return;
    try {
      const res = await axios.post(`${SERVER_URL}/chat/text`, { text, user_id: userId });
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
    } catch (e) {
      console.warn('sendToGojo error', e);
    } finally {
      setSubtitle('');
      if (activeRef.current) await resumeListening();
    }
  };

  // ── 恢复聆听 ──
  const resumeListening = async () => {
    if (!activeRef.current) return;
    setPhaseSync('listening');
    // 切回录音模式
    await Audio.setAudioModeAsync({
      allowsRecordingIOS: true, playsInSilentModeIOS: true,
      staysActiveInBackground: false, shouldDuckAndroid: false, playThroughEarpieceAndroid: false,
    }).catch(() => {});
    await startRecording();
  };

  // ── 播放音频 ──
  const playAudio = (b64: string): Promise<void> =>
    new Promise(async (resolve) => {
      try {
        if (currentSoundRef.current) {
          await currentSoundRef.current.unloadAsync();
          currentSoundRef.current = null;
        }
        // 播放时切换到播放模式
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

  // ── 手动暂停/继续 ──
  const togglePause = async () => {
    if (phase === 'paused') {
      setPhaseSync('listening');
      await startRecording();
    } else if (phase === 'listening' || phase === 'speaking') {
      stopPolling();
      try { await recordingRef.current?.stopAndUnloadAsync(); } catch {}
      recordingRef.current = null;
      setPhaseSync('paused');
    }
  };

  // ── 挂断 ──
  const hangUp = async () => {
    await cleanup();
    // 保存通话记录到聊天
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

  // ── 音量条（5格）──
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
    ended:      '通话结束',
  };

  return (
    <Modal visible animationType="slide" statusBarTranslucent>
      <View style={s.screen}>

        {/* 计时 */}
        <View style={s.topBar}>
          <Text style={s.timer}>{formatDuration(duration)}</Text>
        </View>

        {/* 头像 + 状态 */}
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

          {/* 音量条（说话时显示）*/}
          {(phase === 'listening' || phase === 'speaking') && (
            <View style={s.volRow}>
              {volumeBars().map((filled, i) => (
                <View key={i} style={[s.volBar, filled && s.volBarFilled]} />
              ))}
            </View>
          )}

          {/* 调试信息 */}
          {debugText !== '' && (
            <Text style={s.debugText}>{debugText}</Text>
          )}
        </View>

        {/* 字幕 */}
        {phase === 'responding' && subtitle !== '' && (
          <View style={s.subtitleBox}>
            <Text style={s.subtitleText}>{subtitle}</Text>
          </View>
        )}

        {/* 对话记录 */}
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

        {/* 控制按钮 */}
        <View style={s.controls}>
          {/* 扬声器 */}
          <TouchableOpacity
            style={[s.ctrlBtn, isSpeaker && s.ctrlActive]}
            onPress={() => setIsSpeaker(v => !v)}
          >
            <Text style={s.ctrlIcon}>{isSpeaker ? '🔊' : '🔈'}</Text>
            <Text style={s.ctrlLabel}>{isSpeaker ? '扬声器' : '听筒'}</Text>
          </TouchableOpacity>

          {/* 挂断 */}
          <TouchableOpacity style={s.hangupBtn} onPress={hangUp}>
            <Text style={s.hangupIcon}>📵</Text>
          </TouchableOpacity>

          {/* 暂停/继续麦克风 */}
          <TouchableOpacity
            style={[s.ctrlBtn, phase === 'paused' && s.ctrlActive]}
            onPress={togglePause}
            disabled={phase === 'processing' || phase === 'responding' || phase === 'connecting'}
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

  subtitleBox: { marginHorizontal: 24, marginBottom: 8, backgroundColor: 'rgba(255,255,255,0.07)', borderRadius: 14, padding: 14, maxWidth: width - 48 },
  subtitleText:{ color: '#e2e8f0', fontSize: 14, textAlign: 'center', lineHeight: 22 },

  msgList:     { flex: 1, width: '100%' },
  hint:        { color: '#334155', fontSize: 13, textAlign: 'center', marginTop: 24, lineHeight: 22 },
  debugText:   { color: '#94a3b8', fontSize: 11, marginTop: 8, paddingHorizontal: 20, textAlign: 'center' },
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
});