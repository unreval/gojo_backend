// app/chat/[id].tsx
// 通用聊天页：
//   - 单聊：id 是角色 id（如 'gojo' / 'geto'）→ 走 /chat/text、/chat/image
//   - 群聊：id 是 'group_<gid>'（如 'group_2'）→ 走 /group/chat
// 完整保留 gojo 单聊原有的功能：语音文件持久化、提醒/闹钟、图片暂存、搜索、长按复制。
// 电话按钮：仅 id==='gojo' 时显示。
import AsyncStorage from '@react-native-async-storage/async-storage';
import axios from 'axios';
import { Audio } from 'expo-av';
import * as Clipboard from 'expo-clipboard';
import * as FileSystem from 'expo-file-system/legacy';
import * as ImagePicker from 'expo-image-picker';
import * as IntentLauncher from 'expo-intent-launcher';
import * as Notifications from 'expo-notifications';
import { useFocusEffect, useLocalSearchParams, useRouter } from 'expo-router';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  Dimensions,
  Image,
  Keyboard,
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
import type { Message } from '../../types/message';

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowAlert: true,
    shouldPlaySound: true,
    shouldSetBadge: false,
  }),
});

const { width } = Dimensions.get('window');

const FIXED_USER_ID  = 'user_mofpiyd7442ia7';
const MAX_AUDIO_ENTRIES = 30;
const PROACTIVE_KEY  = 'gojo_proactive_state';
const MSG_DELAY_MS   = 800;

// 每个会话独立的存储 key（按 id 隔离）
const msgStorageKey = (id: string) => `chat_msgs_${id}`;
const audioDir       = (id: string) => `${FileSystem.documentDirectory}chat_audio_${id}/`;

interface Character {
  id: string;
  name: string;
  voice_id?: string;
  greeting?: string;
}
interface GroupMember {
  id: string;
  name: string;
  voice_id?: string;
  is_owner_role?: boolean;
}
interface GroupDetail {
  id: number;
  name: string;
  members: GroupMember[];
}

interface Segment {
  jp: string;
  zh: string;
  audio_b64: string;
}
interface GroupReply {
  sender_id: string;
  sender_name: string;
  jp: string;
  zh: string;
  emotion: string;
  audio_b64: string;
}

interface PendingImage {
  base64: string;
  mediaType: string;
  uri: string;
}

function sleep(ms: number) { return new Promise<void>(r => setTimeout(r, ms)); }
function formatToday(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}

