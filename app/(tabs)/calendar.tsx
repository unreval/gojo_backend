// app/(tabs)/calendar.tsx
// UX 重构：
//   1. 新建任务 → 简洁底部 sheet（只有输入框 + 图标行）
//   2. 点日历图标 → 弹出日期选择弹窗（完整月历 + 快捷选项）
//   3. 点任务卡片 → 打开全屏编辑 Modal
//   4. 每日打卡 → repeat_type='daily'，DAILY 通知触发器

import AsyncStorage from '@react-native-async-storage/async-storage';
import DateTimePicker from '@react-native-community/datetimepicker';
import axios from 'axios';
import * as Notifications from 'expo-notifications';
import React, { useEffect, useState } from 'react';
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
const USER_ID_KEY = 'gojo_user_id';

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

export default function CalendarScreen() {
  const [tasks, setTasks]         = useState<Task[]>([]);
  const [loading, setLoading]     = useState(true);
  const [userId, setUserId]       = useState('');
  const [activeTab, setActiveTab] = useState('所有');

  // 新建 sheet
  const [showAddSheet, setShowAddSheet]   = useState(false);
  const [newTitle, setNewTitle]           = useState('');
  const [newCategory, setNewCategory]     = useState('个人');
  const [newDueDate, setNewDueDate]       = useState<string | null>(null);
  const [newDueTime, setNewDueTime]       = useState<string | null>(null);
  const [newReminder, setNewReminder]     = useState<number | null>(null);
  const [newRepeat, setNewRepeat]         = useState<string>('none');
  const [showCatPicker, setShowCatPicker] = useState(false);

  // 日期弹窗（新建/编辑共用）
  const [showDateModal, setShowDateModal]   = useState(false);
  const [dateCtx, setDateCtx]               = useState<'add'|'edit'>('add');
  const [calYear, setCalYear]               = useState(new Date().getFullYear());
  const [calMonth, setCalMonth]             = useState(new Date().getMonth());
  const [tempDate, setTempDate]             = useState<string | null>(null);
  const [tempTime, setTempTime]             = useState<string | null>(null);
  const [tempReminder, setTempReminder]     = useState<number | null>(null);
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

  // ── 每日打卡判断 ──
  const isTaskCompleted = (t: Task): boolean => {
    if (t.repeat_type === 'daily') return t.last_completed_date === todayStr;
    return t.completed;
  };

  // 日期弹窗是否处于每日打卡模式
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
      const uid = await AsyncStorage.getItem(USER_ID_KEY);
      if (uid) { setUserId(uid); await loadTasks(uid); }
      setLoading(false);
    })();
  }, []);

  const loadTasks = async (uid: string) => {
    try {
      const res = await axios.get(`${SERVER_URL}/tasks?user_id=${uid}`);
      if (res.data?.tasks) setTasks(res.data.tasks);
    } catch {}
  };

  // ── 通知调度（支持 daily + 一次性） ──
  const scheduleNotification = async (
    taskId: number,
    date: string | null,
    time: string,
    rem: number | null,
    title: string,
    repeat: string,
  ) => {
    try {
      const { status } = await Notifications.getPermissionsAsync();
      if (status !== 'granted') {
        const ns = await Notifications.requestPermissionsAsync();
        if (ns.status !== 'granted') return;
      }
      const [h, m] = time.split(':').map(Number);
      let notifId: string;

      if (repeat === 'daily') {
        notifId = await Notifications.scheduleNotificationAsync({
          content: {
            title: '五条悟提醒你打卡',
            body: title,
            sound: 'default',
            ...(Platform.OS === 'android' ? { channelId: 'gojo-reminders' } : {}),
          },
          trigger: { type: Notifications.SchedulableTriggerInputTypes.DAILY, hour: h, minute: m } as any,
        });
      } else {
        if (!date) return;
        const [y, mo, d] = date.split('-').map(Number);
        const trigger = new Date(y, mo-1, d, h, m, 0);
        trigger.setTime(trigger.getTime() - (rem || 0) * 60000);
        if (trigger.getTime() <= Date.now()) return;
        notifId = await Notifications.scheduleNotificationAsync({
          content: {
            title: '五条悟提醒你',
            body: title,
            sound: 'default',
            ...(Platform.OS === 'android' ? { channelId: 'gojo-reminders' } : {}),
          },
          trigger: { type: Notifications.SchedulableTriggerInputTypes.DATE, date: trigger } as any,
        });
      }
      await axios.put(`${SERVER_URL}/tasks/${taskId}`, { notification_id: notifId! });
    } catch {}
  };

  // ── 新建 ──
  const openAddSheet = () => {
    setNewTitle(''); setNewCategory('个人');
    setNewDueDate(null); setNewDueTime(null); setNewReminder(null);
    setNewRepeat('none');
    setShowCatPicker(false);
    setShowAddSheet(true);
  };

  const submitAdd = async () => {
    const title = newTitle.trim();
    if (!title || !userId) return;
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

      if (newDueTime) {
        if (newRepeat === 'daily') {
          await scheduleNotification(taskId, null, newDueTime, null, title, 'daily');
        } else if (newDueDate && newReminder !== null && taskId) {
          await scheduleNotification(taskId, newDueDate, newDueTime, newReminder, title, 'none');
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
    } else {
      setTempDate(editDueDate); setTempTime(editDueTime); setTempReminder(editReminder);
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
    } else {
      setEditDueDate(tempDate); setEditDueTime(tempTime); setEditReminder(tempReminder);
    }
    setShowDateModal(false);
  };

  const quickDates = [
    { label: '今天',       val: formatDate(new Date()) },
    { label: '明天',       val: formatDate(new Date(Date.now() + 86400000)) },
    { label: '3天后',      val: formatDate(new Date(Date.now() + 86400000*3)) },
    { label: '这个星期天', val: getNextSunday() },
    { label: '无日期',     val: null },
  ];

  // ── 编辑 ──
  const openEdit = (task: Task) => {
    setEditTask(task);
    setEditTitle(task.title);
    setEditCategory(task.category);
    setEditDueDate(task.due_date);
    setEditDueTime(task.due_time);
    setEditReminder(task.reminder_minutes);
    setEditRepeat(task.repeat_type || 'none');
    setEditNote('');
    setShowEditCat(false);
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
      // 取消旧通知 → 按新配置重新调度
      if (editTask.notification_id) {
        try { await Notifications.cancelScheduledNotificationAsync(editTask.notification_id); } catch {}
      }
      if (editDueTime) {
        if (editRepeat === 'daily') {
          await scheduleNotification(editTask.id, null, editDueTime, null, title, 'daily');
        } else if (editDueDate && editReminder !== null) {
          await scheduleNotification(editTask.id, editDueDate, editDueTime, editReminder, title, 'none');
        }
      }
      await loadTasks(userId);
    } catch { Alert.alert('保存失败'); }
  };

  // ── 删除 ──
  const deleteTask = (task: Task) => {
    Alert.alert('删除任务', `确认删除「${task.title}」？`, [
      { text: '取消', style: 'cancel' },
      { text: '删除', style: 'destructive', onPress: async () => {
        setShowEditModal(false);
        try {
          if (task.notification_id) {
            try { await Notifications.cancelScheduledNotificationAsync(task.notification_id); } catch {}
          }
          await axios.delete(`${SERVER_URL}/tasks/${task.id}`);
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
  const pending   = filtered.filter(t => !isTaskCompleted(t));
  const completed = filtered.filter(t => isTaskCompleted(t));
  const overdue   = pending.filter(t => {
    if (t.repeat_type === 'daily') return false;
    const d = daysUntil(t.due_date);
    return d !== null && d < 0;
  });
  const upcoming  = pending.filter(t => {
    if (t.repeat_type === 'daily') return true;
    const d = daysUntil(t.due_date);
    return d === null || d >= 0;
  });

  if (loading) return (
    <View style={{flex:1, backgroundColor:C.bg, alignItems:'center', justifyContent:'center'}}>
      <ActivityIndicator color={C.accent} />
    </View>
  );

  return (
    <View style={{flex:1, backgroundColor:C.bg}}>
      <StatusBar barStyle="light-content" backgroundColor={C.bg} />

      <View style={s.header}>
        <Text style={s.headerTitle}>日程</Text>
        <ChibiSprite pose="peek" size={52} />
      </View>

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

      <ScrollView style={{flex:1}} contentContainerStyle={s.list}>
        {overdue.length > 0 && (
          <>
            <Text style={[s.sectionLabel, {color:'#f87171'}]}>已逾期</Text>
            {overdue.map(task => <TaskRow key={task.id} task={task} onPress={openEdit} onCheck={toggleComplete} />)}
          </>
        )}
        {upcoming.length === 0 && overdue.length === 0 && (
          <View style={s.emptyWrap}>
            <Text style={s.emptyText}>没有任务{'\n'}悟在等你来安排 ✦</Text>
          </View>
        )}
        {upcoming.map(task => (
          <TaskRow key={task.id} task={task} onPress={openEdit} onCheck={toggleComplete} />
        ))}
        {completed.length > 0 && (
          <>
            <Text style={s.sectionLabel}>已完成 ({completed.length})</Text>
            {completed.map(task => (
              <TaskRow key={task.id} task={task} onPress={openEdit} onCheck={toggleComplete} done />
            ))}
          </>
        )}
      </ScrollView>

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
            {/* 提示 chips */}
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
                  <Text style={s.hintChipText}>🕐 每天 {newDueTime}</Text>
                </View>
              )}
              {newReminder !== null && newDueTime && newRepeat !== 'daily' && (
                <View style={s.hintChip}>
                  <Text style={s.hintChipText}>🔔 {newReminder === 0 ? '准时提醒' : `提前${newReminder}分钟`}</Text>
                </View>
              )}
            </View>
            {/* 图标行 */}
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
              {/* 每日打卡提示 */}
              {isDailyContext && (
                <View style={s.dailyHint}>
                  <Text style={s.dailyHintText}>🔁 每日打卡模式 — 只需设置提醒时间</Text>
                </View>
              )}

              {/* 日历（非每日打卡时显示） */}
              {!isDailyContext && (
                <>
                  <View style={s.calHeader}>
                    <TouchableOpacity onPress={() => {
                      if (calMonth === 0) { setCalMonth(11); setCalYear(calYear-1); }
                      else setCalMonth(calMonth-1);
                    }}><Text style={s.calNav}>◀</Text></TouchableOpacity>
                    <Text style={s.calHeaderTitle}>{MONTHS[calMonth]} {calYear}</Text>
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
                    {quickDates.map(opt => {
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
                  <View style={s.divider} />
                </>
              )}

              {/* 时间 */}
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
              {/* 原生时钟转盘 */}
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

              {/* 提醒 */}
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
            {/* 分类 */}
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

            {/* 标题 */}
            <TextInput
              style={s.editTitleInput}
              value={editTitle}
              onChangeText={setEditTitle}
              multiline
              placeholder="任务标题"
              placeholderTextColor={C.textMute}
            />

            <View style={s.divider} />

            {/* 重复类型 */}
            <View style={s.editRow}>
              <Text style={s.editRowIcon}>🔁</Text>
              <Text style={s.editRowLabel}>重复</Text>
              <View style={{flexDirection:'row', gap:8}}>
                <TouchableOpacity
                  style={[s.repeatChip, editRepeat === 'none' && {backgroundColor:C.accent+'33', borderColor:C.accent}]}
                  onPress={() => setEditRepeat('none')}
                >
                  <Text style={[s.repeatChipText, editRepeat === 'none' && {color:C.accent}]}>不重复</Text>
                </TouchableOpacity>
                <TouchableOpacity
                  style={[s.repeatChip, editRepeat === 'daily' && {backgroundColor:'#d9770633', borderColor:'#d97706'}]}
                  onPress={() => setEditRepeat('daily')}
                >
                  <Text style={[s.repeatChipText, editRepeat === 'daily' && {color:'#d97706'}]}>每日打卡</Text>
                </TouchableOpacity>
              </View>
            </View>
            <View style={s.divider} />

            {/* 截止日期 */}
            <TouchableOpacity style={s.editRow} onPress={() => openDateModal('edit')}>
              <Text style={s.editRowIcon}>📅</Text>
              <Text style={s.editRowLabel}>截止日期</Text>
              <Text style={s.editRowValue}>
                {editRepeat === 'daily' ? '每日重复' : (editDueDate ? editDueDate.replace(/-/g, '/') : '无')}
              </Text>
            </TouchableOpacity>
            <View style={s.divider} />

            {/* 时间和提醒 */}
            <TouchableOpacity style={s.editRow} onPress={() => openDateModal('edit')}>
              <Text style={s.editRowIcon}>🕐</Text>
              <Text style={s.editRowLabel}>时间和提醒</Text>
              <Text style={s.editRowValue}>
                {editDueTime
                  ? `${editDueTime}${editReminder !== null && editRepeat !== 'daily' ? `  ·  提前${editReminder===0?'准时':`${editReminder}分`}` : ''}`
                  : '无'}
              </Text>
            </TouchableOpacity>
            <View style={s.divider} />

            {/* 备注 */}
            <View style={s.editRow}>
              <Text style={s.editRowIcon}>📝</Text>
              <Text style={s.editRowLabel}>备注</Text>
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
    </View>
  );
}

// ─── TaskRow 组件 ───
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

  return (
    <TouchableOpacity
      style={[s.taskRow, done && {opacity:0.45}]}
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
            <Text style={s.taskDate}>🔁 每日 {task.due_time || ''}</Text>
          )}
          {!isDaily && task.due_date && (
            <Text style={[s.taskDate, isOverdue && {color:'#f87171'}]}>
              {friendlyDate(task.due_date)}{task.due_time ? `  ${task.due_time}` : ''}
            </Text>
          )}
          {task.notification_id && <Text style={{fontSize:11}}>🔔</Text>}
        </View>
      </View>
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
  tabBar: { flexGrow:0, marginBottom:4 },
  tabBarInner: { paddingHorizontal:20, gap:8 },
  tab: {
    paddingHorizontal:16, paddingVertical:7,
    borderRadius:20, backgroundColor:C.card,
    borderWidth:1, borderColor:C.border,
  },
  tabText: { color:C.textMute, fontSize:13 },

  list: { paddingHorizontal:20, paddingBottom:100, paddingTop:8, gap:6 },
  sectionLabel: { color:C.textMute, fontSize:11, letterSpacing:1, marginTop:10, marginBottom:4 },
  emptyWrap: { alignItems:'center', marginTop:80 },
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

  fab: {
    position:'absolute', bottom:28, right:24,
    width:56, height:56, borderRadius:28,
    backgroundColor:C.accent2||'#5BC4FF',
    alignItems:'center', justifyContent:'center',
    elevation:6, shadowColor:'#000', shadowOpacity:0.3,
    shadowRadius:8, shadowOffset:{width:0,height:4},
  },
  fabText: { color:'#fff', fontSize:28, lineHeight:32 },

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
  dailyHintText: { color:'#d97706', fontSize:13, textAlign:'center' },
  calHeader: { flexDirection:'row', alignItems:'center', justifyContent:'space-between', paddingHorizontal:28, marginBottom:16 },
  calHeaderTitle: { color:C.text, fontSize:17, fontWeight:'700' },
  calNav: { color:C.accent2||'#5BC4FF', fontSize:18, padding:4 },
  weekRow: { flexDirection:'row', paddingHorizontal:12, marginBottom:4 },
  weekLabel: { color:C.textMute, fontSize:12, width:(width-24)/7, textAlign:'center' },
  calGrid: { flexDirection:'row', flexWrap:'wrap', paddingHorizontal:12, marginBottom:12 },
  calCell: { width:(width-24)/7, height:40, alignItems:'center', justifyContent:'center' },
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
