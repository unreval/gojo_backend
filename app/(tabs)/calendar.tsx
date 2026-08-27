// app/(tabs)/calendar.tsx
// ★ 完整版多功能日程（基于原版重写，原有逻辑全部保留）：
//   保留：每日打卡(结束日期自动停) / DDL 倒数梯度提醒 / 原生时间转盘 / 聊天取消提醒联动 / 前端去重
//   新增：
//     1. 列表 ⇄ 月历 ⇄ 课表 三视图（月历有任务圆点+课程方点，点日期看当天任务）
//     2. 今日进度卡（完成度进度条 + 最近 DDL 倒计时）
//     3. 列表按时间智能分组：逾期 / 每日打卡 / 今天 / 明天 / 7天内 / 以后 / 无日期
//     4. 任务卡 DDL 倒计时徽章（D-N，越近越红）
//     5. 已完成区可折叠
//     6. DDL 提醒梯度升级：≥14天 → 14/7/3/1/当天 五连提醒
//     7. 备注本机持久化（按任务 id 存 AsyncStorage）
//     8. ★ Phase 2 课程表：周网格视图 + 课程 CRUD + 请假/调课，跟月历联动（月历上课程用方点）

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

// ★ Phase 2 课程表类型 ─────────────────
interface CourseSession {
  id?: number;
  weekday: number;      // 1-7 (周一 ~ 周日)
  start_time: string;   // "08:00"
  end_time: string;     // "09:40"
  weeks: string;        // "1-16" / "1,3,5" / "" (=每周)
}
interface Course {
  id: number;
  name: string;
  teacher: string;
  location: string;
  color: string;
  note: string;
  semester_start: string | null;
  semester_end: string | null;
  sessions: CourseSession[];
}
interface CourseInstance {
  instance_id: string;
  course_id: number;
  session_id: number | null;
  name: string;
  color: string;
  teacher: string;
  note: string;
  date: string;          // YYYY-MM-DD
  weekday: number;       // 1-7
  start_time: string;
  end_time: string;
  location: string;
  is_exception: boolean;
  exception_type: string | null;
  exception_id: number | null;
}

// ★ 课程表网格常量
const DAY_START_HOUR = 8;
const DAY_END_HOUR   = 22;
const HOUR_HEIGHT    = 56;
const TIME_COL_W     = 36;
const GRID_H         = (DAY_END_HOUR - DAY_START_HOUR) * HOUR_HEIGHT;
const DAY_COL_W      = Math.floor((width - TIME_COL_W - 8) / 7);
const COURSE_COLORS  = [
  '#3b82f6', '#60a5fa', '#8b5cf6', '#a78bfa',
  '#ec4899', '#f472b6', '#f59e0b', '#fbbf24',
  '#10b981', '#34d399', '#06b6d4', '#22d3ee',
];
const WEEKDAY_LABELS_MON = ['一', '二', '三', '四', '五', '六', '日'];

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

// ★ 课程表 helper ─────────────────
// 拿一个日期所在周的周一（周日算上一周的末尾，跟中国习惯一致）
function mondayOf(d: Date): Date {
  const n = new Date(d);
  const day = n.getDay();               // 0=日 1=一 ... 6=六
  const diff = day === 0 ? -6 : 1 - day;
  n.setDate(n.getDate() + diff);
  n.setHours(0, 0, 0, 0);
  return n;
}
function addDays(d: Date, n: number): Date {
  const r = new Date(d);
  r.setDate(r.getDate() + n);
  return r;
}
// "08:30" → 8.5，用来算课程卡在网格里的 top / height
function parseHM(t: string): number {
  const [h, m] = (t || '00:00').split(':').map(x => parseInt(x, 10) || 0);
  return h + m / 60;
}
// 学期第几周（semester_start 没给 → 返回 null）
function weekNumberOf(target: Date, semStart: string | null): number | null {
  if (!semStart) return null;
  const s = new Date(semStart); s.setHours(0, 0, 0, 0);
  const sMon = mondayOf(s);
  const tMon = mondayOf(target);
  const days = Math.round((tMon.getTime() - sMon.getTime()) / 86400000);
  if (days < 0) return 0;
  return Math.floor(days / 7) + 1;
}

