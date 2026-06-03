// app/(tabs)/chat.tsx
// 新增：调用系统闹钟（Android）+ 图片识别 + caption
// 改动：图片改为「先选图→暂存预览→再打字→一起发送」
import AsyncStorage from '@react-native-async-storage/async-storage';
import axios from 'axios';
import { Audio } from 'expo-av';
import * as Clipboard from 'expo-clipboard';
import * as ImagePicker from 'expo-image-picker';
import * as IntentLauncher from 'expo-intent-launcher';
import * as Notifications from 'expo-notifications';
import { useFocusEffect } from 'expo-router';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  Dimensions,
  Image,
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
const STORAGE_KEY       = 'gojo_messages_v2';
const AUDIO_STORAGE_KEY = 'gojo_audio_cache_v1';
const MAX_AUDIO_ENTRIES = 30;
const USER_ID_KEY       = 'gojo_user_id';
const PROACTIVE_KEY     = 'gojo_proactive_state';
const MSG_DELAY_MS      = 800;

const FIXED_USER_ID = 'user_mofpiyd7442ia7';

export interface Message {
  id: string;
  role: 'user' | 'gojo';
  text: string;
  subtitle?: string;
  time?: string;
  imageUri?: string;
}

interface GojoSegment {
  jp: string;
  zh: string;
  audio_b64: string;
}

// ★ 暂存待发送图片的结构
interface PendingImage {
  base64: string;
  mediaType: string;
  uri: string;
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

  // ★ 待发送的图片（先选图暂存，等点发送才真正发出去）
  const [pendingImage, setPendingImage] = useState<PendingImage | null>(null);

  const [searchMode, setSearchMode]   = useState(false);
  const [searchQuery, setSearchQuery] = useState('');

  const audioCacheRef   = useRef<Record<string, string>>({});
  const scrollRef       = useRef<ScrollView>(null);
  const searchRef       = useRef<TextInput>(null);
  const currentSoundRef = useRef<Audio.Sound | null>(null);
  const checkingProactiveRef = useRef(false);

  const saveAudioCache = async () => {
    try {
      const entries = Object.entries(audioCacheRef.current);
      const recent = entries.slice(-MAX_AUDIO_ENTRIES);
      await AsyncStorage.setItem(AUDIO_STORAGE_KEY, JSON.stringify(Object.fromEntries(recent)));
    } catch (e) {
      console.warn('保存音频缓存失败', e);
    }
  };

