// app/chat/[id].tsx
// 通用聊天页：
//   - 单聊：id 是角色 id（如 'gojo' / 'geto'）→ 走 /chat/text、/chat/image
//   - 群聊：id 是 'group_<gid>'（如 'group_2'）→ 走 /group/chat
// 完整保留 gojo 单聊原有的功能：语音文件持久化、提醒/闹钟、图片暂存、搜索、长按复制。
// 电话按钮：仅 id==='gojo' 时显示。
// ★ 本版新增：
//   - 群聊@功能：输入"@"弹成员面板；长按角色消息的头像/名字快速@；发送时自动传 mentioned_id
//   - 图片头像:头部、消息行、@面板都支持 avatar_url 图片头像
//   - ★★ 发送防抖：2秒内同样内容不重复发（挡网络卡顿/重试导致的双发，顺便省一次 API+TTS）
//   - ★★ 长按气泡 → 复制 / 删除（删除对用户和角色气泡都生效，只删本地画面+落盘，不动后端记忆）
// ★ 记账升级：单聊里 LLM 检测到 pending_transaction → 塞一条卡片消息进对话
//   - 每次进本页 focus 时刷新账户列表（在别的 tab 加了账户,回来立刻能用）
//   - 卡片状态(pending/saved/dismissed)随 messages 一起持久化,重开 App 状态还在
import AsyncStorage from '@react-native-async-storage/async-storage';
import axios from 'axios';
import { Audio } from 'expo-av';
import * as Clipboard from 'expo-clipboard';
import * as FileSystem from 'expo-file-system/legacy';
import * as ImagePicker from 'expo-image-picker';
import * as IntentLauncher from 'expo-intent-launcher';
import * as Notifications from 'expo-notifications';
import { useFocusEffect, useLocalSearchParams, useRouter } from 'expo-router';
import * as VideoThumbnails from 'expo-video-thumbnails'; // ★ 视频抽帧
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
import PendingTransactionCard, { Account as AcctType } from '../../components/PendingTransactionCard'; // ★ 记账确认卡
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

// ★ 用来替代 Alert.alert('回复异常', ...) 这种出戏弹窗 ——
//   后端返回空 / 网络挂了都当他自己走神,塞条角色气泡糊过去
const FALLBACK_LINES: { jp: string; zh: string }[] = [
  { jp: 'ん…ちょっと今手が離せない。あとでな。', zh: '嗯…现在有点脱不开身，等等再说。' },
  { jp: 'あー、ごめん。何だっけ？',              zh: '啊，抱歉，你刚说什么来着？' },
  { jp: 'ちょっと聞き逃した。もう一回言ってくれる？', zh: '刚才没听清，能再说一遍吗？' },
  { jp: 'んー、電波悪いっぽい。もう一回。',      zh: '嗯…信号好像不太好，再来一次。' },
];

// 每个会话独立的存储 key（按 id 隔离）
const msgStorageKey = (id: string) => `chat_msgs_${id}`;

// ★ 聊天记录云端同步
//   之前单聊气泡只存 AsyncStorage,卸载重装/换手机就全没了。
//   现在写一份到服务器,本地保留当缓存(加载快、离线也能看)。
const CHATLOG_SYNCED_KEY = (id: string) => `chatlog_synced_${id}`;

/** 本地 Message → 服务器格式 */
function toServerMsg(m: any) {
  return {
    client_msg_id: String(m.id || ''),
    // ★ 带上消息真实时间。之前没传,补传的 200 条 ts 全是同一刻,
    //   按时间排序会乱套。
    ts: m.timestamp ? new Date(m.timestamp).toISOString() : undefined,
    role: m.role === 'user' ? 'user' : 'gojo',
    text: m.text || '',
    subtitle: m.subtitle || '',
    emotion: m.emotion || '',
    kind: m.imageUri ? 'image' : (m.callLog ? 'call_log' : 'text'),
    extra: m.imageUri ? JSON.stringify({ imageUri: m.imageUri }) : '',
    has_audio: !!m.audioB64 || !!m.hasAudio,
  };
}

/** 服务器格式 → 本地 Message */
function fromServerMsg(m: any): any {
  let extra: any = {};
  try { extra = m.extra ? JSON.parse(m.extra) : {}; } catch {}
  let tsStr = m.ts || '';
  if (tsStr && !/[Zz]|[+-]\d{2}:?\d{2}$/.test(tsStr)) tsStr += 'Z';
  const d = tsStr ? new Date(tsStr) : new Date();
  return {
    id: m.client_msg_id || `srv_${m.id}`,
    // ★ 保留服务端原始数字 ID —— 分页用 before_id 时要用它,
    //   不然拿 client_msg_id("1723..." 这种时间戳字符串)传过去会出错
    serverId: typeof m.id === 'number' ? m.id : undefined,
    role: m.role === 'user' ? 'user' : 'gojo',
    text: m.text || '',
    subtitle: m.subtitle || undefined,
    emotion: m.emotion || undefined,
    imageUri: extra.imageUri,
    time: `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`,
    timestamp: d.getTime(),
    hasAudio: !!m.has_audio,
  };
}

/** 追加到服务器。失败不阻断 UI —— 本地还有,下次进聊天页会补传。 */
async function syncToServer(userId: string, chatId: string, msgs: any[]) {
  if (!msgs || msgs.length === 0) return;
  try {
    await axios.post(`${SERVER_URL}/chatlog/append`, {
      user_id: userId,
      chat_id: chatId,
      messages: msgs.map(toServerMsg),
    }, { timeout: 8000 });
  } catch (e: any) {
    console.warn('[chatlog] 同步失败(本地已保留):', e?.message);
  }
}
const audioDir       = (id: string) => `${FileSystem.documentDirectory}chat_audio_${id}/`;

interface Character {
  id: string;
  name: string;
  voice_id?: string;
  greeting?: string;
  avatar_url?: string | null;
}
interface GroupMember {
  id: string;
  name: string;
  voice_id?: string;
  avatar_url?: string | null;
  is_owner_role?: boolean;
}
interface GroupDetail {
  id: number;
  name: string;
  members: GroupMember[];
  msg_count?: number;
}

interface Segment {
  jp: string;
  zh: string;
  audio_b64: string;
}
interface GroupReply {
  msg_id?: number;
  sender_id: string;
  sender_name: string;
  jp: string;
  zh: string;
  emotion: string;
  audio_b64: string;
}

interface PendingImage {
  base64: string;      // 单图=图片本身；视频=第一帧（用于本地预览）
  mediaType: string;
  uri: string;         // 本地预览用
  isVideo?: boolean;                                    // ★ 是不是视频
  frames?: { data: string; media_type: string }[];      // ★ 视频抽出的帧（按时间顺序）
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
  // ★ 已同步到服务器的消息 id,避免重复上传
  const syncedIdsRef = useRef<Set<string>>(new Set());
  const [inputText, setInputText] = useState('');
  const [loading, setLoading]     = useState(false);
  const inputText Ref = useRef('');   // ★ IME 双写:发送时从 ref 读最新值,防 composition 未 commit 截断
  const [ready, setReady]         = useState(false);
  const [showCall, setShowCall]   = useState(false);
  const [keyboardHeight, setKeyboardHeight] = useState(0);  // ★ 手动监听键盘高度
  const [pendingImage, setPendingImage] = useState<PendingImage | null>(null);
  const [searchMode, setSearchMode]   = useState(false);
  const [searchQuery, setSearchQuery] = useState('');
  const [showFullTime, setShowFullTime] = useState(false); // 点击时间条切换完整/简短

