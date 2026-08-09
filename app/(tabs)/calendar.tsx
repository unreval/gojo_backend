// app/(tabs)/calendar.tsx
// ★ 完整版多功能日程（基于原版重写，原有逻辑全部保留）：
//   保留：每日打卡(结束日期自动停) / DDL 倒数梯度提醒 / 原生时间转盘 / 聊天取消提醒联动 / 前端去重
//   新增：
//     1. 列表 ⇄ 月历 双视图（月历有任务圆点，点日期看当天任务）
//     2. 今日进度卡（完成度进度条 + 最近 DDL 倒计时）
//     3. 列表按时间智能分组：逾期 / 每日打卡 / 今天 / 明天 / 7天内 / 以后 / 无日期
//     4. 任务卡 DDL 倒计时徽章（D-N，越近越红）
//     5. 已完成区可折叠
//     6. DDL 提醒梯度升级：≥14天 → 14/7/3/1/当天 五连提醒
//     7. 备注本机持久化（按任务 id 存 AsyncStorage）

import AsyncStorage from '@react-native-async-storage/async-storage';
import DateTimePicker from '@react-native-community/datetimepicker';
import axios from 'axios';
import * as Notifications from 'expo-notifications';
import { useFocusEffect } from 'expo-router';
import React, { useCallback, useEffect, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  Dimensions,
  KeyboardAvoidingView,
  Modal,
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
import ChibiSprite from '../../components/ChibiSprite';
import { C, SERVER_URL } from '../../constants/theme';

const { width, height } = Dimensions.get('window');

// ★ 月历格子宽度:必须用 Math.floor 强制取整,不然 (width-56)/7 是小数,
//   RN 底层向上取整时 7 个加起来会超容器宽度,最后一格被 flexWrap 挤到下一行,
//   出现"7 天变 6 列"的锅。宁可右边留一点点空隙。
const CELL_W_BIG = Math.floor((width - 56) / 7);   // 月历视图(月历 tab)
const CELL_W_SM  = Math.floor((width - 24) / 7);   // 日期选择器 modal 里的小月历
const USER_ID_KEY = 'gojo_user_id';
// ★ 兜底:新装的机器 AsyncStorage 是空的,没有这个兜底 userId 会一直是空字符串,
//   导致所有带 `if (!userId) return` 的操作(记录生理期等)静默失效
const FIXED_USER_ID = 'user_mofpiyd7442ia7';

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowAlert: true,
    shouldPlaySound: true,
    shouldSetBadge: false,
  }),
});

interface Task {
  id: number;
  title: string;
  category: string;
  due_date: string | null;
  due_time: string | null;
  reminder_minutes: number | null;
  completed: boolean;
  notification_id: string | null;
  repeat_type?: string;
  last_completed_date?: string | null;
}

const CATEGORY_LIST = ['个人', '工作', '心愿单', '纪念日'];
const CATEGORY_COLORS: Record<string, string> = {
  '工作':   '#3b82f6',
  '个人':   '#0e7490',
  '心愿单': '#d97706',
  '纪念日': '#e879a0',
};
const FILTER_TABS = ['所有', '工作', '个人', '心愿单', '纪念日'];
const MONTHS = ['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月'];
const WEEKDAYS = ['日','一','二','三','四','五','六'];
const REMINDER_OPTIONS = [
  { label: '准时', val: 0 },
  { label: '5分钟前', val: 5 },
  { label: '15分钟前', val: 15 },
  { label: '30分钟前', val: 30 },
  { label: '1小时前', val: 60 },
];
const QUICK_TIMES = ['07:00','09:00','12:00','14:00','18:00','21:00','22:00'];

function formatDate(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
}
function addMonthsStr(n: number): string {
  const d = new Date();
  d.setMonth(d.getMonth() + n);
  return formatDate(d);
}
function friendlyDate(s: string | null): string {
  if (!s) return '无日期';
  const today = formatDate(new Date());
  const tom   = formatDate(new Date(Date.now() + 86400000));
  if (s === today) return '今天';
  if (s === tom)   return '明天';
  return s.slice(5).replace('-', '/');
}
function getNextSunday(): string {
  const d = new Date();
  const gap = d.getDay() === 0 ? 7 : 7 - d.getDay();
  return formatDate(new Date(d.getTime() + 86400000 * gap));
}
function daysUntil(s: string | null): number | null {
  if (!s) return null;
  const t = new Date(s); t.setHours(0,0,0,0);
  const n = new Date();  n.setHours(0,0,0,0);
  return Math.round((t.getTime() - n.getTime()) / 86400000);
}
function getMonthDays(y: number, m: number) {
  const count = new Date(y, m+1, 0).getDate();
  return Array.from({length: count}, (_, i) => ({
    day: i+1,
    date: `${y}-${String(m+1).padStart(2,'0')}-${String(i+1).padStart(2,'0')}`,
  }));
}
// ★ DDL 倒数提醒梯度（升级：超远期任务 14 天前也提醒）
function getDdlOffsets(daysAway: number): number[] {
  if (daysAway >= 14) return [14, 7, 3, 1, 0];
  if (daysAway >= 7)  return [7, 3, 1, 0];
  if (daysAway >= 3)  return [3, 1, 0];
  if (daysAway >= 1)  return [1, 0];
  return [0];
}
function ladderLabel(daysAway: number): string {
  if (daysAway >= 14) return '14/7/3/1天前 + 当天';
  if (daysAway >= 7)  return '7/3/1天前 + 当天';
  if (daysAway >= 3)  return '3/1天前 + 当天';
  return '1天前 + 当天';
}

