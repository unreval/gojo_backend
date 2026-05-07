// app/(tabs)/calendar.tsx — 日程管理页
import AsyncStorage from '@react-native-async-storage/async-storage';
import DateTimePicker from '@react-native-community/datetimepicker';
import axios from 'axios';
import * as Notifications from 'expo-notifications';
import React, { useEffect, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  Dimensions,
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
import { C, SERVER_URL } from '../../constants/theme';

const { width } = Dimensions.get('window');
const USER_ID_KEY = 'gojo_user_id';

// 通知配置：即使 app 在前台也弹通知
Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowAlert: true,
    shouldPlaySound: true,
    shouldSetBadge: false,
  }),
});

// ───────── 类型 ─────────

interface Task {
  id: number;
  title: string;
  category: string;
  due_date: string | null;
  due_time: string | null;
  reminder_minutes: number | null;
  completed: boolean;
}

// ───────── 分类配置 ─────────

const CATEGORIES = [
  { key: '所有', color: '#6366f1' },
  { key: '工作', color: '#1d4ed8' },
  { key: '个人', color: '#0e7490' },
  { key: '心愿单', color: '#d97706' },
];

const CATEGORY_COLORS: Record<string, string> = {
  '工作': '#1d4ed8',
  '个人': '#0e7490',
  '心愿单': '#d97706',
};

// ───────── 日期工具 ─────────

function formatDate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

function friendlyDate(dateStr: string | null): string {
  if (!dateStr) return '无日期';
  const today = formatDate(new Date());
  const tomorrow = formatDate(new Date(Date.now() + 86400000));
  if (dateStr === today) return '今天';
  if (dateStr === tomorrow) return '明天';
  // 显示 MM-DD
  return dateStr.slice(5);
}

function getMonthDays(year: number, month: number): { day: number; date: string }[] {
  const days: { day: number; date: string }[] = [];
  const daysInMonth = new Date(year, month + 1, 0).getDate();
  for (let d = 1; d <= daysInMonth; d++) {
    const dateObj = new Date(year, month, d);
    days.push({ day: d, date: formatDate(dateObj) });
  }
  return days;
}

function getFirstDayOfWeek(year: number, month: number): number {
  return new Date(year, month, 1).getDay();
}

const WEEKDAYS = ['日', '一', '二', '三', '四', '五', '六'];
const MONTHS = ['1月', '2月', '3月', '4月', '5月', '6月', '7月', '8月', '9月', '10月', '11月', '12月'];

// ───────── 提醒选项 ─────────

const REMINDER_OPTIONS = [
  { label: '无提醒', value: null },
  { label: '准时', value: 0 },
  { label: '5分钟前', value: 5 },
  { label: '15分钟前', value: 15 },
  { label: '30分钟前', value: 30 },
  { label: '1小时前', value: 60 },
];

// ───────── 主组件 ─────────

