// app/(tabs)/chat.tsx
import AsyncStorage from '@react-native-async-storage/async-storage';
import axios from 'axios';
import { Audio } from 'expo-av';
import * as Clipboard from 'expo-clipboard';
import * as Notifications from 'expo-notifications';
import { useFocusEffect } from 'expo-router';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  Dimensions,
  KeyboardAvoidingView,
  Platform,
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

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowAlert: true,
    shouldPlaySound: true,
    shouldSetBadge: false,
  }),
});

const { width } = Dimensions.get('window');
const STORAGE_KEY   = 'gojo_messages_v2';
const USER_ID_KEY   = 'gojo_user_id';
const PROACTIVE_KEY = 'gojo_proactive_state';   // 记录哪些任务已经被主动提醒过
const MSG_DELAY_MS  = 800;

// ★ 固定 user_id，重装/换手机都不丢记忆
const FIXED_USER_ID = 'user_mofpiyd7442ia7';

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
  const [showCall, setShowCall]   = useState(false);

  // 搜索
  const [searchMode, setSearchMode]   = useState(false);
  const [searchQuery, setSearchQuery] = useState('');

  // 音频缓存（内存，用于重播）
  const audioCacheRef   = useRef<Record<string, string>>({});
  const scrollRef       = useRef<ScrollView>(null);
  const searchRef       = useRef<TextInput>(null);
  const currentSoundRef = useRef<Audio.Sound | null>(null);
  const checkingProactiveRef = useRef(false);   // 防止并发检查

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

        // ★ 固定 user_id，重装也不丢记忆
        await AsyncStorage.setItem(USER_ID_KEY, FIXED_USER_ID);
        setUserId(FIXED_USER_ID);

        const saved = await AsyncStorage.getItem(STORAGE_KEY);
        if (saved) setMessages(JSON.parse(saved));
      } catch (e) { console.warn('init error', e); }
      setReady(true);
    })();
    return () => { currentSoundRef.current?.unloadAsync().catch(() => {}); };
  }, []);

  useEffect(() => {
    if (!ready) return;
    AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(messages)).catch(() => {});
  }, [messages, ready]);

  // ── 每次进入聊天页时，检查有没有要主动提醒的任务 ──
  useFocusEffect(
    useCallback(() => {
      if (ready && userId) {
        // 稍微延迟，等页面稳定
        const t = setTimeout(() => { checkProactiveTasks(); }, 600);
        return () => clearTimeout(t);
      }
    }, [ready, userId])
  );

  // ── 主动提醒核心逻辑 ──
  const checkProactiveTasks = async () => {
    if (!userId || loading || checkingProactiveRef.current) return;
    checkingProactiveRef.current = true;

    try {
      const res = await axios.get(`${SERVER_URL}/tasks?user_id=${userId}`);
      const tasks = res.data?.tasks || [];

      const stateRaw = await AsyncStorage.getItem(PROACTIVE_KEY);
      const state: Record<string, { reminded?: boolean; askedOverdue?: boolean }> =
        stateRaw ? JSON.parse(stateRaw) : {};

      const now = new Date();
      const todayStr = formatToday();

      for (const task of tasks) {
        if (!task.due_time) continue;

        const isDaily = task.repeat_type === 'daily';
        // 完成状态
        const isDone = isDaily ? (task.last_completed_date === todayStr) : task.completed;
        if (isDone) continue;

        // 计算"今天的截止时刻"
        const dueDateStr = isDaily ? todayStr : task.due_date;
        if (!dueDateStr) continue;

        const [y, mo, d] = dueDateStr.split('-').map(Number);
        const [h, mi]    = task.due_time.split(':').map(Number);
        const dueMoment  = new Date(y, mo - 1, d, h, mi, 0);
        const minsSince  = (now.getTime() - dueMoment.getTime()) / 60000;

        const stateKey  = `${task.id}_${dueDateStr}`;
        const taskState = state[stateKey] || {};

        let mode: 'remind' | 'overdue' | null = null;

        // 到点 ~ 1小时内：温柔提醒（只一次）
        if (minsSince >= -3 && minsSince < 60 && !taskState.reminded) {
          mode = 'remind';
          taskState.reminded = true;
        }
        // 超时 1小时 ~ 24小时：追问做了没（只一次）
        else if (minsSince >= 60 && minsSince < 1440 && !taskState.askedOverdue) {
          mode = 'overdue';
          taskState.askedOverdue = true;
        }

        if (mode) {
          state[stateKey] = taskState;
          await AsyncStorage.setItem(PROACTIVE_KEY, JSON.stringify(state));
          await sendProactive(task.title, mode);
          break;  // 一次只处理一个，避免刷屏
        }
      }
    } catch (e) {
      console.warn('proactive check error', e);
    } finally {
      checkingProactiveRef.current = false;
    }
  };

  // ── 让悟发主动消息 ──
  const sendProactive = async (taskTitle: string, mode: 'remind' | 'overdue') => {
    try {
      const res = await axios.post(`${SERVER_URL}/chat/proactive`, {
        user_id: userId, task_title: taskTitle, mode,
      });
      const segments: GojoSegment[] = res.data?.messages || [];

      for (let i = 0; i < segments.length; i++) {
        const seg = segments[i];
        const msgId = `proactive_${Date.now()}_${i}`;
        if (seg.audio_b64 && seg.audio_b64.length > 100) {
          audioCacheRef.current[msgId] = seg.audio_b64;
        }
        const gojoMsg: Message = { id: msgId, role: 'gojo', text: seg.jp, subtitle: seg.zh, time: nowTime() };
        setMessages(prev => [...prev, gojoMsg]);
        scrollRef.current?.scrollToEnd({ animated: true });
        if (seg.audio_b64 && seg.audio_b64.length > 100) await playAudioAndWait(seg.audio_b64);
        if (i < segments.length - 1) await sleep(MSG_DELAY_MS);
      }
    } catch (e) {
      console.warn('sendProactive error', e);
    }
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
      } catch { resolve(); }
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
        if (ns.status !== 'granted') return;
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
    } catch (e) { console.warn('reminder error', e); }
  };

  // ── 发送消息 ──
  const sendText = async (textOverride?: string) => {
    const text = (textOverride ?? inputText).trim();
    if (!text || loading || !userId) return;
    setInputText('');
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
            {searchMode && searchQuery.trim()
              ? <><Text style={s.emptyEmoji}>🔍</Text><Text style={s.emptyText}>没找到「{searchQuery}」</Text></>
              : <><Text style={s.emptyEmoji}>👋</Text><Text style={s.emptyText}>跟五条悟说点什么吧</Text></>
            }
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
                      <Text style={s.replayHint}>🔊 点击重播</Text>
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

      {/* 输入栏 */}
      <View style={s.inputBar}>
        <TextInput
          style={s.input}
          value={inputText}
          onChangeText={setInputText}
          placeholder={loading ? '五条悟正在回复中...' : '跟五条悟说点什么...'}
          placeholderTextColor={C.textMute}
          multiline
          editable={!loading}
          returnKeyType="send"
          onSubmitEditing={() => sendText()}
          blurOnSubmit={false}
        />
        {/* 有文字→发送；没文字→禁用发送（灰色） */}
        <TouchableOpacity
          style={[
            s.sendBtn,
            { backgroundColor: (!loading && inputText.trim()) ? C.accent : C.textMute + '55' }
          ]}
          onPress={() => sendText()}
          disabled={loading || !inputText.trim()}
        >
          <Text style={[s.sendBtnText, { opacity: inputText.trim() ? 1 : 0.5 }]}>发送</Text>
        </TouchableOpacity>
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
  header:          { flexDirection: 'row', alignItems: 'center', backgroundColor: C.card, paddingHorizontal: 16, paddingTop: Platform.OS === 'ios' ? 50 : 40, paddingBottom: 12, borderBottomWidth: 1, borderBottomColor: C.border, gap: 8 },
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
  replayHint:      { color: C.accent, fontSize: 11, marginTop: 6, opacity: 0.7 },
  msgBottom:       { flexDirection: 'row', alignItems: 'center', marginTop: 4, marginHorizontal: 2 },
  msgTime:         { color: C.textMute, fontSize: 10 },
  inputBar:        { flexDirection: 'row', alignItems: 'flex-end', backgroundColor: C.card, paddingHorizontal: 12, paddingVertical: 10, borderTopWidth: 1, borderTopColor: C.border, gap: 8 },
  input:           { flex: 1, backgroundColor: C.bg, borderRadius: 20, paddingHorizontal: 16, paddingVertical: 10, color: C.text, fontSize: 14, maxHeight: 100, borderWidth: 1, borderColor: C.border },
  sendBtn:         { borderRadius: 20, paddingHorizontal: 18, paddingVertical: 10, minWidth: 60, alignItems: 'center' },
  sendBtnText:     { color: '#fff', fontWeight: '600', fontSize: 14 },
});