// app/(tabs)/calendar.tsx — 日程页
import AsyncStorage from '@react-native-async-storage/async-storage';
import React, { useEffect, useState } from 'react';
import { Alert, Modal, Platform, ScrollView, StatusBar, StyleSheet, Text, TextInput, TouchableOpacity, View } from 'react-native';
import { C, dateKey, MONTHS, TAG_COLORS, TAGS, uid, WEEKDAYS } from '../../constants/theme';

const STORAGE_KEY = 'gojo_schedule';

export interface ScheduleItem {
  id: string;
  date: string;
  title: string;
  startTime: string;
  endTime: string;
  tag: string;
}

const today = new Date();

export default function CalendarScreen() {
  const [year,  setYear]  = useState(today.getFullYear());
  const [month, setMonth] = useState(today.getMonth());
  const [selectedDate, setSelectedDate] = useState(dateKey(today.getFullYear(), today.getMonth(), today.getDate()));
  const [items, setItems] = useState<ScheduleItem[]>([]);
  const [showModal, setShowModal] = useState(false);
  const [form, setForm] = useState({ title:'', startTime:'', endTime:'', tag:'其他' });

  useEffect(() => {
    AsyncStorage.getItem(STORAGE_KEY).then(v => { if (v) setItems(JSON.parse(v)); }).catch(() => {});
  }, []);

  useEffect(() => {
    AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(items)).catch(() => {});
  }, [items]);

  const firstDay = new Date(year, month, 1).getDay();
  const daysInMonth = new Date(year, month+1, 0).getDate();
  const cells: (number|null)[] = [...Array(firstDay).fill(null), ...Array.from({ length:daysInMonth }, (_,i) => i+1)];
  while (cells.length % 7 !== 0) cells.push(null);

  const prevMonth = () => { if (month===0) { setYear(y=>y-1); setMonth(11); } else setMonth(m=>m-1); };
  const nextMonth = () => { if (month===11) { setYear(y=>y+1); setMonth(0); } else setMonth(m=>m+1); };

  const hasEvent = (d:number) => items.some(i => i.date===dateKey(year, month, d));
  const dayItems = items.filter(i => i.date===selectedDate).sort((a,b) => a.startTime.localeCompare(b.startTime));
  const todayKey = dateKey(today.getFullYear(), today.getMonth(), today.getDate());

  const selParts = selectedDate.split('-');
  const selLabel = `${parseInt(selParts[1])}月${parseInt(selParts[2])}日`;

  const addItem = () => {
    if (!form.title.trim()) return Alert.alert('提示', '请输入标题');
    setItems(prev => [...prev, { id:uid(), date:selectedDate, ...form }]);
    setForm({ title:'', startTime:'', endTime:'', tag:'其他' });
    setShowModal(false);
  };

  const delItem = (id:string) => Alert.alert('删除', '确认删除这个日程？', [
    { text:'取消', style:'cancel' },
    { text:'删除', style:'destructive', onPress: () => setItems(prev => prev.filter(i => i.id!==id)) },
  ]);

  return (
    <View style={{ flex:1, backgroundColor:C.bg }}>
      <StatusBar barStyle="light-content" backgroundColor={C.bg}/>
      <ScrollView contentContainerStyle={s.content} showsVerticalScrollIndicator={false}>
        <Text style={s.pageTitle}>📅 日程</Text>

        {/* 月份导航 */}
        <View style={s.calNav}>
          <TouchableOpacity onPress={prevMonth} style={s.navBtn}><Text style={s.navArrow}>‹</Text></TouchableOpacity>
          <Text style={s.navTitle}>{year}年 {MONTHS[month]}</Text>
          <TouchableOpacity onPress={nextMonth} style={s.navBtn}><Text style={s.navArrow}>›</Text></TouchableOpacity>
        </View>

        {/* 星期头 */}
        <View style={s.weekRow}>
          {WEEKDAYS.map(w => (
            <Text key={w} style={[s.weekLabel, (w==='日'||w==='六') && { color:C.accent2 }]}>{w}</Text>
          ))}
        </View>

        {/* 日历格子 */}
        <View style={s.grid}>
          {cells.map((d, i) => {
            if (!d) return <View key={i} style={s.cell}/>;
            const key = dateKey(year, month, d);
            const isToday    = key === todayKey;
            const isSelected = key === selectedDate;
            const isWeekend  = (i%7===0 || i%7===6);
            return (
              <TouchableOpacity key={i} style={[s.cell, isSelected && s.cellSelected, isToday && !isSelected && s.cellToday]}
                onPress={() => setSelectedDate(key)}>
                <Text style={[s.dayText, isSelected && s.dayTextSelected, isWeekend && !isSelected && { color:C.accent2 }, isToday && !isSelected && { color:C.accent }]}>{d}</Text>
                {hasEvent(d) && <View style={[s.dot, isSelected && { backgroundColor:'#fff' }]}/>}
              </TouchableOpacity>
            );
          })}
        </View>

        {/* 当天日程 */}
        <View style={s.daySection}>
          <View style={s.daySectionHeader}>
            <Text style={s.daySectionTitle}>{selLabel} 的日程</Text>
            <TouchableOpacity style={s.addMiniBtn} onPress={() => setShowModal(true)}>
              <Text style={s.addMiniBtnText}>＋ 添加</Text>
            </TouchableOpacity>
          </View>
          {dayItems.length===0 && <Text style={s.emptyText}>今天还没有日程，休息一下吧～</Text>}
          {dayItems.map(item => (
            <View key={item.id} style={s.scheduleCard}>
              <View style={[s.scheduleAccent, { backgroundColor:TAG_COLORS[item.tag]||C.accent }]}/>
              <View style={{ flex:1 }}>
                <Text style={s.scheduleTitle}>{item.title}</Text>
                <Text style={s.scheduleTime}>{item.startTime}{item.endTime ? ` - ${item.endTime}` : ''}</Text>
              </View>
              <View style={[s.tag, { backgroundColor:(TAG_COLORS[item.tag]||C.accent)+'22' }]}>
                <Text style={[s.tagText, { color:TAG_COLORS[item.tag]||C.accent }]}>{item.tag}</Text>
              </View>
              <TouchableOpacity onPress={() => delItem(item.id)} style={s.delBtn}>
                <Text style={s.delBtnText}>×</Text>
              </TouchableOpacity>
            </View>
          ))}
        </View>
      </ScrollView>

      {/* 添加弹窗 */}
      <Modal visible={showModal} transparent animationType="slide">
        <View style={s.overlay}>
          <View style={s.modalBox}>
            <Text style={s.modalTitle}>添加日程 · {selLabel}</Text>
            <TextInput style={s.modalInput} placeholder="日程标题" placeholderTextColor={C.textMute}
              value={form.title} onChangeText={v => setForm(f => ({ ...f, title:v }))}/>
            <View style={{ flexDirection:'row', gap:10 }}>
              <TextInput style={[s.modalInput, { flex:1 }]} placeholder="开始 09:00" placeholderTextColor={C.textMute}
                value={form.startTime} onChangeText={v => setForm(f => ({ ...f, startTime:v }))}/>
              <TextInput style={[s.modalInput, { flex:1 }]} placeholder="结束 10:00" placeholderTextColor={C.textMute}
                value={form.endTime} onChangeText={v => setForm(f => ({ ...f, endTime:v }))}/>
            </View>
            <Text style={s.modalLabel}>标签</Text>
            <View style={s.tagRow}>
              {TAGS.map(t => (
                <TouchableOpacity key={t} style={[s.tagChip, form.tag===t && { backgroundColor:TAG_COLORS[t]+'44', borderColor:TAG_COLORS[t] }]}
                  onPress={() => setForm(f => ({ ...f, tag:t }))}>
                  <Text style={[s.tagChipText, form.tag===t && { color:TAG_COLORS[t] }]}>{t}</Text>
                </TouchableOpacity>
              ))}
            </View>
            <View style={s.btnRow}>
              <TouchableOpacity style={s.cancelBtn} onPress={() => setShowModal(false)}>
                <Text style={s.cancelText}>取消</Text>
              </TouchableOpacity>
              <TouchableOpacity style={s.confirmBtn} onPress={addItem}>
                <Text style={s.confirmText}>确定</Text>
              </TouchableOpacity>
            </View>
          </View>
        </View>
      </Modal>
    </View>
  );
}

