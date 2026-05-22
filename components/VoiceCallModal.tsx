// components/VoiceCallModal.tsx — 语音通话界面
import axios from 'axios';
import { Audio } from 'expo-av';
import React, { useEffect, useRef, useState } from 'react';
import {
  Dimensions,
  Modal,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TouchableOpacity,
  View,
} from 'react-native';
import { C, SERVER_URL, nowTime } from '../constants/theme';
import { Message } from '../app/(tabs)/chat';

let Voice: any = null;
try { Voice = require('@react-native-voice/voice').default; } catch {}

const { width, height } = Dimensions.get('window');

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

type CallState = 'idle' | 'listening' | 'thinking' | 'speaking';

export default function VoiceCallModal({ userId, onClose, onAddMessages }: Props) {
  const [callState, setCallState] = useState<CallState>('idle');
  const [callMsgs, setCallMsgs]   = useState<CallMsg[]>([]);
  const [subtitle, setSubtitle]   = useState(''); // Gojo 说话时的字幕
  const [partialText, setPartialText] = useState('');
  const [duration, setDuration]   = useState(0); // 通话时长(秒)
  const [isMuted, setIsMuted]     = useState(false);
  const [isSpeaker, setIsSpeaker] = useState(true);

  const currentSoundRef = useRef<Audio.Sound | null>(null);
  const scrollRef       = useRef<ScrollView>(null);
  const timerRef        = useRef<ReturnType<typeof setInterval> | null>(null);
  const activeRef       = useRef(true); // 通话是否还在进行

  // 通话计时器
  useEffect(() => {
    timerRef.current = setInterval(() => setDuration(d => d + 1), 1000);
    return () => { if (timerRef.current) clearInterval(timerRef.current); };
  }, []);

  // 语音识别
  useEffect(() => {
    if (!Voice) return;
    Voice.onSpeechStart = () => setCallState('listening');
    Voice.onSpeechEnd   = () => {};
    Voice.onSpeechPartialResults = (e: any) => setPartialText(e.value?.[0] || '');
    Voice.onSpeechResults = async (e: any) => {
      const text = e.value?.[0] || '';
      setPartialText('');
      if (!text.trim() || !activeRef.current) return;

      // 添加用户消息
      const userMsg: CallMsg = { id: Date.now().toString(), role: 'user', zh: text, time: nowTime() };
      setCallMsgs(prev => [...prev, userMsg]);

      // 发给 Gojo
      await sendToGojo(text);
    };
    Voice.onSpeechError = (e: any) => {
      console.warn('Call voice error:', e);
      setCallState('idle');
      setPartialText('');
    };
    return () => {
      Voice?.destroy().then(() => Voice?.removeAllListeners?.());
    };
  }, []);

  // 设置扬声器
  useEffect(() => {
    Audio.setAudioModeAsync({
      allowsRecordingIOS: false,
      playsInSilentModeIOS: true,
      staysActiveInBackground: false,
      shouldDuckAndroid: true,
      playThroughEarpieceAndroid: !isSpeaker,
    }).catch(() => {});
  }, [isSpeaker]);

  const formatDuration = (s: number) => {
    const m = Math.floor(s / 60);
    const sec = s % 60;
    return `${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`;
  };

  // 开始/停止说话
  const toggleListening = async () => {
    if (!Voice) return;
    if (callState === 'listening') {
      await Voice.stop();
      setCallState('idle');
    } else if (callState === 'idle') {
      setPartialText('');
      await Voice.start('zh-CN');
      setCallState('listening');
    }
  };

  // 发送给 Gojo
  const sendToGojo = async (text: string) => {
    if (!activeRef.current) return;
    setCallState('thinking');
    setSubtitle('...');

    try {
      const res = await axios.post(`${SERVER_URL}/chat/text`, { text, user_id: userId });

      if (!activeRef.current) return;
      setCallState('speaking');

      const segments = Array.isArray(res.data?.messages)
        ? res.data.messages
        : res.data?.jp ? [{ jp: res.data.jp, zh: res.data.zh || '', audio_b64: res.data.audio_b64 || '' }]
        : [];

      for (let i = 0; i < segments.length; i++) {
        if (!activeRef.current) break;
        const seg = segments[i];

        const gojoMsg: CallMsg = {
          id: `gojo_${Date.now()}_${i}`,
          role: 'gojo',
          jp: seg.jp,
          zh: seg.zh,
          time: nowTime(),
        };
        setCallMsgs(prev => [...prev, gojoMsg]);
        setSubtitle(seg.zh || seg.jp || '');
        scrollRef.current?.scrollToEnd({ animated: true });

        if (seg.audio_b64 && seg.audio_b64.length > 100) {
          await playAudio(seg.audio_b64);
        } else {
          await new Promise(r => setTimeout(r, 1000));
        }
      }
    } catch (e) {
      console.warn('Call send error', e);
    } finally {
      if (activeRef.current) {
        setCallState('idle');
        setSubtitle('');
      }
    }
  };

  const playAudio = (audio_b64: string): Promise<void> => {
    return new Promise(async (resolve) => {
      try {
        if (currentSoundRef.current) {
          await currentSoundRef.current.unloadAsync();
          currentSoundRef.current = null;
        }
        const { sound } = await Audio.Sound.createAsync(
          { uri: `data:audio/mp3;base64,${audio_b64}` },
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
  };

  // 挂断
  const hangUp = async () => {
    activeRef.current = false;
    if (timerRef.current) clearInterval(timerRef.current);

    // 停止语音识别
    try { await Voice?.stop(); await Voice?.destroy(); } catch {}

    // 停止音频
    try {
      await currentSoundRef.current?.unloadAsync();
      currentSoundRef.current = null;
    } catch {}

    // 恢复音频模式
    await Audio.setAudioModeAsync({
      allowsRecordingIOS: false,
      playsInSilentModeIOS: true,
      staysActiveInBackground: false,
      shouldDuckAndroid: true,
      playThroughEarpieceAndroid: false,
    }).catch(() => {});

    // 把通话记录加入聊天历史
    if (callMsgs.length > 0) {
      const divider: Message = {
        id: `divider_${Date.now()}`,
        role: 'gojo',
        text: `── 通话记录（${formatDuration(duration)}）──`,
        time: nowTime(),
      };
      const chatMsgs: Message[] = callMsgs.map(m => ({
        id: m.id,
        role: m.role,
        text: m.role === 'gojo' ? (m.jp || m.zh) : m.zh,
        subtitle: m.role === 'gojo' ? m.zh : undefined,
        time: m.time,
      }));
      onAddMessages([divider, ...chatMsgs]);
    }

    onClose();
  };

  // 状态文字
  const stateLabel = {
    idle:      '点击麦克风开始说话',
    listening: '正在聆听...',
    thinking:  '五条悟思考中...',
    speaking:  '',
  }[callState];

  const micColor = callState === 'listening' ? '#ef4444' : callState === 'idle' ? '#fff' : '#9ca3af';

  return (
    <Modal visible animationType="slide" statusBarTranslucent>
      <View style={s.screen}>

        {/* 顶部：计时器 */}
        <View style={s.topBar}>
          <Text style={s.timer}>{formatDuration(duration)}</Text>
        </View>

        {/* 头像区 */}
        <View style={s.avatarArea}>
          <View style={[s.avatarRing, callState === 'speaking' && s.avatarRingPulse]}>
            <View style={s.avatar}>
              <Text style={s.avatarText}>悟</Text>
            </View>
          </View>
          <Text style={s.name}>五条悟</Text>
          <Text style={s.stateText}>{stateLabel}</Text>
        </View>

        {/* 字幕区（Gojo 说话时显示） */}
        {(callState === 'speaking' && subtitle) && (
          <View style={s.subtitleBox}>
            <Text style={s.subtitleText}>{subtitle}</Text>
          </View>
        )}

        {/* 实时识别预览 */}
        {partialText !== '' && (
          <View style={s.partialBox}>
            <Text style={s.partialText}>{partialText}</Text>
          </View>
        )}

        {/* 对话记录（可滚动） */}
        <ScrollView
          ref={scrollRef}
          style={s.msgList}
          contentContainerStyle={{ padding: 16 }}
          onContentSizeChange={() => scrollRef.current?.scrollToEnd({ animated: true })}
        >
          {callMsgs.map(msg => (
            <View key={msg.id} style={[s.callMsg, msg.role === 'user' ? s.callMsgUser : s.callMsgGojo]}>
              <Text style={[s.callMsgText, msg.role === 'user' && { color: '#fff' }]}>
                {msg.role === 'gojo' && msg.jp ? msg.jp : msg.zh}
              </Text>
              {msg.role === 'gojo' && msg.zh && (
                <Text style={s.callMsgSub}>{msg.zh}</Text>
              )}
            </View>
          ))}
        </ScrollView>

        {/* 底部控制按钮 */}
        <View style={s.controls}>
          {/* 静音 */}
          <TouchableOpacity
            style={[s.ctrlBtn, isMuted && s.ctrlBtnActive]}
            onPress={() => setIsMuted(m => !m)}
          >
            <Text style={s.ctrlIcon}>{isMuted ? '🔇' : '🎤'}</Text>
            <Text style={s.ctrlLabel}>{isMuted ? '已静音' : '麦克风'}</Text>
          </TouchableOpacity>

          {/* 挂断 */}
          <TouchableOpacity style={s.hangupBtn} onPress={hangUp}>
            <Text style={s.hangupIcon}>📵</Text>
          </TouchableOpacity>

          {/* 扬声器 */}
          <TouchableOpacity
            style={[s.ctrlBtn, isSpeaker && s.ctrlBtnActive]}
            onPress={() => setIsSpeaker(s => !s)}
          >
            <Text style={s.ctrlIcon}>{isSpeaker ? '🔊' : '🔈'}</Text>
            <Text style={s.ctrlLabel}>{isSpeaker ? '扬声器' : '听筒'}</Text>
          </TouchableOpacity>
        </View>

        {/* 麦克风大按钮 */}
        <TouchableOpacity
          style={[
            s.micBtn,
            callState === 'listening' && s.micBtnListening,
            (callState === 'thinking' || callState === 'speaking') && s.micBtnDisabled,
          ]}
          onPress={toggleListening}
          disabled={callState === 'thinking' || callState === 'speaking'}
        >
          <Text style={s.micIcon}>🎙</Text>
          <Text style={[s.micLabel, { color: micColor }]}>
            {callState === 'listening' ? '点击停止' : '点击说话'}
          </Text>
        </TouchableOpacity>

      </View>
    </Modal>
  );
}

const s = StyleSheet.create({
  screen: {
    flex: 1,
    backgroundColor: '#0a0f1a',
    alignItems: 'center',
    paddingTop: Platform.OS === 'ios' ? 50 : 30,
    paddingBottom: 30,
  },

  topBar:  { width: '100%', alignItems: 'center', paddingVertical: 8 },
  timer:   { color: '#94a3b8', fontSize: 16, letterSpacing: 2 },

  avatarArea: { alignItems: 'center', marginTop: 24, marginBottom: 16 },
  avatarRing: {
    width: 140, height: 140, borderRadius: 70,
    borderWidth: 2, borderColor: C.accent + '60',
    alignItems: 'center', justifyContent: 'center',
    marginBottom: 16,
  },
  avatarRingPulse: {
    borderColor: C.accent,
    shadowColor: C.accent,
    shadowOffset: { width: 0, height: 0 },
    shadowOpacity: 0.8,
    shadowRadius: 12,
    elevation: 10,
  },
  avatar: {
    width: 120, height: 120, borderRadius: 60,
    backgroundColor: C.accentDim,
    alignItems: 'center', justifyContent: 'center',
    borderWidth: 2, borderColor: C.accent,
  },
  avatarText: { color: '#fff', fontSize: 48, fontWeight: '700' },
  name:       { color: '#fff', fontSize: 22, fontWeight: '600', marginBottom: 6 },
  stateText:  { color: '#94a3b8', fontSize: 13 },

  subtitleBox: {
    marginHorizontal: 24, marginVertical: 8,
    backgroundColor: 'rgba(255,255,255,0.08)',
    borderRadius: 12, padding: 12,
    maxWidth: width - 48,
  },
  subtitleText: { color: '#e2e8f0', fontSize: 14, textAlign: 'center', lineHeight: 22 },

  partialBox:   {
    marginHorizontal: 24, marginVertical: 4,
    backgroundColor: C.accent + '22',
    borderRadius: 10, padding: 10,
    maxWidth: width - 48,
  },
  partialText:  { color: C.accent, fontSize: 13, textAlign: 'center', fontStyle: 'italic' },

  msgList:      { flex: 1, width: '100%' },
  callMsg:      { marginBottom: 10, maxWidth: width * 0.7, borderRadius: 14, padding: 10 },
  callMsgUser:  { alignSelf: 'flex-end', backgroundColor: C.accent },
  callMsgGojo:  { alignSelf: 'flex-start', backgroundColor: 'rgba(255,255,255,0.08)', borderLeftWidth: 2, borderLeftColor: C.accent },
  callMsgText:  { color: '#e2e8f0', fontSize: 14, lineHeight: 20 },
  callMsgSub:   { color: '#94a3b8', fontSize: 12, marginTop: 4, fontStyle: 'italic' },

  controls:     { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-around', width: '100%', paddingHorizontal: 24, marginTop: 8 },
  ctrlBtn:      { alignItems: 'center', padding: 12, borderRadius: 16, width: 70 },
  ctrlBtnActive:{ backgroundColor: 'rgba(255,255,255,0.15)' },
  ctrlIcon:     { fontSize: 24, marginBottom: 4 },
  ctrlLabel:    { color: '#94a3b8', fontSize: 11 },

  hangupBtn:    {
    width: 68, height: 68, borderRadius: 34,
    backgroundColor: '#ef4444',
    alignItems: 'center', justifyContent: 'center',
    shadowColor: '#ef4444', shadowOffset: { width: 0, height: 4 }, shadowOpacity: 0.4, shadowRadius: 8,
    elevation: 8,
  },
  hangupIcon:   { fontSize: 28 },

  micBtn: {
    width: 80, height: 80, borderRadius: 40,
    backgroundColor: 'rgba(255,255,255,0.1)',
    borderWidth: 2, borderColor: 'rgba(255,255,255,0.2)',
    alignItems: 'center', justifyContent: 'center',
    marginTop: 16,
  },
  micBtnListening: {
    backgroundColor: '#ef444422',
    borderColor: '#ef4444',
    shadowColor: '#ef4444', shadowOffset: { width: 0, height: 0 }, shadowOpacity: 0.6, shadowRadius: 12,
    elevation: 6,
  },
  micBtnDisabled: { opacity: 0.4 },
  micIcon:  { fontSize: 32 },
  micLabel: { fontSize: 11, marginTop: 4 },
});