  // ── 初始化 ──
  useEffect(() => {
    (async () => {
      try {
        await Audio.setAudioModeAsync({
          allowsRecordingIOS: false, playsInSilentModeIOS: true,
          staysActiveInBackground: false, shouldDuckAndroid: true, playThroughEarpieceAndroid: false,
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

        await AsyncStorage.setItem(USER_ID_KEY, FIXED_USER_ID);
        setUserId(FIXED_USER_ID);

        const saved = await AsyncStorage.getItem(STORAGE_KEY);
        if (saved) setMessages(JSON.parse(saved));

        try {
          const audioRaw = await AsyncStorage.getItem(AUDIO_STORAGE_KEY);
          if (audioRaw) {
            audioCacheRef.current = JSON.parse(audioRaw);
            console.log(`🔊 恢复了 ${Object.keys(audioCacheRef.current).length} 条音频缓存`);
          }
        } catch (e) {
          console.warn('加载音频缓存失败', e);
        }
      } catch (e) { console.warn('init error', e); }
      setReady(true);
    })();
    return () => { currentSoundRef.current?.unloadAsync().catch(() => {}); };
  }, []);

  useEffect(() => {
    if (!ready) return;
    AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(messages)).catch(() => {});
  }, [messages, ready]);

  useFocusEffect(
    useCallback(() => {
      if (ready && userId) {
        const t = setTimeout(() => { checkProactiveTasks(); }, 600);
        return () => clearTimeout(t);
      }
    }, [ready, userId])
  );

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
        const isDone = isDaily ? (task.last_completed_date === todayStr) : task.completed;
        if (isDone) continue;
        const dueDateStr = isDaily ? todayStr : task.due_date;
        if (!dueDateStr) continue;
        const [y, mo, d] = dueDateStr.split('-').map(Number);
        const [h, mi]    = task.due_time.split(':').map(Number);
        const dueMoment  = new Date(y, mo - 1, d, h, mi, 0);
        const minsSince  = (now.getTime() - dueMoment.getTime()) / 60000;
        const stateKey   = `${task.id}_${dueDateStr}`;
        const taskState  = state[stateKey] || {};
        let mode: 'remind' | 'overdue' | null = null;

        if (minsSince >= -3 && minsSince < 60 && !taskState.reminded) {
          mode = 'remind'; taskState.reminded = true;
        } else if (minsSince >= 60 && minsSince < 1440 && !taskState.askedOverdue) {
          mode = 'overdue'; taskState.askedOverdue = true;
        }
        if (mode) {
          state[stateKey] = taskState;
          await AsyncStorage.setItem(PROACTIVE_KEY, JSON.stringify(state));
          await sendProactive(task.title, mode);
          break;
        }
      }
    } catch (e) { console.warn('proactive check error', e); }
    finally { checkingProactiveRef.current = false; }
  };

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
          saveAudioCache();
        }
        const gojoMsg: Message = { id: msgId, role: 'gojo', text: seg.jp, subtitle: seg.zh, time: nowTime() };
        setMessages(prev => [...prev, gojoMsg]);
        scrollRef.current?.scrollToEnd({ animated: true });
        if (seg.audio_b64 && seg.audio_b64.length > 100) await playAudioAndWait(seg.audio_b64);
        if (i < segments.length - 1) await sleep(MSG_DELAY_MS);
      }
    } catch (e) { console.warn('sendProactive error', e); }
  };

  const replayAudio = async (msgId: string) => {
    const b64 = audioCacheRef.current[msgId];
    if (!b64) return;
    await playAudioAndWait(b64);
  };

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

  // ── ★ 新增：往系统时钟 App 里加一条闹钟 ──
  // 优点：用闹钟音量，静音/勿扰模式也会响，跟通知是两套独立的系统
  // 限制：只在 Android 有效；只能精确到"下一次出现"的时分，24小时内的提醒最准
  const setSystemAlarm = async (reminder: { date?: string; time?: string; content: string }) => {
    if (Platform.OS !== 'android') return;
    try {
      const [hour, minute] = (reminder.time || '00:00').split(':').map(Number);
      if (isNaN(hour) || isNaN(minute)) return;

      // 24 小时内的才设系统闹钟（因为 SET_ALARM 只能设"下一次出现的时分"，不能指定日期）
      const [y, m, d] = (reminder.date || formatToday()).split('-').map(Number);
      const triggerDate = new Date(y, m - 1, d, hour, minute, 0);
      const hoursUntil  = (triggerDate.getTime() - Date.now()) / (1000 * 60 * 60);
      if (hoursUntil > 24 || hoursUntil < -0.1) {
        console.log(`[alarm] 跳过系统闹钟（${hoursUntil.toFixed(1)}h 太远）`);
        return;
      }

      await IntentLauncher.startActivityAsync('android.intent.action.SET_ALARM', {
        extra: {
          'android.intent.extra.alarm.HOUR': hour,
          'android.intent.extra.alarm.MINUTES': minute,
          'android.intent.extra.alarm.MESSAGE': `🔔 悟：${reminder.content}`,
          'android.intent.extra.alarm.SKIP_UI': true,
          'android.intent.extra.alarm.VIBRATE': true,
        },
      });
      console.log(`[alarm] ✅ 系统闹钟已设置: ${hour}:${String(minute).padStart(2,'0')} - ${reminder.content}`);
    } catch (e) {
      console.warn('[alarm] 系统闹钟设置失败', e);
    }
  };

  // ── 设提醒（通知 + 系统闹钟双保险）──
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

      // ★ 通知设完，再设系统闹钟（双保险，闹钟比通知响很多）
      await setSystemAlarm(reminder);
    } catch (e) { console.warn('reminder error', e); }
  };

  // ── 共用：处理服务端返回（提醒/取消）──
  const processResponseExtras = async (data: any) => {
    if (Array.isArray(data?.cancelled_tasks) && data.cancelled_tasks.length > 0) {
      for (const ct of data.cancelled_tasks) {
        if (ct.notification_id) {
          // ★ 一个 DDL 可能有多条提醒，ID 用逗号拼接，这里拆开逐个取消
          const ids = String(ct.notification_id).split(',').map(x => x.trim()).filter(Boolean);
          for (const id of ids) {
            try { await Notifications.cancelScheduledNotificationAsync(id); }
            catch (e) { console.warn('取消通知失败', e); }
          }
        }
      }
      // ⚠️ 系统闹钟无法被第三方 App 删除，需要用户手动从时钟 App 里删
      Alert.alert('提醒已取消', 'App 内的提醒已删除。如果之前设了系统闹钟，请到手机的时钟 App 里手动删除哦', [{ text: '知道了' }]);
    }
    if (data?.reminder?.date && data.reminder.time) {
      if (data.reminder.duplicate) {
        console.log('🔁 重复提醒，跳过 schedule');
      } else {
        await scheduleReminder(data.reminder);
      }
    }
  };

  // ── 图片选择：★ 改为「只暂存，不立即发送」──
  const pickImage = async (fromCamera: boolean) => {
    try {
      if (fromCamera) {
        const { status } = await ImagePicker.requestCameraPermissionsAsync();
        if (status !== 'granted') {
          Alert.alert('需要相机权限', '请在设置中允许访问相机');
          return;
        }
      } else {
        const { status } = await ImagePicker.requestMediaLibraryPermissionsAsync();
        if (status !== 'granted') {
          Alert.alert('需要相册权限', '请在设置中允许访问相册');
          return;
        }
      }

      const result = fromCamera
        ? await ImagePicker.launchCameraAsync({
            mediaTypes: ['images'], quality: 0.7, base64: true, allowsEditing: false,
          })
        : await ImagePicker.launchImageLibraryAsync({
            mediaTypes: ['images'], quality: 0.7, base64: true, allowsEditing: false,
          });

      if (result.canceled || !result.assets?.[0]) return;
      const asset = result.assets[0];
      const base64 = asset.base64;
      const uri = asset.uri;
      const mediaType = asset.mimeType || 'image/jpeg';

      if (!base64) {
        Alert.alert('错误', '无法获取图片数据');
        return;
      }

      // ★ 不再立即发送，只放进暂存区，显示预览，等用户打完字点发送
      setPendingImage({ base64, mediaType, uri });
    } catch (e: any) {
      console.warn('pickImage error', e);
      Alert.alert('选图失败', e?.message ?? '请重试');
    }
  };

  const showImagePicker = () => {
    Alert.alert('发送图片', '选择图片来源', [
      { text: '📷 拍照', onPress: () => pickImage(true) },
      { text: '🖼 从相册选择', onPress: () => pickImage(false) },
      { text: '取消', style: 'cancel' },
    ]);
  };

  const sendImage = async (base64: string, mediaType: string, localUri: string, caption: string) => {
    if (loading || !userId) return;
    setLoading(true);

    const userMsg: Message = {
      id: Date.now().toString(),
      role: 'user',
      text: caption || '📷 [图片]',
      time: nowTime(),
      imageUri: localUri,
    };
    setMessages(prev => [...prev, userMsg]);
    scrollRef.current?.scrollToEnd({ animated: true });

    try {
      const res = await axios.post(`${SERVER_URL}/chat/image`, {
        user_id: userId,
        image_base64: base64,
        media_type: mediaType,
        text: caption,
      }, { timeout: 60000 });

      if (res.data?.total_days) AsyncStorage.setItem('gojo_chat_days', String(res.data.total_days));
      await processResponseExtras(res.data);

      let segments: GojoSegment[] = [];
      if (Array.isArray(res.data?.messages) && res.data.messages.length > 0) {
        segments = res.data.messages;
      }
      if (segments.length === 0) {
        Alert.alert('回复异常', '没有收到有效回复');
        return;
      }

      for (let i = 0; i < segments.length; i++) {
        const seg = segments[i];
        const msgId = `${Date.now()}_${i}`;
        if (seg.audio_b64 && seg.audio_b64.length > 100) {
          audioCacheRef.current[msgId] = seg.audio_b64;
          saveAudioCache();
        }
        const gojoMsg: Message = {
          id: msgId, role: 'gojo', text: seg.jp, subtitle: seg.zh, time: nowTime(),
        };
        setMessages(prev => [...prev, gojoMsg]);
        scrollRef.current?.scrollToEnd({ animated: true });
        if (seg.audio_b64 && seg.audio_b64.length > 100) await playAudioAndWait(seg.audio_b64);
        if (i < segments.length - 1) await sleep(MSG_DELAY_MS);
      }
    } catch (e: any) {
      Alert.alert('发送失败', e?.message ?? '请确认服务器正常运行');
    } finally { setLoading(false); }
  };

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
      await processResponseExtras(res.data);

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
          saveAudioCache();
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

  // ── ★ 统一发送入口：有暂存图片就发图片(带文字)，否则发纯文字 ──
  const handleSend = () => {
    if (loading || !userId) return;
    if (pendingImage) {
      const caption = inputText.trim();
      const img = pendingImage;
      // 先清空再发送，避免重复发
      setPendingImage(null);
      setInputText('');
      if (searchMode) { setSearchMode(false); setSearchQuery(''); }
      sendImage(img.base64, img.mediaType, img.uri, caption);
    } else {
      sendText();
    }
  };

  const clearHistory = () =>
    Alert.alert('清空记录', '确认清空所有聊天记录？', [
      { text: '取消', style: 'cancel' },
      { text: '清空', style: 'destructive',
        onPress: async () => {
          setMessages([]);
          audioCacheRef.current = {};
          await AsyncStorage.removeItem(STORAGE_KEY);
          await AsyncStorage.removeItem(AUDIO_STORAGE_KEY);
        }
      },
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

  // ★ 有文字 或 有待发图片 时，发送按钮才可点
  const canSend = !loading && (inputText.trim().length > 0 || !!pendingImage);

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
                    {msg.imageUri && (
                      <Image
                        source={{ uri: msg.imageUri }}
                        style={s.bubbleImage}
                        resizeMode="cover"
                      />
                    )}
                    {msg.text && msg.text !== '📷 [图片]' && (
                      <Text style={[s.bubbleText, msg.role === 'user' && s.bubbleTextUser]}>
                        {msg.text}
                      </Text>
                    )}
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

      {/* ★ 待发送图片预览条：选好图后显示在输入框上方，可撤销 */}
      {pendingImage && (
        <View style={s.pendingBar}>
          <Image source={{ uri: pendingImage.uri }} style={s.pendingThumb} resizeMode="cover" />
          <Text style={s.pendingHint}>图片已选好，配点文字一起发吧（也可以直接发）</Text>
          <TouchableOpacity
            onPress={() => setPendingImage(null)}
            style={s.pendingRemove}
            disabled={loading}
          >
            <Text style={s.pendingRemoveText}>✕</Text>
          </TouchableOpacity>
        </View>
      )}

      <View style={s.inputBar}>
        <TouchableOpacity style={s.attachBtn} onPress={showImagePicker} disabled={loading}>
          <Text style={s.attachBtnText}>📎</Text>
        </TouchableOpacity>

        <TextInput
          style={s.input}
          value={inputText}
          onChangeText={setInputText}
          placeholder={
            loading
              ? '五条悟正在回复中...'
              : (pendingImage ? '给图片配句话，或直接发送…' : '跟五条悟说点什么...')
          }
          placeholderTextColor={C.textMute}
          multiline
          editable={!loading}
          returnKeyType="send"
          onSubmitEditing={handleSend}
          blurOnSubmit={false}
        />
        <TouchableOpacity
          style={[
            s.sendBtn,
            { backgroundColor: canSend ? C.accent : C.textMute + '55' }
          ]}
          onPress={handleSend}
          disabled={!canSend}
        >
          <Text style={[s.sendBtnText, { opacity: canSend ? 1 : 0.5 }]}>发送</Text>
        </TouchableOpacity>
      </View>

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

  bubbleImage:     { width: width * 0.55, height: width * 0.42, borderRadius: 10, marginBottom: 6, backgroundColor: C.bg },

  // ★ 待发送图片预览条
  pendingBar:      { flexDirection: 'row', alignItems: 'center', backgroundColor: C.card, paddingHorizontal: 12, paddingTop: 10, paddingBottom: 4, gap: 10, borderTopWidth: 1, borderTopColor: C.border },
  pendingThumb:    { width: 48, height: 48, borderRadius: 8, backgroundColor: C.bg, borderWidth: 1, borderColor: C.border },
  pendingHint:     { flex: 1, color: C.textMute, fontSize: 12, lineHeight: 16 },
  pendingRemove:   { width: 26, height: 26, borderRadius: 13, backgroundColor: C.bg, alignItems: 'center', justifyContent: 'center', borderWidth: 1, borderColor: C.border },
  pendingRemoveText:{ color: C.textMute, fontSize: 14, fontWeight: '600' },

  attachBtn:       { padding: 8, marginRight: 2 },
  attachBtnText:   { fontSize: 22 },

  inputBar:        { flexDirection: 'row', alignItems: 'flex-end', backgroundColor: C.card, paddingHorizontal: 12, paddingVertical: 10, borderTopWidth: 1, borderTopColor: C.border, gap: 8 },
  input:           { flex: 1, backgroundColor: C.bg, borderRadius: 20, paddingHorizontal: 16, paddingVertical: 10, color: C.text, fontSize: 14, maxHeight: 100, borderWidth: 1, borderColor: C.border },
  sendBtn:         { borderRadius: 20, paddingHorizontal: 18, paddingVertical: 10, minWidth: 60, alignItems: 'center' },
  sendBtnText:     { color: '#fff', fontWeight: '600', fontSize: 14 },
});