const s = StyleSheet.create({
  content:          { padding:20, paddingTop:Platform.OS==='ios'?60:50, paddingBottom:40 },
  pageTitle:        { color:C.text, fontSize:22, fontWeight:'700', marginBottom:20 },
  calNav:           { flexDirection:'row', alignItems:'center', justifyContent:'space-between', marginBottom:16 },
  navBtn:           { padding:8 },
  navArrow:         { color:C.accent2, fontSize:24 },
  navTitle:         { color:C.text, fontSize:16, fontWeight:'600' },
  weekRow:          { flexDirection:'row', marginBottom:8 },
  weekLabel:        { flex:1, textAlign:'center', color:C.textMute, fontSize:12 },
  grid:             { flexDirection:'row', flexWrap:'wrap', marginBottom:24 },
  cell:             { width:'14.28%', aspectRatio:1, alignItems:'center', justifyContent:'center' },
  cellSelected:     { backgroundColor:C.accent, borderRadius:20 },
  cellToday:        { borderWidth:1, borderColor:C.accent, borderRadius:20 },
  dayText:          { color:C.text, fontSize:13 },
  dayTextSelected:  { color:'#fff', fontWeight:'700' },
  dot:              { width:4, height:4, borderRadius:2, backgroundColor:C.accent, marginTop:2 },
  daySection:       { backgroundColor:C.card, borderRadius:16, padding:16, borderWidth:1, borderColor:C.border },
  daySectionHeader: { flexDirection:'row', justifyContent:'space-between', alignItems:'center', marginBottom:14 },
  daySectionTitle:  { color:C.text, fontSize:14, fontWeight:'600' },
  emptyText:        { color:C.textMute, fontSize:13, textAlign:'center', paddingVertical:20 },
  scheduleCard:     { flexDirection:'row', alignItems:'center', backgroundColor:C.bg, borderRadius:12, padding:12, marginBottom:10, borderWidth:1, borderColor:C.border },
  scheduleAccent:   { width:3, borderRadius:2, alignSelf:'stretch', marginRight:12 },
  scheduleTitle:    { color:C.text, fontSize:14, fontWeight:'500' },
  scheduleTime:     { color:C.textDim, fontSize:12, marginTop:3 },
  tag:              { borderRadius:8, paddingHorizontal:8, paddingVertical:3, marginLeft:8 },
  tagText:          { fontSize:11, fontWeight:'600' },
  addMiniBtn:       { backgroundColor:C.accent+'22', borderRadius:12, paddingHorizontal:12, paddingVertical:6, borderWidth:1, borderColor:C.accent+'55' },
  addMiniBtnText:   { color:C.accent2, fontSize:12, fontWeight:'600' },
  delBtn:           { width:28, height:28, borderRadius:14, backgroundColor:C.expense+'22', alignItems:'center', justifyContent:'center', marginLeft:4 },
  delBtnText:       { color:C.expense, fontSize:16, fontWeight:'700' },
  overlay:          { flex:1, backgroundColor:'#00000088', justifyContent:'flex-end' },
  modalBox:         { backgroundColor:C.card, borderTopLeftRadius:24, borderTopRightRadius:24, padding:24, paddingBottom:40 },
  modalTitle:       { color:C.text, fontSize:18, fontWeight:'700', marginBottom:20, textAlign:'center' },
  modalInput:       { backgroundColor:C.bg, borderRadius:12, paddingHorizontal:16, paddingVertical:12, color:C.text, fontSize:14, borderWidth:1, borderColor:C.border, marginBottom:12 },
  modalLabel:       { color:C.textMute, fontSize:11, letterSpacing:1, marginBottom:8 },
  tagRow:           { flexDirection:'row', flexWrap:'wrap', gap:8, marginBottom:20 },
  tagChip:          { borderRadius:20, paddingHorizontal:14, paddingVertical:6, borderWidth:1, borderColor:C.border },
  tagChipText:      { color:C.textDim, fontSize:12 },
  btnRow:           { flexDirection:'row', gap:12 },
  cancelBtn:        { flex:1, backgroundColor:C.bg, borderRadius:14, paddingVertical:14, alignItems:'center', borderWidth:1, borderColor:C.border },
  cancelText:       { color:C.textDim, fontWeight:'600' },
  confirmBtn:       { flex:1, backgroundColor:C.accent, borderRadius:14, paddingVertical:14, alignItems:'center' },
  confirmText:      { color:'#fff', fontWeight:'700' },
});