export default function CalendarScreen() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [userId, setUserId] = useState('');
  const [activeCategory, setActiveCategory] = useState('所有');
  const [showAddModal, setShowAddModal] = useState(false);
  const [refreshing, setRefreshing] = useState(false);

  // 新任务表单
  const [newTitle, setNewTitle] = useState('');
  const [newCategory, setNewCategory] = useState('个人');
  const [newDueDate, setNewDueDate] = useState<string | null>(null);
  const [newDueTime, setNewDueTime] = useState<string | null>(null);
  const [newReminder, setNewReminder] = useState<number | null>(null);
  const [showCalendar, setShowCalendar] = useState(false);
  const [showTimePicker, setShowTimePicker] = useState(false);
  const [showReminderPicker, setShowReminderPicker] = useState(false);
  const [calYear, setCalYear] = useState(new Date().getFullYear());
  const [calMonth, setCalMonth] = useState(new Date().getMonth());

  // 初始化
  useEffect(() => {
    (async () => {
      // 请求通知权限
      await Notifications.requestPermissionsAsync();

      // Android 8+ 必须创建通知频道，否则通知会被静默丢弃
      if (Platform.OS === 'android') {
        await Notifications.setNotificationChannelAsync('gojo-reminders', {
          name: '五条悟提醒',
          importance: Notifications.AndroidImportance.HIGH,
          sound: 'default',
          vibrationPattern: [0, 250, 250, 250],
        });
      }

      const uid = await AsyncStorage.getItem(USER_ID_KEY);
      if (uid) {
        setUserId(uid);
        await fetchTasks(uid);
      }
      setLoading(false);
    })();
  }, []);

  const fetchTasks = async (uid: string) => {
    try {
      const res = await axios.get(`${SERVER_URL}/tasks?user_id=${uid}`);
      if (res.data?.tasks) {
        setTasks(res.data.tasks);
      }
    } catch (e) {
      console.warn('获取任务失败', e);
    }
  };

  const refresh = async () => {
    setRefreshing(true);
    await fetchTasks(userId);
    setRefreshing(false);
  };

  // 添加任务
  const addTask = async () => {
    const title = newTitle.trim();
    if (!title || !userId) return;
    try {
      await axios.post(`${SERVER_URL}/tasks`, {
        user_id: userId,
        title,
        category: newCategory,
        due_date: newDueDate,
        due_time: newDueTime,
        reminder_minutes: newReminder,
      });

      // 设置本地通知提醒
      if (newDueDate && newDueTime && newReminder !== null) {
        try {
          // 检查权限
          const { status } = await Notifications.getPermissionsAsync();
          if (status !== 'granted') {
            const newStatus = await Notifications.requestPermissionsAsync();
            if (newStatus.status !== 'granted') {
              Alert.alert('通知权限未授予', '任务已添加，但无法设置通知。请到手机设置 → 应用 → GojoAssistant → 通知，开启权限。');
              setNewTitle('');
              setNewDueDate(null);
              setNewDueTime(null);
              setNewReminder(null);
              setShowAddModal(false);
              await fetchTasks(userId);
              return;
            }
          }

          const [hour, minute] = newDueTime.split(':').map(Number);
          const [year, month, day] = newDueDate.split('-').map(Number);
          const dueDate = new Date(year, month - 1, day, hour, minute, 0);
          const triggerDate = new Date(dueDate.getTime() - (newReminder || 0) * 60 * 1000);
          const now = new Date();
          const secondsUntil = Math.floor((triggerDate.getTime() - now.getTime()) / 1000);

          if (secondsUntil > 0) {
            const id = await Notifications.scheduleNotificationAsync({
              content: {
                title: '五条悟提醒你',
                body: title,
                sound: 'default',
                ...(Platform.OS === 'android' ? { channelId: 'gojo-reminders' } : {}),
              },
              trigger: {
                type: Notifications.SchedulableTriggerInputTypes.DATE,
                date: triggerDate,
              } as any,
            });
            Alert.alert(
              '✅ 提醒已设置',
              `${triggerDate.toLocaleString('zh-CN')}\n剩余：${Math.floor(secondsUntil / 60)}分${secondsUntil % 60}秒`
            );
          } else {
            Alert.alert('提醒时间已过', '设置的提醒时间已经过了');
          }
        } catch (notifErr: any) {
          Alert.alert('设置通知失败', String(notifErr?.message || notifErr));
        }
      }

      setNewTitle('');
      setNewDueDate(null);
      setNewDueTime(null);
      setNewReminder(null);
      setShowAddModal(false);
      await fetchTasks(userId);
    } catch (e) {
      Alert.alert('添加失败', '请检查网络连接');
    }
  };

  // 切换完成状态
  const toggleComplete = async (task: Task) => {
    try {
      await axios.put(`${SERVER_URL}/tasks/${task.id}`, {
        completed: !task.completed,
      });
      setTasks(prev =>
        prev.map(t => (t.id === task.id ? { ...t, completed: !t.completed } : t))
      );
    } catch (e) {
      console.warn('更新失败', e);
    }
  };

  // 删除任务
  const deleteTask = (task: Task) => {
    Alert.alert('删除任务', `确认删除「${task.title}」？`, [
      { text: '取消', style: 'cancel' },
      {
        text: '删除',
        style: 'destructive',
        onPress: async () => {
          try {
            await axios.delete(`${SERVER_URL}/tasks/${task.id}`);
            setTasks(prev => prev.filter(t => t.id !== task.id));
          } catch (e) {
            Alert.alert('删除失败');
          }
        },
      },
    ]);
  };

  // 过滤任务
  const filteredTasks = activeCategory === '所有'
    ? tasks
    : tasks.filter(t => t.category === activeCategory);

  // 分组：未完成 + 已完成
  const pendingTasks = filteredTasks.filter(t => !t.completed);
  const completedTasks = filteredTasks.filter(t => t.completed);

  // 快捷日期
  const setQuickDate = (option: string) => {
    const now = new Date();
    switch (option) {
      case '今天':
        setNewDueDate(formatDate(now));
        break;
      case '明天':
        setNewDueDate(formatDate(new Date(now.getTime() + 86400000)));
        break;
      case '3天后':
        setNewDueDate(formatDate(new Date(now.getTime() + 86400000 * 3)));
        break;
      case '无日期':
        setNewDueDate(null);
        break;
    }
    setShowCalendar(false);
  };

  if (loading) {
    return (
      <View style={{ flex: 1, backgroundColor: C.bg, alignItems: 'center', justifyContent: 'center' }}>
        <ActivityIndicator color={C.accent} />
      </View>
    );
  }

  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
      <StatusBar barStyle="light-content" backgroundColor={C.bg} />

      {/* 顶栏 */}
      <View style={s.header}>
        <Text style={s.headerTitle}>日程安排</Text>
        <TouchableOpacity onPress={refresh} style={s.refreshBtn}>
          <Text style={s.refreshText}>{refreshing ? '...' : '刷新'}</Text>
        </TouchableOpacity>
      </View>

      {/* 分类 Tab */}
      <View style={s.categoryBar}>
        {CATEGORIES.map(cat => (
          <TouchableOpacity
            key={cat.key}
            style={[
              s.categoryTab,
              activeCategory === cat.key && { backgroundColor: cat.color + '33', borderColor: cat.color },
            ]}
            onPress={() => setActiveCategory(cat.key)}
          >
            <Text
              style={[
                s.categoryText,
                activeCategory === cat.key && { color: cat.color },
              ]}
            >
              {cat.key}
            </Text>
          </TouchableOpacity>
        ))}
      </View>

      {/* 任务列表 */}
      <ScrollView style={s.taskList} contentContainerStyle={{ paddingBottom: 100 }}>
        {pendingTasks.length === 0 && completedTasks.length === 0 && (
          <View style={s.emptyWrap}>
            <Text style={s.emptyEmoji}>📝</Text>
            <Text style={s.emptyText}>暂无任务，点击下方添加</Text>
          </View>
        )}

        {/* 未完成 */}
        {pendingTasks.map(task => (
          <TouchableOpacity
            key={task.id}
            style={s.taskCard}
            onPress={() => toggleComplete(task)}
            onLongPress={() => deleteTask(task)}
            activeOpacity={0.7}
          >
            <View style={[s.checkbox, { borderColor: CATEGORY_COLORS[task.category] || C.accent }]}>
              {/* 空圆圈 */}
            </View>
            <View style={s.taskInfo}>
              <Text style={s.taskTitle}>{task.title}</Text>
              <View style={s.taskMeta}>
                <View style={[s.categoryBadge, { backgroundColor: (CATEGORY_COLORS[task.category] || C.accent) + '22' }]}>
                  <Text style={[s.categoryBadgeText, { color: CATEGORY_COLORS[task.category] || C.accent }]}>
                    {task.category}
                  </Text>
                </View>
                {task.due_date && (
                  <Text style={s.taskDate}>
                    📅 {friendlyDate(task.due_date)}
                    {task.due_time ? ` ${task.due_time}` : ''}
                  </Text>
                )}
                {task.reminder_minutes !== null && (
                  <Text style={s.taskReminder}>🔔</Text>
                )}
              </View>
            </View>
          </TouchableOpacity>
        ))}

        {/* 已完成 */}
        {completedTasks.length > 0 && (
          <>
            <Text style={s.sectionLabel}>已完成 ({completedTasks.length})</Text>
            {completedTasks.map(task => (
              <TouchableOpacity
                key={task.id}
                style={[s.taskCard, { opacity: 0.5 }]}
                onPress={() => toggleComplete(task)}
                onLongPress={() => deleteTask(task)}
                activeOpacity={0.7}
              >
                <View style={[s.checkbox, s.checkboxDone, { borderColor: CATEGORY_COLORS[task.category] || C.accent }]}>
                  <Text style={{ color: '#fff', fontSize: 12 }}>✓</Text>
                </View>
                <View style={s.taskInfo}>
                  <Text style={[s.taskTitle, s.taskTitleDone]}>{task.title}</Text>
                </View>
              </TouchableOpacity>
            ))}
          </>
        )}
      </ScrollView>

      {/* 添加按钮 */}
      <TouchableOpacity
        style={s.addBtn}
        onPress={() => setShowAddModal(true)}
        activeOpacity={0.8}
      >
        <Text style={s.addBtnText}>＋</Text>
      </TouchableOpacity>

      {/* 添加任务弹窗 */}
      <Modal visible={showAddModal} transparent animationType="slide">
        <Pressable style={s.modalOverlay} onPress={() => setShowAddModal(false)}>
          <Pressable style={s.modalContent} onPress={e => e.stopPropagation()}>
            <Text style={s.modalTitle}>新任务</Text>

            {/* 标题输入 */}
            <TextInput
              style={s.modalInput}
              value={newTitle}
              onChangeText={setNewTitle}
              placeholder="输入任务内容..."
              placeholderTextColor={C.textMute}
              autoFocus
            />

            {/* 分类选择 */}
            <Text style={s.modalLabel}>分类</Text>
            <View style={s.optionRow}>
              {['工作', '个人', '心愿单'].map(cat => (
                <TouchableOpacity
                  key={cat}
                  style={[
                    s.optionBtn,
                    newCategory === cat && {
                      backgroundColor: (CATEGORY_COLORS[cat] || C.accent) + '33',
                      borderColor: CATEGORY_COLORS[cat] || C.accent,
                    },
                  ]}
                  onPress={() => setNewCategory(cat)}
                >
                  <Text
                    style={[
                      s.optionText,
                      newCategory === cat && { color: CATEGORY_COLORS[cat] || C.accent },
                    ]}
                  >
                    {cat}
                  </Text>
                </TouchableOpacity>
              ))}
            </View>

            {/* 日期选择 */}
            <Text style={s.modalLabel}>日期</Text>
            <View style={s.optionRow}>
              {['今天', '明天', '3天后', '无日期'].map(opt => (
                <TouchableOpacity
                  key={opt}
                  style={[
                    s.optionBtn,
                    newDueDate === (opt === '今天' ? formatDate(new Date()) :
                      opt === '明天' ? formatDate(new Date(Date.now() + 86400000)) :
                      opt === '3天后' ? formatDate(new Date(Date.now() + 86400000 * 3)) : null
                    ) && opt !== '无日期' && { backgroundColor: C.accent + '33', borderColor: C.accent },
                    opt === '无日期' && !newDueDate && { backgroundColor: C.accent + '33', borderColor: C.accent },
                  ]}
                  onPress={() => setQuickDate(opt)}
                >
                  <Text style={s.optionText}>{opt}</Text>
                </TouchableOpacity>
              ))}
            </View>

            {/* 日历按钮 */}
            <TouchableOpacity
              style={s.calendarToggle}
              onPress={() => setShowCalendar(!showCalendar)}
            >
              <Text style={s.calendarToggleText}>
                📅 {newDueDate ? `已选：${newDueDate}` : '选择具体日期'}
              </Text>
            </TouchableOpacity>

            {/* 日历视图 */}
            {showCalendar && (
              <View style={s.calendar}>
                <View style={s.calHeader}>
                  <TouchableOpacity onPress={() => {
                    if (calMonth === 0) { setCalMonth(11); setCalYear(calYear - 1); }
                    else setCalMonth(calMonth - 1);
                  }}>
                    <Text style={s.calNav}>◀</Text>
                  </TouchableOpacity>
                  <Text style={s.calTitle}>{calYear}年 {MONTHS[calMonth]}</Text>
                  <TouchableOpacity onPress={() => {
                    if (calMonth === 11) { setCalMonth(0); setCalYear(calYear + 1); }
                    else setCalMonth(calMonth + 1);
                  }}>
                    <Text style={s.calNav}>▶</Text>
                  </TouchableOpacity>
                </View>

                <View style={s.calWeekRow}>
                  {WEEKDAYS.map(w => (
                    <Text key={w} style={s.calWeekDay}>{w}</Text>
                  ))}
                </View>

                <View style={s.calGrid}>
                  {/* 月初空白 */}
                  {Array.from({ length: getFirstDayOfWeek(calYear, calMonth) }).map((_, i) => (
                    <View key={`empty-${i}`} style={s.calCell} />
                  ))}
                  {/* 日期 */}
                  {getMonthDays(calYear, calMonth).map(({ day, date }) => {
                    const isSelected = newDueDate === date;
                    const isToday = date === formatDate(new Date());
                    return (
                      <TouchableOpacity
                        key={date}
                        style={[
                          s.calCell,
                          isSelected && { backgroundColor: C.accent, borderRadius: 20 },
                          isToday && !isSelected && { borderWidth: 1, borderColor: C.accent, borderRadius: 20 },
                        ]}
                        onPress={() => {
                          setNewDueDate(date);
                          setShowCalendar(false);
                        }}
                      >
                        <Text
                          style={[
                            s.calDay,
                            isSelected && { color: '#fff', fontWeight: '700' },
                            isToday && !isSelected && { color: C.accent },
                          ]}
                        >
                          {day}
                        </Text>
                      </TouchableOpacity>
                    );
                  })}
                </View>
              </View>
            )}

            {/* 时间选择 */}
            <Text style={s.modalLabel}>时间</Text>
            <TouchableOpacity
              style={s.calendarToggle}
              onPress={() => setShowTimePicker(true)}
            >
              <Text style={s.calendarToggleText}>
                🕐 {newDueTime || '无'}
              </Text>
            </TouchableOpacity>

            {/* 快捷时间 */}
            <View style={s.optionRow}>
              {['07:00', '09:00', '10:00', '12:00', '14:00', '16:00', '18:00'].map(t => (
                <TouchableOpacity
                  key={t}
                  style={[s.optionBtn, newDueTime === t && { backgroundColor: C.accent + '33', borderColor: C.accent }]}
                  onPress={() => setNewDueTime(t)}
                >
                  <Text style={s.optionText}>{t}</Text>
                </TouchableOpacity>
              ))}
              <TouchableOpacity
                style={[s.optionBtn, !newDueTime && { backgroundColor: C.accent + '33', borderColor: C.accent }]}
                onPress={() => setNewDueTime(null)}
              >
                <Text style={s.optionText}>无时间</Text>
              </TouchableOpacity>
            </View>

            {/* 原生时钟选择器（可旋转选时间） */}
            {showTimePicker && (
              <DateTimePicker
                value={(() => {
                  const d = new Date();
                  if (newDueTime) {
                    const [h, m] = newDueTime.split(':').map(Number);
                    d.setHours(h, m, 0, 0);
                  }
                  return d;
                })()}
                mode="time"
                is24Hour={true}
                display="spinner"
                onChange={(event: any, selectedDate?: Date) => {
                  setShowTimePicker(false);
                  if (event.type === 'set' && selectedDate) {
                    const h = String(selectedDate.getHours()).padStart(2, '0');
                    const m = String(selectedDate.getMinutes()).padStart(2, '0');
                    setNewDueTime(`${h}:${m}`);
                  }
                }}
              />
            )}

            {/* 提醒选择 */}
            <Text style={s.modalLabel}>提醒</Text>
            <TouchableOpacity
              style={s.calendarToggle}
              onPress={() => setShowReminderPicker(!showReminderPicker)}
            >
              <Text style={s.calendarToggleText}>
                🔔 {REMINDER_OPTIONS.find(r => r.value === newReminder)?.label || '无提醒'}
              </Text>
            </TouchableOpacity>

            {showReminderPicker && (
              <View style={s.optionRow}>
                {REMINDER_OPTIONS.map(r => (
                  <TouchableOpacity
                    key={r.label}
                    style={[s.optionBtn, newReminder === r.value && { backgroundColor: C.accent + '33', borderColor: C.accent }]}
                    onPress={() => { setNewReminder(r.value); setShowReminderPicker(false); }}
                  >
                    <Text style={s.optionText}>{r.label}</Text>
                  </TouchableOpacity>
                ))}
              </View>
            )}

            {/* 提交按钮 */}
            <View style={s.modalActions}>
              <TouchableOpacity
                style={s.cancelBtn}
                onPress={() => setShowAddModal(false)}
              >
                <Text style={s.cancelText}>取消</Text>
              </TouchableOpacity>
              <TouchableOpacity
                style={[s.submitBtn, { opacity: newTitle.trim() ? 1 : 0.4 }]}
                onPress={addTask}
                disabled={!newTitle.trim()}
              >
                <Text style={s.submitText}>添加</Text>
              </TouchableOpacity>
            </View>
          </Pressable>
        </Pressable>
      </Modal>
    </View>
  );
}