export default function CalendarScreen() {
  const [tasks, setTasks]         = useState<Task[]>([]);
  const [loading, setLoading]     = useState(true);
  const [userId, setUserId]       = useState('');
  const [activeTab, setActiveTab] = useState('所有');
  const [viewMode, setViewMode]   = useState<'list'|'month'>('list');   // ★ 双视图
  const [showCompleted, setShowCompleted] = useState(false);            // ★ 已完成折叠
  const [selDate, setSelDate]     = useState<string>(formatDate(new Date())); // ★ 月历选中日
  const [viewYear, setViewYear]   = useState(new Date().getFullYear());
  const [viewMonth, setViewMonth] = useState(new Date().getMonth());

  // 新建 sheet
  const [showAddSheet, setShowAddSheet]   = useState(false);
  const [newTitle, setNewTitle]           = useState('');
  const [newCategory, setNewCategory]     = useState('个人');
  const [newDueDate, setNewDueDate]       = useState<string | null>(null);
  const [newDueTime, setNewDueTime]       = useState<string | null>(null);
  const [newReminder, setNewReminder]     = useState<number | null>(null);
  const [newRepeat, setNewRepeat]         = useState<string>('none');
  const [showCatPicker, setShowCatPicker] = useState(false);

  // 日期弹窗
  const [showDateModal, setShowDateModal]   = useState(false);
  const [dateCtx, setDateCtx]               = useState<'add'|'edit'>('add');
  const [calYear, setCalYear]               = useState(new Date().getFullYear());
  const [calMonth, setCalMonth]             = useState(new Date().getMonth());
  const [tempDate, setTempDate]             = useState<string | null>(null);
  const [tempTime, setTempTime]             = useState<string | null>(null);
  const [tempReminder, setTempReminder]     = useState<number | null>(null);
  const [tempOffsets, setTempOffsets]       = useState<number[] | null>(null);   // ★ DDL 提前几天，null=自动梯度
  const [newOffsets, setNewOffsets]         = useState<number[] | null>(null);
  const [editOffsets, setEditOffsets]       = useState<number[] | null>(null);
  const [showTimePicker, setShowTimePicker] = useState(false);
  const [showNativeTime, setShowNativeTime] = useState(false);

  // 编辑 Modal
  const [editTask, setEditTask]           = useState<Task | null>(null);
  const [showEditModal, setShowEditModal] = useState(false);
  const [editTitle, setEditTitle]         = useState('');
  const [editCategory, setEditCategory]   = useState('个人');
  const [editDueDate, setEditDueDate]     = useState<string | null>(null);
  const [editDueTime, setEditDueTime]     = useState<string | null>(null);
  const [editReminder, setEditReminder]   = useState<number | null>(null);
  const [editRepeat, setEditRepeat]       = useState<string>('none');
  const [editNote, setEditNote]           = useState('');
  const [showEditCat, setShowEditCat]     = useState(false);

  const todayStr = formatDate(new Date());

  // ★ 生理期
  const [periodStatus, setPeriodStatus] = useState<any>(null);
  const [periodRecords, setPeriodRecords] = useState<any[]>([]);
  const [showPeriod, setShowPeriod] = useState(false);
  const [showPStartPicker, setShowPStartPicker] = useState(false);
  const [showPEndPicker, setShowPEndPicker] = useState(false);

  const loadPeriod = async (uid: string) => {
    try {
      const [st, rc] = await Promise.all([
        axios.get(`${SERVER_URL}/period/status?user_id=${uid}`),
        axios.get(`${SERVER_URL}/period/records?user_id=${uid}`),
      ]);
      setPeriodStatus(st.data);
      setPeriodRecords(rc.data?.records || []);
    } catch { setPeriodStatus(null); }
  };

  const recordPeriod = async (startDate: string, endDate?: string | null) => {
    // ★ 不再静默 return —— userId 万一还没就绪就用兜底值,
    //   之前这里直接 return 导致"点了完全没反应",连错误提示都没有
    const uid = userId || FIXED_USER_ID;
    try {
      await axios.post(`${SERVER_URL}/period/record`, {
        user_id: uid, start_date: startDate, end_date: endDate || '',
      });
      await loadPeriod(uid);
    } catch (e: any) {
      Alert.alert('记录失败', e?.response?.data?.error ?? e?.message ?? '检查后端是否已更新');
    }
  };

  const deletePeriodRecord = (rid: number) => {
    Alert.alert('删除这条记录？', '', [
      { text: '取消', style: 'cancel' },
      { text: '删除', style: 'destructive', onPress: async () => {
        try {
          await axios.delete(`${SERVER_URL}/period/record/${rid}`);
          await loadPeriod(userId || FIXED_USER_ID);
        } catch {}
      }},
    ]);
  };

  const isDailyEnded = (t: Task): boolean =>
    t.repeat_type === 'daily' && !!t.due_date && t.due_date < todayStr;

  const isTaskCompleted = (t: Task): boolean => {
    if (t.repeat_type === 'daily') return t.last_completed_date === todayStr;
    return t.completed;
  };

  const isDailyContext = dateCtx === 'add' ? newRepeat === 'daily' : editRepeat === 'daily';

  useEffect(() => {
    (async () => {
      if (Platform.OS === 'android') {
        await Notifications.setNotificationChannelAsync('gojo-reminders', {
          name: '五条悟提醒',
          importance: Notifications.AndroidImportance.HIGH,
          sound: 'default',
          vibrationPattern: [0, 250, 250, 250],
        });
      }
      let uid = await AsyncStorage.getItem(USER_ID_KEY);
      if (!uid) {
        // 首次安装/换机:AsyncStorage 为空,用固定 id 并写回去
        uid = FIXED_USER_ID;
        try { await AsyncStorage.setItem(USER_ID_KEY, uid); } catch {}
      }
      setUserId(uid);
      await loadTasks(uid);
      loadPeriod(uid);
      setLoading(false);
    })();
  }, []);

  useFocusEffect(
    useCallback(() => {
      if (userId) loadTasks(userId);
    }, [userId])
  );

  const loadTasks = async (uid: string) => {
    try {
      const res = await axios.get(`${SERVER_URL}/tasks?user_id=${uid}`);
      if (res.data?.tasks) {
        const list: Task[] = res.data.tasks;
        setTasks(list);
        reconcileExpiredDailies(list);
      }
    } catch {}
  };

  // ── 取消一个任务下的全部通知（支持逗号分隔多 ID）──
  const cancelNotifs = async (idStr: string | null | undefined) => {
    if (!idStr) return;
    const ids = idStr.split(',').map(x => x.trim()).filter(Boolean);
    for (const id of ids) {
      try { await Notifications.cancelScheduledNotificationAsync(id); } catch {}
    }
  };

  // ── 每日打卡到了结束日期：停通知 ──
  const reconcileExpiredDailies = async (list: Task[]) => {
    for (const t of list) {
      if (isDailyEnded(t) && t.notification_id) {
        await cancelNotifs(t.notification_id);
        try { await axios.put(`${SERVER_URL}/tasks/${t.id}`, { notification_id: null }); } catch {}
      }
    }
  };

  // ── 调度任务通知（每日 DAILY / 一次性 DDL 梯度）──
  const scheduleTaskNotifications = async (
    taskId: number,
    opts: { date: string | null; time: string; reminder: number | null; repeat: string; title: string; customOffsets?: number[] | null },
  ) => {
    try {
      const { status } = await Notifications.getPermissionsAsync();
      if (status !== 'granted') {
        const ns = await Notifications.requestPermissionsAsync();
        if (ns.status !== 'granted') return;
      }
      const { date, time, reminder, repeat, title, customOffsets } = opts;
      const [h, m] = time.split(':').map(Number);
      const ids: string[] = [];

      if (repeat === 'daily') {
        const id = await Notifications.scheduleNotificationAsync({
          content: {
            title: '打卡时间到',
            body: title,
            sound: 'default',
            ...(Platform.OS === 'android' ? { channelId: 'gojo-reminders' } : {}),
          },
          trigger: { type: Notifications.SchedulableTriggerInputTypes.DAILY, hour: h, minute: m } as any,
        });
        ids.push(id);
      } else {
        if (!date) return;
        const [y, mo, d] = date.split('-').map(Number);
        const due = new Date(y, mo - 1, d, h, m, 0);
        const daysAway = Math.ceil((due.getTime() - Date.now()) / 86400000);
        // ★ 用户自定义了提前天数就用用户的（当天必含），否则自动梯度
        const offsets = (customOffsets && customOffsets.length > 0)
          ? Array.from(new Set([...customOffsets.filter(o => o >= 1 && o <= daysAway), 0])).sort((a, b) => b - a)
          : getDdlOffsets(daysAway);

        for (const off of offsets) {
          let when: Date;
          let body: string;
          if (off === 0) {
            when = new Date(due.getTime() - (reminder || 0) * 60000);
            body = title;
          } else {
            when = new Date(due.getTime() - off * 86400000);
            body = `还有${off}天 · ${title}`;
          }
          if (when.getTime() <= Date.now()) continue;
          const id = await Notifications.scheduleNotificationAsync({
            content: {
              title: '别忘了这件事',
              body,
              sound: 'default',
              ...(Platform.OS === 'android' ? { channelId: 'gojo-reminders' } : {}),
            },
            trigger: { type: Notifications.SchedulableTriggerInputTypes.DATE, date: when } as any,
          });
          ids.push(id);
        }
      }

      if (ids.length > 0) {
        await axios.put(`${SERVER_URL}/tasks/${taskId}`, { notification_id: ids.join(',') });
      }
    } catch {}
  };

  // ── 新建 ──
  const openAddSheet = () => {
    setNewTitle(''); setNewCategory('个人');
    setNewDueDate(null); setNewDueTime(null); setNewReminder(null);
    setNewRepeat('none');
    setNewOffsets(null);
    setShowCatPicker(false);
    setShowAddSheet(true);
  };

  const submitAdd = async () => {
    const title = newTitle.trim();
    if (!title || !userId) return;

    const dup = tasks.find(t =>
      !t.completed &&
      t.title === title &&
      (t.due_date || null) === (newDueDate || null) &&
      (t.due_time || null) === (newDueTime || null)
    );
    if (dup) {
      Alert.alert('已存在相同任务', `「${title}」已经在列表里了，无需重复添加。`);
      setShowAddSheet(false);
      return;
    }

    setShowAddSheet(false);
    try {
      const res = await axios.post(`${SERVER_URL}/tasks`, {
        user_id: userId, title,
        category: newCategory,
        due_date: newDueDate,
        due_time: newDueTime,
        reminder_minutes: newReminder,
        repeat_type: newRepeat,
      });
      const taskId: number = res.data?.id;

      // ★ DDL 自定义提前天数存本机（通知在本机调度，存本机即可）
      if (taskId && newOffsets && newOffsets.length > 0) {
        await AsyncStorage.setItem(`task_ddl_${taskId}`, JSON.stringify(newOffsets)).catch(() => {});
      }

      if (newDueTime && taskId) {
        if (newRepeat === 'daily') {
          await scheduleTaskNotifications(taskId, { date: null, time: newDueTime, reminder: null, repeat: 'daily', title });
        } else if (newDueDate && newReminder !== null) {
          await scheduleTaskNotifications(taskId, { date: newDueDate, time: newDueTime, reminder: newReminder, repeat: 'none', title, customOffsets: newOffsets });
        }
      }
      await loadTasks(userId);
    } catch { Alert.alert('添加失败'); }
  };

  // ── 日期弹窗 ──
  const openDateModal = (ctx: 'add' | 'edit') => {
    setDateCtx(ctx);
    if (ctx === 'add') {
      setTempDate(newDueDate); setTempTime(newDueTime); setTempReminder(newReminder);
      setTempOffsets(newOffsets);
    } else {
      setTempDate(editDueDate); setTempTime(editDueTime); setTempReminder(editReminder);
      setTempOffsets(editOffsets);
    }
    setCalYear(new Date().getFullYear());
    setCalMonth(new Date().getMonth());
    setShowTimePicker(false);
    setShowNativeTime(false);
    setShowDateModal(true);
  };

  const confirmDate = () => {
    if (dateCtx === 'add') {
      setNewDueDate(tempDate); setNewDueTime(tempTime); setNewReminder(tempReminder);
      setNewOffsets(tempOffsets);
    } else {
      setEditDueDate(tempDate); setEditDueTime(tempTime); setEditReminder(tempReminder);
      setEditOffsets(tempOffsets);
    }
    setShowDateModal(false);
  };

  const quickDates = [
    { label: '今天',       val: formatDate(new Date()) },
    { label: '明天',       val: formatDate(new Date(Date.now() + 86400000)) },
    { label: '3天后',      val: formatDate(new Date(Date.now() + 86400000*3)) },
    { label: '这个星期天', val: getNextSunday() },
    { label: '无日期',     val: null as string | null },
  ];
  const dailyEndQuick = [
    { label: '一直重复', val: null as string | null },
    { label: '1周后',    val: formatDate(new Date(Date.now() + 86400000*7)) },
    { label: '1个月后',  val: addMonthsStr(1) },
    { label: '3个月后',  val: addMonthsStr(3) },
  ];
  const quickOptions = isDailyContext ? dailyEndQuick : quickDates;

  // ── 编辑（★ 备注改为本机持久化）──
  const openEdit = async (task: Task) => {
    setEditTask(task);
    setEditTitle(task.title);
    setEditCategory(task.category);
    setEditDueDate(task.due_date);
    setEditDueTime(task.due_time);
    setEditReminder(task.reminder_minutes);
    setEditRepeat(task.repeat_type || 'none');
    setShowEditCat(false);
    try {
      const note = await AsyncStorage.getItem(`task_note_${task.id}`);
      setEditNote(note || '');
    } catch { setEditNote(''); }
    try {
      const off = await AsyncStorage.getItem(`task_ddl_${task.id}`);
      setEditOffsets(off ? JSON.parse(off) : null);
    } catch { setEditOffsets(null); }
    setShowEditModal(true);
  };

  const saveEdit = async () => {
    if (!editTask) return;
    const title = editTitle.trim();
    if (!title) return;
    setShowEditModal(false);
    try {
      await axios.put(`${SERVER_URL}/tasks/${editTask.id}`, {
        title, category: editCategory,
        due_date: editDueDate,
        due_time: editDueTime,
        reminder_minutes: editReminder,
        repeat_type: editRepeat,
      });
      // ★ 备注存本机
      try {
        if (editNote.trim()) await AsyncStorage.setItem(`task_note_${editTask.id}`, editNote.trim());
        else await AsyncStorage.removeItem(`task_note_${editTask.id}`);
      } catch {}
      // ★ DDL 自定义提前天数存本机
      try {
        if (editOffsets && editOffsets.length > 0) await AsyncStorage.setItem(`task_ddl_${editTask.id}`, JSON.stringify(editOffsets));
        else await AsyncStorage.removeItem(`task_ddl_${editTask.id}`);
      } catch {}
      await cancelNotifs(editTask.notification_id);
      if (editDueTime) {
        if (editRepeat === 'daily') {
          await scheduleTaskNotifications(editTask.id, { date: null, time: editDueTime, reminder: null, repeat: 'daily', title });
        } else if (editDueDate && editReminder !== null) {
          await scheduleTaskNotifications(editTask.id, { date: editDueDate, time: editDueTime, reminder: editReminder, repeat: 'none', title, customOffsets: editOffsets });
        }
      }
      await loadTasks(userId);
    } catch { Alert.alert('保存失败'); }
  };

  const deleteTask = (task: Task) => {
    Alert.alert('删除任务', `确认删除「${task.title}」？`, [
      { text: '取消', style: 'cancel' },
      { text: '删除', style: 'destructive', onPress: async () => {
        setShowEditModal(false);
        try {
          await cancelNotifs(task.notification_id);
          await axios.delete(`${SERVER_URL}/tasks/${task.id}`);
          await AsyncStorage.removeItem(`task_note_${task.id}`).catch(() => {});
          await AsyncStorage.removeItem(`task_ddl_${task.id}`).catch(() => {});
          setTasks(prev => prev.filter(t => t.id !== task.id));
        } catch { Alert.alert('删除失败'); }
      }},
    ]);
  };

  const toggleComplete = async (task: Task) => {
    try {
      if (task.repeat_type === 'daily') {
        const wasDone = task.last_completed_date === todayStr;
        const newVal = wasDone ? null : todayStr;
        await axios.put(`${SERVER_URL}/tasks/${task.id}`, { last_completed_date: newVal });
        setTasks(prev => prev.map(t => t.id === task.id ? { ...t, last_completed_date: newVal } : t));
      } else {
        await axios.put(`${SERVER_URL}/tasks/${task.id}`, { completed: !task.completed });
        setTasks(prev => prev.map(t => t.id === task.id ? { ...t, completed: !t.completed } : t));
      }
    } catch {}
  };

  // ── 过滤 & 分组 ──
  const filtered  = activeTab === '所有' ? tasks : tasks.filter(t => t.category === activeTab);
  const pending   = filtered.filter(t => !isTaskCompleted(t) && !isDailyEnded(t));
  const completed = filtered.filter(t => isTaskCompleted(t) || isDailyEnded(t));

  const dailies   = pending.filter(t => t.repeat_type === 'daily');
  const nonDaily  = pending.filter(t => t.repeat_type !== 'daily');
  const overdue   = nonDaily.filter(t => { const d = daysUntil(t.due_date); return d !== null && d < 0; });
  const dueToday  = nonDaily.filter(t => daysUntil(t.due_date) === 0);
  const dueTomorrow = nonDaily.filter(t => daysUntil(t.due_date) === 1);
  const dueWeek   = nonDaily.filter(t => { const d = daysUntil(t.due_date); return d !== null && d >= 2 && d <= 7; });
  const dueLater  = nonDaily.filter(t => { const d = daysUntil(t.due_date); return d !== null && d > 7; });
  const noDate    = nonDaily.filter(t => !t.due_date);

  // ★ 今日进度：今天到期的任务 + 今天的打卡
  const todayScope = [...filtered.filter(t => t.repeat_type === 'daily' && !isDailyEnded(t)),
                      ...nonDaily.filter(t => daysUntil(t.due_date) === 0),
                      ...filtered.filter(t => t.repeat_type !== 'daily' && t.completed && t.due_date === todayStr)];
  const todayUniq  = Array.from(new Map(todayScope.map(t => [t.id, t])).values());
  const todayDone  = todayUniq.filter(isTaskCompleted).length;
  const todayTotal = todayUniq.length;
  const progress   = todayTotal > 0 ? todayDone / todayTotal : 0;

  // ★ 最近的 DDL（未完成、未来最近的一个）
  const nextDdl = nonDaily
    .filter(t => { const d = daysUntil(t.due_date); return d !== null && d >= 0; })
    .sort((a, b) => (daysUntil(a.due_date)! - daysUntil(b.due_date)!))[0] || null;

  // ★ 月历视图：某天有哪些任务（打卡按有效期算）
  const tasksOnDate = (dateStr: string): Task[] => {
    return filtered.filter(t => {
      if (isTaskCompleted(t) && t.repeat_type !== 'daily') {
        return t.due_date === dateStr;   // 已完成的也在它的日期上显示（打勾态）
      }
      if (t.repeat_type === 'daily') {
        if (dateStr < todayStr && !t.last_completed_date) return false;
        return !t.due_date || t.due_date >= dateStr;
      }
      return t.due_date === dateStr;
    });
  };

  const addDaysAway = (!isDailyContext && tempDate) ? daysUntil(tempDate) : null;

  if (loading) return (
    <View style={{flex:1, backgroundColor:C.bg, alignItems:'center', justifyContent:'center'}}>
      <ActivityIndicator color={C.accent} />
    </View>
  );

  const selDayTasks = tasksOnDate(selDate);

  return (
    <View style={{flex:1, backgroundColor:C.bg}}>
      <StatusBar barStyle="light-content" backgroundColor={C.bg} />

      {/* ── 头部：标题 + 视图切换 ── */}
      <View style={s.header}>
        <View>
          <Text style={s.headerTitle}>日程</Text>
          <Text style={s.headerSub}>
            {new Date().getMonth()+1}月{new Date().getDate()}日 · 周{WEEKDAYS[new Date().getDay()]}
          </Text>
        </View>
        <View style={{flexDirection:'row', alignItems:'center', gap:10}}>
          <View style={s.viewToggle}>
            <TouchableOpacity
              style={[s.viewToggleBtn, viewMode==='list' && s.viewToggleActive]}
              onPress={() => setViewMode('list')}>
              <Text style={[s.viewToggleText, viewMode==='list' && {color:'#fff'}]}>列表</Text>
            </TouchableOpacity>
            <TouchableOpacity
              style={[s.viewToggleBtn, viewMode==='month' && s.viewToggleActive]}
              onPress={() => { setViewMode('month'); setSelDate(todayStr); setViewYear(new Date().getFullYear()); setViewMonth(new Date().getMonth()); }}>
              <Text style={[s.viewToggleText, viewMode==='month' && {color:'#fff'}]}>月历</Text>
            </TouchableOpacity>
          </View>
          <ChibiSprite pose="peek" size={48} />
        </View>
      </View>

      {/* ── 今日进度卡 ── */}
      <View style={s.statCard}>
        <View style={{flex:1}}>
          <Text style={s.statTitle}>
            今日 {todayDone}/{todayTotal} 完成
            {todayTotal > 0 && todayDone === todayTotal ? '  🎉 全部搞定' : ''}
          </Text>
          <View style={s.progressTrack}>
            <View style={[s.progressFill, { width: `${Math.round(progress*100)}%` }]} />
          </View>
          {nextDdl && (
            <Text style={s.statDdl} numberOfLines={1}>
              ⏳ 最近 DDL：{nextDdl.title} · {daysUntil(nextDdl.due_date) === 0 ? '就是今天' : `还有 ${daysUntil(nextDdl.due_date)} 天`}
            </Text>
          )}
        </View>
      </View>

      {/* ── 🌸 生理期卡 ── */}
      <TouchableOpacity style={s.periodCard} activeOpacity={0.8} onPress={() => setShowPeriod(true)}>
        <Text style={s.periodEmoji}>🌸</Text>
        <Text style={s.periodText} numberOfLines={1}>
          {periodStatus?.has_data
            ? `${periodStatus.phase} · 下次预计 ${String(periodStatus.next_predicted).slice(5).replace('-','/')}`
            : '生理期 · 还没有记录，点这里开始'}
        </Text>
        <Text style={s.periodArrow}>›</Text>
      </TouchableOpacity>

      {/* ── 分类筛选 ── */}
      <ScrollView horizontal showsHorizontalScrollIndicator={false}
        style={s.tabBar} contentContainerStyle={s.tabBarInner}>
        {FILTER_TABS.map(tab => {
          const col = CATEGORY_COLORS[tab] || C.accent;
          const active = activeTab === tab;
          return (
            <TouchableOpacity key={tab}
              style={[s.tab, active && { backgroundColor: col }]}
              onPress={() => setActiveTab(tab)}>
              <Text style={[s.tabText, active && { color: '#fff', fontWeight: '700' }]}>{tab}</Text>
            </TouchableOpacity>
          );
        })}
      </ScrollView>

      {viewMode === 'list' ? (
        /* ════ 列表视图 ════ */
        <ScrollView style={{flex:1}} contentContainerStyle={s.list}>
          {overdue.length > 0 && (
            <>
              <Text style={[s.sectionLabel, {color:'#f87171'}]}>⚠️ 已逾期 ({overdue.length})</Text>
              {overdue.map(task => <TaskRow key={task.id} task={task} onPress={openEdit} onCheck={toggleComplete} />)}
            </>
          )}
          {dailies.length > 0 && (
            <>
              <Text style={[s.sectionLabel, {color:'#d97706'}]}>🔁 每日打卡 ({dailies.filter(isTaskCompleted).length}/{dailies.length})</Text>
              {dailies.map(task => (
                <TaskRow key={task.id} task={task} onPress={openEdit} onCheck={toggleComplete}
                  done={isTaskCompleted(task)} />
              ))}
            </>
          )}
          {dueToday.length > 0 && (
            <>
              <Text style={[s.sectionLabel, {color:C.accent2||'#5BC4FF'}]}>📌 今天 ({dueToday.length})</Text>
              {dueToday.map(task => <TaskRow key={task.id} task={task} onPress={openEdit} onCheck={toggleComplete} />)}
            </>
          )}
          {dueTomorrow.length > 0 && (
            <>
              <Text style={s.sectionLabel}>明天</Text>
              {dueTomorrow.map(task => <TaskRow key={task.id} task={task} onPress={openEdit} onCheck={toggleComplete} />)}
            </>
          )}
          {dueWeek.length > 0 && (
            <>
              <Text style={s.sectionLabel}>7 天内</Text>
              {dueWeek.map(task => <TaskRow key={task.id} task={task} onPress={openEdit} onCheck={toggleComplete} />)}
            </>
          )}
          {dueLater.length > 0 && (
            <>
              <Text style={s.sectionLabel}>以后</Text>
              {dueLater.map(task => <TaskRow key={task.id} task={task} onPress={openEdit} onCheck={toggleComplete} />)}
            </>
          )}
          {noDate.length > 0 && (
            <>
              <Text style={s.sectionLabel}>随时 / 无日期</Text>
              {noDate.map(task => <TaskRow key={task.id} task={task} onPress={openEdit} onCheck={toggleComplete} />)}
            </>
          )}
          {pending.length === 0 && (
            <View style={s.emptyWrap}>
              <Text style={s.emptyText}>没有待办{'\n'}悟在等你来安排 ✦</Text>
            </View>
          )}
          {completed.length > 0 && (
            <>
              <TouchableOpacity onPress={() => setShowCompleted(v => !v)}>
                <Text style={s.sectionLabel}>
                  {showCompleted ? '▾' : '▸'} 已完成 / 已结束 ({completed.length})
                </Text>
              </TouchableOpacity>
              {showCompleted && completed.map(task => (
                <TaskRow key={task.id} task={task} onPress={openEdit} onCheck={toggleComplete} done />
              ))}
            </>
          )}
        </ScrollView>
      ) : (
        /* ════ 月历视图 ════ */
        <ScrollView style={{flex:1}} contentContainerStyle={{paddingBottom:100}}>
          <View style={s.monthCard}>
            <View style={s.calHeader}>
              <TouchableOpacity onPress={() => {
                if (viewMonth === 0) { setViewMonth(11); setViewYear(viewYear-1); }
                else setViewMonth(viewMonth-1);
              }}><Text style={s.calNav}>◀</Text></TouchableOpacity>
              <TouchableOpacity onPress={() => {
                setViewYear(new Date().getFullYear()); setViewMonth(new Date().getMonth()); setSelDate(todayStr);
              }}>
                <Text style={s.calHeaderTitle}>{MONTHS[viewMonth]} {viewYear}</Text>
              </TouchableOpacity>
              <TouchableOpacity onPress={() => {
                if (viewMonth === 11) { setViewMonth(0); setViewYear(viewYear+1); }
                else setViewMonth(viewMonth+1);
              }}><Text style={s.calNav}>▶</Text></TouchableOpacity>
            </View>
            <View style={s.weekRowM}>
              {WEEKDAYS.map(w => <Text key={w} style={s.weekLabelM}>{w}</Text>)}
            </View>
            <View style={s.calGrid}>
              {Array.from({length: new Date(viewYear, viewMonth, 1).getDay()}).map((_,i) =>
                <View key={`e${i}`} style={s.calCellBig} />
              )}
              {getMonthDays(viewYear, viewMonth).map(({day, date}) => {
                const dayTasks = tasksOnDate(date);
                const isSel = selDate === date;
                const isTd  = date === todayStr;
                const dots = dayTasks.slice(0, 3);
                return (
                  <TouchableOpacity key={date} style={s.calCellBig} onPress={() => setSelDate(date)}>
                    <View style={[
                      s.calDayWrapBig,
                      isSel && {backgroundColor: C.accent2 || '#5BC4FF'},
                      isTd && !isSel && {borderWidth:1.5, borderColor: C.accent2 || '#5BC4FF'},
                    ]}>
                      <Text style={[
                        s.calDayText,
                        isSel && {color:'#fff', fontWeight:'700'},
                        isTd && !isSel && {color: C.accent2 || '#5BC4FF'},
                      ]}>{day}</Text>
                    </View>
                    <View style={s.dotRow}>
                      {dots.map((t, i) => (
                        <View key={i} style={[s.taskDot, {backgroundColor: CATEGORY_COLORS[t.category] || C.accent}]} />
                      ))}
                      {dayTasks.length > 3 && <Text style={s.dotMore}>+</Text>}
                    </View>
                  </TouchableOpacity>
                );
              })}
            </View>
          </View>

          {/* 选中日的任务 */}
          <View style={{paddingHorizontal:20, gap:6}}>
            <Text style={s.sectionLabel}>
              {selDate === todayStr ? '今天' : selDate.slice(5).replace('-','/')} 的安排 ({selDayTasks.length})
            </Text>
            {selDayTasks.length === 0 && (
              <Text style={[s.emptyText, {marginTop:16}]}>这天没有安排</Text>
            )}
            {selDayTasks.map(task => (
              <TaskRow key={task.id} task={task} onPress={openEdit} onCheck={toggleComplete}
                done={isTaskCompleted(task)} />
            ))}
          </View>
        </ScrollView>
      )}

      <TouchableOpacity style={s.fab} onPress={openAddSheet} activeOpacity={0.85}>
        <Text style={s.fabText}>＋</Text>
      </TouchableOpacity>


      {/* ═══ 新建底部 Sheet ═══ */}
      <Modal visible={showAddSheet} transparent animationType="slide">
        <KeyboardAvoidingView style={s.sheetOverlay} behavior={Platform.OS === 'ios' ? 'padding' : 'height'}>
          <Pressable style={{flex:1}} onPress={() => setShowAddSheet(false)} />
          <View style={s.addSheet}>
            <TextInput
              style={s.addInput}
              value={newTitle}
              onChangeText={setNewTitle}
              placeholder={newRepeat === 'daily' ? '输入每日打卡任务' : '在这里输入新任务'}
              placeholderTextColor={C.textMute}
              autoFocus
              multiline
            />
            <View style={s.addHints}>
              {newRepeat === 'daily' && (
                <View style={[s.hintChip, {backgroundColor:'#d9770622'}]}>
                  <Text style={[s.hintChipText, {color:'#d97706'}]}>🔁 每日打卡</Text>
                </View>
              )}
              {newDueDate && newRepeat !== 'daily' && (
                <View style={s.hintChip}>
                  <Text style={s.hintChipText}>📅 {friendlyDate(newDueDate)}{newDueTime ? ` ${newDueTime}` : ''}</Text>
                </View>
              )}
              {newDueTime && newRepeat === 'daily' && (
                <View style={s.hintChip}>
                  <Text style={s.hintChipText}>
                    🕐 每天 {newDueTime}{newDueDate ? ` · 至${newDueDate.slice(5).replace('-','/')}` : ''}
                  </Text>
                </View>
              )}
              {newReminder !== null && newDueTime && newRepeat !== 'daily' && (
                <View style={s.hintChip}>
                  <Text style={s.hintChipText}>🔔 {newReminder === 0 ? '准时提醒' : `提前${newReminder}分钟`}</Text>
                </View>
              )}
            </View>
            <View style={s.addIconRow}>
              <TouchableOpacity style={s.catChip} onPress={() => setShowCatPicker(!showCatPicker)}>
                <View style={[s.catDot, {backgroundColor: CATEGORY_COLORS[newCategory] || C.accent}]} />
                <Text style={s.catChipText}>{newCategory} ▼</Text>
              </TouchableOpacity>
              <TouchableOpacity
                style={[s.repeatBtn, newRepeat === 'daily' && {backgroundColor:'#d97706', borderColor:'#d97706'}]}
                onPress={() => {
                  const next = newRepeat === 'daily' ? 'none' : 'daily';
                  setNewRepeat(next);
                  setNewDueDate(null);
                }}
              >
                <Text style={[s.repeatBtnText, newRepeat === 'daily' && {color:'#fff'}]}>🔁</Text>
              </TouchableOpacity>
              <View style={{flex:1}} />
              <TouchableOpacity style={s.iconBtn} onPress={() => openDateModal('add')}>
                <Text style={[
                  s.iconBtnText,
                  (newDueDate || (newRepeat === 'daily' && newDueTime)) ? {color: C.accent2 || '#5BC4FF'} : {},
                ]}>
                  {newRepeat === 'daily' ? '🕐' : '📅'}
                </Text>
              </TouchableOpacity>
              <TouchableOpacity
                style={[s.sendBtn, !newTitle.trim() && {opacity:0.35}]}
                onPress={submitAdd}
                disabled={!newTitle.trim()}
              >
                <Text style={s.sendBtnText}>▲</Text>
              </TouchableOpacity>
            </View>
            {showCatPicker && (
              <View style={s.catFloatMenu}>
                {CATEGORY_LIST.map(cat => (
                  <TouchableOpacity key={cat} style={s.catFloatItem}
                    onPress={() => { setNewCategory(cat); setShowCatPicker(false); }}>
                    <View style={[s.catDot, {backgroundColor: CATEGORY_COLORS[cat] || C.accent}]} />
                    <Text style={[s.catFloatText, newCategory===cat && {fontWeight:'700', color:C.text}]}>{cat}</Text>
                  </TouchableOpacity>
                ))}
              </View>
            )}
          </View>
        </KeyboardAvoidingView>
      </Modal>


      {/* ═══ 日期选择 Modal ═══ */}
      <Modal visible={showDateModal} transparent animationType="slide">
        <View style={{flex:1}}>
          <Pressable style={{flex:1, backgroundColor:'#00000055'}} onPress={() => setShowDateModal(false)} />
          <View style={s.dateSheet}>
            <ScrollView showsVerticalScrollIndicator={false}>
              {isDailyContext && (
                <View style={s.dailyHint}>
                  <Text style={s.dailyHintText}>🔁 每日打卡 — 设提醒时间，并可选「结束日期」（选「一直重复」则永久）</Text>
                </View>
              )}

              <View style={s.calHeader}>
                <TouchableOpacity onPress={() => {
                  if (calMonth === 0) { setCalMonth(11); setCalYear(calYear-1); }
                  else setCalMonth(calMonth-1);
                }}><Text style={s.calNav}>◀</Text></TouchableOpacity>
                <Text style={s.calHeaderTitle}>
                  {isDailyContext ? '结束日期 · ' : ''}{MONTHS[calMonth]} {calYear}
                </Text>
                <TouchableOpacity onPress={() => {
                  if (calMonth === 11) { setCalMonth(0); setCalYear(calYear+1); }
                  else setCalMonth(calMonth+1);
                }}><Text style={s.calNav}>▶</Text></TouchableOpacity>
              </View>
              <View style={s.weekRow}>
                {WEEKDAYS.map(w => <Text key={w} style={s.weekLabel}>{w}</Text>)}
              </View>
              <View style={s.calGrid}>
                {Array.from({length: new Date(calYear, calMonth, 1).getDay()}).map((_,i) =>
                  <View key={`e${i}`} style={s.calCell} />
                )}
                {getMonthDays(calYear, calMonth).map(({day, date}) => {
                  const isSel   = tempDate === date;
                  const isToday = date === formatDate(new Date());
                  return (
                    <TouchableOpacity key={date} style={s.calCell} onPress={() => setTempDate(date)}>
                      <View style={[
                        s.calDayWrap,
                        isSel && {backgroundColor: C.accent2 || '#5BC4FF'},
                        isToday && !isSel && {borderWidth:1.5, borderColor: C.accent2 || '#5BC4FF'},
                      ]}>
                        <Text style={[
                          s.calDayText,
                          isSel && {color:'#fff', fontWeight:'700'},
                          isToday && !isSel && {color: C.accent2 || '#5BC4FF'},
                        ]}>{day}</Text>
                      </View>
                    </TouchableOpacity>
                  );
                })}
              </View>
              <View style={s.quickRow}>
                {quickOptions.map(opt => {
                  const sel = opt.val === null ? tempDate === null : tempDate === opt.val;
                  return (
                    <TouchableOpacity key={opt.label}
                      style={[s.quickBtn, sel && {backgroundColor: C.accent2||'#5BC4FF', borderColor: C.accent2||'#5BC4FF'}]}
                      onPress={() => setTempDate(opt.val)}>
                      <Text style={[s.quickText, sel && {color:'#fff', fontWeight:'700'}]}>{opt.label}</Text>
                    </TouchableOpacity>
                  );
                })}
              </View>

              {!isDailyContext && addDaysAway !== null && addDaysAway >= 1 && (
                <View style={s.ddlNote}>
                  <Text style={s.ddlNoteText}>
                    📚 距今 {addDaysAway} 天 · 提前几天提醒（当天必提醒）：
                  </Text>
                  <View style={s.offsetRow}>
                    <TouchableOpacity
                      style={[s.offsetChip, tempOffsets === null && s.offsetChipOn]}
                      onPress={() => setTempOffsets(null)}>
                      <Text style={[s.offsetChipText, tempOffsets === null && s.offsetChipTextOn]}>
                        自动（{ladderLabel(addDaysAway)}）
                      </Text>
                    </TouchableOpacity>
                    {[1, 2, 3, 5, 7, 14].filter(o => o <= addDaysAway).map(o => {
                      const on = !!tempOffsets?.includes(o);
                      return (
                        <TouchableOpacity key={o}
                          style={[s.offsetChip, on && s.offsetChipOn]}
                          onPress={() => {
                            setTempOffsets(prev => {
                              const cur = prev ? [...prev] : [];
                              return cur.includes(o) ? cur.filter(x => x !== o) : [...cur, o];
                            });
                          }}>
                          <Text style={[s.offsetChipText, on && s.offsetChipTextOn]}>{o}天前</Text>
                        </TouchableOpacity>
                      );
                    })}
                  </View>
                  {tempOffsets !== null && tempOffsets.length > 0 && (
                    <Text style={s.ddlNoteText}>
                      已选：{[...tempOffsets].sort((a, b) => b - a).map(o => `${o}天前`).join('、')} + 当天
                    </Text>
                  )}
                </View>
              )}

              <View style={s.divider} />

              <TouchableOpacity style={s.dateRow} onPress={() => setShowTimePicker(!showTimePicker)}>
                <Text style={s.dateRowIcon}>🕐</Text>
                <Text style={s.dateRowLabel}>时间</Text>
                <Text style={s.dateRowValue}>{tempTime || '无'}</Text>
              </TouchableOpacity>
              {showTimePicker && (
                <View style={s.timeChipRow}>
                  {QUICK_TIMES.map(t => (
                    <TouchableOpacity key={t}
                      style={[s.timeChip, tempTime===t && {backgroundColor:(C.accent2||'#5BC4FF')+'33', borderColor:C.accent2||'#5BC4FF'}]}
                      onPress={() => { setTempTime(t); setShowTimePicker(false); }}>
                      <Text style={[s.timeChipText, tempTime===t && {color:C.accent2||'#5BC4FF'}]}>{t}</Text>
                    </TouchableOpacity>
                  ))}
                  <TouchableOpacity
                    style={[s.timeChip, {borderStyle:'dashed'}]}
                    onPress={() => setShowNativeTime(true)}>
                    <Text style={s.timeChipText}>自定义...</Text>
                  </TouchableOpacity>
                  <TouchableOpacity
                    style={[s.timeChip, !tempTime && {backgroundColor:(C.accent2||'#5BC4FF')+'33', borderColor:C.accent2||'#5BC4FF'}]}
                    onPress={() => { setTempTime(null); setShowTimePicker(false); }}>
                    <Text style={[s.timeChipText, !tempTime && {color:C.accent2||'#5BC4FF'}]}>无</Text>
                  </TouchableOpacity>
                </View>
              )}
              {showNativeTime && (
                <DateTimePicker
                  value={(() => {
                    const d = new Date();
                    if (tempTime) {
                      const [h, m] = tempTime.split(':').map(Number);
                      d.setHours(h, m, 0, 0);
                    }
                    return d;
                  })()}
                  mode="time"
                  is24Hour={true}
                  display="default"
                  onChange={(event: any, selectedDate?: Date) => {
                    setShowNativeTime(false);
                    if (event.type === 'set' && selectedDate) {
                      const h = String(selectedDate.getHours()).padStart(2, '0');
                      const m = String(selectedDate.getMinutes()).padStart(2, '0');
                      setTempTime(`${h}:${m}`);
                      setShowTimePicker(false);
                    }
                  }}
                />
              )}
              <View style={s.divider} />

              {!isDailyContext && (
                <View style={[s.dateRow, !tempTime && {opacity:0.3}]}>
                  <Text style={s.dateRowIcon}>🔔</Text>
                  <Text style={s.dateRowLabel}>提醒</Text>
                  <ScrollView horizontal showsHorizontalScrollIndicator={false}>
                    <View style={{flexDirection:'row', gap:8}}>
                      {REMINDER_OPTIONS.map(opt => (
                        <TouchableOpacity key={opt.val} disabled={!tempTime}
                          style={[s.remChip, tempReminder===opt.val && {backgroundColor:(C.accent2||'#5BC4FF')+'33', borderColor:C.accent2||'#5BC4FF'}]}
                          onPress={() => setTempReminder(opt.val)}>
                          <Text style={[s.remChipText, tempReminder===opt.val && {color:C.accent2||'#5BC4FF'}]}>{opt.label}</Text>
                        </TouchableOpacity>
                      ))}
                      <TouchableOpacity disabled={!tempTime}
                        style={[s.remChip, tempReminder===null && {backgroundColor:(C.accent2||'#5BC4FF')+'33', borderColor:C.accent2||'#5BC4FF'}]}
                        onPress={() => setTempReminder(null)}>
                        <Text style={[s.remChipText, tempReminder===null && {color:C.accent2||'#5BC4FF'}]}>不提醒</Text>
                      </TouchableOpacity>
                    </View>
                  </ScrollView>
                </View>
              )}
              <View style={{height:20}} />
            </ScrollView>
            <View style={s.dateFooter}>
              <TouchableOpacity style={s.dateFooterBtn} onPress={() => setShowDateModal(false)}>
                <Text style={s.dateFooterCancel}>取消</Text>
              </TouchableOpacity>
              <TouchableOpacity style={s.dateFooterBtn} onPress={confirmDate}>
                <Text style={s.dateFooterConfirm}>完成</Text>
              </TouchableOpacity>
            </View>
          </View>
        </View>
      </Modal>


      {/* ═══ 编辑全屏 Modal ═══ */}
      <Modal visible={showEditModal} transparent={false} animationType="slide">
        <View style={s.editFull}>
          <StatusBar barStyle="light-content" backgroundColor={C.card} />
          <View style={s.editHeader}>
            <TouchableOpacity style={s.editBack} onPress={() => setShowEditModal(false)}>
              <Text style={s.editBackText}>←  返回</Text>
            </TouchableOpacity>
            <View style={{flex:1}} />
            <TouchableOpacity style={s.editBack} onPress={() => editTask && deleteTask(editTask)}>
              <Text style={[s.editBackText, {color:'#f87171'}]}>删除</Text>
            </TouchableOpacity>
          </View>

          <ScrollView contentContainerStyle={s.editBody}>
            <TouchableOpacity style={s.editCatRow} onPress={() => setShowEditCat(!showEditCat)}>
              <View style={[s.catDot, {backgroundColor: CATEGORY_COLORS[editCategory]||C.accent}]} />
              <Text style={s.editCatText}>{editCategory} ▼</Text>
            </TouchableOpacity>
            {showEditCat && (
              <View style={s.editCatMenu}>
                {CATEGORY_LIST.map(cat => (
                  <TouchableOpacity key={cat} style={s.editCatItem}
                    onPress={() => { setEditCategory(cat); setShowEditCat(false); }}>
                    <View style={[s.catDot, {backgroundColor: CATEGORY_COLORS[cat]||C.accent}]} />
                    <Text style={[s.editCatItemText, editCategory===cat && {fontWeight:'700', color:C.text}]}>{cat}</Text>
                  </TouchableOpacity>
                ))}
              </View>
            )}

            <TextInput
              style={s.editTitleInput}
              value={editTitle}
              onChangeText={setEditTitle}
              multiline
              placeholder="任务标题"
              placeholderTextColor={C.textMute}
            />

            <View style={s.divider} />

            <View style={s.editRow}>
              <Text style={s.editRowIcon}>🔁</Text>
              <Text style={s.editRowLabel}>重复</Text>
              <View style={{flexDirection:'row', gap:8}}>
                <TouchableOpacity
                  style={[s.repeatChip, editRepeat === 'none' && {backgroundColor:C.accent+'33', borderColor:C.accent}]}
                  onPress={() => { setEditRepeat('none'); setEditDueDate(null); }}
                >
                  <Text style={[s.repeatChipText, editRepeat === 'none' && {color:C.accent}]}>不重复</Text>
                </TouchableOpacity>
                <TouchableOpacity
                  style={[s.repeatChip, editRepeat === 'daily' && {backgroundColor:'#d9770633', borderColor:'#d97706'}]}
                  onPress={() => { setEditRepeat('daily'); setEditDueDate(null); }}
                >
                  <Text style={[s.repeatChipText, editRepeat === 'daily' && {color:'#d97706'}]}>每日打卡</Text>
                </TouchableOpacity>
              </View>
            </View>
            <View style={s.divider} />

            <TouchableOpacity style={s.editRow} onPress={() => openDateModal('edit')}>
              <Text style={s.editRowIcon}>📅</Text>
              <Text style={s.editRowLabel}>{editRepeat === 'daily' ? '结束日期' : '截止日期'}</Text>
              <Text style={s.editRowValue}>
                {editRepeat === 'daily'
                  ? (editDueDate ? `至 ${editDueDate.replace(/-/g,'/')}` : '一直重复')
                  : (editDueDate ? editDueDate.replace(/-/g, '/') : '无')}
              </Text>
            </TouchableOpacity>
            <View style={s.divider} />

            <TouchableOpacity style={s.editRow} onPress={() => openDateModal('edit')}>
              <Text style={s.editRowIcon}>🕐</Text>
              <Text style={s.editRowLabel}>时间和提醒</Text>
              <Text style={s.editRowValue}>
                {editDueTime
                  ? `${editDueTime}${editReminder !== null && editRepeat !== 'daily' ? `  ·  ${editReminder===0?'准时':`提前${editReminder}分`}` : ''}`
                  : '无'}
              </Text>
            </TouchableOpacity>
            <View style={s.divider} />

            <View style={s.editRow}>
              <Text style={s.editRowIcon}>📝</Text>
              <Text style={s.editRowLabel}>备注（存在本机）</Text>
            </View>
            <TextInput
              style={s.editNoteInput}
              value={editNote}
              onChangeText={setEditNote}
              placeholder="添加备注..."
              placeholderTextColor={C.textMute}
              multiline
            />
            <View style={s.divider} />
          </ScrollView>

          <TouchableOpacity style={s.editSaveBtn} onPress={saveEdit}>
            <Text style={s.editSaveText}>保存</Text>
          </TouchableOpacity>
        </View>
      </Modal>

      {/* ═══ 🌸 生理期弹窗 ═══ */}
      <Modal visible={showPeriod} transparent animationType="slide">
        <View style={{flex:1}}>
          <Pressable style={{flex:1, backgroundColor:'#00000055'}} onPress={() => setShowPeriod(false)} />
          <View style={s.dateSheet}>
            <ScrollView showsVerticalScrollIndicator={false} contentContainerStyle={{paddingBottom:16}}>
              <Text style={s.periodTitle}>🌸 生理期</Text>

              {periodStatus?.has_data ? (
                <View style={s.periodStatusBox}>
                  <Text style={s.periodStatusMain}>{periodStatus.phase}</Text>
                  <Text style={s.periodStatusSub}>
                    下次预计 {periodStatus.next_predicted} · 还有 {periodStatus.days_until} 天{'\n'}
                    平均周期 {periodStatus.avg_cycle} 天 · 平均经期 {periodStatus.avg_length} 天 · 已记录 {periodStatus.records_count} 次
                  </Text>
                </View>
              ) : (
                <View style={s.periodStatusBox}>
                  <Text style={s.periodStatusSub}>
                    还没有记录。先记一次开始日期，记满两个周期后预测就准了。{'\n'}
                    临近和经期中，角色也会悄悄多一分体贴。
                  </Text>
                </View>
              )}

              <Text style={s.periodSection}>记录一次</Text>
              <View style={s.periodBtnRow}>
                <TouchableOpacity style={s.periodBtn} onPress={() => recordPeriod(todayStr)}>
                  <Text style={s.periodBtnText}>今天开始了</Text>
                </TouchableOpacity>
                <TouchableOpacity style={s.periodBtn}
                  onPress={() => recordPeriod(formatDate(new Date(Date.now() - 86400000)))}>
                  <Text style={s.periodBtnText}>昨天开始的</Text>
                </TouchableOpacity>
                <TouchableOpacity style={[s.periodBtn, {borderStyle:'dashed'}]} onPress={() => setShowPStartPicker(true)}>
                  <Text style={s.periodBtnText}>选日期...</Text>
                </TouchableOpacity>
              </View>
              <Text style={s.periodHint}>结束那天再来点「记录结束日」即可（不记也行，会按平均长度估算）</Text>
              <View style={s.periodBtnRow}>
                <TouchableOpacity style={[s.periodBtn, {borderColor:(C.accent2||'#5BC4FF')+'88'}]} onPress={() => setShowPEndPicker(true)}>
                  <Text style={[s.periodBtnText, {color:C.accent2||'#5BC4FF'}]}>记录最近一次的结束日</Text>
                </TouchableOpacity>
              </View>

              {showPStartPicker && (
                <DateTimePicker
                  value={new Date()} mode="date" display="default" maximumDate={new Date()}
                  onChange={(event: any, d?: Date) => {
                    setShowPStartPicker(false);
                    if (event.type === 'set' && d) recordPeriod(formatDate(d));
                  }}
                />
              )}
              {showPEndPicker && (
                <DateTimePicker
                  value={new Date()} mode="date" display="default" maximumDate={new Date()}
                  onChange={(event: any, d?: Date) => {
                    setShowPEndPicker(false);
                    if (event.type === 'set' && d && periodRecords.length > 0) {
                      recordPeriod(periodRecords[0].start_date, formatDate(d));
                    }
                  }}
                />
              )}

              {periodRecords.length > 0 && (
                <>
                  <Text style={s.periodSection}>历史记录（点右侧删除记错的）</Text>
                  {periodRecords.map(r => (
                    <View key={r.id} style={s.periodRecRow}>
                      <Text style={s.periodRecText}>
                        {r.start_date}{r.end_date ? ` ~ ${r.end_date}` : ' 开始'}
                      </Text>
                      <TouchableOpacity onPress={() => deletePeriodRecord(r.id)} hitSlop={{top:8,bottom:8,left:8,right:8}}>
                        <Text style={{color:'#f87171', fontSize:15}}>🗑</Text>
                      </TouchableOpacity>
                    </View>
                  ))}
                </>
              )}
            </ScrollView>
            <View style={s.dateFooter}>
              <TouchableOpacity style={s.dateFooterBtn} onPress={() => setShowPeriod(false)}>
                <Text style={s.dateFooterConfirm}>关闭</Text>
              </TouchableOpacity>
            </View>
          </View>
        </View>
      </Modal>
    </View>
  );
}

