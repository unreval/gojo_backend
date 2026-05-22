// app/(tabs)/chat.tsx
import AsyncStorage from '@react-native-async-storage/async-storage';
import axios from 'axios';
import { Audio } from 'expo-av';
import * as Clipboard from 'expo-clipboard';
import * as Notifications from 'expo-notifications';
import React, { useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  Dimensions,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StatusBar,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  View,
} from 'react-native';
import VoiceCallModal from '../../components/VoiceCallModal';
import { C, SERVER_URL, nowTime } from '../../constants/theme';

// 动态引入语音识别（避免模拟器/未安装时报错）
let Voice: any = null;
try { Voice = require('@react-native-voice/voice').default; } catch {}

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowAlert: true,
    shouldPlaySound: true,
    shouldSetBadge: false,
  }),
});

const { width } = Dimensions.get('window');
const STORAGE_KEY  = 'gojo_messages_v2';
const USER_ID_KEY  = 'gojo_user_id';
const MSG_DELAY_MS = 800;

export interface Message {
  id: string;
  role: 'user' | 'gojo';
  text: string;
  subtitle?: string;
  time?: string;
}

interface GojoSegment {
  jp: string;
  zh: string;
  audio_b64: string;
}

function generateUserId(): string {
  return 'user_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

function sleep(ms: number) {
  return new Promise<void>(r => setTimeout(r, ms));
}

function formatToday(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

export default function ChatScreen() {
  const [messages, setMessages]   = useState<Message[]>([]);
  const [inputText, setInputText] = useState('');
  const [loading, setLoading]     = useState(false);
  const [ready, setReady]         = useState(false);
  const [userId, setUserId]       = useState('');

  // 语音输入相关
  const [voiceMode, setVoiceMode]     = useState(false);
  const [isListening, setIsListening] = useState(false);
  const [partialText, setPartialText] = useState('');

  // 音频缓存（内存，用于重播）
  const audioCacheRef = useRef<Record<string, string>>({});

  // 语音通话
  const [showCall, setShowCall] = useState(false);

  // 搜索
  const [searchMode, setSearchMode]   = useState(false);
  const [searchQuery, setSearchQuery] = useState('');

  const scrollRef       = useRef<ScrollView>(null);
  const searchRef       = useRef<TextInput>(null);
  const currentSoundRef = useRef<Audio.Sound | null>(null);

  // ── 初始化 ──
  useEffect(() => {
    (async () => {
      try {
        await Audio.setAudioModeAsync({
          allowsRecordingIOS: false,
          playsInSilentModeIOS: true,
          staysActiveInBackground: false,
          shouldDuckAndroid: true,
          playThroughEarpieceAndroid: false,
        });
        const { status } = await Notifications.requestPermissionsAsync();
        if (status !== 'granted') console.warn('通知权限未授予');
        if (Platform.OS === 'android') {
          await Notifications.setNotificationChannelAsync('gojo-reminders', {
            name: '五条悟提醒',
            importance: Notifications.AndroidImportance.HIGH,
            sound: 'default',
            vibrationPattern: [0, 250, 250, 250],
          });
        }
        let uid = await AsyncStorage.getItem(USER_ID_KEY);
        if (!uid) { uid = generateUserId(); await AsyncStorage.setItem(USER_ID_KEY, uid); }
        setUserId(uid);
        const saved = await AsyncStorage.getItem(STORAGE_KEY);
        if (saved) setMessages(JSON.parse(saved));
      } catch (e) { console.warn('init error', e); }
      setReady(true);
    })();

    // 语音识别事件绑定
    if (Voice) {
      Voice.onSpeechStart = () => setIsListening(true);
      Voice.onSpeechEnd   = () => setIsListening(false);
      Voice.onSpeechPartialResults = (e: any) => setPartialText(e.value?.[0] || '');
      Voice.onSpeechResults = (e: any) => {
        const text = e.value?.[0] || '';
        setInputText(text);
        setPartialText('');
        setIsListening(false);
      };
      Voice.onSpeechError = (e: any) => {
        console.warn('Voice error:', e);
        setIsListening(false);
        setPartialText('');
      };
    }

    return () => {
      currentSoundRef.current?.unloadAsync().catch(() => {});
      if (Voice) Voice.destroy().then(() => Voice?.removeAllListeners?.());
    };
  }, []);

  useEffect(() => {
    if (!ready) return;
    AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(messages)).catch(() => {});
  }, [messages, ready]);

  // ── 语音识别 ──
  const startVoiceInput = async () => {
    if (!Voice) { Alert.alert('提示', '请先安装 @react-native-voice/voice 并重新 build'); return; }
    try {
      setInputText('');
      setPartialText('');
      await Voice.start('zh-CN');
    } catch (e) { console.warn('start voice error', e); }
  };

  const stopVoiceInput = async () => {
    if (!Voice) return;
    try { await Voice.stop(); } catch {}
  };

  // ── 音频重播 ──
  const replayAudio = async (msgId: string) => {
    const b64 = audioCacheRef.current[msgId];
    if (!b64) return;
    await playAudioAndWait(b64);
  };

  // ── 播放音频 ──
  const playAudioAndWait = async (audio_b64: string): Promise<void> => {
    try {
      if (currentSoundRef.current) {
        await currentSoundRef.current.unloadAsync();
        currentSoundRef.current = null;
      }
    } catch {}
    return new Promise<void>(async (resolve) => {
      try {
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
      } catch (e) { resolve(); }
    });
  };

  // ── 提醒 ──
  const scheduleReminder = async (reminder: {
    date: string; time: string; content: string; notification?: string; task_id?: number;
  }) => {
    try {
      const { status } = await Notifications.getPermissionsAsync();
      if (status !== 'granted') {
        const ns = await Notifications.requestPermissionsAsync();
        if (ns.status !== 'granted') { Alert.alert('通知权限未授予'); return; }
      }
      const [hour, minute] = (reminder.time || '00:00').split(':').map(Number);
      const [year, month, day] = (reminder.date || formatToday()).split('-').map(Number);
      const triggerDate = new Date(year, month - 1, day, hour, minute, 0);
      if (triggerDate <= new Date()) return;
      const notifId = await Notifications.scheduleNotificationAsync({
        content: {
          title: '五条悟',
          body: reminder.notification || `おい、${reminder.content}の時間だよ。\n（喂，该${reminder.content}了。）`,
          sound: 'default',
          ...(Platform.OS === 'android' ? { channelId: 'gojo-reminders' } : {}),
        },
        trigger: { type: Notifications.SchedulableTriggerInputTypes.DATE, date: triggerDate } as any,
      });
      if (reminder.task_id && notifId) {
        await axios.put(`${SERVER_URL}/tasks/${reminder.task_id}`, { notification_id: notifId }).catch(() => {});
      }
    } catch (e: any) { console.warn('reminder error', e); }
  };

  // ── 发送消息 ──
  const sendText = async (textOverride?: string) => {
    const text = (textOverride ?? inputText).trim();
    if (!text || loading || !userId) return;
    setInputText('');
    setPartialText('');
    if (searchMode) { setSearchMode(false); setSearchQuery(''); }

    const userMsg: Message = { id: Date.now().toString(), role: 'user', text, time: nowTime() };
    setMessages(prev => [...prev, userMsg]);
    setLoading(true);

    try {
      const res = await axios.post(`${SERVER_URL}/chat/text`, { text, user_id: userId });
      if (res.data?.total_days) AsyncStorage.setItem('gojo_chat_days', String(res.data.total_days));
      if (res.data?.reminder?.date && res.data.reminder.time) await scheduleReminder(res.data.reminder);

      let segments: GojoSegment[] = [];
      if (Array.isArray(res.data?.messages) && res.data.messages.length > 0) {
        segments = res.data.messages;
      } else if (res.data?.jp) {
        segments = [{ jp: res.data.jp, zh: res.data.zh ?? '', audio_b64: res.data.audio_b64 ?? '' }];
      }

      if (segments.length === 0) { Alert.alert('回复异常', '没有收到有效回复'); return; }

      for (let i = 0; i < segments.length; i++) {
        const seg = segments[i];
        const msgId = `${Date.now()}_${i}`;
        // 缓存音频（用于重播）
        if (seg.audio_b64 && seg.audio_b64.length > 100) {
          audioCacheRef.current[msgId] = seg.audio_b64;
        }
        const gojoMsg: Message = { id: msgId, role: 'gojo', text: seg.jp, subtitle: seg.zh, time: nowTime() };
        setMessages(prev => [...prev, gojoMsg]);
        if (seg.audio_b64 && seg.audio_b64.length > 100) await playAudioAndWait(seg.audio_b64);
        if (i < segments.length - 1) await sleep(MSG_DELAY_MS);
      }
    } catch (e: any) {
      Alert.alert('连接失败', e?.message ?? '请确认服务器正常运行');
    } finally { setLoading(false); }
  };

  const clearHistory = () =>
    Alert.alert('清空记录', '确认清空所有聊天记录？', [
      { text: '取消', style: 'cancel' },
      { text: '清空', style: 'destructive',
        onPress: async () => { setMessages([]); await AsyncStorage.removeItem(STORAGE_KEY); } },
    ]);

  const toggleSearch = () => {
    if (searchMode) { setSearchMode(false); setSearchQuery(''); }
    else { setSearchMode(true); setTimeout(() => searchRef.current?.focus(), 100); }
  };

  const copyMessage = async (msg: Message) => {
    const text = msg.subtitle ? `${msg.text}\n${msg.subtitle}` : msg.text;
    await Clipboard.setStringAsync(text);
    Alert.alert('已复制', '', [{ text: '好', style: 'cancel' }], { cancelable: true });
  };

  const displayMessages = searchMode && searchQuery.trim()
    ? messages.filter(m =>
        m.text.toLowerCase().includes(searchQuery.toLowerCase()) ||
        (m.subtitle || '').toLowerCase().includes(searchQuery.toLowerCase()))
    : messages;

  if (!ready) return (
    <View style={{ flex: 1, backgroundColor: C.bg, alignItems: 'center', justifyContent: 'center' }}>
      <ActivityIndicator color={C.accent} />
    </View>
  );

  return (
    <KeyboardAvoidingView
      style={{ flex: 1, backgroundColor: C.bg }}
      behavior={Platform.OS === 'ios' ? 'padding' : 'height'}
    >
      <StatusBar barStyle="light-content" backgroundColor={C.card} />

      {/* 顶栏 */}
      <View style={s.header}>
        {!searchMode ? (
          <>
            <View style={s.avatarSmall}><Text style={s.avatarSmallText}>悟</Text></View>
            <View style={{ flex: 1 }}>
              <Text style={s.headerName}>五条悟</Text>
              <Text style={s.headerSub}>最强的男人</Text>
            </View>
            <TouchableOpacity onPress={() => setShowCall(true)} style={s.iconBtn}>
              <Text style={s.iconBtnText}>📞</Text>
            </TouchableOpacity>
            <TouchableOpacity onPress={toggleSearch} style={s.iconBtn}>
              <Text style={s.iconBtnText}>🔍</Text>
            </TouchableOpacity>
            <TouchableOpacity onPress={clearHistory} style={s.clearBtn}>
              <Text style={s.clearBtnText}>清空</Text>
            </TouchableOpacity>
          </>
        ) : (
          <>
            <TextInput
              ref={searchRef}
              style={s.searchInput}
              value={searchQuery}
              onChangeText={setSearchQuery}
              placeholder="搜索聊天记录..."
              placeholderTextColor={C.textMute}
            />
            <TouchableOpacity onPress={toggleSearch} style={s.cancelSearchBtn}>
              <Text style={s.cancelSearchText}>取消</Text>
            </TouchableOpacity>
          </>
        )}
      </View>

      {searchMode && searchQuery.trim() && (
        <View style={s.searchResultBar}>
          <Text style={s.searchResultText}>找到 {displayMessages.length} 条结果</Text>
        </View>
      )}

      {/* 消息列表 */}
      <ScrollView
        ref={scrollRef}
        style={s.chatArea}
        contentContainerStyle={s.chatContent}
        onContentSizeChange={() => { if (!searchMode) scrollRef.current?.scrollToEnd({ animated: true }); }}
      >
        {displayMessages.length === 0 && (
          <View style={s.emptyWrap}>
            {searchMode && searchQuery.trim() ? (
              <><Text style={s.emptyEmoji}>🔍</Text><Text style={s.emptyText}>没找到「{searchQuery}」</Text></>
            ) : (
              <><Text style={s.emptyEmoji}>👋</Text><Text style={s.emptyText}>跟五条悟说点什么吧</Text></>
            )}
          </View>
        )}

        {displayMessages.map(msg => {
          const hasAudio = !!audioCacheRef.current[msg.id];
          const isHighlighted = searchMode && searchQuery.trim() &&
            (msg.text.toLowerCase().includes(searchQuery.toLowerCase()) ||
             (msg.subtitle || '').toLowerCase().includes(searchQuery.toLowerCase()));

          return (
            <View key={msg.id} style={[s.msgRow, msg.role === 'user' ? s.msgRowUser : s.msgRowGojo]}>
              {msg.role === 'gojo' && (
                <View style={s.msgAvatar}><Text style={s.msgAvatarText}>悟</Text></View>
              )}
              <View style={[s.msgMain, msg.role === 'user' && { alignItems: 'flex-end' }]}>
                {msg.role === 'gojo' && <Text style={s.msgSender}>五条悟</Text>}
                <TouchableOpacity
                  activeOpacity={0.85}
                  onPress={() => { if (msg.role === 'gojo' && hasAudio) replayAudio(msg.id); }}
                  onLongPress={() => copyMessage(msg)}
                  delayLongPress={400}
                >
                  <View style={[
                    s.bubble,
                    msg.role === 'user' ? s.bubbleUser : s.bubbleGojo,
                    isHighlighted && s.bubbleHighlight,
                  ]}>
                    <Text style={[s.bubbleText, msg.role === 'user' && s.bubbleTextUser]}>
                      {msg.text}
                    </Text>
                    {msg.subtitle && <Text style={s.subtitle}>{msg.subtitle}</Text>}
                    {msg.role === 'gojo' && hasAudio && (
                      <Text style={s.replayIcon}>🔊 点击重播</Text>
                    )}
                  </View>
                </TouchableOpacity>
                <View style={s.msgBottom}>
                  <Text style={s.msgTime}>{msg.time}</Text>
                </View>
              </View>
            </View>
          );
        })}

        {loading && (
          <View style={s.msgRow}>
            <View style={s.msgAvatar}><Text style={s.msgAvatarText}>悟</Text></View>
            <View style={[s.bubble, s.bubbleGojo, { flexDirection: 'row', alignItems: 'center', gap: 8 }]}>
              <ActivityIndicator size="small" color={C.accent} />
              <Text style={{ color: C.textMute, fontSize: 13 }}>思考中...</Text>
            </View>
          </View>
        )}
      </ScrollView>

      {/* 语音实时预览 */}
      {isListening && partialText !== '' && (
        <View style={s.partialBar}>
          <Text style={s.partialText}>{partialText}</Text>
        </View>
      )}

      {/* 输入栏 */}
      <View style={s.inputBar}>
        {/* 左按钮：切换文字/语音消息模式 */}
        <TouchableOpacity
          style={s.modeBtn}
          onPress={() => { setVoiceMode(v => !v); if (isListening) stopVoiceInput(); }}
        >
          <Text style={s.modeBtnText}>{voiceMode ? '⌨️' : '🎙'}</Text>
        </TouchableOpacity>

        {/* 中间区域 */}
        {!voiceMode ? (
          <TextInput
            style={s.input}
            value={inputText}
            onChangeText={setInputText}
            placeholder={loading ? '五条悟正在回复中...' : isListening ? '正在聆听...' : '跟五条悟说点什么...'}
            placeholderTextColor={isListening ? C.accent : C.textMute}
            multiline
            editable={!loading}
          />
        ) : (
          /* 按住说话按钮 */
          <Pressable
            style={[s.voiceHoldBtn, isListening && s.voiceHoldBtnActive]}
            onPressIn={startVoiceInput}
            onPressOut={async () => {
              await stopVoiceInput();
              setTimeout(() => {
                if (inputText.trim()) { sendText(); setVoiceMode(false); }
              }, 700);
            }}
          >
            <Text style={s.voiceHoldText}>
              {isListening ? '🔴  松开 发送' : '🎙  按住 说话'}
            </Text>
          </Pressable>
        )}

        {/* 右按钮：有文字→发送；无文字+文字模式→🎤语音填充；语音模式不显示 */}
        {!voiceMode && (
          inputText.trim().length > 0 ? (
            <TouchableOpacity
              style={[s.sendBtn, { backgroundColor: loading ? C.textMute : C.accent }]}
              onPress={() => sendText()}
              disabled={loading}
            >
              <Text style={s.sendBtnText}>发送</Text>
            </TouchableOpacity>
          ) : (
            <TouchableOpacity
              style={[s.sendBtn, {
                backgroundColor: isListening ? C.accent : C.card,
                borderWidth: 1, borderColor: isListening ? C.accent : C.border,
              }]}
              onPress={isListening ? stopVoiceInput : startVoiceInput}
              disabled={loading}
            >
              <Text style={{ color: isListening ? '#fff' : C.textMute, fontSize: 18 }}>
                {isListening ? '✓' : '🎤'}
              </Text>
            </TouchableOpacity>
          )
        )}
      </View>

      {/* 语音通话弹窗 */}
      {showCall && (
        <VoiceCallModal
          userId={userId}
          onClose={() => setShowCall(false)}
          onAddMessages={(newMsgs: Message[]) => setMessages(prev => [...prev, ...newMsgs])}
        />
      )}
    </KeyboardAvoidingView>
  );
}

const s = StyleSheet.create({
  header: {
    flexDirection: 'row', alignItems: 'center',
    backgroundColor: C.card,
    paddingHorizontal: 16,
    paddingTop: Platform.OS === 'ios' ? 50 : 40,
    paddingBottom: 12,
    borderBottomWidth: 1, borderBottomColor: C.border,
    gap: 8,
  },
  avatarSmall:     { width: 40, height: 40, borderRadius: 20, backgroundColor: C.accentDim, alignItems: 'center', justifyContent: 'center', borderWidth: 1, borderColor: C.accent },
  avatarSmallText: { color: '#fff', fontWeight: '700', fontSize: 16 },
  headerName:      { color: C.text, fontSize: 16, fontWeight: '600' },
  headerSub:       { color: C.textMute, fontSize: 11, marginTop: 2 },
  iconBtn:         { padding: 8 },
  iconBtnText:     { fontSize: 18 },
  clearBtn:        { paddingHorizontal: 10, paddingVertical: 6, borderRadius: 10, borderWidth: 1, borderColor: C.border },
  clearBtnText:    { color: C.textMute, fontSize: 12 },
  searchInput:     { flex: 1, backgroundColor: C.bg, borderRadius: 12, borderWidth: 1, borderColor: C.border, paddingHorizontal: 14, paddingVertical: 8, color: C.text, fontSize: 14 },
  cancelSearchBtn: { paddingHorizontal: 8, paddingVertical: 6 },
  cancelSearchText:{ color: C.accent2 || '#5BC4FF', fontSize: 14 },
  searchResultBar: { backgroundColor: C.card, paddingHorizontal: 16, paddingVertical: 6, borderBottomWidth: 1, borderBottomColor: C.border },
  searchResultText:{ color: C.textMute, fontSize: 12 },
  chatArea:        { flex: 1, backgroundColor: C.bg },
  chatContent:     { padding: 16, paddingBottom: 8, flexGrow: 1 },
  emptyWrap:       { flex: 1, alignItems: 'center', justifyContent: 'center', paddingTop: 120 },
  emptyEmoji:      { fontSize: 48, marginBottom: 16 },
  emptyText:       { color: C.textMute, fontSize: 15 },
  msgRow:          { flexDirection: 'row', marginBottom: 16, alignItems: 'flex-start' },
  msgRowUser:      { flexDirection: 'row-reverse' },
  msgRowGojo:      {},
  msgAvatar:       { width: 34, height: 34, borderRadius: 17, backgroundColor: (C.accentDim || '#1e3a4a') + '55', alignItems: 'center', justifyContent: 'center', marginRight: 8, borderWidth: 1, borderColor: C.border },
  msgAvatarText:   { color: C.accent2 || '#5BC4FF', fontSize: 13, fontWeight: '700' },
  msgMain:         { maxWidth: width * 0.72 },
  msgSender:       { color: C.textMute, fontSize: 11, marginBottom: 4, marginLeft: 2 },
  bubble:          { borderRadius: 16, padding: 12 },
  bubbleGojo:      { backgroundColor: C.card, borderTopLeftRadius: 4, borderLeftWidth: 2, borderLeftColor: C.accent },
  bubbleUser:      { backgroundColor: C.userBubble || C.accent, borderRadius: 16, borderTopRightRadius: 4 },
  bubbleHighlight: { borderWidth: 1.5, borderColor: C.accent2 || '#5BC4FF' },
  bubbleText:      { color: C.text, fontSize: 15, lineHeight: 22 },
  bubbleTextUser:  { color: '#fff' },
  subtitle:        { color: C.textDim || C.textMute, fontSize: 12, marginTop: 6, lineHeight: 18, fontStyle: 'italic' },
  replayIcon:      { color: C.accent, fontSize: 11, marginTop: 6, opacity: 0.7 },
  msgBottom:       { flexDirection: 'row', alignItems: 'center', marginTop: 4, marginHorizontal: 2 },
  msgTime:         { color: C.textMute, fontSize: 10 },
  partialBar:      { backgroundColor: C.card + 'ee', paddingHorizontal: 16, paddingVertical: 8, borderTopWidth: 1, borderTopColor: C.border },
  partialText:     { color: C.accent, fontSize: 13, fontStyle: 'italic' },
  inputBar:        { flexDirection: 'row', alignItems: 'flex-end', backgroundColor: C.card, paddingHorizontal: 10, paddingVertical: 10, borderTopWidth: 1, borderTopColor: C.border, gap: 8 },
  modeBtn:         { width: 38, height: 38, borderRadius: 19, backgroundColor: C.bg, alignItems: 'center', justifyContent: 'center', borderWidth: 1, borderColor: C.border },
  modeBtnText:     { fontSize: 18 },
  input:           { flex: 1, backgroundColor: C.bg, borderRadius: 20, paddingHorizontal: 14, paddingVertical: 9, color: C.text, fontSize: 14, maxHeight: 100, borderWidth: 1, borderColor: C.border },
  voiceHoldBtn:    { flex: 1, height: 42, borderRadius: 21, backgroundColor: C.bg, borderWidth: 1, borderColor: C.border, alignItems: 'center', justifyContent: 'center' },
  voiceHoldBtnActive: { backgroundColor: C.accent + '22', borderColor: C.accent },
  voiceHoldText:   { color: C.text, fontSize: 14, fontWeight: '500' },
  sendBtn:         { minWidth: 60, height: 38, borderRadius: 20, paddingHorizontal: 14, alignItems: 'center', justifyContent: 'center' },
  sendBtnText:     { color: '#fff', fontWeight: '600', fontSize: 14 },
});