// ───────── 样式 ─────────

const s = StyleSheet.create({
  header:         { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingHorizontal: 20, paddingTop: Platform.OS === 'ios' ? 54 : 44, paddingBottom: 14, backgroundColor: C.bg },
  headerTitle:    { color: C.text, fontSize: 22, fontWeight: '600' },
  refreshBtn:     { paddingHorizontal: 12, paddingVertical: 6, borderRadius: 10, borderWidth: 1, borderColor: C.border },
  refreshText:    { color: C.textMute, fontSize: 12 },

  categoryBar:    { flexDirection: 'row', paddingHorizontal: 16, gap: 8, marginBottom: 12 },
  categoryTab:    { paddingHorizontal: 16, paddingVertical: 8, borderRadius: 20, borderWidth: 1, borderColor: C.border },
  categoryText:   { color: C.textMute, fontSize: 13 },

  taskList:       { flex: 1, paddingHorizontal: 16 },
  emptyWrap:      { alignItems: 'center', paddingTop: 100 },
  emptyEmoji:     { fontSize: 48, marginBottom: 16 },
  emptyText:      { color: C.textMute, fontSize: 14 },

  taskCard:       { flexDirection: 'row', alignItems: 'center', backgroundColor: C.card, borderRadius: 14, padding: 14, marginBottom: 10 },
  checkbox:       { width: 24, height: 24, borderRadius: 12, borderWidth: 2, alignItems: 'center', justifyContent: 'center', marginRight: 12 },
  checkboxDone:   { backgroundColor: '#22c55e' },
  taskInfo:       { flex: 1 },
  taskTitle:      { color: C.text, fontSize: 15, fontWeight: '500', marginBottom: 4 },
  taskTitleDone:  { textDecorationLine: 'line-through', color: C.textMute },
  taskMeta:       { flexDirection: 'row', alignItems: 'center', gap: 8 },
  categoryBadge:  { paddingHorizontal: 8, paddingVertical: 2, borderRadius: 6 },
  categoryBadgeText: { fontSize: 11 },
  taskDate:       { color: C.textDim, fontSize: 11 },
  taskReminder:   { fontSize: 11 },
  sectionLabel:   { color: C.textMute, fontSize: 12, letterSpacing: 1, marginTop: 16, marginBottom: 8, marginLeft: 4 },

  addBtn:         { position: 'absolute', bottom: 24, right: 24, width: 56, height: 56, borderRadius: 28, backgroundColor: C.accent, alignItems: 'center', justifyContent: 'center', elevation: 4, shadowColor: C.accent, shadowOffset: { width: 0, height: 4 }, shadowOpacity: 0.3, shadowRadius: 8 },
  addBtnText:     { color: '#fff', fontSize: 28, fontWeight: '300', marginTop: -2 },

  modalOverlay:   { flex: 1, backgroundColor: 'rgba(0,0,0,0.6)', justifyContent: 'flex-end' },
  modalContent:   { backgroundColor: C.card, borderTopLeftRadius: 24, borderTopRightRadius: 24, padding: 24, paddingBottom: Platform.OS === 'ios' ? 40 : 24, maxHeight: '90%' },
  modalTitle:     { color: C.text, fontSize: 18, fontWeight: '600', marginBottom: 16 },
  modalInput:     { backgroundColor: C.bg, borderRadius: 12, paddingHorizontal: 16, paddingVertical: 12, color: C.text, fontSize: 15, borderWidth: 1, borderColor: C.border, marginBottom: 16 },
  modalLabel:     { color: C.textMute, fontSize: 12, marginBottom: 8, letterSpacing: 1 },

  optionRow:      { flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginBottom: 12 },
  optionBtn:      { paddingHorizontal: 14, paddingVertical: 8, borderRadius: 16, borderWidth: 1, borderColor: C.border },
  optionText:     { color: C.textMute, fontSize: 13 },

  calendarToggle: { backgroundColor: C.bg, borderRadius: 10, padding: 12, marginBottom: 12, borderWidth: 1, borderColor: C.border },
  calendarToggleText: { color: C.textDim, fontSize: 13 },

  calendar:       { backgroundColor: C.bg, borderRadius: 12, padding: 12, marginBottom: 12, borderWidth: 1, borderColor: C.border },
  calHeader:      { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 },
  calNav:         { color: C.accent, fontSize: 16, paddingHorizontal: 12 },
  calTitle:       { color: C.text, fontSize: 15, fontWeight: '500' },
  calWeekRow:     { flexDirection: 'row', marginBottom: 4 },
  calWeekDay:     { width: (width - 96) / 7, textAlign: 'center', color: C.textMute, fontSize: 12 },
  calGrid:        { flexDirection: 'row', flexWrap: 'wrap' },
  calCell:        { width: (width - 96) / 7, height: 36, alignItems: 'center', justifyContent: 'center' },
  calDay:         { color: C.text, fontSize: 14 },

  modalActions:   { flexDirection: 'row', gap: 12, marginTop: 8 },
  cancelBtn:      { flex: 1, paddingVertical: 14, borderRadius: 14, borderWidth: 1, borderColor: C.border, alignItems: 'center' },
  cancelText:     { color: C.textMute, fontSize: 15 },
  submitBtn:      { flex: 1, paddingVertical: 14, borderRadius: 14, backgroundColor: C.accent, alignItems: 'center' },
  submitText:     { color: '#fff', fontSize: 15, fontWeight: '600' },
});