// ─── TaskRow 组件（★ 加 DDL 倒计时徽章 + 左侧分类色条）───
function TaskRow({ task, onPress, onCheck, done }: {
  task: Task;
  onPress: (t: Task) => void;
  onCheck: (t: Task) => void;
  done?: boolean;
}) {
  const catColor = CATEGORY_COLORS[task.category] || '#6366f1';
  const days = daysUntil(task.due_date);
  const isDaily = task.repeat_type === 'daily';
  const isOverdue = !isDaily && days !== null && days < 0;
  const today = formatDate(new Date());
  const dailyEnded = isDaily && !!task.due_date && task.due_date < today;

  // ★ DDL 徽章：越近越红
  let badge: { text: string; color: string } | null = null;
  if (!isDaily && !done && days !== null && days >= 0) {
    if (days === 0)      badge = { text: '今天', color: '#f87171' };
    else if (days === 1) badge = { text: 'D-1', color: '#fb923c' };
    else if (days <= 3)  badge = { text: `D-${days}`, color: '#fbbf24' };
    else if (days <= 7)  badge = { text: `D-${days}`, color: C.accent2 || '#5BC4FF' };
    else                 badge = { text: `D-${days}`, color: '#64748b' };
  }
  if (isOverdue && !done) badge = { text: `逾期${Math.abs(days!)}天`, color: '#f87171' };

  return (
    <TouchableOpacity
      style={[s.taskRow, { borderLeftWidth: 3, borderLeftColor: catColor + (done ? '55' : 'ee') }, done && {opacity:0.45}]}
      onPress={() => onPress(task)}
      activeOpacity={0.75}
    >
      <TouchableOpacity
        style={[s.check, done && {backgroundColor: catColor, borderColor: catColor}]}
        onPress={() => onCheck(task)}
        hitSlop={{top:10, bottom:10, left:10, right:10}}
      >
        {done && <Text style={{color:'#fff', fontSize:11, fontWeight:'700'}}>✓</Text>}
      </TouchableOpacity>
      <View style={{flex:1}}>
        <Text style={[s.taskTitle, done && {textDecorationLine:'line-through', color:C.textMute}]}
          numberOfLines={1}>{task.title}</Text>
        <View style={s.taskMeta}>
          <View style={[s.catTag, {backgroundColor: catColor+'22'}]}>
            <Text style={[s.catTagText, {color:catColor}]}>{task.category}</Text>
          </View>
          {isDaily && (
            <Text style={s.taskDate}>
              {dailyEnded ? '🔁 已结束' : `🔁 每日 ${task.due_time || ''}`}
              {!dailyEnded && task.due_date ? `  ·  至${task.due_date.slice(5).replace('-','/')}` : ''}
            </Text>
          )}
          {!isDaily && task.due_date && (
            <Text style={[s.taskDate, isOverdue && {color:'#f87171'}]}>
              {friendlyDate(task.due_date)}{task.due_time ? `  ${task.due_time}` : ''}
            </Text>
          )}
          {task.notification_id && <Text style={{fontSize:11}}>🔔</Text>}
        </View>
      </View>
      {badge && (
        <View style={[s.ddlBadge, {backgroundColor: badge.color + '22', borderColor: badge.color + '66'}]}>
          <Text style={[s.ddlBadgeText, {color: badge.color}]}>{badge.text}</Text>
        </View>
      )}
      <Text style={s.taskArrow}>›</Text>
    </TouchableOpacity>
  );
}

