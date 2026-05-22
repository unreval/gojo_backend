// app/components/VoiceCallModal.tsx — 语音通话界面（打字输入 + 五条悟语音回复）
import axios from 'axios';
import { Audio } from 'expo-av';
import React, { useEffect, useRef, useState } from 'react';
import {
  Dimensions,
  KeyboardAvoidingView,
  Modal,
  Platform,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  View,
} from 'react-native';
import { Message } from '../app/(tabs)/chat';
import { C, SERVER_URL, nowTime } from '../constants/theme';

const { width } = Dimensions.get('window');

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

type CallState = 'idle' | 'thinking' | 'speaking';

export default function VoiceCallModal({ userId, onClose, onAddMessages }: Props) {
  const [callState, setCallState] = useState<CallState>('idle');
  const [callMsgs, setCallMsgs]   = useState<CallMsg[]>([]);
  const [subtitle, setSubtitle]   = useState('');
  const [inputText, setInputText] = useState('');
  const [duration, setDuration]   = useState(0);
  const [isSpeaker, setIsSpeaker] = useState(true);

  const currentSoundRef = useRef<Audio.Sound | null>(null);
  const scrollRef       = useRef<ScrollView>(null);
  const timerRef        = useRef<ReturnType<typeof setInterval> | null>(null);
  const activeRef       = useRef(true);
  const inputRef        = useRef<TextInput>(null);

  // 计时器
  useEffect(() => {
    timerRef.current = setInterval(() => setDuration(d => d + 1), 1000);
    return () => { if (timerRef.current) clearInterval(timerRef.current); };
  }, []);

  // 扬声器设置
  useEffect(() => {
    Audio.setAudioModeAsync({
      allowsRecordingIOS: false,
      playsInSilentModeIOS: true,
      staysActiveInBackground: false,
      shouldDuckAndroid: true,
      playThroughEarpieceAndroid: !isSpeaker,
    }).catch(() => {});
  }, [isSpeaker]);

  const formatDuration = (s: number) =>
    `${String(Math.floor(s/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')}`;

  // 发送消息给 Gojo
  const sendMessage = async () => {
    const text = inputText.trim();
    if (!text || callState !== 'idle') return;
    setInputText('');

    const userMsg: CallMsg = { id: Date.now().toString(), role: 'user', zh: text, time: nowTime() };
    setCallMsgs(prev => [...prev, userMsg]);
    setCallState('thinking');
    setSubtitle('...');
    scrollRef.current?.scrollToEnd({ animated: true });

    try {
      const res = await axios.post(`${SERVER_URL}/chat/text`, { text, user_id: userId });
      if (!activeRef.current) return;

      setCallState('speaking');

      const segments = Array.isArray(res.data?.messages)
        ? res.data.messages
        : res.data?.jp
          ? [{ jp: res.data.jp, zh: res.data.zh || '', audio_b64: res.data.audio_b64 || '' }]
          : [];

      for (let i = 0; i < segments.length; i++) {
        if (!activeRef.current) break;
        const seg = segments[i];
        const gojoMsg: CallMsg = {
          id: `gojo_${Date.now()}_${i}`,
          role: 'gojo', jp: seg.jp, zh: seg.zh, time: nowTime(),
        };
        setCallMsgs(prev => [...prev, gojoMsg]);
        setSubtitle(seg.zh || seg.jp || '');
        scrollRef.current?.scrollToEnd({ animated: true });

        if (seg.audio_b64 && seg.audio_b64.length > 100) {
          await playAudio(seg.audio_b64);
        } else {
          await new Promise(r => setTimeout(r, 800));
        }
      }
    } catch (e) {
      console.warn('Call send error', e);
    } finally {
      if (activeRef.current) {
        setCallState('idle');
        setSubtitle('');
        setTimeout(() => inputRef.current?.focus(), 100);
      }
    }
  };

  const playAudio = (b64: string): Promise<void> =>
    new Promise(async (resolve) => {
      try {
        if (currentSoundRef.current) {
          await currentSoundRef.current.unloadAsync();
          currentSoundRef.current = null;
        }
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

  // 挂断
  const hangUp = async () => {
    activeRef.current = false;
    if (timerRef.current) clearInterval(timerRef.current);
    try {
      await currentSoundRef.current?.unloadAsync();
      currentSoundRef.current = null;
    } catch {}
    await Audio.setAudioModeAsync({
      allowsRecordingIOS: false, playsInSilentModeIOS: true,
      staysActiveInBackground: false, shouldDuckAndroid: true, playThroughEarpieceAndroid: false,
    }).catch(() => {});

    // 把通话记录加入聊天历史
    if (callMsgs.length > 0) {
      const divider: Message = {
        id: `divider_${Date.now()}`, role: 'gojo',
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

  const stateLabel = {
    idle:     '输入文字开始对话',
    thinking: '五条悟思考中...',
    speaking: '',
  }[callState];

  return (
    <Modal visible animationType="slide" statusBarTranslucent>
      <KeyboardAvoidingView
        style={{ flex: 1 }}
        behavior={Platform.OS === 'ios' ? 'padding' : 'height'}
      >
        <View style={s.screen}>

          {/* 顶部计时器 */}
          <View style={s.topBar}>
            <Text style={s.timer}>{formatDuration(duration)}</Text>
          </View>

          {/* 头像 */}
          <View style={s.avatarArea}>
            <View style={[s.avatarRing, callState === 'speaking' && s.avatarRingPulse]}>
              <View style={s.avatar}>
                <Text style={s.avatarText}>悟</Text>
              </View>
            </View>
            <Text style={s.name}>五条悟</Text>
            <Text style={s.stateText}>{stateLabel}</Text>
          </View>

          {/* 字幕（说话时显示） */}
          {callState === 'speaking' && subtitle !== '' && (
            <View style={s.subtitleBox}>
              <Text style={s.subtitleText}>{subtitle}</Text>
            </View>
          )}

          {/* 对话记录 */}
          <ScrollView
            ref={scrollRef}
            style={s.msgList}
            contentContainerStyle={{ padding: 16, paddingBottom: 8 }}
            onContentSizeChange={() => scrollRef.current?.scrollToEnd({ animated: true })}
          >
            {callMsgs.length === 0 && (
              <Text style={s.emptyHint}>发送消息，五条悟会用语音回复你</Text>
            )}
            {callMsgs.map(msg => (
              <View key={msg.id} style={[s.callMsg, msg.role === 'user' ? s.callMsgUser : s.callMsgGojo]}>
                <Text style={[s.callMsgText, msg.role === 'user' && { color: '#fff' }]}>
                  {msg.role === 'gojo' && msg.jp ? msg.jp : msg.zh}
                </Text>
                {msg.role === 'gojo' && msg.zh && msg.jp && msg.zh !== msg.jp && (
                  <Text style={s.callMsgSub}>{msg.zh}</Text>
                )}
              </View>
            ))}
          </ScrollView>

          {/* 底部控制 */}
          <View style={s.controls}>
            <TouchableOpacity style={s.ctrlBtn} onPress={() => setIsSpeaker(s => !s)}>
              <Text style={s.ctrlIcon}>{isSpeaker ? '🔊' : '🔈'}</Text>
              <Text style={s.ctrlLabel}>{isSpeaker ? '扬声器' : '听筒'}</Text>
            </TouchableOpacity>

            <TouchableOpacity style={s.hangupBtn} onPress={hangUp}>
              <Text style={s.hangupIcon}>📵</Text>
            </TouchableOpacity>

            <View style={s.ctrlBtn}>
              <Text style={s.ctrlIcon}>💬</Text>
              <Text style={s.ctrlLabel}>文字通话</Text>
            </View>
          </View>

          {/* 输入框 */}
          <View style={s.inputBar}>
            <TextInput
              ref={inputRef}
              style={s.input}
              value={inputText}
              onChangeText={setInputText}
              placeholder={callState !== 'idle' ? '等待五条悟回复...' : '说点什么...'}
              placeholderTextColor="#64748b"
              editable={callState === 'idle'}
              multiline
              maxLength={200}
            />
            <TouchableOpacity
              style={[s.sendBtn, { backgroundColor: (inputText.trim() && callState === 'idle') ? C.accent : '#334155' }]}
              onPress={sendMessage}
              disabled={!inputText.trim() || callState !== 'idle'}
            >
              <Text style={s.sendBtnText}>发送</Text>
            </TouchableOpacity>
          </View>

        </View>
      </KeyboardAvoidingView>
    </Modal>
  );
}

const s = StyleSheet.create({
  screen:    { flex: 1, backgroundColor: '#0a0f1a', alignItems: 'center', paddingTop: Platform.OS === 'ios' ? 50 : 30 },
  topBar:    { width: '100%', alignItems: 'center', paddingVertical: 8 },
  timer:     { color: '#94a3b8', fontSize: 16, letterSpacing: 2 },
  avatarArea:{ alignItems: 'center', marginTop: 16, marginBottom: 12 },
  avatarRing:{ width: 120, height: 120, borderRadius: 60, borderWidth: 2, borderColor: C.accent + '60', alignItems: 'center', justifyContent: 'center', marginBottom: 12 },
  avatarRingPulse: { borderColor: C.accent, shadowColor: C.accent, shadowOffset: { width:0, height:0 }, shadowOpacity: 0.8, shadowRadius: 12, elevation: 10 },
  avatar:    { width: 100, height: 100, borderRadius: 50, backgroundColor: C.accentDim, alignItems: 'center', justifyContent: 'center', borderWidth: 2, borderColor: C.accent },
  avatarText:{ color: '#fff', fontSize: 40, fontWeight: '700' },
  name:      { color: '#fff', fontSize: 20, fontWeight: '600', marginBottom: 4 },
  stateText: { color: '#94a3b8', fontSize: 13 },
  subtitleBox:{ marginHorizontal: 24, marginBottom: 8, backgroundColor: 'rgba(255,255,255,0.08)', borderRadius: 12, padding: 12, maxWidth: width - 48 },
  subtitleText:{ color: '#e2e8f0', fontSize: 14, textAlign: 'center', lineHeight: 22 },
  msgList:   { flex: 1, width: '100%' },
  emptyHint: { color: '#475569', fontSize: 13, textAlign: 'center', marginTop: 20 },
  callMsg:   { marginBottom: 10, maxWidth: width * 0.72, borderRadius: 14, padding: 10 },
  callMsgUser:{ alignSelf: 'flex-end', backgroundColor: C.accent },
  callMsgGojo:{ alignSelf: 'flex-start', backgroundColor: 'rgba(255,255,255,0.08)', borderLeftWidth: 2, borderLeftColor: C.accent },
  callMsgText:{ color: '#e2e8f0', fontSize: 14, lineHeight: 20 },
  callMsgSub: { color: '#94a3b8', fontSize: 12, marginTop: 4, fontStyle: 'italic' },
  controls:  { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-around', width: '100%', paddingHorizontal: 24, paddingVertical: 12 },
  ctrlBtn:   { alignItems: 'center', padding: 10, width: 70 },
  ctrlIcon:  { fontSize: 24, marginBottom: 4 },
  ctrlLabel: { color: '#94a3b8', fontSize: 11 },
  hangupBtn: { width: 64, height: 64, borderRadius: 32, backgroundColor: '#ef4444', alignItems: 'center', justifyContent: 'center', shadowColor: '#ef4444', shadowOffset: { width:0, height:4 }, shadowOpacity: 0.4, shadowRadius: 8, elevation: 8 },
  hangupIcon:{ fontSize: 26 },
  inputBar:  { flexDirection: 'row', alignItems: 'flex-end', width: '100%', paddingHorizontal: 12, paddingVertical: 10, borderTopWidth: 1, borderTopColor: '#1e293b', gap: 8, backgroundColor: '#0f172a' },
  input:     { flex: 1, backgroundColor: '#1e293b', borderRadius: 20, paddingHorizontal: 16, paddingVertical: 10, color: '#e2e8f0', fontSize: 14, maxHeight: 80, borderWidth: 1, borderColor: '#334155' },
  sendBtn:   { borderRadius: 20, paddingHorizontal: 18, paddingVertical: 10, minWidth: 60, alignItems: 'center' },
  sendBtnText:{ color: '#fff', fontWeight: '600', fontSize: 14 },
});