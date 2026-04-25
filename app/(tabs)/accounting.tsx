// app/(tabs)/accounting.tsx — 记账页
import AsyncStorage from '@react-native-async-storage/async-storage';
import React, { useEffect, useState } from 'react';
import { Alert, Modal, Platform, ScrollView, StatusBar, StyleSheet, Text, TextInput, TouchableOpacity, View } from 'react-native';
import { C, CATEGORIES, todayStr, uid } from '../../constants/theme';

const STORAGE_KEY = 'gojo_accounting';

export interface AccountRecord {
  id: string;
  type: 'in' | 'out';
  category: string;
  desc: string;
  amount: number;
  date: string;
}

export default function AccountingScreen() {
  const [records, setRecords] = useState<AccountRecord[]>([]);
  const [showModal, setShowModal] = useState(false);
  const [form, setForm] = useState({ type:'out' as 'in'|'out', category:'餐饮', desc:'', amount:'' });

  useEffect(() => {
    AsyncStorage.getItem(STORAGE_KEY).then(v => { if (v) setRecords(JSON.parse(v)); }).catch(() => {});
  }, []);

  useEffect(() => {
    AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(records)).catch(() => {});
  }, [records]);

  const totalIn  = records.filter(r => r.type==='in').reduce((s,r) => s+r.amount, 0);
  const totalOut = records.filter(r => r.type==='out').reduce((s,r) => s+r.amount, 0);
  const balance  = totalIn - totalOut;

  const catStats: Record<string,number> = {};
  records.filter(r => r.type==='out').forEach(r => { catStats[r.category] = (catStats[r.category]||0) + r.amount; });
  const catList = Object.entries(catStats).sort((a,b) => b[1]-a[1]);

  const addRecord = () => {
    const amt = parseFloat(form.amount);
    if (!form.desc.trim()) return Alert.alert('提示', '请输入描述');
    if (isNaN(amt) || amt <= 0) return Alert.alert('提示', '请输入正确金额');
    setRecords(prev => [{ id:uid(), type:form.type, category:form.category, desc:form.desc, amount:amt, date:todayStr() }, ...prev]);
    setForm({ type:'out', category:'餐饮', desc:'', amount:'' });
    setShowModal(false);
  };

  const delRecord = (id:string) => Alert.alert('删除', '确认删除这条记录？', [
    { text:'取消', style:'cancel' },
    { text:'删除', style:'destructive', onPress: () => setRecords(prev => prev.filter(r => r.id!==id)) },
  ]);

  return (
    <View style={{ flex:1, backgroundColor:C.bg }}>
      <StatusBar barStyle="light-content" backgroundColor={C.bg}/>
      <ScrollView contentContainerStyle={s.content} showsVerticalScrollIndicator={false}>
        <Text style={s.pageTitle}>💰 记账本</Text>

        {/* 余额卡 */}
        <View style={s.balanceCard}>
          <Text style={s.balanceLabel}>本月结余</Text>
          <Text style={[s.balanceAmount, { color: balance>=0 ? C.income : C.expense }]}>
            {balance>=0?'+':''}{balance.toFixed(2)}
            <Text style={{ fontSize:14 }}> 元</Text>
          </Text>
          <View style={s.balanceRow}>
            <View style={s.balanceItem}>
              <Text style={s.balanceItemLabel}>收入</Text>
              <Text style={[s.balanceItemVal, { color:C.income }]}>+¥{totalIn.toFixed(2)}</Text>
            </View>
            <View style={s.balanceDivider}/>
            <View style={s.balanceItem}>
              <Text style={s.balanceItemLabel}>支出</Text>
              <Text style={[s.balanceItemVal, { color:C.expense }]}>-¥{totalOut.toFixed(2)}</Text>
            </View>
          </View>
        </View>

        {/* 支出分类 */}
        {catList.length > 0 && (
          <>
            <Text style={s.sectionLabel}>支出分类</Text>
            <View style={s.catCard}>
              {catList.map(([cat, amt]) => (
                <View key={cat} style={s.catRow}>
                  <Text style={s.catLabel}>{cat}</Text>
                  <View style={s.catBarWrap}>
                    <View style={[s.catBar, { width: `${Math.min((amt/totalOut)*100, 100)}%` as any }]}/>
                  </View>
                  <Text style={s.catAmt}>¥{amt.toFixed(0)}</Text>
                </View>
              ))}
            </View>
          </>
        )}

        {/* 明细记录 */}
        <View style={s.recordHeader}>
          <Text style={s.sectionLabel}>明细记录</Text>
          <TouchableOpacity style={s.addMiniBtn} onPress={() => setShowModal(true)}>
            <Text style={s.addMiniBtnText}>＋ 添加</Text>
          </TouchableOpacity>
        </View>
        {records.length===0 && <Text style={s.emptyText}>还没有记录，快来记一笔吧～</Text>}
        {records.map(r => (
          <View key={r.id} style={s.recordRow}>
            <View style={[s.recordIcon, { backgroundColor:r.type==='in' ? C.income+'22' : C.expense+'22' }]}>
              <Text style={{ fontSize:18 }}>{r.type==='in' ? '📥' : '📤'}</Text>
            </View>
            <View style={{ flex:1 }}>
              <Text style={s.recordDesc}>{r.desc}</Text>
              <Text style={s.recordCat}>{r.category} · {r.date}</Text>
            </View>
            <Text style={[s.recordAmt, { color:r.type==='in' ? C.income : C.expense }]}>
              {r.type==='in' ? '+' : '-'}¥{r.amount.toFixed(2)}
            </Text>
            <TouchableOpacity onPress={() => delRecord(r.id)} style={s.delBtn}>
              <Text style={s.delBtnText}>×</Text>
            </TouchableOpacity>
          </View>
        ))}
      </ScrollView>

      {/* 添加弹窗 */}
      <Modal visible={showModal} transparent animationType="slide">
        <View style={s.overlay}>
          <View style={s.modalBox}>
            <Text style={s.modalTitle}>添加记录</Text>
            <View style={s.typeSwitch}>
              {(['out','in'] as const).map(t => (
                <TouchableOpacity key={t} style={[s.typeSwitchBtn, form.type===t && { backgroundColor:t==='in' ? C.income : C.expense }]}
                  onPress={() => setForm(f => ({ ...f, type:t }))}>
                  <Text style={[s.typeSwitchText, form.type===t && { color:'#fff' }]}>{t==='in' ? '收入' : '支出'}</Text>
                </TouchableOpacity>
              ))}
            </View>
            <TextInput style={s.modalInput} placeholder="描述（如：奶茶）" placeholderTextColor={C.textMute}
              value={form.desc} onChangeText={v => setForm(f => ({ ...f, desc:v }))}/>
            <TextInput style={s.modalInput} placeholder="金额（如：18）" placeholderTextColor={C.textMute}
              value={form.amount} onChangeText={v => setForm(f => ({ ...f, amount:v }))} keyboardType="decimal-pad"/>
            <Text style={s.modalLabel}>分类</Text>
            <View style={s.tagRow}>
              {CATEGORIES.map(c => (
                <TouchableOpacity key={c} style={[s.tagChip, form.category===c && { backgroundColor:C.accent+'44', borderColor:C.accent }]}
                  onPress={() => setForm(f => ({ ...f, category:c }))}>
                  <Text style={[s.tagChipText, form.category===c && { color:C.accent2 }]}>{c}</Text>
                </TouchableOpacity>
              ))}
            </View>
            <View style={s.btnRow}>
              <TouchableOpacity style={s.cancelBtn} onPress={() => setShowModal(false)}>
                <Text style={s.cancelText}>取消</Text>
              </TouchableOpacity>
              <TouchableOpacity style={s.confirmBtn} onPress={addRecord}>
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
  content:        { padding:20, paddingTop:Platform.OS==='ios'?60:50, paddingBottom:40 },
  pageTitle:      { color:C.text, fontSize:22, fontWeight:'700', marginBottom:20 },
  sectionLabel:   { color:C.textMute, fontSize:11, letterSpacing:2, marginBottom:8, marginTop:4 },
  balanceCard:    { backgroundColor:C.card2, borderRadius:20, padding:24, marginBottom:20, borderWidth:1, borderColor:C.border },
  balanceLabel:   { color:C.textDim, fontSize:12, marginBottom:8 },
  balanceAmount:  { fontSize:36, fontWeight:'700', marginBottom:20 },
  balanceRow:     { flexDirection:'row', alignItems:'center' },
  balanceItem:    { flex:1, alignItems:'center' },
  balanceItemLabel:{ color:C.textMute, fontSize:11, marginBottom:4 },
  balanceItemVal: { fontSize:16, fontWeight:'600' },
  balanceDivider: { width:1, height:32, backgroundColor:C.border },
  catCard:        { backgroundColor:C.card, borderRadius:16, padding:16, marginBottom:16, borderWidth:1, borderColor:C.border },
  catRow:         { flexDirection:'row', alignItems:'center', marginBottom:12 },
  catLabel:       { color:C.textDim, fontSize:12, width:36 },
  catBarWrap:     { flex:1, height:6, backgroundColor:C.border, borderRadius:3, marginHorizontal:10 },
  catBar:         { height:6, backgroundColor:C.accent, borderRadius:3 },
  catAmt:         { color:C.text, fontSize:12, width:48, textAlign:'right' },
  recordHeader:   { flexDirection:'row', justifyContent:'space-between', alignItems:'center', marginTop:8 },
  emptyText:      { color:C.textMute, fontSize:13, textAlign:'center', paddingVertical:24 },
  recordRow:      { flexDirection:'row', alignItems:'center', backgroundColor:C.card, borderRadius:14, padding:14, marginBottom:10, borderWidth:1, borderColor:C.border },
  recordIcon:     { width:40, height:40, borderRadius:10, alignItems:'center', justifyContent:'center', marginRight:12 },
  recordDesc:     { color:C.text, fontSize:14, fontWeight:'500' },
  recordCat:      { color:C.textDim, fontSize:12, marginTop:2 },
  recordAmt:      { fontSize:15, fontWeight:'700', marginRight:8 },
  addMiniBtn:     { backgroundColor:C.accent+'22', borderRadius:12, paddingHorizontal:12, paddingVertical:6, borderWidth:1, borderColor:C.accent+'55' },
  addMiniBtnText: { color:C.accent2, fontSize:12, fontWeight:'600' },
  delBtn:         { width:28, height:28, borderRadius:14, backgroundColor:C.expense+'22', alignItems:'center', justifyContent:'center' },
  delBtnText:     { color:C.expense, fontSize:16, fontWeight:'700' },
  overlay:        { flex:1, backgroundColor:'#00000088', justifyContent:'flex-end' },
  modalBox:       { backgroundColor:C.card, borderTopLeftRadius:24, borderTopRightRadius:24, padding:24, paddingBottom:40 },
  modalTitle:     { color:C.text, fontSize:18, fontWeight:'700', marginBottom:20, textAlign:'center' },
  typeSwitch:     { flexDirection:'row', backgroundColor:C.bg, borderRadius:12, padding:4, marginBottom:16 },
  typeSwitchBtn:  { flex:1, paddingVertical:8, borderRadius:10, alignItems:'center' },
  typeSwitchText: { color:C.textDim, fontSize:14, fontWeight:'600' },
  modalInput:     { backgroundColor:C.bg, borderRadius:12, paddingHorizontal:16, paddingVertical:12, color:C.text, fontSize:14, borderWidth:1, borderColor:C.border, marginBottom:12 },
  modalLabel:     { color:C.textMute, fontSize:11, letterSpacing:1, marginBottom:8 },
  tagRow:         { flexDirection:'row', flexWrap:'wrap', gap:8, marginBottom:20 },
  tagChip:        { borderRadius:20, paddingHorizontal:14, paddingVertical:6, borderWidth:1, borderColor:C.border },
  tagChipText:    { color:C.textDim, fontSize:12 },
  btnRow:         { flexDirection:'row', gap:12 },
  cancelBtn:      { flex:1, backgroundColor:C.bg, borderRadius:14, paddingVertical:14, alignItems:'center', borderWidth:1, borderColor:C.border },
  cancelText:     { color:C.textDim, fontWeight:'600' },
  confirmBtn:     { flex:1, backgroundColor:C.accent, borderRadius:14, paddingVertical:14, alignItems:'center' },
  confirmText:    { color:'#fff', fontWeight:'700' },
});