export default function ChatRoom() {
  const { id: rawId } = useLocalSearchParams<{ id: string }>();
  const router = useRouter();

  const chatId = (rawId || '') as string;
  const isGroup = chatId.startsWith('group_');
  const groupId = isGroup ? Number(chatId.replace('group_', '')) : null;

  const STORAGE_KEY = msgStorageKey(chatId);
  const AUDIO_DIR   = audioDir(chatId);

  // 标题区数据
  const [character, setCharacter] = useState<Character | null>(null);
  const [group, setGroup]         = useState<GroupDetail | null>(null);

  // 聊天状态
  const [messages, setMessages]   = useState<Message[]>([]);
  const [inputText, setInputText] = useState('');
  const [loading, setLoading]     = useState(false);
  const [ready, setReady]         = useState(false);
  const [showCall, setShowCall]   = useState(false);
  const [keyboardHeight, setKeyboardHeight] = useState(0);  // ★ 手动监听键盘高度
  const [pendingImage, setPendingImage] = useState<PendingImage | null>(null);
  const [searchMode, setSearchMode]   = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [showFullTime, setShowFullTime] = useState(false); // 点击时间条切换完整/简短

  const audioCacheRef   = useRef<Record<string, string>>({});
  const scrollRef       = useRef<ScrollView>(null);
  const searchRef       = useRef<TextInput>(null);
  const currentSoundRef = useRef<Audio.Sound | null>(null);
  const checkingProactiveRef = useRef(false);

  // ── 语音文件工具 ──
  const ensureAudioDir = async () => {
    try {
      const info = await FileSystem.getInfoAsync(AUDIO_DIR);
      if (!info.exists) {
        await FileSystem.makeDirectoryAsync(AUDIO_DIR, { intermediates: true });
      }
    } catch (e) { console.warn('ensureAudioDir', e); }
  };
  const saveAudioFile = async (msgId: string, base64: string): Promise<string | null> => {
    try {
      await ensureAudioDir();
      const uri = `${AUDIO_DIR}${msgId}.mp3`;
      await FileSystem.writeAsStringAsync(uri, base64, { encoding: FileSystem.EncodingType.Base64 });
      return uri;
    } catch (e) { console.warn('saveAudioFile', e); return null; }
  };
  const pruneAudioFiles = async () => {
    try {
      const files = await FileSystem.readDirectoryAsync(AUDIO_DIR);
      const mp3s = files.filter(f => f.endsWith('.mp3'));
      if (mp3s.length <= MAX_AUDIO_ENTRIES) return;
      const withTime = await Promise.all(mp3s.map(async f => {
        const info: any = await FileSystem.getInfoAsync(`${AUDIO_DIR}${f}`);
        return { f, t: (info?.modificationTime ?? 0) };
      }));
      withTime.sort((a, b) => a.t - b.t);
      const toDelete = withTime.slice(0, withTime.length - MAX_AUDIO_ENTRIES);
      for (const { f } of toDelete) {
        try { await FileSystem.deleteAsync(`${AUDIO_DIR}${f}`, { idempotent: true }); } catch {}
        delete audioCacheRef.current[f.replace('.mp3', '')];
      }
    } catch (e) { console.warn('pruneAudioFiles', e); }
  };
  const loadAudioIndex = async () => {
    try {
      await ensureAudioDir();
      const files = await FileSystem.readDirectoryAsync(AUDIO_DIR);
      const map: Record<string, string> = {};
      for (const f of files) {
        if (f.endsWith('.mp3')) map[f.replace('.mp3', '')] = `${AUDIO_DIR}${f}`;
      }
      audioCacheRef.current = map;
    } catch (e) { console.warn('loadAudioIndex', e); }
  };

  // ── 拉对话对象信息 + 历史消息 ──
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
            name: '提醒通知',
            importance: Notifications.AndroidImportance.HIGH,
            sound: 'default',
            vibrationPattern: [0, 250, 250, 250],
          });
        }

        // 拉对话对象详情
        if (isGroup && groupId != null) {
          try {
            const res = await axios.get(`${SERVER_URL}/group/${groupId}`);
            setGroup({
              id: res.data.id,
              name: res.data.name,
              members: res.data.members || [],
            });
          } catch (e) { console.warn('load group error', e); }
        } else {
          try {
            const res = await axios.get(`${SERVER_URL}/characters/${chatId}`);
            setCharacter(res.data);
          } catch (e) { console.warn('load character error', e); }
        }

        const saved = await AsyncStorage.getItem(STORAGE_KEY);
        if (saved) setMessages(JSON.parse(saved));

        await loadAudioIndex();
      } catch (e) { console.warn('init error', e); }
      setReady(true);
    })();
    return () => { currentSoundRef.current?.unloadAsync().catch(() => {}); };
  }, [chatId]);

  // ★ 手动监听键盘——比 KeyboardAvoidingView 在 Android 上靠谱得多
  // 用屏幕高度 - 键盘顶部Y坐标,这样 MIUI 那种带工具栏的键盘高度也能算对
  useEffect(() => {
    const screenH = Dimensions.get('window').height;
    const showEvt = Platform.OS === 'ios' ? 'keyboardWillShow' : 'keyboardDidShow';
    const hideEvt = Platform.OS === 'ios' ? 'keyboardWillHide' : 'keyboardDidHide';
    const showSub = Keyboard.addListener(showEvt, e => {
      const screenY = e.endCoordinates.screenY ?? 0;
      const reportedH = e.endCoordinates.height ?? 0;
      // 优先用 screenY 算真实键盘高度(含工具栏),回退到 reportedH
      const realH = screenY > 0 ? Math.max(screenH - screenY, reportedH) : reportedH;
      setKeyboardHeight(realH);
      // 键盘弹起时,顺手滚到底,让最新消息可见
      setTimeout(() => scrollRef.current?.scrollToEnd({ animated: true }), 50);
    });
    const hideSub = Keyboard.addListener(hideEvt, () => {
      setKeyboardHeight(0);
    });
    return () => {
      showSub.remove();
      hideSub.remove();
    };
  }, []);

  useEffect(() => {
    if (!ready) return;
    AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(messages)).catch(() => {});
  }, [messages, ready]);

  // 五条单聊保留的"主动提醒"轮询
  useFocusEffect(useCallback(() => {
    if (ready && !isGroup && chatId === 'gojo') {
      const t = setTimeout(() => { checkProactiveTasks(); }, 600);
      return () => clearTimeout(t);
    }
  }, [ready, chatId, isGroup]));

  const checkProactiveTasks = async () => {
    if (loading || checkingProactiveRef.current) return;
    checkingProactiveRef.current = true;
    try {
      const res = await axios.get(`${SERVER_URL}/tasks?user_id=${FIXED_USER_ID}`);
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
        user_id: FIXED_USER_ID, task_title: taskTitle, mode,
      });
      const segments: Segment[] = res.data?.messages || [];
      for (let i = 0; i < segments.length; i++) {
        const seg = segments[i];
        const msgId = `proactive_${Date.now()}_${i}`;
        let audioUri: string | null = null;
        if (seg.audio_b64 && seg.audio_b64.length > 100) {
          audioUri = await saveAudioFile(msgId, seg.audio_b64);
          if (audioUri) audioCacheRef.current[msgId] = audioUri;
        }
        const msg: Message = { id: msgId, role: 'gojo', text: seg.jp, subtitle: seg.zh, time: nowTime(), timestamp: Date.now(), timestamp: Date.now() };
        setMessages(prev => [...prev, msg]);
        scrollRef.current?.scrollToEnd({ animated: true });
        if (audioUri) await playAudioAndWait(audioUri);
        if (i < segments.length - 1) await sleep(MSG_DELAY_MS);
      }
      pruneAudioFiles();
    } catch (e) { console.warn('sendProactive error', e); }
  };

  // 重播
  const replayAudio = async (msgId: string) => {
    const uri = audioCacheRef.current[msgId];
    if (!uri) return;
    await playAudioAndWait(uri);
  };
  const playAudioAndWait = async (uri: string): Promise<void> => {
    try {
      if (currentSoundRef.current) {
        await currentSoundRef.current.unloadAsync();
        currentSoundRef.current = null;
      }
    } catch {}
    return new Promise<void>(async (resolve) => {
      try {
        const { sound } = await Audio.Sound.createAsync(
          { uri },
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

  // ── 提醒/系统闹钟（保留五条原有功能）──
  const setSystemAlarm = async (reminder: { date?: string; time?: string; content: string }) => {
    if (Platform.OS !== 'android') return;
    try {
      const [hour, minute] = (reminder.time || '00:00').split(':').map(Number);
      if (isNaN(hour) || isNaN(minute)) return;
      const [y, m, d] = (reminder.date || formatToday()).split('-').map(Number);
      const triggerDate = new Date(y, m - 1, d, hour, minute, 0);
      const hoursUntil  = (triggerDate.getTime() - Date.now()) / (1000 * 60 * 60);
      if (hoursUntil > 24 || hoursUntil < -0.1) return;
      await IntentLauncher.startActivityAsync('android.intent.action.SET_ALARM', {
        extra: {
          'android.intent.extra.alarm.HOUR': hour,
          'android.intent.extra.alarm.MINUTES': minute,
          'android.intent.extra.alarm.MESSAGE': `🔔 ${reminder.content}`,
          'android.intent.extra.alarm.SKIP_UI': true,
          'android.intent.extra.alarm.VIBRATE': true,
        },
      });
    } catch (e) { console.warn('[alarm] 失败', e); }
  };
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
          title: character?.name || '提醒',
          body: reminder.notification || `おい、${reminder.content}の時間だよ。\n（喂，该${reminder.content}了。）`,
          sound: 'default',
          ...(Platform.OS === 'android' ? { channelId: 'gojo-reminders' } : {}),
        },
        trigger: { type: Notifications.SchedulableTriggerInputTypes.DATE, date: triggerDate } as any,
      });
      if (reminder.task_id && notifId) {
        await axios.put(`${SERVER_URL}/tasks/${reminder.task_id}`, { notification_id: notifId }).catch(() => {});
      }
      await setSystemAlarm(reminder);
    } catch (e) { console.warn('reminder error', e); }
  };
  const processResponseExtras = async (data: any) => {
    if (Array.isArray(data?.cancelled_tasks) && data.cancelled_tasks.length > 0) {
      for (const ct of data.cancelled_tasks) {
        if (ct.notification_id) {
          const ids = String(ct.notification_id).split(',').map(x => x.trim()).filter(Boolean);
          for (const id of ids) {
            try { await Notifications.cancelScheduledNotificationAsync(id); }
            catch (e) { console.warn('取消通知失败', e); }
          }
        }
      }
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

  // ── 图片选择 ──
  const pickImage = async (fromCamera: boolean) => {
    try {
      if (fromCamera) {
        const { status } = await ImagePicker.requestCameraPermissionsAsync();
        if (status !== 'granted') { Alert.alert('需要相机权限', '请在设置中允许访问相机'); return; }
      } else {
        const { status } = await ImagePicker.requestMediaLibraryPermissionsAsync();
        if (status !== 'granted') { Alert.alert('需要相册权限', '请在设置中允许访问相册'); return; }
      }
      const result = fromCamera
        ? await ImagePicker.launchCameraAsync({ mediaTypes: ['images'], quality: 0.7, base64: true, allowsEditing: false })
        : await ImagePicker.launchImageLibraryAsync({ mediaTypes: ['images'], quality: 0.7, base64: true, allowsEditing: false });
      if (result.canceled || !result.assets?.[0]) return;
      const asset = result.assets[0];
      if (!asset.base64) { Alert.alert('错误', '无法获取图片数据'); return; }
      setPendingImage({ base64: asset.base64, mediaType: asset.mimeType || 'image/jpeg', uri: asset.uri });
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

  // 把后端返回的 segments 渲染成消息并配音
  const appendSegments = async (segments: Segment[], baseId: string) => {
    for (let i = 0; i < segments.length; i++) {
      const seg = segments[i];
      const msgId = `${baseId}_${i}`;
      let audioUri: string | null = null;
      if (seg.audio_b64 && seg.audio_b64.length > 100) {
        audioUri = await saveAudioFile(msgId, seg.audio_b64);
        if (audioUri) audioCacheRef.current[msgId] = audioUri;
      }
      const msg: Message = { id: msgId, role: 'gojo', text: seg.jp, subtitle: seg.zh, time: nowTime(), timestamp: Date.now(), timestamp: Date.now() };
      setMessages(prev => [...prev, msg]);
      scrollRef.current?.scrollToEnd({ animated: true });
      if (audioUri) await playAudioAndWait(audioUri);
      if (i < segments.length - 1) await sleep(MSG_DELAY_MS);
    }
  };

  // 群聊：把多个角色的回复依次渲染
  const appendGroupReplies = async (replies: GroupReply[]) => {
    for (let i = 0; i < replies.length; i++) {
      const r = replies[i];
      const msgId = `${Date.now()}_${i}_${r.sender_id}`;
      let audioUri: string | null = null;
      if (r.audio_b64 && r.audio_b64.length > 100) {
        audioUri = await saveAudioFile(msgId, r.audio_b64);
        if (audioUri) audioCacheRef.current[msgId] = audioUri;
      }
      const msg: Message = {
        id: msgId, role: 'gojo',
        text: r.jp, subtitle: r.zh, time: nowTime(), timestamp: Date.now(),
        senderId: r.sender_id, senderName: r.sender_name,
      };
      setMessages(prev => [...prev, msg]);
      scrollRef.current?.scrollToEnd({ animated: true });
      if (audioUri) await playAudioAndWait(audioUri);
      if (i < replies.length - 1) await sleep(MSG_DELAY_MS);
    }
  };

  // ── 发送（统一入口）──
  const sendImage = async (base64: string, mediaType: string, localUri: string, caption: string) => {
    if (loading) return;
    if (isGroup) {
      Alert.alert('提示', '群聊暂不支持图片，等以后再开放');
      return;
    }
    setLoading(true);
    const userMsg: Message = {
      id: Date.now().toString(), role: 'user',
      text: caption || '📷 [图片]', time: nowTime(), timestamp: Date.now(), imageUri: localUri,
    };
    setMessages(prev => [...prev, userMsg]);
    scrollRef.current?.scrollToEnd({ animated: true });

    try {
      const res = await axios.post(`${SERVER_URL}/chat/image`, {
        user_id: FIXED_USER_ID,
        image_base64: base64,
        media_type: mediaType,
        text: caption,
        character_id: chatId,
      }, { timeout: 60000 });
      await processResponseExtras(res.data);
      const segments: Segment[] = res.data?.messages || [];
      if (segments.length === 0) { Alert.alert('回复异常', '没有收到有效回复'); return; }
      await appendSegments(segments, `${Date.now()}`);
      pruneAudioFiles();
    } catch (e: any) {
      Alert.alert('发送失败', e?.message ?? '请确认服务器正常运行');
    } finally { setLoading(false); }
  };

  const sendText = async (textOverride?: string) => {
    const text = (textOverride ?? inputText).trim();
    if (!text || loading) return;
    setInputText('');
    if (searchMode) { setSearchMode(false); setSearchQuery(''); }

    const userMsg: Message = { id: Date.now().toString(), role: 'user', text, time: nowTime(), timestamp: Date.now(), timestamp: Date.now() };
    setMessages(prev => [...prev, userMsg]);
    setLoading(true);

    try {
      if (isGroup) {
        const res = await axios.post(`${SERVER_URL}/group/chat`, {
          group_id: groupId,
          text,
          user_id: FIXED_USER_ID,
        });
        const replies: GroupReply[] = res.data?.replies || [];
        if (replies.length === 0) {
          // 没人接话的情况
          const sys: Message = {
            id: `${Date.now()}_sys`, role: 'gojo',
            text: '（群里暂时没人接话）', time: nowTime(), timestamp: Date.now(),
          };
          setMessages(prev => [...prev, sys]);
        } else {
          await appendGroupReplies(replies);
        }
      } else {
        const res = await axios.post(`${SERVER_URL}/chat/text`, {
          text, user_id: FIXED_USER_ID, character_id: chatId,
        });
        await processResponseExtras(res.data);
        let segments: Segment[] = [];
        if (Array.isArray(res.data?.messages) && res.data.messages.length > 0) {
          segments = res.data.messages;
        } else if (res.data?.jp) {
          segments = [{ jp: res.data.jp, zh: res.data.zh ?? '', audio_b64: res.data.audio_b64 ?? '' }];
        }
        if (segments.length === 0) { Alert.alert('回复异常', '没有收到有效回复'); return; }
        await appendSegments(segments, `${Date.now()}`);
      }
      pruneAudioFiles();
    } catch (e: any) {
      Alert.alert('连接失败', e?.message ?? '请确认服务器正常运行');
    } finally { setLoading(false); }
  };

  const handleSend = () => {
    if (loading) return;
    if (pendingImage) {
      const caption = inputText.trim();
      const img = pendingImage;
      setPendingImage(null);
      setInputText('');
      if (searchMode) { setSearchMode(false); setSearchQuery(''); }
      sendImage(img.base64, img.mediaType, img.uri, caption);
    } else {
      sendText();
    }
  };

  const clearHistory = () =>
    Alert.alert('清空记录', '只清空这个会话的记录，确认？', [
      { text: '取消', style: 'cancel' },
      { text: '清空', style: 'destructive',
        onPress: async () => {
          setMessages([]);
          audioCacheRef.current = {};
          await AsyncStorage.removeItem(STORAGE_KEY);
          try { await FileSystem.deleteAsync(AUDIO_DIR, { idempotent: true }); } catch {}
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

  // ── 时间分隔条工具 ──
  const WEEKDAYS_CN = ['日', '一', '二', '三', '四', '五', '六'];
  const shouldShowSeparator = (cur: Message, prev: Message | null): boolean => {
    if (!prev) return true;
    if (!cur.timestamp || !prev.timestamp) return false;
    return cur.timestamp - prev.timestamp > 5 * 60 * 1000; // 5 分钟间隔
  };
  const formatSeparatorTime = (ts: number, full: boolean): string => {
    const d = new Date(ts);
    const now = new Date();
    const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    const isToday = d.toDateString() === now.toDateString();
    const yesterday = new Date(now); yesterday.setDate(now.getDate() - 1);
    const isYesterday = d.toDateString() === yesterday.toDateString();
    if (!full) {
      if (isToday) return hm;
      if (isYesterday) return `昨天 ${hm}`;
      return `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
    }
    return `${d.getMonth() + 1}月${d.getDate()}日 星期${WEEKDAYS_CN[d.getDay()]} ${hm}`;
  };

  // 标题区
  const headerTitle = isGroup
    ? (group?.name || '群聊')
    : (character?.name || chatId);
  const headerSub = isGroup
    ? (group ? `${group.members.length} 个成员` : '加载中...')
    : (chatId === 'gojo' ? '最强的男人' : '');

  // 渲染过滤
  const displayMessages = searchMode && searchQuery.trim()
    ? messages.filter(m =>
        m.text.toLowerCase().includes(searchQuery.toLowerCase()) ||
        (m.subtitle || '').toLowerCase().includes(searchQuery.toLowerCase()))
    : messages;

  const canSend = !loading && (inputText.trim().length > 0 || !!pendingImage);

  if (!ready) return (
    <View style={{ flex: 1, backgroundColor: C.bg, alignItems: 'center', justifyContent: 'center' }}>
      <ActivityIndicator color={C.accent} />
    </View>
  );

  return (
    <View
      style={{ flex: 1, backgroundColor: C.bg }}
    >
      <StatusBar barStyle="light-content" backgroundColor={C.card} />

      <View style={s.header}>
        {!searchMode ? (
          <>
            <TouchableOpacity onPress={() => router.back()} style={s.backBtn}>
              <Text style={s.backText}>‹</Text>
            </TouchableOpacity>
            <View style={[s.avatarSmall, { borderColor: isGroup ? C.accent2 : C.accent }]}>
              <Text style={s.avatarSmallText}>{isGroup ? '群' : (headerTitle?.[0] || '?')}</Text>
            </View>
            <View style={{ flex: 1 }}>
              <Text style={s.headerName} numberOfLines={1}>{headerTitle}</Text>
              {headerSub ? <Text style={s.headerSub} numberOfLines={1}>{headerSub}</Text> : null}
            </View>
            {/* 电话按钮：仅 gojo 单聊有 */}
            {!isGroup && chatId === 'gojo' && (
              <TouchableOpacity onPress={() => setShowCall(true)} style={s.iconBtn}>
                <Text style={s.iconBtnText}>📞</Text>
              </TouchableOpacity>
            )}
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
              : <><Text style={s.emptyEmoji}>👋</Text><Text style={s.emptyText}>
                  {isGroup ? '在群里说点什么吧' : `跟${character?.name || '对方'}说点什么吧`}
                </Text></>
            }
          </View>
        )}

        {displayMessages.map((msg, idx) => {
          const hasAudio = !!audioCacheRef.current[msg.id];
          const isHighlighted = searchMode && searchQuery.trim() &&
            (msg.text.toLowerCase().includes(searchQuery.toLowerCase()) ||
             (msg.subtitle || '').toLowerCase().includes(searchQuery.toLowerCase()));
          const speakerName = msg.senderName || character?.name || (isGroup ? '?' : (chatId === 'gojo' ? '五条悟' : chatId));
          const speakerInitial = speakerName?.[0] || '?';
          const prevMsg = idx > 0 ? displayMessages[idx - 1] : null;
          const showSep = shouldShowSeparator(msg, prevMsg);

          return (
            <React.Fragment key={msg.id}>
              {showSep && msg.timestamp && (
                <TouchableOpacity
                  activeOpacity={0.7}
                  onPress={() => setShowFullTime(v => !v)}
                  style={s.timeSepWrap}
                >
                  <Text style={s.timeSepText}>
                    {formatSeparatorTime(msg.timestamp, showFullTime)}
                  </Text>
                </TouchableOpacity>
              )}
            <View style={[s.msgRow, msg.role === 'user' ? s.msgRowUser : s.msgRowGojo]}>
              {msg.role === 'gojo' && (
                <View style={s.msgAvatar}>
                  <Text style={s.msgAvatarText}>{speakerInitial}</Text>
                </View>
              )}
              <View style={[s.msgMain, msg.role === 'user' && { alignItems: 'flex-end' }]}>
                {msg.role === 'gojo' && <Text style={s.msgSender}>{speakerName}</Text>}
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
                      <Image source={{ uri: msg.imageUri }} style={s.bubbleImage} resizeMode="cover" />
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
            </React.Fragment>
          );
        })}

        {loading && (
          <View style={s.msgRow}>
            <View style={s.msgAvatar}><Text style={s.msgAvatarText}>…</Text></View>
            <View style={[s.bubble, s.bubbleGojo, { flexDirection: 'row', alignItems: 'center', gap: 8 }]}>
              <ActivityIndicator size="small" color={C.accent} />
              <Text style={{ color: C.textMute, fontSize: 13 }}>
                {isGroup ? '群里在思考...' : '思考中...'}
              </Text>
            </View>
          </View>
        )}
      </ScrollView>

      {pendingImage && (
        <View style={s.pendingBar}>
          <Image source={{ uri: pendingImage.uri }} style={s.pendingThumb} resizeMode="cover" />
          <Text style={s.pendingHint}>
            {isGroup ? '群聊暂不支持图片' : '图片已选好，配点文字一起发吧'}
          </Text>
          <TouchableOpacity onPress={() => setPendingImage(null)} style={s.pendingRemove} disabled={loading}>
            <Text style={s.pendingRemoveText}>✕</Text>
          </TouchableOpacity>
        </View>
      )}

      <View style={[s.inputBar, { marginBottom: keyboardHeight }]}>
        {!isGroup && (
          <TouchableOpacity style={s.attachBtn} onPress={showImagePicker} disabled={loading}>
            <Text style={s.attachBtnText}>📎</Text>
          </TouchableOpacity>
        )}

        <TextInput
          style={s.input}
          value={inputText}
          onChangeText={setInputText}
          placeholder={
            loading
              ? (isGroup ? '群里回复中...' : '回复中...')
              : (pendingImage ? '给图片配句话，或直接发送…' : (isGroup ? '在群里说点什么...' : '说点什么...'))
          }
          placeholderTextColor={C.textMute}
          multiline
          editable={!loading}
          returnKeyType="send"
          onSubmitEditing={handleSend}
          blurOnSubmit={false}
        />
        <TouchableOpacity
          style={[s.sendBtn, { backgroundColor: canSend ? C.accent : C.textMute + '55' }]}
          onPress={handleSend}
          disabled={!canSend}
        >
          <Text style={[s.sendBtnText, { opacity: canSend ? 1 : 0.5 }]}>发送</Text>
        </TouchableOpacity>
      </View>

      {showCall && (
        <VoiceCallModal
          userId={FIXED_USER_ID}
          onClose={() => setShowCall(false)}
          onAddMessages={(newMsgs: Message[]) => setMessages(prev => [...prev, ...newMsgs])}
        />
      )}
    </View>
  );
}

const s = StyleSheet.create({
  header:          { flexDirection: 'row', alignItems: 'center', backgroundColor: C.card, paddingHorizontal: 12, paddingTop: Platform.OS === 'ios' ? 50 : 40, paddingBottom: 12, borderBottomWidth: 1, borderBottomColor: C.border, gap: 6 },
  backBtn:         { paddingHorizontal: 6, paddingVertical: 4 },
  backText:        { color: C.text, fontSize: 30, lineHeight: 32, fontWeight: '300' },
  avatarSmall:     { width: 40, height: 40, borderRadius: 20, backgroundColor: C.accentDim, alignItems: 'center', justifyContent: 'center', borderWidth: 1 },
  avatarSmallText: { color: '#fff', fontWeight: '700', fontSize: 16 },
  headerName:      { color: C.text, fontSize: 16, fontWeight: '600' },
  headerSub:       { color: C.textMute, fontSize: 11, marginTop: 2 },
  iconBtn:         { padding: 8 },
  iconBtnText:     { fontSize: 18 },
  clearBtn:        { paddingHorizontal: 10, paddingVertical: 6, borderRadius: 10, borderWidth: 1, borderColor: C.border },
  clearBtnText:    { color: C.textMute, fontSize: 12 },
  searchInput:     { flex: 1, backgroundColor: C.bg, borderRadius: 12, borderWidth: 1, borderColor: C.border, paddingHorizontal: 14, paddingVertical: 8, color: C.text, fontSize: 14 },
  cancelSearchBtn: { paddingHorizontal: 8, paddingVertical: 6 },
  cancelSearchText:{ color: C.accent2, fontSize: 14 },
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
  msgAvatar:       { width: 34, height: 34, borderRadius: 17, backgroundColor: C.accentDim + '55', alignItems: 'center', justifyContent: 'center', marginRight: 8, borderWidth: 1, borderColor: C.border },
  msgAvatarText:   { color: C.accent2, fontSize: 13, fontWeight: '700' },
  msgMain:         { maxWidth: width * 0.72 },
  msgSender:       { color: C.textMute, fontSize: 11, marginBottom: 4, marginLeft: 2 },
  bubble:          { borderRadius: 16, padding: 12 },
  bubbleGojo:      { backgroundColor: C.card, borderTopLeftRadius: 4, borderLeftWidth: 2, borderLeftColor: C.accent },
  bubbleUser:      { backgroundColor: C.userBubble, borderRadius: 16, borderTopRightRadius: 4 },
  bubbleHighlight: { borderWidth: 1.5, borderColor: C.accent2 },
  bubbleText:      { color: C.text, fontSize: 15, lineHeight: 22 },
  bubbleTextUser:  { color: '#fff' },
  subtitle:        { color: C.textDim, fontSize: 12, marginTop: 6, lineHeight: 18, fontStyle: 'italic' },
  replayHint:      { color: C.accent, fontSize: 11, marginTop: 6, opacity: 0.7 },
  msgBottom:       { flexDirection: 'row', alignItems: 'center', marginTop: 4, marginHorizontal: 2 },
  msgTime:         { color: C.textMute, fontSize: 10 },

  timeSepWrap:     { alignSelf: 'center', marginVertical: 12, paddingHorizontal: 14, paddingVertical: 4, borderRadius: 10, backgroundColor: C.card },
  timeSepText:     { color: C.textMute, fontSize: 11 },

  bubbleImage:     { width: width * 0.55, height: width * 0.42, borderRadius: 10, marginBottom: 6, backgroundColor: C.bg },

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