  // ★ 记账账户列表(给 pending_transaction 确认卡用)——单聊才拉
  const [accounts, setAccounts] = useState<AcctType[]>([]);

  const audioCacheRef   = useRef<Record<string, string>>({});
  const scrollRef       = useRef<ScrollView>(null);
  const searchRef       = useRef<TextInput>(null);
  const currentSoundRef = useRef<Audio.Sound | null>(null);
  const checkingProactiveRef = useRef(false);
  const interactionActiveRef = useRef(false);  // ★ 群聊互动轮询是否在进行
  const [thinkingName, setThinkingName] = useState<string | null>(null);  // ★ 正在思考的角色名
  const [showMention, setShowMention] = useState(false);  // ★ @成员选择面板
  const focusedRef = useRef(true);            // ★ 人是否还在这个页面
  const [hasMore, setHasMore] = useState(false);      // ★ 群聊：还有更早的消息
  const [loadingMore, setLoadingMore] = useState(false);
  const messagesRef = useRef<Message[]>([]);  // ★ 消息镜像（离开后仍能落盘）
  const lastSentRef = useRef<{ text: string; at: number }>({ text: '', at: 0 });  // ★ 防抖：挡网络卡顿导致的重复发送

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
  // ★ 不再按条数自动删音频——老消息的重播要永久可用。
  //   万一哪天占用太大，可在"清空记录"里一并清掉。
  const pruneAudioFiles = async () => { /* no-op：保留全部语音 */ };
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
              msg_count: res.data.msg_count,
            });
            // ★ 群聊以服务器为准：把服务器历史转成本地消息（离开期间生成的回复也能看到）
            const serverMsgs: Message[] = (res.data.messages || []).map((m: any) => ({
              id: m.msg_id != null ? `g_${m.msg_id}` : `srv_${m.ts || Math.random()}`,
              role: m.sender_type === 'user' ? 'user' : 'gojo',
              text: m.sender_type === 'user' ? (m.zh || '') : (m.jp || m.zh || ''),
              subtitle: m.sender_type === 'user' ? undefined : (m.zh || undefined),
              time: m.ts
                ? `${String(new Date(m.ts).getHours()).padStart(2, '0')}:${String(new Date(m.ts).getMinutes()).padStart(2, '0')}`
                : '',
              timestamp: m.ts || Date.now(),
              senderId: m.sender_id,
              senderName: m.sender_type === 'user' ? undefined : m.sender_name,
            }));
            setMessages(serverMsgs);
            setHasMore(!!res.data.has_more);   // ★ 还有更早的消息可往前翻
            // ★ 进群即已读：记录当前消息总数，列表页据此算未读红点
            try {
              await AsyncStorage.setItem(
                `group_read_count_${groupId}`,
                String(res.data.msg_count ?? serverMsgs.length)
              );
            } catch {}
          } catch (e) { console.warn('load group error', e); }
        } else {
          try {
            const res = await axios.get(`${SERVER_URL}/characters/${chatId}`);
            setCharacter(res.data);
          } catch (e) { console.warn('load character error', e); }
          // ★ 记账账户列表:提前拉,渲染前 accounts 就已就绪(旧的 pending 卡也能立刻可用)
          try {
            const accRes = await axios.get(`${SERVER_URL}/accounts?user_id=${FIXED_USER_ID}`);
            setAccounts(accRes.data?.accounts || []);
          } catch (e) { console.warn('load accounts error', e); }
          // ★ 服务器优先:卸载重装/换手机后本地是空的,但服务器有完整记录
          let loaded = false;
          try {
            const logRes = await axios.get(`${SERVER_URL}/chatlog`, {
              params: { user_id: FIXED_USER_ID, chat_id: chatId, limit: 500 },  // ★ 200→500,覆盖几个月的日常聊天
              timeout: 15000,
            });
            const srv = (logRes.data?.messages || []).map(fromServerMsg);
            if (srv.length > 0) {
              // 从服务器来的都算已同步,别再传回去
              srv.forEach((m: any) => syncedIdsRef.current.add(String(m.id)));
              // ★ server 数据没有 readAt 字段,从本地 sidecar 合并回来,不然重进会丢已读
              const withRead = await applyReadStatusFromStorage(srv);
              setMessages(withRead);
              setHasMore(!!logRes.data?.has_more);
              AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(withRead)).catch(() => {});
              loaded = true;
            }
          } catch (e: any) {
            console.warn('[chatlog] 拉取失败,改用本地缓存:', e?.message);
          }

          if (!loaded) {
            // 服务器没有(第一次用)或拉取失败 → 用本地缓存
            const saved = await AsyncStorage.getItem(STORAGE_KEY);
            if (saved) {
              const localMsgs = JSON.parse(saved);
              // ★ 本地缓存有可能存了 readAt(旧路径),但为了统一,还是走一遍 sidecar 合并
              const withRead = await applyReadStatusFromStorage(localMsgs);
              setMessages(withRead);
              // ★ 本地有、服务器没有 → 补传上去(老用户首次升级的迁移)
              const syncedFlag = await AsyncStorage.getItem(CHATLOG_SYNCED_KEY(chatId));
              if (!syncedFlag && localMsgs.length > 0) {
                const toMigrate = localMsgs.slice(-500);   // 尽量多传,别再漏
                toMigrate.forEach((m: any) => syncedIdsRef.current.add(String(m.id)));
                syncToServer(FIXED_USER_ID, chatId, toMigrate)
                  .then(() => AsyncStorage.setItem(CHATLOG_SYNCED_KEY(chatId), '1'))
                  .catch(() => {});
              }
            }
          }
        }

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

  // ★ 本地缓存只留最近这么多条。
  //   教训:之前无上限地整体覆写,数组撑大后 AsyncStorage 写入静默失败,
  //   本地记录在 8/3 就停止更新且六天无人察觉。
  //   服务器已有完整历史,本地只是为了打开快,留 120 条足够。
  const LOCAL_CACHE_MAX = 120;

  useEffect(() => {
    if (!ready) return;
    messagesRef.current = messages;

    // ★ 写入失败必须能看见 —— 不再 .catch(() => {}) 静默吞掉
    const toCache = messages.length > LOCAL_CACHE_MAX
      ? messages.slice(-LOCAL_CACHE_MAX)
      : messages;
    AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(toCache)).catch((e: any) => {
      console.warn('[本地缓存] 写入失败(服务器仍有记录):', e?.message);
    });

    // ★ 同步新消息到服务器(群聊本来就在服务器上,不用重复存)
    //   注意:这段【不依赖】上面的本地写入,本地坏了服务器照常收
    if (!isGroup && messages.length > 0) {
      const pending = messages.filter(m => {
        const mid = String((m as any).id || '');
        if (!mid || syncedIdsRef.current.has(mid)) return false;
        if (mid.startsWith('srv_')) return false;      // 从服务器拉下来的,别再传回去
        return true;
      });
      if (pending.length > 0) {
        pending.forEach(m => syncedIdsRef.current.add(String((m as any).id)));
        syncToServer(FIXED_USER_ID, chatId, pending);
      }
    }
  }, [messages, ready, isGroup, chatId]);

  // ★ 已读计数 +delta（列表页红点 = 服务器总数 - 本地已读数）
  const bumpRead = async (delta: number) => {
    if (!isGroup || groupId == null) return;
    try {
      const k = `group_read_count_${groupId}`;
      const v = parseInt((await AsyncStorage.getItem(k)) || '0', 10);
      await AsyncStorage.setItem(k, String(v + delta));
    } catch {}
  };

  // ★ 焦点管理：进入本页 → 标记在场、清掉本会话未读；
  //   离开本页 → 停群互动轮询、停正在播的语音、后续消息静默落盘计未读
  useFocusEffect(useCallback(() => {
    focusedRef.current = true;
    if (!isGroup) AsyncStorage.setItem(`char_unread_${chatId}`, '0').catch(() => {});
    return () => {
      focusedRef.current = false;
      interactionActiveRef.current = false;
      setThinkingName(null);
      currentSoundRef.current?.unloadAsync().catch(() => {});
      currentSoundRef.current = null;
    };
  }, [chatId, isGroup]));

  // ★ 记账账户列表:进入/切回本页都刷新一次(别的 tab 加了账户,回来立刻能用)
  useFocusEffect(useCallback(() => {
    if (isGroup) return;
    axios.get(`${SERVER_URL}/accounts?user_id=${FIXED_USER_ID}`)
      .then(r => setAccounts(r.data?.accounts || []))
      .catch(() => {});
  }, [isGroup]));

  // ★ 主动消息:进入本页拉一次,把服务器上生成的主动汇报塞进聊天列表
  //   (proactive_scheduler 会存到 proactive_msg 表,前端进来才知道)
  useFocusEffect(useCallback(() => {
    if (!ready || isGroup) return;
    // 稍延迟,让主 init 加载完成的 messages 先落地,避免和 fetchPendingProactive 的 setMessages 打架
    const t = setTimeout(() => { fetchPendingProactive(); }, 300);
    return () => clearTimeout(t);
  }, [ready, chatId, isGroup]));

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
        const msg: Message = { id: msgId, role: 'gojo', text: seg.jp, subtitle: seg.zh, time: nowTime(), timestamp: Date.now() };
        setMessages(prev => [...prev, msg]);
        scrollRef.current?.scrollToEnd({ animated: true });
        if (audioUri) await playAudioAndWait(audioUri);
        if (i < segments.length - 1) await sleep(MSG_DELAY_MS);
      }
      pruneAudioFiles();
    } catch (e) { console.warn('sendProactive error', e); }
  };

  // ★ 往前翻更早的聊天记录（服务器永久保存,随便翻）—— 群聊和单聊都走这个
  const loadEarlier = async () => {
    if (loadingMore) return;

    // ── 群聊分支（原逻辑保留）──
    if (isGroup) {
      if (groupId == null) return;
      const oldest = messages.find(m => m.id.startsWith('g_'));
      if (!oldest) return;
      const beforeId = parseInt(oldest.id.replace('g_', ''), 10);
      if (!beforeId) return;
      try {
        setLoadingMore(true);
        const res = await axios.get(`${SERVER_URL}/group/${groupId}/history`, {
          params: { before_id: beforeId, limit: 30 },
        });
        const older: Message[] = (res.data?.messages || []).map((m: any) => ({
          id: m.msg_id != null ? `g_${m.msg_id}` : `srv_${m.ts || Math.random()}`,
          role: m.sender_type === 'user' ? 'user' : 'gojo',
          text: m.sender_type === 'user' ? (m.zh || '') : (m.jp || m.zh || ''),
          subtitle: m.sender_type === 'user' ? undefined : (m.zh || undefined),
          time: m.ts
            ? `${String(new Date(m.ts).getHours()).padStart(2, '0')}:${String(new Date(m.ts).getMinutes()).padStart(2, '0')}`
            : '',
          timestamp: m.ts || Date.now(),
          senderId: m.sender_id,
          senderName: m.sender_type === 'user' ? undefined : m.sender_name,
        }));
        if (older.length > 0) setMessages(prev => [...older, ...prev]);
        setHasMore(!!res.data?.has_more);
      } catch (e: any) {
        console.warn('loadEarlier(group)', e?.message);
      } finally {
        setLoadingMore(false);
      }
      return;
    }

    // ── 单聊分支 ──
    //   用当前 messages 里最小的 serverId 作为分页锚点。
    //   本地新发的消息没 serverId,只看从 server 拉下来那批。
    let oldestSrvId: number | null = null;
    for (const m of messages) {
      const sid = (m as any).serverId;
      if (typeof sid === 'number') {
        if (oldestSrvId === null || sid < oldestSrvId) oldestSrvId = sid;
      }
    }
    if (oldestSrvId === null) return;   // 没有 server 消息可翻(全是本地新的)

    try {
      setLoadingMore(true);
      const res = await axios.get(`${SERVER_URL}/chatlog`, {
        params: {
          user_id: FIXED_USER_ID, chat_id: chatId,
          before_id: oldestSrvId, limit: 200,   // 一次翻 200 条,后端硬顶 500
        },
        timeout: 15000,
      });
      const older = (res.data?.messages || []).map(fromServerMsg);
      if (older.length > 0) {
        // ★ 已读状态从 sidecar 合并
        const olderWithRead = await applyReadStatusFromStorage(older);
        // 标记为已同步,避免被 sync 补传回去
        olderWithRead.forEach((m: any) => syncedIdsRef.current.add(String(m.id)));
        setMessages(prev => [...olderWithRead, ...prev]);
      }
      setHasMore(!!res.data?.has_more);
    } catch (e: any) {
      console.warn('loadEarlier(single)', e?.message);
    } finally {
      setLoadingMore(false);
    }
  };

  // 重播（★ 本地没有音频就现场重新合成——任何年代的老消息都能重播）
  const [resynthing, setResynthing] = useState<string | null>(null);
  const replayAudio = async (msgId: string) => {
    const uri = audioCacheRef.current[msgId];
    if (uri) { await playAudioAndWait(uri); return; }

    // 找到这条消息，拿日语原文和说话人
    const msg = messagesRef.current.find(m => m.id === msgId) || messages.find(m => m.id === msgId);
    if (!msg || msg.role === 'user' || !msg.text) return;
    const speakerId = msg.senderId || (isGroup ? null : chatId);
    if (!speakerId) return;

    try {
      setResynthing(msgId);
      const res = await axios.post(`${SERVER_URL}/tts/resynth`, {
        text: msg.text, character_id: speakerId,
      }, { timeout: 30000 });
      const b64 = res.data?.audio_b64;
      if (!b64) { Alert.alert('语音合成失败', '稍后再试一次'); return; }
      const newUri = await saveAudioFile(msgId, b64);
      if (newUri) {
        audioCacheRef.current[msgId] = newUri;
        await playAudioAndWait(newUri);
      }
    } catch (e: any) {
      Alert.alert('语音合成失败', e?.response?.data?.error === 'tts_failed'
        ? '语音服务忙（并发超限），过几秒再点一次'
        : (e?.message ?? '稍后再试'));
    } finally {
      setResynthing(null);
    }
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

  // ★ 记账:把后端返回的 pending_transaction 塞成一条卡片消息
  //   放在 appendSegments 之后调用,这样顺序是 [用户消息 → 角色的日语气泡 → 确认卡]
  const insertPendingCard = (pt: any) => {
    if (!pt || (pt.type !== 'in' && pt.type !== 'out')) return;
    const cardMsg: Message = {
      id: `${Date.now()}_pt_${Math.random().toString(36).slice(2, 8)}`,
      role: 'gojo',
      text: '',
      timestamp: Date.now(),
      pendingTx: {
        type: pt.type,
        category: pt.category || '其他',
        amount: Number(pt.amount) || 0,
        desc: pt.desc || '',
        account_hint: pt.account_hint || '',
        date: pt.date || null,
        time: pt.time || null,
      },
      pendingStatus: 'pending',
    };
    if (focusedRef.current) {
      setMessages(prev => [...prev, cardMsg]);
      scrollRef.current?.scrollToEnd({ animated: true });
    } else {
      // 人已离开:静默写入,回来时能看到
      messagesRef.current = [...messagesRef.current, cardMsg];
      AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(messagesRef.current)).catch(() => {});
    }
    // 顺手刷账户列表,确认卡上的余额是最新的
    axios.get(`${SERVER_URL}/accounts?user_id=${FIXED_USER_ID}`)
      .then(r => setAccounts(r.data?.accounts || []))
      .catch(() => {});
  };

  // ★ 主动消息:拉服务器上"未读的主动汇报/问候",塞进聊天列表 + 标记已读
  //   —— 群聊不用;proactive_scheduler 只发给单聊角色
  const fetchPendingProactive = async () => {
    if (isGroup) return;
    try {
      const res = await axios.get(`${SERVER_URL}/proactive/pending`, {
        params: { user_id: FIXED_USER_ID, character_id: chatId },
        timeout: 10000,
      });
      const proactives: any[] = res.data?.messages || [];
      if (proactives.length === 0) return;

      const readIds: number[] = [];
      const newMsgs: Message[] = [];
      for (const p of proactives) {
        const msgId = `proactive_${p.id}`;
        // 保存语音文件,前端能点击重播
        if (p.audio_b64 && p.audio_b64.length > 100) {
          const audioUri = await saveAudioFile(msgId, p.audio_b64);
          if (audioUri) audioCacheRef.current[msgId] = audioUri;
        }
        // 用服务器时间戳,不是 Date.now(),这样多条按真实先后顺序排
        const ts = p.created_at
          ? new Date(p.created_at.replace(' ', 'T') + (p.created_at.includes('Z') ? '' : 'Z')).getTime()
          : Date.now();
        const t = new Date(ts);
        newMsgs.push({
          id: msgId,
          role: 'gojo',
          text: p.jp || '',
          subtitle: p.zh || undefined,
          time: `${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}`,
          timestamp: ts,
        });
        readIds.push(p.id);
      }

      // 合并去重(按 id) + 按时间戳排序(处理"离线期间生成的旧消息"这种情况)
      setMessages(prev => {
        const existing = new Set(prev.map(m => m.id));
        const toAdd = newMsgs.filter(m => !existing.has(m.id));
        if (toAdd.length === 0) return prev;
        const merged = [...prev, ...toAdd].sort(
          (a, b) => (a.timestamp ?? 0) - (b.timestamp ?? 0)
        );
        return merged;
      });

      // 滚到底让新消息可见
      setTimeout(() => scrollRef.current?.scrollToEnd({ animated: true }), 100);

      // ★ 已读时机:他通过 proactive 发消息了 = 他"忙完/上线"看到了之前的消息 →
      //   把之前所有未读的 user 消息标已读。这正是"他忙完再回你"的自然时机。
      if (newMsgs.length > 0) {
        markPendingUserMessagesRead();
      }

      // 标记已读(失败也不管,反正下次进来还会拉,是幂等的)
      if (readIds.length > 0) {
        axios.post(`${SERVER_URL}/proactive/read`, { msg_ids: readIds }).catch(() => {});
      }
    } catch (e: any) {
      console.warn('fetchPendingProactive', e?.message);
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
  // ★ 发视频：Claude 只能看图片，看不了视频文件。
  //   所以按时间顺序抽 4 张关键帧当"连环画"发过去，让他把这段当成连续发生的一件事来看。
  const VIDEO_FRAMES = 4;
  const pickVideo = async () => {
    try {
      const { status } = await ImagePicker.requestMediaLibraryPermissionsAsync();
      if (status !== 'granted') { Alert.alert('需要相册权限', '请在设置中允许访问相册'); return; }
      const result = await ImagePicker.launchImageLibraryAsync({
        mediaTypes: ['videos'], quality: 0.7, allowsEditing: false,
      });
      if (result.canceled || !result.assets?.[0]) return;
      const asset = result.assets[0];
      const durationMs = Math.max(asset.duration ?? 3000, 500);
      if (durationMs > 120000) {
        Alert.alert('视频太长了', '选 2 分钟以内的片段吧，太长的话抽出来的画面跳太狠，他看不明白。');
        return;
      }

      setLoading(true);
      // 在 0% / 33% / 66% / 95% 处各抽一帧
      const points = Array.from({ length: VIDEO_FRAMES }, (_, i) =>
        Math.floor(durationMs * (i / (VIDEO_FRAMES - 1)) * 0.95)
      );
      const frames: { data: string; media_type: string }[] = [];
      let firstUri = asset.uri;
      for (const t of points) {
        try {
          const { uri } = await VideoThumbnails.getThumbnailAsync(asset.uri, { time: t, quality: 0.6 });
          const b64 = await FileSystem.readAsStringAsync(uri, { encoding: 'base64' as any });
          frames.push({ data: b64, media_type: 'image/jpeg' });
          if (frames.length === 1) firstUri = uri;
        } catch (err) {
          console.warn('thumbnail fail at', t, err);
        }
      }
      setLoading(false);
      if (frames.length === 0) { Alert.alert('读取视频失败', '换一个视频试试'); return; }

      setPendingImage({
        base64: frames[0].data, mediaType: 'image/jpeg', uri: firstUri,
        isVideo: true, frames,
      });
    } catch (e: any) {
      setLoading(false);
      console.warn('pickVideo error', e);
      Alert.alert('选视频失败', e?.message ?? '请重试');
    }
  };

  const showImagePicker = () => {
    Alert.alert('发送', '选择要发什么', [
      { text: '📷 拍照', onPress: () => pickImage(true) },
      { text: '🖼 从相册选图片', onPress: () => pickImage(false) },
      { text: '🎬 发送视频', onPress: () => pickVideo() },
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
      const msg: Message = { id: msgId, role: 'gojo', text: seg.jp, subtitle: seg.zh, time: nowTime(), timestamp: Date.now() };
      if (focusedRef.current) {
        setMessages(prev => [...prev, msg]);
        scrollRef.current?.scrollToEnd({ animated: true });
      } else {
        // ★ 人已离开：静默写入本机存储 + 计未读，回来能看到、语音可点重播
        messagesRef.current = [...messagesRef.current, msg];
        AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(messagesRef.current)).catch(() => {});
        try {
          const k = `char_unread_${chatId}`;
          const v = parseInt((await AsyncStorage.getItem(k)) || '0', 10);
          await AsyncStorage.setItem(k, String(v + 1));
        } catch {}
      }
      if (audioUri && focusedRef.current) await playAudioAndWait(audioUri);
      if (i < segments.length - 1 && focusedRef.current) await sleep(MSG_DELAY_MS);
    }
  };

  // 群聊：把多个角色的回复依次渲染
  const appendGroupReplies = async (replies: GroupReply[]) => {
    for (let i = 0; i < replies.length; i++) {
      const r = replies[i];
      const msgId = r.msg_id != null ? `g_${r.msg_id}` : `${Date.now()}_${i}_${r.sender_id}`;
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
      bumpRead(1);
      if (audioUri) await playAudioAndWait(audioUri);
      if (i < replies.length - 1) await sleep(MSG_DELAY_MS);
    }
  };

  // ★ 单条群聊回复追加(流式用)。用服务器 msg_id 作 id，重进群后与历史对得上（重播也能续上）
  const appendOneGroupReply = async (r: GroupReply) => {
    const msgId = r.msg_id != null ? `g_${r.msg_id}` : `${Date.now()}_${r.sender_id}`;
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
    if (!focusedRef.current) {
      // ★ 人已离开：不进状态、不播音、不计已读——服务器存着，回来时从历史加载并显示红点
      return;
    }
    setMessages(prev => [...prev, msg]);
    scrollRef.current?.scrollToEnd({ animated: true });
    bumpRead(1);
    if (audioUri) await playAudioAndWait(audioUri);
  };

  // ★ 群聊互动轮询(可打断)
  const runGroupInteraction = async (originalText: string, turnsUsed: number) => {
    interactionActiveRef.current = true;
    let turns = turnsUsed;

    while (interactionActiveRef.current && turns < 8) {
      setThinkingName('有人想接话...');
      try {
        const contRes = await axios.post(`${SERVER_URL}/group/chat/continue`, {
          group_id: groupId, turns_used: turns, user_text: originalText,
        }, { timeout: 30000 });

        if (!interactionActiveRef.current) break;
        if (contRes.data?.done || !contRes.data?.replies?.length) break;

        setThinkingName(null);
        for (const r of contRes.data.replies) {
          if (!interactionActiveRef.current) break;
          await appendOneGroupReply(r);
        }
        turns++;
      } catch (e) {
        break;
      }
    }

    setThinkingName(null);
    interactionActiveRef.current = false;
    pruneAudioFiles();
  };

  // ★ 兜底气泡:后端返回空 segments / 网络挂时用,不再弹系统 Alert
  //   Alert.alert('回复异常', ...) 会瞬间把角色扮演那层皮撕开,
  //   塞条 "没听清,再说一遍" 的气泡当他自己走神,沉浸感不断。
  const appendFallbackBubble = () => {
    const line = FALLBACK_LINES[Math.floor(Math.random() * FALLBACK_LINES.length)];
    const msg: Message = {
      id: `fallback_${Date.now()}`,
      role: 'gojo',
      text: line.jp,
      subtitle: line.zh,
      time: nowTime(),
      timestamp: Date.now(),
    };
    setMessages(prev => [...prev, msg]);
    scrollRef.current?.scrollToEnd({ animated: true });
  };

  // ★ 已读标记:把所有还没读的 user 消息标为"对方已读"
  //   语义 = 微信:后端返回 200(不管是正常回复还是 busy=true),都算他"看到了"。
  //   什么时候不标:HTTP 失败/超时 —— 那说明消息压根没送到,还是"未读"。
  //
  //   ★ 时机:加 800-2000ms 随机延迟,让"读"这个动作看起来自然,
  //     不然 busy 响应几百毫秒就回来,已读瞬间亮起来很出戏。
  //   ★ 持久化:同时写一份到本地 sidecar(read_at_${chatId})。
  //     重进聊天时 server 数据没这个字段,得从 sidecar 合并回来才不会丢。
  const READ_AT_KEY = (id: string) => `read_at_${id}`;

  const markPendingUserMessagesRead = async () => {
    // 自然一点的"读"延迟 —— 别让人觉得对面是台机器
    await sleep(800 + Math.floor(Math.random() * 1200));
    const now = Date.now();
    setMessages(prev => {
      const updated = prev.map(m =>
        m.role === 'user' && !m.readAt ? { ...m, readAt: now } : m
      );
      // 顺手落地到 sidecar,重进不会丢
      const readAtMap: Record<string, number> = {};
      updated.forEach(m => {
        if (m.role === 'user' && m.readAt) readAtMap[m.id] = m.readAt;
      });
      AsyncStorage.setItem(READ_AT_KEY(chatId), JSON.stringify(readAtMap)).catch(() => {});
      return updated;
    });
  };

  // ★ 从 sidecar 读回已读状态,合并到当前 messages 上。
  //   在任何一次 setMessages(来自 server/local/history)之后调用。
  const applyReadStatusFromStorage = async (msgs: Message[]): Promise<Message[]> => {
    try {
      const saved = await AsyncStorage.getItem(READ_AT_KEY(chatId));
      if (!saved) return msgs;
      const readAtMap: Record<string, number> = JSON.parse(saved);
      return msgs.map(m =>
        m.role === 'user' && readAtMap[m.id] ? { ...m, readAt: readAtMap[m.id] } : m
      );
    } catch { return msgs; }
  };

  // ── 发送（统一入口）──
  const sendImage = async (
    base64: string, mediaType: string, localUri: string, caption: string,
    video?: { frames: { data: string; media_type: string }[] },   // ★ 传了就是视频
  ) => {
    // ★ 打断互动
    if (isGroup && interactionActiveRef.current) {
      interactionActiveRef.current = false;
      setThinkingName(null);
      await sleep(100);
    }
    if (loading) return;
    setLoading(true);
    const userMsg: Message = {
      id: Date.now().toString(), role: 'user',
      text: caption || (video ? '🎬 [视频]' : '📷 [图片]'),
      time: nowTime(), timestamp: Date.now(), imageUri: localUri,
    };
    setMessages(prev => [...prev, userMsg]);
    scrollRef.current?.scrollToEnd({ animated: true });
    if (isGroup) bumpRead(1);

    try {
      if (isGroup) {
        if (video) {
          Alert.alert('群聊暂不支持视频', '先在单聊里发给他吧，群聊的视频支持稍后再加。');
          setMessages(prev => prev.filter(m => m.id !== userMsg.id));
          setLoading(false);
          return;
        }
        // ★ 群聊发图:第一波(不互动)
        const res = await axios.post(`${SERVER_URL}/group/chat`, {
          group_id: groupId,
          text: caption,
          user_id: FIXED_USER_ID,
          image_base64: base64,
          media_type: mediaType,
          allow_interaction: false,
        }, { timeout: 60000 });
        const replies: GroupReply[] = res.data?.replies || [];
        if (replies.length === 0) {
          const sys: Message = {
            id: `${Date.now()}_sys`, role: 'gojo',
            text: '（群里暂时没人接话）', time: nowTime(), timestamp: Date.now(),
          };
          setMessages(prev => [...prev, sys]);
        } else {
          for (const r of replies) {
            await appendOneGroupReply(r);
          }
        }
        setLoading(false);
        if (replies.length > 0) {
          runGroupInteraction(caption || '📷 [图片]', replies.length);
        }
        return;
      } else {
        // 单聊发图/发视频（视频=按时间顺序的多帧）
        const payload: any = {
          user_id: FIXED_USER_ID,
          text: caption,
          character_id: chatId,
        };
        if (video) {
          payload.images = video.frames;
          payload.is_video = true;
        } else {
          payload.image_base64 = base64;
          payload.media_type = mediaType;
        }
        const res = await axios.post(`${SERVER_URL}/chat/image`, payload,
          { timeout: video ? 90000 : 60000 });
        await processResponseExtras(res.data);
        // ★ 同上:busy 时他没看,别秒已读
        if (!res.data?.busy) {
          markPendingUserMessagesRead();
        }
        const segments: Segment[] = res.data?.messages || [];
        if (segments.length === 0) { appendFallbackBubble(); return; }
        await appendSegments(segments, `${Date.now()}`);
        // ★ 记账:LLM 检测到消费就插确认卡(放在气泡之后)
        if (res.data?.pending_transaction) insertPendingCard(res.data.pending_transaction);
      }
      pruneAudioFiles();
    } catch (e: any) {
      // ★ 不弹 Alert.alert('发送失败') —— 出戏。塞条角色气泡替代,当他自己走神/信号差
      console.warn('[sendImage] failed:', e?.message);
      appendFallbackBubble();
    } finally { setLoading(false); }
  };

  const sendText = async (textOverride?: string) => {
    const text = (textOverride ?? inputText).trim();
    if (!text) return;
    // ★ 防抖：2秒内同样内容不重复发（挡网络卡顿/重试导致的双发，也省一次 API+TTS）
    const now = Date.now();
    if (text === lastSentRef.current.text && now - lastSentRef.current.at < 2000) {
      console.log('[防抖] 拦截重复发送：', text);
      return;
    }
    lastSentRef.current = { text, at: now };
    // ★ 打断:如果群里还在互动,立刻停掉,让新消息优先
    if (isGroup && interactionActiveRef.current) {
      interactionActiveRef.current = false;
      setThinkingName(null);
      await sleep(100);
    }
    if (loading) return;
    setInputText('');
    inputTextRef.current = '';   // ★ 顺手清 ref
    setShowMention(false);
    if (searchMode) { setSearchMode(false); setSearchQuery(''); }

    const userMsg: Message = { id: Date.now().toString(), role: 'user', text, time: nowTime(), timestamp: Date.now() };
    setMessages(prev => [...prev, userMsg]);
    if (isGroup) bumpRead(1);
    setLoading(true);

    try {
      if (isGroup) {
        // ★ 扫描文本里的 @角色名 → 传给后端，被@的人强制先回话
        let mentionedId: string | null = null;
        if (group) {
          for (const m of group.members) {
            if (text.includes('@' + m.name)) { mentionedId = m.id; break; }
          }
        }
        // ★ 第一波:只拿直接回复,不做互动循环
        const res = await axios.post(`${SERVER_URL}/group/chat`, {
          group_id: groupId,
          text,
          user_id: FIXED_USER_ID,
          mentioned_id: mentionedId,
          allow_interaction: false,
        });
        // ★ 群聊拿到 200 也算已读(哪怕没人接话)
        markPendingUserMessagesRead();
        const replies: GroupReply[] = res.data?.replies || [];
        if (replies.length === 0) {
          const sys: Message = {
            id: `${Date.now()}_sys`, role: 'gojo',
            text: '（群里暂时没人接话）', time: nowTime(), timestamp: Date.now(),
          };
          setMessages(prev => [...prev, sys]);
        } else {
          // 逐条显示第一波回复
          for (const r of replies) {
            await appendOneGroupReply(r);
          }
        }
        // ★ 第一波完了,解锁输入,开始互动轮询(用户随时可以打断)
        setLoading(false);
        if (replies.length > 0) {
          runGroupInteraction(text, replies.length);
        }
        return; // ← 不走 finally 的 setLoading(false),因为已经手动设了
      } else {
        const res = await axios.post(`${SERVER_URL}/chat/text`, {
          text, user_id: FIXED_USER_ID, character_id: chatId,
        });
        await processResponseExtras(res.data);
        // ★ 已读的语义:【他真的看到并回复了】才算已读。
        //   busy=true 时他正忙着,消息进了 promise 队列,他自己都没看 —— 不该秒已读。
        //   等他忙完通过 proactive 或下次交互回复时,那时候才会通过其他路径标已读。
        if (!res.data?.busy) {
          markPendingUserMessagesRead();
        }
        let segments: Segment[] = [];
        if (Array.isArray(res.data?.messages) && res.data.messages.length > 0) {
          segments = res.data.messages;
        } else if (res.data?.jp) {
          segments = [{ jp: res.data.jp, zh: res.data.zh ?? '', audio_b64: res.data.audio_b64 ?? '' }];
        }
        if (segments.length === 0) { appendFallbackBubble(); return; }
        await appendSegments(segments, `${Date.now()}`);
        // ★ 记账:LLM 检测到消费就插确认卡(放在气泡之后)
        if (res.data?.pending_transaction) insertPendingCard(res.data.pending_transaction);
      }
      pruneAudioFiles();
    } catch (e: any) {
      // ★ 不弹 Alert.alert('连接失败') —— 出戏。塞条角色气泡替代,当他自己走神/信号差
      console.warn('[sendText] failed:', e?.message);
      appendFallbackBubble();
    } finally { setLoading(false); }
  };

  const handleSend = async () => {
    if (loading) return;

    // ★★ IME 截断修复:
    //   中文输入法在拼音候选还没选字确认时,TextInput state 可能没接住最后一次 keystroke。
    //   Keyboard.dismiss() 强制 IME commit,sleep 让 state 追上,再从 ref 读最新值。
    Keyboard.dismiss();
    await sleep(80);
    const latestText = inputTextRef.current || inputText;

    if (pendingImage) {
      const caption = latestText.trim();
      const img = pendingImage;
      setPendingImage(null);
      setInputText('');
      inputTextRef.current = '';
      if (searchMode) { setSearchMode(false); setSearchQuery(''); }
      sendImage(img.base64, img.mediaType, img.uri, caption,
                img.isVideo && img.frames ? { frames: img.frames } : undefined);
    } else {
      sendText(latestText);   // ★ 显式传入,不再让 sendText 从 state 读
    }
  };

  const clearHistory = () =>
    Alert.alert('清空记录', isGroup ? '会连服务器上的群记录一起清空，确认？' : '只清空这个会话的记录，确认？', [
      { text: '取消', style: 'cancel' },
      { text: '清空', style: 'destructive',
        onPress: async () => {
          setMessages([]);
          audioCacheRef.current = {};
          await AsyncStorage.removeItem(STORAGE_KEY);
          try { await FileSystem.deleteAsync(AUDIO_DIR, { idempotent: true }); } catch {}
          // ★ 群聊以服务器为准：不清服务器的话，一进群又全回来了
          if (isGroup && groupId != null) {
            try { await axios.delete(`${SERVER_URL}/group/${groupId}/messages`); } catch {}
            try { await AsyncStorage.setItem(`group_read_count_${groupId}`, '0'); } catch {}
          } else {
            // ★ 单聊同理:不清服务器的话,重进聊天页记录又全回来了
            try {
              await axios.delete(`${SERVER_URL}/chatlog`, {
                params: { user_id: FIXED_USER_ID, chat_id: chatId },
              });
            } catch (e: any) { console.warn('[chatlog] 清空服务器失败:', e?.message); }
            try { await AsyncStorage.removeItem(CHATLOG_SYNCED_KEY(chatId)); } catch {}
            syncedIdsRef.current.clear();
          }
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

  // ★ 删除单个气泡：从画面移除 + 落盘 + 删掉本地语音文件（用户和角色的气泡都能删）
  //   注意：只删本地画面，不动后端短期记忆——那条很快会滑出上下文窗口，无需硬删。
  const deleteMessage = (msg: Message) => {
    Alert.alert('删除这条', msg.subtitle || msg.text || '', [
      { text: '取消', style: 'cancel' },
      { text: '删除', style: 'destructive', onPress: async () => {
        setMessages(prev => prev.filter(m => m.id !== msg.id));
        // 顺手删掉这条的本地语音文件（如果有）
        const uri = audioCacheRef.current[msg.id];
        if (uri) {
          delete audioCacheRef.current[msg.id];
          try { await FileSystem.deleteAsync(uri, { idempotent: true }); } catch {}
        }
      }},
    ]);
  };

  // ★ 长按气泡：复制 or 删除
  const onBubbleLongPress = (msg: Message) => {
    Alert.alert('这条消息', '', [
      { text: '📋 复制', onPress: () => copyMessage(msg) },
      { text: '🗑 删除', style: 'destructive', onPress: () => deleteMessage(msg) },
      { text: '取消', style: 'cancel' },
    ]);
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
            <View style={[s.avatarSmall, { borderColor: isGroup ? C.accent2 : C.accent, overflow: 'hidden' }]}>
              {!isGroup && character?.avatar_url ? (
                <Image source={{ uri: character.avatar_url }} style={{ width: '100%', height: '100%' }} resizeMode="cover" />
              ) : (
                <Text style={s.avatarSmallText}>{isGroup ? '群' : (headerTitle?.[0] || '?')}</Text>
              )}
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
            {/* ★ 日记入口：仅单聊显示。点一下弹"看他的日记 / 写我的日记" */}
            {!isGroup && (
              <TouchableOpacity
                onPress={() => Alert.alert('日记', '要做什么？', [
                  { text: '📔 看他的日记', onPress: () => router.push(`/diary/${chatId}` as any) },
                  { text: '🖊 写我的日记', onPress: () => router.push('/diary/mine' as any) },
                  { text: '取消', style: 'cancel' },
                ])}
                style={s.iconBtn}
              >
                <Text style={s.iconBtnText}>📔</Text>
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
        {/* ★ 载入更早(单聊 + 群聊都有,聊天记录永久存在服务器,不占 token) */}
        {hasMore && !searchMode && (
          <TouchableOpacity style={s.loadMoreBtn} onPress={loadEarlier} disabled={loadingMore}>
            {loadingMore
              ? <ActivityIndicator size="small" color={C.accent2 || '#5BC4FF'} />
              : <Text style={s.loadMoreText}>↑ 载入更早的消息</Text>}
          </TouchableOpacity>
        )}

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
          const prevMsg = idx > 0 ? displayMessages[idx - 1] : null;
          const showSep = shouldShowSeparator(msg, prevMsg);

          // ★ 记账确认卡 —— 不走普通气泡渲染,单独用组件展示
          if (msg.pendingTx) {
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
                <View style={{ marginBottom: 12 }}>
                  <PendingTransactionCard
                    userId={FIXED_USER_ID}
                    transaction={msg.pendingTx}
                    accounts={accounts}
                    initialStatus={msg.pendingStatus || 'pending'}
                    onStatusChange={(status) => {
                      setMessages(prev => prev.map(m =>
                        m.id === msg.id ? { ...m, pendingStatus: status } : m
                      ));
                    }}
                    onSaved={() => {
                      // 记账成功后刷余额,让下条卡的账户余额是最新的
                      axios.get(`${SERVER_URL}/accounts?user_id=${FIXED_USER_ID}`)
                        .then(r => setAccounts(r.data?.accounts || []))
                        .catch(() => {});
                    }}
                  />
                </View>
              </React.Fragment>
            );
          }

          // ── 下面是原来的普通消息渲染 ──
          // ★ 角色消息一律可重播：有本地音频直接放，没有就点了现场重合成
          const hasAudio = msg.role !== 'user' && !!msg.text;
          const isHighlighted = searchMode && searchQuery.trim() &&
            (msg.text.toLowerCase().includes(searchQuery.toLowerCase()) ||
             (msg.subtitle || '').toLowerCase().includes(searchQuery.toLowerCase()));
          const speakerName = msg.senderName || character?.name || (isGroup ? '?' : (chatId === 'gojo' ? '五条悟' : chatId));
          const speakerInitial = speakerName?.[0] || '?';
          // ★ 头像图：群聊按 senderId 从成员里找，单聊用角色自己的
          const avatarUri = isGroup
            ? (group?.members.find(x => x.id === msg.senderId)?.avatar_url || null)
            : (character?.avatar_url || null);
          // ★ 长按头像/名字 → @这个人（仅群聊）
          const mentionThis = () => {
            if (isGroup && msg.senderName) {
              setInputText(prev => prev + '@' + msg.senderName + ' ');
            }
          };

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
                <TouchableOpacity onLongPress={mentionThis} delayLongPress={300} activeOpacity={0.7}>
                  <View style={[s.msgAvatar, { overflow: 'hidden' }]}>
                    {avatarUri ? (
                      <Image source={{ uri: avatarUri }} style={{ width: '100%', height: '100%' }} resizeMode="cover" />
                    ) : (
                      <Text style={s.msgAvatarText}>{speakerInitial}</Text>
                    )}
                  </View>
                </TouchableOpacity>
              )}
              <View style={[s.msgMain, msg.role === 'user' && { alignItems: 'flex-end' }]}>
                {msg.role === 'gojo' && (
                  <TouchableOpacity onLongPress={mentionThis} delayLongPress={300} activeOpacity={0.7}>
                    <Text style={s.msgSender}>{speakerName}</Text>
                  </TouchableOpacity>
                )}
                <TouchableOpacity
                  activeOpacity={0.85}
                  onPress={() => { if (msg.role === 'gojo' && hasAudio) replayAudio(msg.id); }}
                  onLongPress={() => onBubbleLongPress(msg)}
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
                      <Text style={s.replayHint}>
                        {resynthing === msg.id
                          ? '🔄 生成语音中…'
                          : (audioCacheRef.current[msg.id] ? '🔊 点击重播' : '🔊 点击播放')}
                      </Text>
                    )}
                  </View>
                </TouchableOpacity>
                <View style={s.msgBottom}>
                  <Text style={s.msgTime}>{msg.time}</Text>
                  {/* ★ 已读标识:只在 user 消息且拿到 readAt 后显示 —— 微信语义,对方看到了就亮 */}
                  {msg.role === 'user' && msg.readAt && (
                    <Text style={s.msgReadMark}>已读</Text>
                  )}
                </View>
              </View>
            </View>
            </React.Fragment>
          );
        })}

        {(loading || thinkingName) && (
          <View style={s.msgRow}>
            <View style={s.msgAvatar}><Text style={s.msgAvatarText}>…</Text></View>
            <View style={[s.bubble, s.bubbleGojo, { flexDirection: 'row', alignItems: 'center', gap: 8 }]}>
              <ActivityIndicator size="small" color={C.accent} />
              <Text style={{ color: C.textMute, fontSize: 13 }}>
                {thinkingName || (isGroup ? '群里在思考...' : '思考中...')}
              </Text>
            </View>
          </View>
        )}
      </ScrollView>

      {pendingImage && (
        <View style={s.pendingBar}>
          <Image source={{ uri: pendingImage.uri }} style={s.pendingThumb} resizeMode="cover" />
          {pendingImage.isVideo && (
            <View style={s.videoTag}>
              <Text style={s.videoTagText}>🎬 {pendingImage.frames?.length || 0} 帧</Text>
            </View>
          )}
          <Text style={s.pendingHint}>
            图片已选好，配点文字一起发吧
          </Text>
          <TouchableOpacity onPress={() => setPendingImage(null)} style={s.pendingRemove} disabled={loading}>
            <Text style={s.pendingRemoveText}>✕</Text>
          </TouchableOpacity>
        </View>
      )}

      {/* ★ @成员选择面板：输入框打出"@"时弹出 */}
      {isGroup && showMention && group && (
        <View style={s.mentionBar}>
          <Text style={s.mentionTitle}>@ 谁？</Text>
          {group.members.map(m => (
            <TouchableOpacity
              key={m.id}
              style={s.mentionItem}
              onPress={() => {
                setInputText(prev =>
                  prev.endsWith('@') ? prev + m.name + ' ' : prev + '@' + m.name + ' ');
                setShowMention(false);
              }}
            >
              <View style={s.mentionAvatar}>
                {m.avatar_url ? (
                  <Image source={{ uri: m.avatar_url }} style={{ width: '100%', height: '100%' }} resizeMode="cover" />
                ) : (
                  <Text style={s.mentionAvatarText}>{m.name?.[0] || '?'}</Text>
                )}
              </View>
              <Text style={s.mentionName}>{m.name}</Text>
            </TouchableOpacity>
          ))}
          <TouchableOpacity style={s.mentionItem} onPress={() => setShowMention(false)}>
            <Text style={[s.mentionName, { color: C.textMute }]}>取消</Text>
          </TouchableOpacity>
        </View>
      )}

      <View style={[s.inputBar, { marginBottom: keyboardHeight }]}>
        <TouchableOpacity style={s.attachBtn} onPress={showImagePicker} disabled={loading}>
          <Text style={s.attachBtnText}>📎</Text>
        </TouchableOpacity>

        <TextInput
          style={s.input}
          value={inputText}
          onChangeText={(t) => {
            setInputText(t);
            if (isGroup) {
              if (t.endsWith('@')) setShowMention(true);
              else if (showMention) setShowMention(false);
            }
          }}
          placeholder={
            loading
              ? (isGroup ? '群里回复中...' : '回复中...')
              : (pendingImage
                  ? (pendingImage.isVideo ? '给视频配句话，或直接发送…' : '给图片配句话，或直接发送…')
                  : (isGroup ? '在群里说点什么...' : '说点什么...'))
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
  loadMoreBtn: {
    alignSelf: 'center', paddingHorizontal: 16, paddingVertical: 8,
    borderRadius: 14, borderWidth: 1, borderColor: C.border,
    backgroundColor: 'rgba(255,255,255,0.04)', marginBottom: 10, minWidth: 130,
    alignItems: 'center',
  },
  loadMoreText: { color: C.textMute, fontSize: 12 },
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
  msgReadMark:     { color: C.textMute, fontSize: 10, marginLeft: 6, opacity: 0.7 },

  timeSepWrap:     { alignSelf: 'center', marginVertical: 12, paddingHorizontal: 14, paddingVertical: 4, borderRadius: 10, backgroundColor: C.card },
  timeSepText:     { color: C.textMute, fontSize: 11 },

  bubbleImage:     { width: width * 0.55, height: width * 0.42, borderRadius: 10, marginBottom: 6, backgroundColor: C.bg },

  pendingBar:      { flexDirection: 'row', alignItems: 'center', backgroundColor: C.card, paddingHorizontal: 12, paddingTop: 10, paddingBottom: 4, gap: 10, borderTopWidth: 1, borderTopColor: C.border },
  pendingThumb:    { width: 48, height: 48, borderRadius: 8, backgroundColor: C.bg, borderWidth: 1, borderColor: C.border },
  videoTag: {
    position: 'absolute', left: 4, bottom: 4,
    backgroundColor: 'rgba(0,0,0,0.65)', borderRadius: 6,
    paddingHorizontal: 5, paddingVertical: 1,
  },
  videoTagText: { color: '#fff', fontSize: 9, fontWeight: '600' },
  pendingHint:     { flex: 1, color: C.textMute, fontSize: 12, lineHeight: 16 },
  pendingRemove:   { width: 26, height: 26, borderRadius: 13, backgroundColor: C.bg, alignItems: 'center', justifyContent: 'center', borderWidth: 1, borderColor: C.border },
  pendingRemoveText:{ color: C.textMute, fontSize: 14, fontWeight: '600' },

  attachBtn:       { padding: 8, marginRight: 2 },
  attachBtnText:   { fontSize: 22 },

  // ★ @成员面板
  mentionBar:        { backgroundColor: C.card, borderTopWidth: 1, borderTopColor: C.border, paddingVertical: 6, paddingHorizontal: 12 },
  mentionTitle:      { color: C.textMute, fontSize: 11, marginBottom: 4 },
  mentionItem:       { flexDirection: 'row', alignItems: 'center', paddingVertical: 8, gap: 10 },
  mentionAvatar:     { width: 28, height: 28, borderRadius: 14, backgroundColor: C.accentDim, alignItems: 'center', justifyContent: 'center', overflow: 'hidden' },
  mentionAvatarText: { color: '#fff', fontSize: 12, fontWeight: '700' },
  mentionName:       { color: C.text, fontSize: 14 },

  inputBar:        { flexDirection: 'row', alignItems: 'flex-end', backgroundColor: C.card, paddingHorizontal: 12, paddingVertical: 10, borderTopWidth: 1, borderTopColor: C.border, gap: 8 },
  input:           { flex: 1, backgroundColor: C.bg, borderRadius: 20, paddingHorizontal: 16, paddingVertical: 10, color: C.text, fontSize: 14, maxHeight: 100, borderWidth: 1, borderColor: C.border },
  sendBtn:         { borderRadius: 20, paddingHorizontal: 18, paddingVertical: 10, minWidth: 60, alignItems: 'center' },
  sendBtnText:     { color: '#fff', fontWeight: '600', fontSize: 14 },
});