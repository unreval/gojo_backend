// app/components/VoiceCallModal.tsx
// 改动说明（本次唯一改动）：
//   原来 import { Message } from '../app/(tabs)/chat'  ← 反向依赖聊天页
//   现在 import type { Message } from '../types/message'  ← 共享类型文件
// 其他所有 gojo 语音通话逻辑（VAD、沉默主动开口、繁转简、扬声器切换、挂断保存）完全不动。
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
import { C, SERVER_URL, nowTime } from '../constants/theme';
import type { Message } from '../types/message';

const { width } = Dimensions.get('window');

// ───── VAD 参数 ─────
const SPEECH_THRESHOLD     = -32;
const SPEECH_MIN_MS        = 400;
const POLL_INTERVAL_MS     = 120;
const MIN_AUDIO_SIZE       = 10000;
const CONSECUTIVE_FRAMES   = 3;

// ───── 沉默主动开口参数 ─────
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

  const phaseRef            = useRef<Phase>('connecting');
  const speechStartRef      = useRef<number | null>(null);
  const silenceStartRef     = useRef<number | null>(null);
  const consecutiveSpeechRef = useRef(0);
  const speechConfirmedRef  = useRef(false);
  const recordingStartTimeRef = useRef<number>(0);

  const lastActiveTimeRef   = useRef<number>(Date.now());
  const proactiveCountRef   = useRef(0);

  const setPhaseSync = (p: Phase) => {
    phaseRef.current = p;
    setPhase(p);
  };

  // ───── 通话计时器 ─────
  useEffect(() => {
    callTimerRef.current = setInterval(() => setDuration(d => d + 1), 1000);
    return () => { if (callTimerRef.current) clearInterval(callTimerRef.current); };
  }, []);

  // ───── 初始化 ─────
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

        await sendConnectGreeting();
        if (!activeRef.current) return;

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

  // ───── 扬声器切换 ─────
  useEffect(() => {
    Audio.setAudioModeAsync({
      allowsRecordingIOS: true,
      playsInSilentModeIOS: true,
      staysActiveInBackground: false,
      shouldDuckAndroid: true,
      playThroughEarpieceAndroid: !isSpeaker,
    }).catch(() => {});
  }, [isSpeaker]);

  // ───── 清理 ─────
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
      allowsRecordingIOS: false,
      playsInSilentModeIOS: true,
      staysActiveInBackground: false,
      shouldDuckAndroid: true,
      playThroughEarpieceAndroid: false,
    }).catch(() => {});
  };

  const sendConnectGreeting = async () => {
    if (!activeRef.current) return;
    try {
      setPhaseSync('responding');
      const res = await axios.post(`${SERVER_URL}/chat/voice/proactive`, {
        user_id: userId,
        mode: 'greeting',
        silence_seconds: 0,
      });
      if (!activeRef.current) return;

      const segments = res.data?.messages || [];
      for (let i = 0; i < segments.length; i++) {
        if (!activeRef.current) break;
        const seg = segments[i];
        const gojoMsg: CallMsg = {
          id: `greet_${Date.now()}_${i}`, role: 'gojo',
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
      console.warn('connect greeting error', e);
    } finally {
      setSubtitle('');
    }
  };

  const startIdleDetection = () => {
    stopIdleDetection();
    lastActiveTimeRef.current = Date.now();
    proactiveCountRef.current = 0;

    idleTimerRef.current = setInterval(async () => {
      if (!activeRef.current) return;

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

  const triggerProactiveMessage = async (mode: 'idle' | 'missed', silenceSeconds: number) => {
    if (!activeRef.current) return;

    const wasPaused = phaseRef.current === 'paused';

    try {
      if (!wasPaused) {
        stopPolling();
        try { await recordingRef.current?.stopAndUnloadAsync(); } catch {}
        recordingRef.current = null;
      }

      setPhaseSync('responding');

      const res = await axios.post(`${SERVER_URL}/chat/voice/proactive`, {
        user_id: userId,
        mode,
        silence_seconds: silenceSeconds,
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
      lastActiveTimeRef.current = Date.now();
      if (activeRef.current) {
        if (wasPaused) {
          setPhaseSync('paused');
        } else {
          await resumeListening();
        }
      }
    }
  };

  const startRecording = async () => {
    if (!activeRef.current) return;
    try {
      await Audio.setAudioModeAsync({
        allowsRecordingIOS: true,
        playsInSilentModeIOS: true,
        staysActiveInBackground: false,
        shouldDuckAndroid: false,
        playThroughEarpieceAndroid: false,
      });

      const { recording } = await Audio.Recording.createAsync({
        android: {
          extension: '.wav',
          outputFormat: Audio.AndroidOutputFormat.DEFAULT,
          audioEncoder: Audio.AndroidAudioEncoder.DEFAULT,
          sampleRate: 16000,
          numberOfChannels: 1,
          bitRate: 256000,
        },
        ios: {
          extension: '.wav',
          audioQuality: Audio.IOSAudioQuality.HIGH,
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
      recordingStartTimeRef.current = Date.now();
      startPolling();
    } catch (e) {
      console.warn('startRecording failed', e);
      setDebugText(`❌ 录音启动失败：${e}`);
      setTimeout(() => setDebugText(''), 4000);
    }
  };

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
              recordingStartTimeRef.current = now;
              setPhaseSync('speaking');
            }
          }
        } else {
          consecutiveSpeechRef.current = 0;

          if (speechConfirmedRef.current && currentPhase === 'speaking') {
            if (!silenceStartRef.current) silenceStartRef.current = now;
            const silenceDuration = now - silenceStartRef.current;
            const speechDuration = speechStartRef.current ? now - speechStartRef.current : 0;
            const recordingDuration = now - recordingStartTimeRef.current;

            const dynamicSilenceMs = recordingDuration > 3000 ? 3500 : 2500;

            if (silenceDuration >= dynamicSilenceMs) {
              if (speechDuration >= SPEECH_MIN_MS) {
                stopPolling();
                setPhaseSync('processing');
                speechConfirmedRef.current = false;
                consecutiveSpeechRef.current = 0;
                await processRecording();
              } else {
                speechConfirmedRef.current = false;
                speechStartRef.current = null;
                silenceStartRef.current = null;
                consecutiveSpeechRef.current = 0;
                setDebugText('⚠️ 太短，忽略');
                setTimeout(() => setDebugText(''), 1500);
              }
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
        setDebugText(`❌ 录音停止失败：${stopErr}`);
        setTimeout(() => setDebugText(''), 4000);
        await resumeListening();
        return;
      }

      if (!uri) {
        setDebugText('❌ 录音URI为空');
        setTimeout(() => setDebugText(''), 3000);
        await resumeListening();
        return;
      }

      const info = await FileSystem.getInfoAsync(uri);
      const fileSize = info.exists && 'size' in info ? info.size : 0;

      if (fileSize < MIN_AUDIO_SIZE) {
        setDebugText(`⚠️ 音频太小(${fileSize}B)，忽略`);
        setTimeout(() => setDebugText(''), 1500);
        await resumeListening();
        return;
      }

      setDebugText(`📁 ${Math.round(fileSize / 1024)}KB 发送中...`);

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
          audio_base64: base64,
          user_id: userId,
        }, { timeout: 30000 });
      } catch (netErr: any) {
        setDebugText(`❌ 网络：${netErr?.message}`);
        setTimeout(() => setDebugText(''), 4000);
        if (activeRef.current) await resumeListening();
        return;
      }

      const text = sttRes.data?.text?.trim();
      const filtered = sttRes.data?.filtered;
      const reason = sttRes.data?.reason || '';

      if (filtered) {
        setDebugText(`⚠️ 过滤(${reason})`);
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

      lastActiveTimeRef.current = Date.now();
      proactiveCountRef.current = 0;

      setDebugText(`✅ ${text}`);

      const userMsg: CallMsg = {
        id: Date.now().toString(),
        role: 'user',
        zh: text,
        time: nowTime(),
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

  // ★ B档流式:XHR + onprogress 逐行解析 NDJSON,音频入队边收边播
  //   老的 axios.post + 完整 JSON 兜底见下方 sendToGojoLegacy
  const sendToGojo = async (text: string) => {
    if (!activeRef.current) return;

    interface AudioChunk {
      seq: number; jp: string; zh: string; emotion: string; audio_b64: string;
    }

    await new Promise<void>((resolve) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', `${SERVER_URL}/chat/voice_stream`);
      xhr.setRequestHeader('Content-Type', 'application/json');

      let lastPos = 0;
      let buffer = '';
      const audioQueue: AudioChunk[] = [];
      let streamDone = false;
      let playIndex = 0;
      let playing = false;
      let finalized = false;

      const finalize = () => {
        if (finalized) return;
        finalized = true;
        setSubtitle('');
        setDebugText('');
        lastActiveTimeRef.current = Date.now();
        proactiveCountRef.current = 0;
        if (activeRef.current) {
          resumeListening().finally(() => resolve());
        } else {
          resolve();
        }
      };

      const drainQueue = async () => {
        if (playing) return;
        playing = true;
        try {
          while (activeRef.current && playIndex < audioQueue.length) {
            const chunk = audioQueue[playIndex++];
            setPhaseSync('responding');
            const gojoMsg: CallMsg = {
              id: `g_${Date.now()}_${chunk.seq}`,
              role: 'gojo',
              jp: chunk.jp,
              zh: chunk.zh,
              time: nowTime(),
            };
            setCallMsgs(prev => [...prev, gojoMsg]);
            setSubtitle(chunk.zh || chunk.jp || '');
            scrollRef.current?.scrollToEnd({ animated: true });

            if (chunk.audio_b64 && chunk.audio_b64.length > 100) {
              await playAudio(chunk.audio_b64);
            } else {
              await sleep(600);
            }
          }
        } finally {
          playing = false;
        }
        // 队列播完 + 服务端也 done 了 → 收尾
        if (streamDone && playIndex >= audioQueue.length) {
          finalize();
        }
      };

      const processEvent = (evt: any) => {
        if (!evt || typeof evt !== 'object') return;
        if (evt.type === 'text_jp') {
          // 字幕预显示——暂不用,避免和真正播放时的字幕跳来跳去
          return;
        }
        if (evt.type === 'audio') {
          audioQueue.push(evt as AudioChunk);
          drainQueue();  // 非阻塞触发
          return;
        }
        if (evt.type === 'done') {
          streamDone = true;
          drainQueue();  // 有可能音频都播完了,靠这个触发 finalize
          return;
        }
        if (evt.type === 'error') {
          console.warn('[voice_stream] server error:', evt.msg);
          setDebugText(`❌ ${evt.msg}`);
          setTimeout(() => setDebugText(''), 3000);
          streamDone = true;
          drainQueue();
          return;
        }
      };

      xhr.onprogress = () => {
        try {
          const newChunk = xhr.responseText.substring(lastPos);
          lastPos = xhr.responseText.length;
          buffer += newChunk;
          const lines = buffer.split('\n');
          buffer = lines.pop() || '';   // 最后一段可能是半行
          for (const raw of lines) {
            const line = raw.trim();
            if (!line) continue;
            try {
              const evt = JSON.parse(line);
              processEvent(evt);
            } catch (e) {
              console.warn('[voice_stream] parse fail:', line.slice(0, 80));
            }
          }
        } catch (e) {
          console.warn('[voice_stream] onprogress err:', e);
        }
      };

      xhr.onload = () => {
        // flush 剩下的
        if (buffer.trim()) {
          try {
            processEvent(JSON.parse(buffer.trim()));
          } catch {}
          buffer = '';
        }
        streamDone = true;
        drainQueue();
      };

      xhr.onerror = () => {
        console.warn('[voice_stream] xhr error');
        setDebugText('❌ 流式连接失败');
        setTimeout(() => setDebugText(''), 3000);
        streamDone = true;
        drainQueue();
        // 就算队列空,也强制 finalize 避免卡死
        if (playIndex >= audioQueue.length) finalize();
      };

      xhr.ontimeout = () => {
        console.warn('[voice_stream] xhr timeout');
        setDebugText('❌ 流式超时');
        streamDone = true;
        drainQueue();
        if (playIndex >= audioQueue.length) finalize();
      };

      xhr.timeout = 60000;   // 60 秒兜底

      try {
        xhr.send(JSON.stringify({
          text,
          user_id: userId,
          character_id: 'gojo',
        }));
      } catch (e) {
        console.warn('[voice_stream] send err:', e);
        finalize();
      }
    });
  };

  const resumeListening = async () => {
    if (!activeRef.current) return;
    setPhaseSync('listening');
    lastActiveTimeRef.current = Date.now();
    await Audio.setAudioModeAsync({
      allowsRecordingIOS: true,
      playsInSilentModeIOS: true,
      staysActiveInBackground: false,
      shouldDuckAndroid: false,
      playThroughEarpieceAndroid: false,
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
          allowsRecordingIOS: false,
          playsInSilentModeIOS: true,
          staysActiveInBackground: false,
          shouldDuckAndroid: true,
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
      proactiveCountRef.current = 0;
      await startRecording();
    } else if (phase === 'listening' || phase === 'speaking') {
      stopPolling();
      try { await recordingRef.current?.stopAndUnloadAsync(); } catch {}
      recordingRef.current = null;
      setPhaseSync('paused');
      lastActiveTimeRef.current = Date.now();
      proactiveCountRef.current = 0;
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
      allowsRecordingIOS: false,
      playsInSilentModeIOS: true,
      staysActiveInBackground: false,
      shouldDuckAndroid: true,
      playThroughEarpieceAndroid: false,
    }).catch(() => {});

    if (callMsgs.length > 0) {
      const divider: Message = {
        id: `div_${Date.now()}`, role: 'gojo',
        text: `── 通话记录（${formatDuration(duration)}）──`,
        time: nowTime(),
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
    `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;

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
  ringSpeak:   { borderColor: '#22c55e', shadowColor: '#22c55e', shadowOffset: { width: 0, height: 0 }, shadowOpacity: 0.7, shadowRadius: 16, elevation: 10 },
  ringRespond: { borderColor: C.accent, shadowColor: C.accent, shadowOffset: { width: 0, height: 0 }, shadowOpacity: 0.8, shadowRadius: 16, elevation: 10 },
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
  hangupBtn:   { width: 66, height: 66, borderRadius: 33, backgroundColor: '#ef4444', alignItems: 'center', justifyContent: 'center', shadowColor: '#ef4444', shadowOffset: { width: 0, height: 4 }, shadowOpacity: 0.4, shadowRadius: 8, elevation: 8 },
  hangupIcon:  { fontSize: 28 },

  endedOverlay:  { position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, backgroundColor: '#080e1a', alignItems: 'center', justifyContent: 'center', zIndex: 99 },
  endedIcon:     { fontSize: 70, marginBottom: 24, opacity: 0.7 },
  endedTitle:    { color: '#fff', fontSize: 26, fontWeight: '700', marginBottom: 12, letterSpacing: 1 },
  endedDuration: { color: '#94a3b8', fontSize: 15, marginBottom: 8 },
  endedHint:     { color: '#475569', fontSize: 12, marginTop: 20 },
});