export default function CalendarScreen() {
  const [tasks, setTasks]         = useState<Task[]>([]);
  const [loading, setLoading]     = useState(true);
  const [userId, setUserId]       = useState('');
  const [activeTab, setActiveTab] = useState('所有');
  const [viewMode, setViewMode]   = useState<'list'|'month'|'timetable'>('list');   // ★ 三视图
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

  // ★ Phase 2 课程表 state ─────────────────
  const [courses, setCourses] = useState<Course[]>([]);
  const [courseInstances, setCourseInstances] = useState<CourseInstance[]>([]);   // 课表 tab 里当前显示的那一周
  const [monthCourseInstances, setMonthCourseInstances] = useState<CourseInstance[]>([]);  // 月历上要显示的（跨多周）
  const [ttMonday, setTtMonday] = useState<Date>(mondayOf(new Date()));           // 课表当前显示的那个周一

  // 课程编辑 Modal（新建 + 编辑共用）
  const [showCourseEdit, setShowCourseEdit] = useState(false);
  const [editingCourse, setEditingCourse] = useState<Course | null>(null);
  const [cName, setCName] = useState('');
  const [cTeacher, setCTeacher] = useState('');
  const [cLocation, setCLocation] = useState('');
  const [cColor, setCColor] = useState(COURSE_COLORS[0]);
  const [cNote, setCNote] = useState('');
  const [cSemStart, setCSemStart] = useState('');
  const [cSemEnd, setCSemEnd] = useState('');
  const [cSessions, setCSessions] = useState<CourseSession[]>([]);

  // 课程卡片操作菜单（点某节课弹出）
  const [showCourseAction, setShowCourseAction] = useState(false);
  const [actionInstance, setActionInstance] = useState<CourseInstance | null>(null);

  // 调课 Modal
  const [showResched, setShowResched] = useState(false);
  const [rNewDate, setRNewDate] = useState('');
  const [rNewStart, setRNewStart] = useState('');
  const [rNewEnd, setRNewEnd] = useState('');
  const [rNewLocation, setRNewLocation] = useState('');
  const [rNote, setRNote] = useState('');

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

  // ★ Phase 2 课程表：拉全部课程（编辑时用 + 算学期第几周用）
  const loadCourses = async (uid: string) => {
    try {
      const r = await axios.get(`${SERVER_URL}/courses`, { params: { user_id: uid }, timeout: 8000 });
      setCourses(r.data?.courses || []);
    } catch (e: any) { console.warn('loadCourses', e?.message); }
  };
  // ★ 拉某一周的课程实例（课表 tab 用）
  const loadWeekInstances = async (uid: string, mon: Date) => {
    try {
      const r = await axios.get(`${SERVER_URL}/courses/week`, {
        params: { user_id: uid, monday: formatDate(mon) },
        timeout: 8000,
      });
      setCourseInstances(r.data?.instances || []);
    } catch (e: any) { console.warn('loadWeekInstances', e?.message); }
  };
  // ★ 拉月历显示所需的多周课程实例（当前月前后各一个月）
  const loadMonthInstances = async (uid: string, year: number, month: number) => {
    try {
      const results: CourseInstance[] = [];
      const seen = new Set<string>();
      // 覆盖 month 前后各 6 周（保守），足够画整月
      const base = mondayOf(new Date(year, month, 1));
      for (let offset = -1; offset <= 6; offset++) {
        const mon = addDays(base, offset * 7);
        try {
          const r = await axios.get(`${SERVER_URL}/courses/week`, {
            params: { user_id: uid, monday: formatDate(mon) },
            timeout: 8000,
          });
          const arr: CourseInstance[] = r.data?.instances || [];
          for (const ins of arr) {
            if (!seen.has(ins.instance_id)) {
              seen.add(ins.instance_id);
              results.push(ins);
            }
          }
        } catch {}
      }
      setMonthCourseInstances(results);
    } catch {}
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
      loadCourses(uid);                        // ★ 课程表
      loadWeekInstances(uid, ttMonday);        // ★ 课程表当前周
      loadMonthInstances(uid, new Date().getFullYear(), new Date().getMonth());  // ★ 月历课程
      setLoading(false);
    })();
  }, []);

  useFocusEffect(
    useCallback(() => {
      if (userId) {
        loadTasks(userId);
        loadCourses(userId);
        loadWeekInstances(userId, ttMonday);
        loadMonthInstances(userId, viewYear, viewMonth);
      }
    }, [userId, ttMonday, viewYear, viewMonth])
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

  // ═══════════════════════════════════════════════════════════
  //  ★ Phase 2 课程表：CRUD + 请假 + 调课
  // ═══════════════════════════════════════════════════════════

  const openNewCourse = () => {
    setEditingCourse(null);
    setCName('');
    setCTeacher('');
    setCLocation('');
    setCColor(COURSE_COLORS[courses.length % COURSE_COLORS.length]);
    setCNote('');
    setCSemStart(formatDate(ttMonday));
    setCSemEnd(formatDate(addDays(ttMonday, 18 * 7 - 1)));
    setCSessions([{ weekday: 1, start_time: '08:00', end_time: '09:40', weeks: '' }]);
    setShowCourseEdit(true);
  };

  const openEditCourse = (course: Course) => {
    setEditingCourse(course);
    setCName(course.name);
    setCTeacher(course.teacher);
    setCLocation(course.location);
    setCColor(course.color || COURSE_COLORS[0]);
    setCNote(course.note);
    setCSemStart(course.semester_start || '');
    setCSemEnd(course.semester_end || '');
    setCSessions(
      course.sessions.length > 0
        ? course.sessions.map(s => ({ ...s }))
        : [{ weekday: 1, start_time: '08:00', end_time: '09:40', weeks: '' }]
    );
    setShowCourseEdit(true);
  };

  const saveCourse = async () => {
    const name = cName.trim();
    if (!name) { Alert.alert('请填写课程名'); return; }
    const validSessions = cSessions.filter(s => s.start_time && s.end_time);
    if (validSessions.length === 0) { Alert.alert('至少要有一个上课时段'); return; }
    setShowCourseEdit(false);
    try {
      const body: any = {
        user_id: userId,
        name,
        teacher: cTeacher.trim(),
        location: cLocation.trim(),
        color: cColor,
        note: cNote.trim(),
        semester_start: cSemStart || null,
        semester_end: cSemEnd || null,
        sessions: validSessions,
      };
      if (editingCourse) {
        await axios.put(`${SERVER_URL}/courses/${editingCourse.id}`, body);
      } else {
        await axios.post(`${SERVER_URL}/courses`, body);
      }
      await loadCourses(userId);
      await loadWeekInstances(userId, ttMonday);
      await loadMonthInstances(userId, viewYear, viewMonth);
    } catch (e: any) {
      Alert.alert('保存失败', e?.response?.data?.error ?? e?.message ?? '');
    }
  };

  const deleteCourse = () => {
    if (!editingCourse) return;
    const c = editingCourse;
    Alert.alert('删除课程', `确认删除「${c.name}」？这会连带删除全部上课记录和请假/调课记录。`, [
      { text: '取消', style: 'cancel' },
      {
        text: '删除', style: 'destructive', onPress: async () => {
          setShowCourseEdit(false);
          try {
            await axios.delete(`${SERVER_URL}/courses/${c.id}`);
            await loadCourses(userId);
            await loadWeekInstances(userId, ttMonday);
            await loadMonthInstances(userId, viewYear, viewMonth);
          } catch { Alert.alert('删除失败'); }
        }
      },
    ]);
  };

  const cancelCourseInstance = async (ins: CourseInstance) => {
    setShowCourseAction(false);
    try {
      await axios.post(`${SERVER_URL}/course/exceptions`, {
        course_id: ins.course_id,
        session_id: ins.session_id,
        exception_date: ins.date,
        exception_type: 'cancel',
      });
      await loadWeekInstances(userId, ttMonday);
      await loadMonthInstances(userId, viewYear, viewMonth);
    } catch (e: any) {
      Alert.alert('请假失败', e?.response?.data?.error ?? e?.message ?? '');
    }
  };

  const restoreCourseInstance = async (ins: CourseInstance) => {
    if (!ins.exception_id) return;
    setShowCourseAction(false);
    try {
      await axios.delete(`${SERVER_URL}/course/exceptions/${ins.exception_id}`);
      await loadWeekInstances(userId, ttMonday);
      await loadMonthInstances(userId, viewYear, viewMonth);
    } catch { Alert.alert('恢复失败'); }
  };

  const openResched = (ins: CourseInstance) => {
    setShowCourseAction(false);
    setRNewDate(ins.date);
    setRNewStart(ins.start_time);
    setRNewEnd(ins.end_time);
    setRNewLocation(ins.location || '');
    setRNote('');
    setActionInstance(ins);
    setShowResched(true);
  };
  const submitReschedule = async () => {
    if (!actionInstance) return;
    if (!rNewDate || !rNewStart || !rNewEnd) {
      Alert.alert('请填写新日期和时间'); return;
    }
    setShowResched(false);
    try {
      await axios.post(`${SERVER_URL}/course/exceptions`, {
        course_id: actionInstance.course_id,
        session_id: actionInstance.session_id,
        exception_date: actionInstance.date,
        exception_type: 'reschedule',
        new_date: rNewDate,
        new_start_time: rNewStart,
        new_end_time: rNewEnd,
        new_location: rNewLocation.trim(),
        note: rNote.trim(),
      });
      await loadWeekInstances(userId, ttMonday);
      await loadMonthInstances(userId, viewYear, viewMonth);
    } catch (e: any) {
      Alert.alert('调课失败', e?.response?.data?.error ?? e?.message ?? '');
    }
  };

  const onCourseCardPress = (ins: CourseInstance) => {
    setActionInstance(ins);
    setShowCourseAction(true);
  };

  // 课表当前那一周 → 学期第几周（用第一个有 semester_start 的课程算）
  const ttWeekNum = (() => {
    const c = courses.find(x => x.semester_start);
    return c ? weekNumberOf(ttMonday, c.semester_start) : null;
  })();
  const ttWeekLabel = (() => {
    const sun = addDays(ttMonday, 6);
    return `${ttMonday.getMonth()+1}/${ttMonday.getDate()} ~ ${sun.getMonth()+1}/${sun.getDate()}`;
  })();

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

  // ★ 月历上某天有哪些课（跟任务分开显示，方点 vs 圆点）
  const coursesOnDate = (dateStr: string): CourseInstance[] => {
    return monthCourseInstances.filter(c => c.date === dateStr);
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
            <TouchableOpacity
              style={[s.viewToggleBtn, viewMode==='timetable' && s.viewToggleActive]}
              onPress={() => { setViewMode('timetable'); setTtMonday(mondayOf(new Date())); }}>
              <Text style={[s.viewToggleText, viewMode==='timetable' && {color:'#fff'}]}>课表</Text>
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

      {viewMode === 'list' && (
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
      )}
      {viewMode === 'month' && (
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
                const dayCourses = coursesOnDate(date);         // ★ 课程点
                const isSel = selDate === date;
                const isTd  = date === todayStr;
                const dots = dayTasks.slice(0, 3);
                const courseDots = dayCourses.slice(0, 2);      // 课程最多显 2 个方点，任务圆点跟后面
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
                      {/* ★ 课程 = 方点（跟任务圆点视觉区分） */}
                      {courseDots.map((c, i) => (
                        <View key={`c${i}`} style={[s.taskDot, s.courseSquare, {backgroundColor: c.color}]} />
                      ))}
                      {dots.map((t, i) => (
                        <View key={i} style={[s.taskDot, {backgroundColor: CATEGORY_COLORS[t.category] || C.accent}]} />
                      ))}
                      {(dayTasks.length + dayCourses.length) > (dots.length + courseDots.length) && (
                        <Text style={s.dotMore}>+</Text>
                      )}
                    </View>
                  </TouchableOpacity>
                );
              })}
            </View>
          </View>

          {/* 选中日的任务 + 课程 */}
          <View style={{paddingHorizontal:20, gap:6}}>
            {(() => {
              const selDayCourses = coursesOnDate(selDate).sort((a,b)=>a.start_time.localeCompare(b.start_time));
              const totalCount = selDayTasks.length + selDayCourses.length;
              return (
                <>
                  <Text style={s.sectionLabel}>
                    {selDate === todayStr ? '今天' : selDate.slice(5).replace('-','/')} 的安排 ({totalCount})
                  </Text>
                  {totalCount === 0 && (
                    <Text style={[s.emptyText, {marginTop:16}]}>这天没有安排</Text>
                  )}
                  {/* ★ 先列课程（一天里课通常有固定时间，排前面） */}
                  {selDayCourses.map(ins => (
                    <TouchableOpacity key={ins.instance_id} style={s.courseMiniRow}
                      onPress={() => { setViewMode('timetable'); setTtMonday(mondayOf(new Date(ins.date))); }}
                      activeOpacity={0.75}>
                      <View style={[s.courseMiniBar, {backgroundColor: ins.color}]} />
                      <View style={{flex:1}}>
                        <Text style={s.courseMiniTitle}>
                          📚 {ins.name}
                          {ins.is_exception && <Text style={{color: C.textMute}}>（调课）</Text>}
                        </Text>
                        <Text style={s.courseMiniMeta}>
                          {ins.start_time}~{ins.end_time}{ins.location ? ` · @${ins.location}` : ''}
                        </Text>
                      </View>
                    </TouchableOpacity>
                  ))}
                  {selDayTasks.map(task => (
                    <TaskRow key={task.id} task={task} onPress={openEdit} onCheck={toggleComplete}
                      done={isTaskCompleted(task)} />
                  ))}
                </>
              );
            })()}
          </View>
        </ScrollView>
      )}
      {viewMode === 'timetable' && (
        /* ════ ★ Phase 2 课表视图 ════ */
        <View style={{flex:1}}>
          {/* 周切换条 */}
          <View style={s.ttWeekBar}>
            <TouchableOpacity style={s.ttNavBtn} onPress={() => setTtMonday(addDays(ttMonday, -7))}>
              <Text style={s.ttNavText}>‹</Text>
            </TouchableOpacity>
            <TouchableOpacity onPress={() => setTtMonday(mondayOf(new Date()))} style={{flex:1, alignItems:'center'}}>
              <Text style={s.ttWeekTitle}>
                {ttWeekNum ? `第 ${ttWeekNum} 周 · ` : ''}{ttWeekLabel}
              </Text>
              <Text style={s.ttWeekSub}>点击回到本周</Text>
            </TouchableOpacity>
            <TouchableOpacity style={s.ttNavBtn} onPress={() => setTtMonday(addDays(ttMonday, 7))}>
              <Text style={s.ttNavText}>›</Text>
            </TouchableOpacity>
          </View>

          {/* 星期头行 */}
          <View style={s.ttHeader}>
            <View style={{width: TIME_COL_W}} />
            {WEEKDAY_LABELS_MON.map((label, i) => {
              const day = addDays(ttMonday, i);
              const isTd = formatDate(day) === todayStr;
              return (
                <View key={i} style={[s.ttDayHead, {width: DAY_COL_W}]}>
                  <Text style={[s.ttDayHeadWk, isTd && {color: C.accent2 || '#5BC4FF'}]}>{label}</Text>
                  <Text style={[s.ttDayHeadDate, isTd && {color: C.accent2 || '#5BC4FF', fontWeight:'700'}]}>
                    {day.getMonth()+1}/{day.getDate()}
                  </Text>
                </View>
              );
            })}
          </View>

          {/* 网格主体 */}
          <ScrollView style={{flex:1}} contentContainerStyle={{paddingBottom: 140}}>
            <View style={{flexDirection:'row', height: GRID_H}}>
              {/* 左侧时间轴 */}
              <View style={{width: TIME_COL_W}}>
                {Array.from({length: DAY_END_HOUR - DAY_START_HOUR}).map((_, i) => (
                  <View key={i} style={{
                    height: HOUR_HEIGHT,
                    borderTopWidth: 1, borderColor: C.border + '44',
                  }}>
                    <Text style={s.ttHourLabel}>{DAY_START_HOUR + i}:00</Text>
                  </View>
                ))}
              </View>

              {/* 7 天列 + 绝对定位的课程卡 */}
              <View style={{flex:1, flexDirection:'row', position:'relative'}}>
                {WEEKDAY_LABELS_MON.map((_, i) => (
                  <View key={i} style={{
                    width: DAY_COL_W,
                    borderLeftWidth: 1, borderColor: C.border + '44',
                    height: GRID_H,
                  }}>
                    {Array.from({length: DAY_END_HOUR - DAY_START_HOUR}).map((_, h) => (
                      <View key={h} style={{
                        height: HOUR_HEIGHT,
                        borderTopWidth: 1, borderColor: C.border + '22',
                      }} />
                    ))}
                  </View>
                ))}
                {courseInstances.map(ins => {
                  const startH = parseHM(ins.start_time);
                  const endH   = parseHM(ins.end_time);
                  const top    = (startH - DAY_START_HOUR) * HOUR_HEIGHT;
                  const height = Math.max(28, (endH - startH) * HOUR_HEIGHT);
                  const left   = (ins.weekday - 1) * DAY_COL_W;
                  if (top < 0 || top >= GRID_H) return null;
                  return (
                    <TouchableOpacity key={ins.instance_id}
                      activeOpacity={0.85}
                      onPress={() => onCourseCardPress(ins)}
                      style={[s.ttCard, {
                        top, height, left, width: DAY_COL_W - 2,
                        backgroundColor: ins.color + 'DD',
                        borderLeftColor: ins.color,
                      }]}
                    >
                      <Text style={s.ttCardTitle} numberOfLines={2}>{ins.name}</Text>
                      {!!ins.location && (
                        <Text style={s.ttCardMeta} numberOfLines={1}>@{ins.location}</Text>
                      )}
                      {ins.is_exception && <Text style={s.ttCardExc}>调</Text>}
                    </TouchableOpacity>
                  );
                })}
              </View>
            </View>

            {/* 全部课程列表（便于编辑） */}
            <View style={{paddingHorizontal: 16, marginTop: 20}}>
              <Text style={s.sectionLabel}>所有课程 ({courses.length})</Text>
              {courses.length === 0 && (
                <Text style={[s.emptyText, {marginTop: 12}]}>还没有课程{'\n'}点右下角 + 添加</Text>
              )}
              {courses.map(c => (
                <TouchableOpacity key={c.id} style={s.ttCourseRow} onPress={() => openEditCourse(c)}>
                  <View style={[s.ttCourseDot, {backgroundColor: c.color}]} />
                  <View style={{flex:1}}>
                    <Text style={s.ttCourseName}>{c.name}</Text>
                    <Text style={s.ttCourseSub} numberOfLines={1}>
                      {c.teacher || '未设老师'} · {c.sessions.length} 个时段
                      {c.location ? ` · ${c.location}` : ''}
                    </Text>
                  </View>
                  <Text style={s.taskArrow}>›</Text>
                </TouchableOpacity>
              ))}
            </View>
          </ScrollView>
        </View>
      )}

      {/* FAB：课表模式建课程，其他模式建任务 */}
      <TouchableOpacity style={s.fab}
        onPress={viewMode === 'timetable' ? openNewCourse : openAddSheet}
        activeOpacity={0.85}>
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

      {/* ═══ ★ Phase 2 课程编辑 Modal（新建+编辑共用）═══ */}
      <Modal visible={showCourseEdit} transparent animationType="slide">
        <KeyboardAvoidingView style={{flex:1}} behavior={Platform.OS==='ios' ? 'padding' : 'height'}>
          <Pressable style={{flex:1, backgroundColor:'#00000055'}} onPress={() => setShowCourseEdit(false)} />
          <View style={s.courseEditSheet}>
            <ScrollView showsVerticalScrollIndicator={false} contentContainerStyle={{paddingBottom:16}}>
              <Text style={s.courseEditTitle}>{editingCourse ? '编辑课程' : '新建课程'}</Text>

              <Text style={s.ceFieldLabel}>课程名 *</Text>
              <TextInput style={s.ceInput} value={cName} onChangeText={setCName}
                placeholder="高等数学" placeholderTextColor={C.textMute} />

              <Text style={s.ceFieldLabel}>老师</Text>
              <TextInput style={s.ceInput} value={cTeacher} onChangeText={setCTeacher}
                placeholder="选填" placeholderTextColor={C.textMute} />

              <Text style={s.ceFieldLabel}>教室</Text>
              <TextInput style={s.ceInput} value={cLocation} onChangeText={setCLocation}
                placeholder="选填，例如 教一 201" placeholderTextColor={C.textMute} />

              <Text style={s.ceFieldLabel}>颜色</Text>
              <View style={s.ceColorRow}>
                {COURSE_COLORS.map(col => (
                  <TouchableOpacity key={col} onPress={() => setCColor(col)}
                    style={[
                      s.ceColorDot,
                      { backgroundColor: col },
                      cColor === col && { borderWidth: 3, borderColor: C.text },
                    ]} />
                ))}
              </View>

              <Text style={s.ceFieldLabel}>学期起止（算学期第几周用，不填就一直显示）</Text>
              <View style={{flexDirection:'row', gap:8}}>
                <TextInput style={[s.ceInput, {flex:1, marginTop:0}]}
                  value={cSemStart} onChangeText={setCSemStart}
                  placeholder="2026-09-01" placeholderTextColor={C.textMute}
                  autoCapitalize="none" />
                <TextInput style={[s.ceInput, {flex:1, marginTop:0}]}
                  value={cSemEnd} onChangeText={setCSemEnd}
                  placeholder="2027-01-15" placeholderTextColor={C.textMute}
                  autoCapitalize="none" />
              </View>

              <Text style={s.ceFieldLabel}>上课时段 * ({cSessions.length})</Text>
              {cSessions.map((sess, idx) => (
                <View key={idx} style={s.ceSessionBox}>
                  <View style={{flexDirection:'row', alignItems:'center', marginBottom: 8}}>
                    <Text style={{color:C.textMute, fontSize:12}}>时段 {idx+1}</Text>
                    <View style={{flex:1}} />
                    {cSessions.length > 1 && (
                      <TouchableOpacity onPress={() => setCSessions(prev => prev.filter((_,i) => i !== idx))}>
                        <Text style={{color:'#f87171', fontSize:12}}>删除</Text>
                      </TouchableOpacity>
                    )}
                  </View>
                  <View style={{flexDirection:'row', flexWrap:'wrap', gap:6, marginBottom: 8}}>
                    {[1,2,3,4,5,6,7].map(wd => (
                      <TouchableOpacity key={wd}
                        style={[s.ceWdChip, sess.weekday === wd && s.ceWdChipOn]}
                        onPress={() => setCSessions(prev => prev.map((x,i) => i===idx ? {...x, weekday: wd} : x))}>
                        <Text style={[s.ceWdChipText, sess.weekday === wd && {color:'#fff', fontWeight:'700'}]}>
                          周{WEEKDAY_LABELS_MON[wd-1]}
                        </Text>
                      </TouchableOpacity>
                    ))}
                  </View>
                  <View style={{flexDirection:'row', gap:8}}>
                    <TextInput style={[s.ceInput, {flex:1, marginTop:0}]}
                      value={sess.start_time}
                      onChangeText={v => setCSessions(prev => prev.map((x,i) => i===idx ? {...x, start_time:v} : x))}
                      placeholder="08:00" placeholderTextColor={C.textMute} />
                    <TextInput style={[s.ceInput, {flex:1, marginTop:0}]}
                      value={sess.end_time}
                      onChangeText={v => setCSessions(prev => prev.map((x,i) => i===idx ? {...x, end_time:v} : x))}
                      placeholder="09:40" placeholderTextColor={C.textMute} />
                  </View>
                  <TextInput style={[s.ceInput, {marginTop:6}]}
                    value={sess.weeks}
                    onChangeText={v => setCSessions(prev => prev.map((x,i) => i===idx ? {...x, weeks:v} : x))}
                    placeholder="周次：1-16 或 1,3,5,7-16。空=每周都有"
                    placeholderTextColor={C.textMute} />
                </View>
              ))}
              <TouchableOpacity style={s.ceAddSessionBtn} onPress={() => {
                setCSessions(prev => [...prev, { weekday: 1, start_time: '08:00', end_time: '09:40', weeks: '' }]);
              }}>
                <Text style={s.ceAddSessionText}>+ 加一个时段</Text>
              </TouchableOpacity>

              <Text style={s.ceFieldLabel}>备注</Text>
              <TextInput style={[s.ceInput, {minHeight:60}]}
                value={cNote} onChangeText={setCNote} multiline
                placeholder="选填" placeholderTextColor={C.textMute} />
            </ScrollView>

            <View style={s.ceFooter}>
              {editingCourse && (
                <TouchableOpacity style={s.ceDeleteBtn} onPress={deleteCourse}>
                  <Text style={s.ceDeleteText}>删除</Text>
                </TouchableOpacity>
              )}
              <TouchableOpacity style={s.ceCancelBtn} onPress={() => setShowCourseEdit(false)}>
                <Text style={s.ceCancelText}>取消</Text>
              </TouchableOpacity>
              <TouchableOpacity style={s.ceSaveBtn} onPress={saveCourse}>
                <Text style={s.ceSaveText}>保存</Text>
              </TouchableOpacity>
            </View>
          </View>
        </KeyboardAvoidingView>
      </Modal>

      {/* ═══ ★ 课程卡操作菜单 ═══ */}
      <Modal visible={showCourseAction} transparent animationType="fade">
        <Pressable style={{flex:1, backgroundColor:'#00000066'}} onPress={() => setShowCourseAction(false)} />
        <View style={s.caSheet}>
          {actionInstance && (
            <>
              <View style={s.caHead}>
                <View style={[s.caDot, {backgroundColor: actionInstance.color}]} />
                <View style={{flex:1}}>
                  <Text style={s.caTitle}>{actionInstance.name}</Text>
                  <Text style={s.caSub}>
                    {actionInstance.date.slice(5).replace('-','/')} · {actionInstance.start_time}~{actionInstance.end_time}
                    {actionInstance.location ? ` · @${actionInstance.location}` : ''}
                  </Text>
                </View>
              </View>
              <TouchableOpacity style={s.caBtn} onPress={() => {
                const c = courses.find(x => x.id === actionInstance.course_id);
                if (c) { setShowCourseAction(false); openEditCourse(c); }
              }}>
                <Text style={s.caBtnText}>📝 编辑课程</Text>
              </TouchableOpacity>
              {actionInstance.is_exception && actionInstance.exception_id ? (
                <TouchableOpacity style={s.caBtn} onPress={() => restoreCourseInstance(actionInstance)}>
                  <Text style={s.caBtnText}>↩️ 撤销这次调课</Text>
                </TouchableOpacity>
              ) : (
                <>
                  <TouchableOpacity style={s.caBtn} onPress={() => cancelCourseInstance(actionInstance)}>
                    <Text style={s.caBtnText}>🚫 请假 / 停课这一次</Text>
                  </TouchableOpacity>
                  <TouchableOpacity style={s.caBtn} onPress={() => openResched(actionInstance)}>
                    <Text style={s.caBtnText}>🔀 调到别的时间</Text>
                  </TouchableOpacity>
                </>
              )}
              <TouchableOpacity style={[s.caBtn, s.caBtnCancel]} onPress={() => setShowCourseAction(false)}>
                <Text style={[s.caBtnText, {color: C.textMute}]}>取消</Text>
              </TouchableOpacity>
            </>
          )}
        </View>
      </Modal>

      {/* ═══ ★ 调课 Modal ═══ */}
      <Modal visible={showResched} transparent animationType="slide">
        <KeyboardAvoidingView style={{flex:1}} behavior={Platform.OS==='ios' ? 'padding' : 'height'}>
          <Pressable style={{flex:1, backgroundColor:'#00000055'}} onPress={() => setShowResched(false)} />
          <View style={s.courseEditSheet}>
            <ScrollView>
              <Text style={s.courseEditTitle}>调课</Text>
              {actionInstance && (
                <Text style={{color:C.textMute, fontSize:12, marginBottom:8}}>
                  原时间：{actionInstance.date.slice(5).replace('-','/')} {actionInstance.start_time}~{actionInstance.end_time}
                </Text>
              )}
              <Text style={s.ceFieldLabel}>新日期 *</Text>
              <TextInput style={s.ceInput} value={rNewDate} onChangeText={setRNewDate}
                placeholder="YYYY-MM-DD" placeholderTextColor={C.textMute} autoCapitalize="none" />
              <Text style={s.ceFieldLabel}>新时间 *</Text>
              <View style={{flexDirection:'row', gap:8}}>
                <TextInput style={[s.ceInput, {flex:1, marginTop:0}]}
                  value={rNewStart} onChangeText={setRNewStart}
                  placeholder="08:00" placeholderTextColor={C.textMute} />
                <TextInput style={[s.ceInput, {flex:1, marginTop:0}]}
                  value={rNewEnd} onChangeText={setRNewEnd}
                  placeholder="09:40" placeholderTextColor={C.textMute} />
              </View>
              <Text style={s.ceFieldLabel}>新教室</Text>
              <TextInput style={s.ceInput} value={rNewLocation} onChangeText={setRNewLocation}
                placeholder="不改就留空" placeholderTextColor={C.textMute} />
              <Text style={s.ceFieldLabel}>备注</Text>
              <TextInput style={[s.ceInput, {minHeight:60}]} value={rNote} onChangeText={setRNote}
                multiline placeholder="选填" placeholderTextColor={C.textMute} />
            </ScrollView>
            <View style={s.ceFooter}>
              <TouchableOpacity style={s.ceCancelBtn} onPress={() => setShowResched(false)}>
                <Text style={s.ceCancelText}>取消</Text>
              </TouchableOpacity>
              <TouchableOpacity style={s.ceSaveBtn} onPress={submitReschedule}>
                <Text style={s.ceSaveText}>确认调课</Text>
              </TouchableOpacity>
            </View>
          </View>
        </KeyboardAvoidingView>
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

  // ═══ ★ Phase 2 课程表 ═══
  // 月历上的课程方点（跟 taskDot 共用大小,只是变方）
  courseSquare: { borderRadius: 1 },
  // 月历"这天有什么"里的课程小卡片
  courseMiniRow: {
    flexDirection:'row', alignItems:'center', gap:10,
    backgroundColor:C.card, borderRadius:12,
    borderWidth:1, borderColor:C.border,
    paddingHorizontal:12, paddingVertical:10,
  },
  courseMiniBar: { width:3, height:32, borderRadius:2 },
  courseMiniTitle: { color:C.text, fontSize:14, fontWeight:'600' },
  courseMiniMeta: { color:C.textMute, fontSize:11, marginTop:2 },

  // 课表周切换条
  ttWeekBar: {
    flexDirection:'row', alignItems:'center',
    paddingHorizontal:12, paddingVertical:8,
    backgroundColor:C.card,
    borderTopWidth:1, borderBottomWidth:1, borderColor:C.border,
  },
  ttNavBtn: {
    width:40, height:40, alignItems:'center', justifyContent:'center',
    borderRadius:10, backgroundColor:C.bg,
    borderWidth:1, borderColor:C.border,
  },
  ttNavText: { color:C.text, fontSize:20, lineHeight:22 },
  ttWeekTitle: { color:C.text, fontSize:14, fontWeight:'700' },
  ttWeekSub:   { color:C.textMute, fontSize:10, marginTop:2 },

  // 课表星期头
  ttHeader: {
    flexDirection:'row', paddingVertical:8, paddingLeft:4,
    backgroundColor:C.card,
    borderBottomWidth:1, borderColor:C.border,
  },
  ttDayHead: { alignItems:'center' },
  ttDayHeadWk: { color:C.text, fontSize:12, fontWeight:'700' },
  ttDayHeadDate: { color:C.textMute, fontSize:11, marginTop:2 },

  // 时间轴 label
  ttHourLabel: {
    color:C.textMute, fontSize:9,
    textAlign:'right', paddingRight:4, marginTop:-6,
  },

  // 课程卡（绝对定位在网格上）
  ttCard: {
    position:'absolute',
    borderRadius:6, borderLeftWidth:3,
    padding:4, overflow:'hidden',
  },
  ttCardTitle: { color:'#fff', fontSize:11, fontWeight:'700', lineHeight:14 },
  ttCardMeta:  { color:'#fff', fontSize:9, opacity:0.85, marginTop:2 },
  ttCardExc: {
    position:'absolute', top:2, right:2,
    backgroundColor:'#00000055', color:'#fff',
    fontSize:8, paddingHorizontal:3, paddingVertical:1, borderRadius:3,
  },

  // 课表底部的"所有课程"列表
  ttCourseRow: {
    flexDirection:'row', alignItems:'center', gap:10,
    backgroundColor:C.card, borderRadius:12,
    borderWidth:1, borderColor:C.border,
    paddingHorizontal:12, paddingVertical:12, marginBottom:6,
  },
  ttCourseDot: { width:10, height:10, borderRadius:5 },
  ttCourseName: { color:C.text, fontSize:14, fontWeight:'600' },
  ttCourseSub:  { color:C.textMute, fontSize:11, marginTop:3 },

  // 课程编辑 Sheet
  courseEditSheet: {
    position:'absolute', bottom:0, left:0, right:0,
    maxHeight: '90%',
    backgroundColor:C.card,
    borderTopLeftRadius:20, borderTopRightRadius:20,
    paddingHorizontal:16, paddingTop:20, paddingBottom: Platform.OS==='ios' ? 36 : 20,
  },
  courseEditTitle: { color:C.text, fontSize:18, fontWeight:'700', marginBottom:12 },
  ceFieldLabel: { color:C.textMute, fontSize:12, marginTop:12, marginBottom:6 },
  ceInput: {
    backgroundColor:C.bg, color:C.text, fontSize:14,
    paddingHorizontal:12, paddingVertical:10,
    borderRadius:8, borderWidth:1, borderColor:C.border,
    marginTop:4,
  },
  ceColorRow: { flexDirection:'row', flexWrap:'wrap', gap:8, marginTop:4 },
  ceColorDot: { width:32, height:32, borderRadius:16 },
  ceSessionBox: {
    backgroundColor:C.bg, borderRadius:10,
    borderWidth:1, borderColor:C.border,
    padding:10, marginTop:8,
  },
  ceWdChip: {
    paddingHorizontal:10, paddingVertical:5, borderRadius:6,
    borderWidth:1, borderColor:C.border, backgroundColor:C.card,
  },
  ceWdChipOn: { backgroundColor:C.accent, borderColor:C.accent },
  ceWdChipText: { color:C.textMute, fontSize:12 },
  ceAddSessionBtn: {
    marginTop:8, alignItems:'center', paddingVertical:10,
    borderRadius:8, borderWidth:1, borderColor:C.border, borderStyle:'dashed',
  },
  ceAddSessionText: { color:C.accent2 || '#5BC4FF', fontSize:13 },
  ceFooter: { flexDirection:'row', gap:8, marginTop:16 },
  ceDeleteBtn: {
    paddingHorizontal:14, paddingVertical:12,
    borderRadius:10, backgroundColor:'#7f1d1d',
  },
  ceDeleteText: { color:'#fff', fontSize:14, fontWeight:'600' },
  ceCancelBtn: {
    flex:1, alignItems:'center', paddingVertical:12,
    borderRadius:10, backgroundColor:C.bg,
    borderWidth:1, borderColor:C.border,
  },
  ceCancelText: { color:C.textMute, fontSize:14 },
  ceSaveBtn: {
    flex:2, alignItems:'center', paddingVertical:12,
    borderRadius:10, backgroundColor:C.accent2 || '#5BC4FF',
  },
  ceSaveText: { color:'#fff', fontSize:14, fontWeight:'700' },

  // 课程卡操作菜单
  caSheet: {
    position:'absolute', bottom:0, left:0, right:0,
    backgroundColor:C.card,
    borderTopLeftRadius:20, borderTopRightRadius:20,
    paddingHorizontal:16, paddingTop:16, paddingBottom: Platform.OS==='ios' ? 36 : 20,
  },
  caHead: {
    flexDirection:'row', alignItems:'center', gap:10,
    paddingBottom:12, borderBottomWidth:1, borderColor:C.border, marginBottom:6,
  },
  caDot: { width:12, height:12, borderRadius:6 },
  caTitle: { color:C.text, fontSize:15, fontWeight:'700' },
  caSub:   { color:C.textMute, fontSize:11, marginTop:3 },
  caBtn:   { paddingVertical:14, paddingHorizontal:8 },
  caBtnText: { color:C.text, fontSize:15 },
  caBtnCancel: { borderTopWidth:1, borderColor:C.border, marginTop:4 },
});