const s = StyleSheet.create({
  header: {
    flexDirection:'row', justifyContent:'space-between', alignItems:'center',
    paddingHorizontal:20, paddingTop:52, paddingBottom:8,
  },
  headerTitle: { color:C.text, fontSize:24, fontWeight:'800' },
  headerSub:   { color:C.textMute, fontSize:12, marginTop:2 },
  viewToggle: {
    flexDirection:'row', backgroundColor:C.card,
    borderRadius:12, borderWidth:1, borderColor:C.border, overflow:'hidden',
  },
  viewToggleBtn: { paddingHorizontal:14, paddingVertical:7 },
  viewToggleActive: { backgroundColor: C.accent2 || '#5BC4FF' },
  viewToggleText: { color:C.textMute, fontSize:12, fontWeight:'600' },

  // 今日进度卡
  statCard: {
    flexDirection:'row', alignItems:'center',
    marginHorizontal:20, marginBottom:10,
    backgroundColor:C.card, borderRadius:16,
    borderWidth:1, borderColor:C.border,
    paddingHorizontal:16, paddingVertical:12,
  },
  statTitle: { color:C.text, fontSize:14, fontWeight:'700', marginBottom:8 },
  progressTrack: {
    height:6, backgroundColor:C.bg, borderRadius:3, overflow:'hidden',
    borderWidth:1, borderColor:C.border,
  },
  progressFill: { height:'100%', backgroundColor: C.accent2 || '#5BC4FF', borderRadius:3 },
  statDdl: { color:C.textMute, fontSize:11, marginTop:8 },

  // 🌸 生理期
  periodCard: {
    flexDirection:'row', alignItems:'center', gap:8,
    marginHorizontal:20, marginBottom:10,
    backgroundColor:'#e879a0'+'14', borderRadius:14,
    borderWidth:1, borderColor:'#e879a0'+'44',
    paddingHorizontal:14, paddingVertical:10,
  },
  periodEmoji: { fontSize:15 },
  periodText:  { color:'#e8a0bb', fontSize:12.5, flex:1, fontWeight:'600' },
  periodArrow: { color:'#e8a0bb', fontSize:18 },
  periodTitle: { color:C.text, fontSize:17, fontWeight:'700', paddingHorizontal:20, marginBottom:12 },
  periodStatusBox: {
    marginHorizontal:16, borderRadius:14, padding:14,
    backgroundColor:'#e879a0'+'12', borderWidth:1, borderColor:'#e879a0'+'33',
  },
  periodStatusMain: { color:'#e8a0bb', fontSize:16, fontWeight:'700', marginBottom:6 },
  periodStatusSub:  { color:C.textMute, fontSize:12, lineHeight:19 },
  periodSection: { color:C.textMute, fontSize:11, letterSpacing:1, fontWeight:'700', paddingHorizontal:20, marginTop:16, marginBottom:8 },
  periodBtnRow:  { flexDirection:'row', flexWrap:'wrap', gap:8, paddingHorizontal:16 },
  periodBtn: {
    paddingHorizontal:14, paddingVertical:9, borderRadius:10,
    borderWidth:1, borderColor:C.border, backgroundColor:C.bg,
  },
  periodBtnText: { color:C.text, fontSize:13 },
  periodHint: { color:C.textMute, fontSize:11, paddingHorizontal:20, marginTop:8, marginBottom:6, lineHeight:16 },
  periodRecRow: {
    flexDirection:'row', alignItems:'center', justifyContent:'space-between',
    marginHorizontal:16, paddingHorizontal:12, paddingVertical:10,
    borderRadius:10, backgroundColor:'rgba(255,255,255,0.03)',
    borderWidth:1, borderColor:C.border, marginBottom:6,
  },
  periodRecText: { color:C.text, fontSize:13 },

  // ★ DDL 自定义天数
  offsetRow: { flexDirection:'row', flexWrap:'wrap', gap:6, marginVertical:8 },
  offsetChip: {
    paddingHorizontal:10, paddingVertical:6, borderRadius:9,
    borderWidth:1, borderColor:C.border, backgroundColor:C.bg,
  },
  offsetChipOn: { backgroundColor:(C.accent2||'#5BC4FF')+'33', borderColor:C.accent2||'#5BC4FF' },
  offsetChipText: { color:C.textMute, fontSize:12 },
  offsetChipTextOn: { color:C.accent2||'#5BC4FF', fontWeight:'700' },

  tabBar: { flexGrow:0, marginBottom:4 },
  tabBarInner: { paddingHorizontal:20, gap:8 },
  tab: {
    paddingHorizontal:16, paddingVertical:7,
    borderRadius:20, backgroundColor:C.card,
    borderWidth:1, borderColor:C.border,
  },
  tabText: { color:C.textMute, fontSize:13 },

  list: { paddingHorizontal:20, paddingBottom:100, paddingTop:8, gap:6 },
  sectionLabel: { color:C.textMute, fontSize:11, letterSpacing:1, marginTop:12, marginBottom:4, fontWeight:'700' },
  emptyWrap: { alignItems:'center', marginTop:60 },
  emptyText: { color:C.textMute, fontSize:14, textAlign:'center', lineHeight:24 },

  taskRow: {
    flexDirection:'row', alignItems:'center',
    backgroundColor:C.card, borderRadius:14,
    borderWidth:1, borderColor:C.border,
    paddingVertical:14, paddingHorizontal:14, gap:12,
  },
  check: {
    width:24, height:24, borderRadius:12,
    borderWidth:2, borderColor:C.border,
    alignItems:'center', justifyContent:'center',
  },
  taskTitle: { color:C.text, fontSize:15, fontWeight:'500', marginBottom:4 },
  taskMeta: { flexDirection:'row', alignItems:'center', gap:8 },
  catTag: { borderRadius:6, paddingHorizontal:6, paddingVertical:2 },
  catTagText: { fontSize:10, fontWeight:'600' },
  taskDate: { color:C.textMute, fontSize:11 },
  taskArrow: { color:C.textMute, fontSize:22 },
  ddlBadge: {
    borderRadius:8, borderWidth:1,
    paddingHorizontal:8, paddingVertical:3,
  },
  ddlBadgeText: { fontSize:11, fontWeight:'800' },

  fab: {
    position:'absolute', bottom:28, right:24,
    width:56, height:56, borderRadius:28,
    backgroundColor:C.accent2||'#5BC4FF',
    alignItems:'center', justifyContent:'center',
    elevation:6, shadowColor:'#000', shadowOpacity:0.3,
    shadowRadius:8, shadowOffset:{width:0,height:4},
  },
  fabText: { color:'#fff', fontSize:28, lineHeight:32 },

  // 月历视图
  monthCard: {
    marginHorizontal:16, marginBottom:8,
    backgroundColor:C.card, borderRadius:18,
    borderWidth:1, borderColor:C.border,
    paddingTop:14, paddingBottom:8,
  },
  calCellBig: { width: CELL_W_BIG, height:52, alignItems:'center', justifyContent:'flex-start', paddingTop:2 },
  weekRowM:   { flexDirection:'row', paddingHorizontal:12, marginBottom:4 },
  weekLabelM: { color:C.textMute, fontSize:12, width: CELL_W_BIG, textAlign:'center' },
  calDayWrapBig: { width:32, height:32, borderRadius:16, alignItems:'center', justifyContent:'center' },
  dotRow: { flexDirection:'row', gap:2, marginTop:2, alignItems:'center', height:6 },
  taskDot: { width:5, height:5, borderRadius:2.5 },
  dotMore: { color:C.textMute, fontSize:8, lineHeight:8 },

  // 新建 sheet
  sheetOverlay: { flex:1, justifyContent:'flex-end' },
  addSheet: {
    backgroundColor:C.card,
    borderTopLeftRadius:20, borderTopRightRadius:20,
    paddingTop:16, paddingBottom: Platform.OS==='ios' ? 36 : 16,
    paddingHorizontal:16,
    borderTopWidth:1, borderColor:C.border,
  },
  addInput: {
    color:C.text, fontSize:17,
    paddingVertical:8, paddingHorizontal:4,
    maxHeight:120, marginBottom:8,
  },
  addHints: { flexDirection:'row', flexWrap:'wrap', gap:6, marginBottom:8 },
  hintChip: {
    backgroundColor:(C.accent2||'#5BC4FF')+'22',
    borderRadius:8, paddingHorizontal:10, paddingVertical:4,
  },
  hintChipText: { color:C.accent2||'#5BC4FF', fontSize:12 },
  addIconRow: { flexDirection:'row', alignItems:'center', gap:8 },
  catChip: {
    flexDirection:'row', alignItems:'center', gap:5,
    backgroundColor:C.bg, borderRadius:10,
    paddingHorizontal:10, paddingVertical:6,
    borderWidth:1, borderColor:C.border,
  },
  catDot: { width:8, height:8, borderRadius:4 },
  catChipText: { color:C.text, fontSize:12 },
  repeatBtn: {
    paddingHorizontal:10, paddingVertical:6,
    borderRadius:10, backgroundColor:C.bg,
    borderWidth:1, borderColor:C.border,
    alignItems:'center', justifyContent:'center',
  },
  repeatBtnText: { fontSize:16, color:C.textMute },
  iconBtn: { padding:8 },
  iconBtnText: { fontSize:22, color:C.textMute },
  sendBtn: {
    width:40, height:40, borderRadius:20,
    backgroundColor:C.accent2||'#5BC4FF',
    alignItems:'center', justifyContent:'center',
  },
  sendBtnText: { color:'#fff', fontSize:16, fontWeight:'700' },
  catFloatMenu: {
    position:'absolute', left:16, bottom:72,
    backgroundColor:C.bg, borderRadius:14,
    borderWidth:1, borderColor:C.border,
    paddingVertical:8, paddingHorizontal:4,
    elevation:8, zIndex:100,
  },
  catFloatItem: { flexDirection:'row', alignItems:'center', gap:10, paddingHorizontal:14, paddingVertical:10 },
  catFloatText: { color:C.textMute, fontSize:14 },

  // 日期 Modal
  dateSheet: {
    position:'absolute', bottom:0, left:0, right:0,
    backgroundColor:C.card,
    borderTopLeftRadius:24, borderTopRightRadius:24,
    maxHeight: height*0.88, paddingTop:20,
  },
  dailyHint: {
    backgroundColor:'#d97706'+'22',
    marginHorizontal:20, marginBottom:12,
    paddingHorizontal:16, paddingVertical:10,
    borderRadius:10,
  },
  dailyHintText: { color:'#d97706', fontSize:13, textAlign:'center', lineHeight:18 },
  ddlNote: {
    backgroundColor:(C.accent2||'#5BC4FF')+'1A',
    marginHorizontal:16, marginTop:4, marginBottom:4,
    paddingHorizontal:14, paddingVertical:8, borderRadius:10,
  },
  ddlNoteText: { color:C.accent2||'#5BC4FF', fontSize:12, lineHeight:17 },
  calHeader: { flexDirection:'row', alignItems:'center', justifyContent:'space-between', paddingHorizontal:28, marginBottom:16 },
  calHeaderTitle: { color:C.text, fontSize:17, fontWeight:'700' },
  calNav: { color:C.accent2||'#5BC4FF', fontSize:18, padding:4 },
  weekRow: { flexDirection:'row', paddingHorizontal:12, marginBottom:4 },
  weekLabel: { color:C.textMute, fontSize:12, width: CELL_W_SM, textAlign:'center' },
  calGrid: { flexDirection:'row', flexWrap:'wrap', paddingHorizontal:12, marginBottom:12 },
  calCell: { width: CELL_W_SM, height:40, alignItems:'center', justifyContent:'center' },
  calDayWrap: { width:34, height:34, borderRadius:17, alignItems:'center', justifyContent:'center' },
  calDayText: { color:C.text, fontSize:14 },
  quickRow: { flexDirection:'row', flexWrap:'wrap', paddingHorizontal:16, gap:8, marginBottom:16 },
  quickBtn: {
    paddingHorizontal:14, paddingVertical:8,
    borderRadius:10, borderWidth:1, borderColor:C.border, backgroundColor:C.bg,
  },
  quickText: { color:C.text, fontSize:13 },
  divider: { height:1, backgroundColor:C.border, marginHorizontal:16, marginVertical:4 },
  dateRow: { flexDirection:'row', alignItems:'center', paddingHorizontal:20, paddingVertical:14, gap:12 },
  dateRowIcon: { fontSize:18 },
  dateRowLabel: { color:C.text, fontSize:15, flex:1 },
  dateRowValue: { color:C.textMute, fontSize:14 },
  timeChipRow: { flexDirection:'row', flexWrap:'wrap', paddingHorizontal:20, gap:8, marginBottom:8 },
  timeChip: { paddingHorizontal:14, paddingVertical:7, borderRadius:10, borderWidth:1, borderColor:C.border, backgroundColor:C.bg },
  timeChipText: { color:C.text, fontSize:13 },
  remChip: { paddingHorizontal:12, paddingVertical:7, borderRadius:10, borderWidth:1, borderColor:C.border, backgroundColor:C.bg },
  remChipText: { color:C.textMute, fontSize:12 },
  dateFooter: { flexDirection:'row', borderTopWidth:1, borderColor:C.border, paddingBottom: Platform.OS==='ios' ? 24 : 12 },
  dateFooterBtn: { flex:1, alignItems:'center', paddingVertical:16 },
  dateFooterCancel: { color:C.textMute, fontSize:16 },
  dateFooterConfirm: { color:C.accent2||'#5BC4FF', fontSize:16, fontWeight:'700' },

  // 编辑全屏
  editFull: { flex:1, backgroundColor:C.bg },
  editHeader: {
    flexDirection:'row', alignItems:'center',
    paddingTop:52, paddingBottom:12, paddingHorizontal:20,
    borderBottomWidth:1, borderColor:C.border, backgroundColor:C.card,
  },
  editBack: { padding:4 },
  editBackText: { color:C.text, fontSize:17 },
  editBody: { paddingBottom:120 },
  editCatRow: { flexDirection:'row', alignItems:'center', gap:8, paddingHorizontal:20, paddingVertical:14 },
  editCatText: { color:C.textMute, fontSize:13 },
  editCatMenu: {
    backgroundColor:C.card, marginHorizontal:20,
    borderRadius:12, borderWidth:1, borderColor:C.border,
    marginBottom:8, overflow:'hidden',
  },
  editCatItem: { flexDirection:'row', alignItems:'center', gap:10, paddingHorizontal:16, paddingVertical:12 },
  editCatItemText: { color:C.textMute, fontSize:14 },
  editTitleInput: {
    color:C.text, fontSize:22, fontWeight:'600',
    paddingHorizontal:20, paddingVertical:12, minHeight:60,
  },
  editRow: { flexDirection:'row', alignItems:'center', paddingHorizontal:20, paddingVertical:16, gap:16 },
  editRowIcon: { fontSize:20, width:28 },
  editRowLabel: { color:C.text, fontSize:15, flex:1 },
  editRowValue: { color:C.textMute, fontSize:14 },
  repeatChip: {
    paddingHorizontal:12, paddingVertical:6,
    borderRadius:8, borderWidth:1, borderColor:C.border,
    backgroundColor:C.bg,
  },
  repeatChipText: { color:C.textMute, fontSize:12 },
  editNoteInput: {
    color:C.textMute, fontSize:14,
    paddingHorizontal:60, paddingVertical:8, minHeight:44,
  },
  editSaveBtn: {
    position:'absolute', bottom:28, left:20, right:20,
    backgroundColor:C.accent2||'#5BC4FF',
    borderRadius:16, paddingVertical:16, alignItems:'center',
  },
  editSaveText: { color:'#fff', fontSize:17, fontWeight:'700' },
});