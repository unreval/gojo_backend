// app/diary/mine.tsx
// 「我的日记」:手写信纸质感 + 书名可改 + 写日记(可见/私密+密码)+ 他的访客记号。
//   ★ 不对称:他偷看你日记必留访客记号;🔓=他"猜对密码"解开了私密篇(大事件)。
// ★ v2 键盘修复:去掉 KeyboardAvoidingView,用手动监听键盘高度给 modal 加 marginBottom
//   (MIUI/Redmi 上 KeyboardAvoidingView 的 'height' behavior 会怪异挤压 modal)
import axios from 'axios';
import { useFonts } from 'expo-font';
import { useFocusEffect, useRouter } from 'expo-router';
import React, { useCallback, useEffect, useState } from 'react';
import {
  ActivityIndicator, Alert, Dimensions, Keyboard, Modal, Platform, RefreshControl,
  ScrollView, StatusBar, StyleSheet, Text, TextInput, TouchableOpacity, View,
} from 'react-native';
import { SERVER_URL } from '../../constants/theme';

const FIXED_USER_ID = 'user_mofpiyd7442ia7';
const HAND = 'LongCang';

interface Visit { character_id: string; unlocked: boolean; visited_at: string | null; }
interface UserDiary {
  id: number; content: string; visibility: 'open' | 'locked';
  has_password: boolean; created_at: string | null; visits: Visit[];
}

// ★ 键盘高度 hook —— MIUI 也稳
function useKeyboardHeight() {
  const [h, setH] = useState(0);
  useEffect(() => {
    const screenH = Dimensions.get('window').height;
    const showEvt = Platform.OS === 'ios' ? 'keyboardWillShow' : 'keyboardDidShow';
    const hideEvt = Platform.OS === 'ios' ? 'keyboardWillHide' : 'keyboardDidHide';
    const showSub = Keyboard.addListener(showEvt, e => {
      const screenY = e.endCoordinates.screenY ?? 0;
      const reportedH = e.endCoordinates.height ?? 0;
      setH(screenY > 0 ? Math.max(screenH - screenY, reportedH) : reportedH);
    });
    const hideSub = Keyboard.addListener(hideEvt, () => setH(0));
    return () => { showSub.remove(); hideSub.remove(); };
  }, []);
  return h;
}

function fmt(ts: string | null): string {
  if (!ts) return '';
  const d = new Date(ts.replace(' ', 'T') + (ts.includes('Z') ? '' : 'Z'));
  if (isNaN(d.getTime())) return ts.slice(5, 16);
  return `${d.getMonth() + 1}月${d.getDate()}日 ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

export default function MyDiaryScreen() {
  const router = useRouter();
  const [fontsLoaded] = useFonts({ [HAND]: require('../../assets/fonts/LongCang-Regular.ttf') });
  const hand = fontsLoaded ? { fontFamily: HAND } : {};
  const kbH = useKeyboardHeight();   // ★ 供三个 modal 共用

  const [bookTitle, setBookTitle] = useState('我的日记');
  const [diaries, setDiaries] = useState<UserDiary[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const [showWrite, setShowWrite] = useState(false);
  const [content, setContent] = useState('');
  const [locked, setLocked] = useState(false);
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const [renaming, setRenaming] = useState(false);
  const [newTitle, setNewTitle] = useState('');

  const [pwModalFor, setPwModalFor] = useState<UserDiary | null>(null);
  const [newPw, setNewPw] = useState('');

  const load = async () => {
    try {
      const [bookRes, listRes] = await Promise.all([
        axios.get(`${SERVER_URL}/diary/book/user`, { params: { user_id: FIXED_USER_ID } }),
        axios.get(`${SERVER_URL}/diary/user`, { params: { user_id: FIXED_USER_ID } }),
      ]);
      setBookTitle(bookRes.data?.title || '我的日记');
      setDiaries(listRes.data?.diaries || []);
    } catch (e: any) { console.warn('load my diary', e?.message); }
  };

  useFocusEffect(useCallback(() => {
    (async () => { setLoading(true); await load(); setLoading(false); })();
  }, []));

  const onRefresh = async () => { setRefreshing(true); await load(); setRefreshing(false); };

  const submit = async () => {
    if (!content.trim()) { Alert.alert('提示', '写点什么再发'); return; }
    if (locked && !password.trim()) { Alert.alert('提示', '私密日记要设个密码'); return; }
    setSubmitting(true);
    try {
      await axios.post(`${SERVER_URL}/diary/user`, {
        user_id: FIXED_USER_ID, content: content.trim(),
        visibility: locked ? 'locked' : 'open',
        password: locked ? password.trim() : null,
      });
      setContent(''); setPassword(''); setLocked(false); setShowWrite(false);
      await load();
    } catch (e: any) { Alert.alert('发布失败', e?.message ?? '请重试'); }
    finally { setSubmitting(false); }
  };

  const submitRename = async () => {
    if (!newTitle.trim()) return;
    try {
      await axios.put(`${SERVER_URL}/diary/book/user`, { user_id: FIXED_USER_ID, title: newTitle.trim() });
      setRenaming(false); setNewTitle('');
      await load();
    } catch (e: any) { Alert.alert('改名失败', e?.message ?? '请重试'); }
  };

  const deleteDiary = (d: UserDiary) => {
    Alert.alert('删除这篇', d.content.slice(0, 30) + '…', [
      { text: '取消', style: 'cancel' },
      { text: '删除', style: 'destructive', onPress: async () => {
        try {
          await axios.delete(`${SERVER_URL}/diary/user/${d.id}`, { params: { user_id: FIXED_USER_ID } });
          await load();
        } catch (e: any) { Alert.alert('删除失败', e?.message ?? '请重试'); }
      }},
    ]);
  };

  const changeLock = (d: UserDiary) => {
    if (d.visibility === 'locked') {
      Alert.alert('这篇是私密的', '要改成给他看,还是改密码?', [
        { text: '改成给他看', onPress: async () => {
          await axios.post(`${SERVER_URL}/diary/user/${d.id}/password`, { user_id: FIXED_USER_ID, password: '' });
          await load();
        }},
        { text: '改密码', onPress: () => { setPwModalFor(d); setNewPw(''); } },
        { text: '取消', style: 'cancel' },
      ]);
    } else {
      Alert.alert('这篇他能看到', '要把它设成私密(上锁)吗?', [
        { text: '设为私密', onPress: () => { setPwModalFor(d); setNewPw(''); } },
        { text: '取消', style: 'cancel' },
      ]);
    }
  };

  const submitNewPassword = async () => {
    if (!pwModalFor || !newPw.trim()) return;
    try {
      await axios.post(`${SERVER_URL}/diary/user/${pwModalFor.id}/password`, {
        user_id: FIXED_USER_ID, password: newPw.trim(),
      });
      setPwModalFor(null); setNewPw('');
      await load();
    } catch (e: any) { Alert.alert('设置失败', e?.message ?? '请重试'); }
  };

  // ★ 清空整本"我的日记"(带上他的访客记号一起清)
  const clearAll = () => {
    Alert.alert('清空整本我的日记?', `会把「${bookTitle}」里的所有 ${diaries.length} 篇日记(和他留下的访客记号)全部删除,不能恢复。`, [
      { text: '取消', style: 'cancel' },
      { text: '全部清空', style: 'destructive', onPress: async () => {
        try {
          await axios.delete(`${SERVER_URL}/diary/user_all`, {
            params: { user_id: FIXED_USER_ID },
          });
          await load();
        } catch (e: any) { Alert.alert('清空失败', e?.message ?? '请重试'); }
      }},
    ]);
  };

  return (
    <View style={{ flex: 1, backgroundColor: '#e8ddc7' }}>
      <StatusBar barStyle="dark-content" backgroundColor="#e8ddc7" />

      <View style={s.header}>
        <TouchableOpacity onPress={() => router.back()} style={s.backBtn}>
          <Text style={s.backText}>‹</Text>
        </TouchableOpacity>
        <TouchableOpacity
          style={{ flex: 1, alignItems: 'center' }}
          onPress={() => { setNewTitle(bookTitle); setRenaming(true); }}
          activeOpacity={0.7}
        >
          <Text style={[s.bookTitle, hand]} numberOfLines={1}>{bookTitle}</Text>
          <Text style={s.bookHint}>点标题可改名 · 他会偷看</Text>
        </TouchableOpacity>
        <TouchableOpacity
          onPress={clearAll}
          disabled={diaries.length === 0}
          style={s.clearHeaderBtn}
          activeOpacity={0.7}
        >
          <Text style={[s.clearHeaderText, diaries.length === 0 && { opacity: 0.3 }]}>🗑</Text>
        </TouchableOpacity>
        <TouchableOpacity style={s.writeBtn} onPress={() => setShowWrite(true)}>
          <Text style={s.writeBtnText}>✎</Text>
        </TouchableOpacity>
      </View>

      {loading ? (
        <View style={s.center}><ActivityIndicator color="#8a7a5c" /></View>
      ) : (
        <ScrollView
          contentContainerStyle={{ padding: 16, paddingBottom: 40 }}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor="#8a7a5c" />}
        >
          {diaries.length === 0 && (
            <View style={s.empty}>
              <Text style={s.emptyEmoji}>🖊</Text>
              <Text style={[s.emptyText, hand]}>还没写日记。{'\n'}写一篇,看他会不会偷偷来看。</Text>
            </View>
          )}

          {diaries.map(d => (
            <View key={d.id} style={s.paper}>
              <View style={s.paperTop}>
                <Text style={s.lockTag}>{d.visibility === 'locked' ? '🔒 私密' : '👁 他能看'}</Text>
                <Text style={s.date}>{fmt(d.created_at)}</Text>
              </View>
              <Text style={[s.diaryText, hand]}>{d.content}</Text>

              {d.visits.length > 0 && (
                <View style={s.visitBox}>
                  {d.visits.map((v, i) => (
                    <Text key={i} style={[s.visitText, v.unlocked && s.visitUnlocked]}>
                      {v.unlocked ? '🔓 他解开了这篇私密日记' : '👀 他看过'} · {fmt(v.visited_at)}
                    </Text>
                  ))}
                </View>
              )}

              <View style={s.actionRow}>
                <TouchableOpacity onPress={() => changeLock(d)}>
                  <Text style={s.actionText}>{d.visibility === 'locked' ? '改锁' : '上锁'}</Text>
                </TouchableOpacity>
                <TouchableOpacity onPress={() => deleteDiary(d)}>
                  <Text style={[s.actionText, { color: '#c0553e' }]}>删除</Text>
                </TouchableOpacity>
              </View>
            </View>
          ))}
        </ScrollView>
      )}

      {/* 写日记 —— ★ 键盘弹起时整个 modalCard 上抬 */}
      <Modal visible={showWrite} animationType="slide" transparent statusBarTranslucent>
        <View style={s.modalBackdrop}>
          <View style={[s.modalCard, { marginBottom: kbH }]}>
            <Text style={s.modalTitle}>写日记</Text>
            <TextInput
              style={[s.modalInput, s.writeArea, hand]} value={content} onChangeText={setContent}
              placeholder="今天想记点什么…" placeholderTextColor="#b3a48a" multiline autoFocus
            />
            <TouchableOpacity style={s.lockToggle} onPress={() => setLocked(v => !v)}>
              <View style={[s.checkbox, locked && s.checkboxOn]}>
                <Text style={{ color: locked ? '#fff' : 'transparent', fontSize: 13 }}>✓</Text>
              </View>
              <Text style={s.lockToggleText}>设为私密(上锁)—— 他默认看不到,除非"猜对密码"</Text>
            </TouchableOpacity>
            {locked && (
              <TextInput
                style={[s.modalInput, { minHeight: 0, marginTop: 10 }]} value={password} onChangeText={setPassword}
                placeholder="给这篇设个密码" placeholderTextColor="#b3a48a"
              />
            )}
            <View style={s.modalBtnRow}>
              <TouchableOpacity style={[s.modalBtn, s.ghost]} onPress={() => setShowWrite(false)} disabled={submitting}>
                <Text style={s.ghostText}>取消</Text>
              </TouchableOpacity>
              <TouchableOpacity style={[s.modalBtn, s.primary]} onPress={submit} disabled={submitting}>
                {submitting ? <ActivityIndicator color="#fff" /> : <Text style={s.primaryText}>发布</Text>}
              </TouchableOpacity>
            </View>
          </View>
        </View>
      </Modal>

      {/* 改书名 —— ★ 同款键盘上抬 */}
      <Modal visible={renaming} animationType="fade" transparent statusBarTranslucent>
        <View style={s.modalBackdrop}>
          <View style={[s.modalCard, { marginBottom: kbH }]}>
            <Text style={s.modalTitle}>给这本日记改个名</Text>
            <TextInput
              style={[s.modalInput, { minHeight: 0 }]} value={newTitle} onChangeText={setNewTitle}
              placeholder="日记本的名字" placeholderTextColor="#b3a48a" autoFocus maxLength={20}
            />
            <View style={s.modalBtnRow}>
              <TouchableOpacity style={[s.modalBtn, s.ghost]} onPress={() => setRenaming(false)}>
                <Text style={s.ghostText}>取消</Text>
              </TouchableOpacity>
              <TouchableOpacity style={[s.modalBtn, s.primary]} onPress={submitRename} disabled={!newTitle.trim()}>
                <Text style={s.primaryText}>改名</Text>
              </TouchableOpacity>
            </View>
          </View>
        </View>
      </Modal>

      {/* 改密码 —— ★ 同款键盘上抬 */}
      <Modal visible={!!pwModalFor} animationType="fade" transparent statusBarTranslucent>
        <View style={s.modalBackdrop}>
          <View style={[s.modalCard, { marginBottom: kbH }]}>
            <Text style={s.modalTitle}>设置密码</Text>
            <TextInput
              style={[s.modalInput, { minHeight: 0 }]} value={newPw} onChangeText={setNewPw}
              placeholder="输入新密码" placeholderTextColor="#b3a48a" autoFocus
            />
            <View style={s.modalBtnRow}>
              <TouchableOpacity style={[s.modalBtn, s.ghost]} onPress={() => setPwModalFor(null)}>
                <Text style={s.ghostText}>取消</Text>
              </TouchableOpacity>
              <TouchableOpacity style={[s.modalBtn, s.primary]} onPress={submitNewPassword} disabled={!newPw.trim()}>
                <Text style={s.primaryText}>确定</Text>
              </TouchableOpacity>
            </View>
          </View>
        </View>
      </Modal>
    </View>
  );
}

const s = StyleSheet.create({
  header: { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 12, paddingTop: Platform.OS === 'ios' ? 52 : 42, paddingBottom: 12 },
  backBtn: { width: 34, paddingVertical: 4 },
  backText: { color: '#5a4d33', fontSize: 30, lineHeight: 32, fontWeight: '300' },
  bookTitle: { color: '#3d3320', fontSize: 26, letterSpacing: 2 },
  bookHint: { color: '#a5946f', fontSize: 10, marginTop: 1 },
  writeBtn: { width: 34, height: 34, borderRadius: 17, alignItems: 'center', justifyContent: 'center', backgroundColor: '#c0a058' },
  writeBtnText: { color: '#fff', fontSize: 16 },
  clearHeaderBtn: { width: 34, paddingVertical: 4, alignItems: 'center', marginRight: 4 },
  clearHeaderText: { fontSize: 18 },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  empty: { alignItems: 'center', paddingTop: 90 },
  emptyEmoji: { fontSize: 44, marginBottom: 14 },
  emptyText: { color: '#8a7a5c', fontSize: 22, textAlign: 'center', lineHeight: 34 },

  paper: {
    backgroundColor: '#fffdf5', borderRadius: 8, padding: 18, marginBottom: 16,
    borderWidth: 1, borderColor: '#e6dcc2',
    shadowColor: '#000', shadowOpacity: 0.08, shadowRadius: 6, shadowOffset: { width: 0, height: 2 }, elevation: 2,
  },
  paperTop: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 },
  lockTag: { color: '#9a824e', fontSize: 12, fontWeight: '600' },
  date: { color: '#a99a76', fontSize: 12 },
  diaryText: { color: '#3d3320', fontSize: 22, lineHeight: 40 },

  visitBox: { marginTop: 14, paddingTop: 10, borderTopWidth: 1, borderTopColor: '#efe7d2', gap: 5 },
  visitText: { color: '#b07d3c', fontSize: 13 },
  visitUnlocked: { color: '#d9820a', fontWeight: '700' },

  actionRow: { flexDirection: 'row', gap: 18, marginTop: 12 },
  actionText: { color: '#a5946f', fontSize: 12 },

  modalBackdrop: { flex: 1, backgroundColor: 'rgba(0,0,0,0.4)', justifyContent: 'flex-end' },
  modalCard: { backgroundColor: '#fffdf5', paddingHorizontal: 20, paddingTop: 18, paddingBottom: 30, borderTopLeftRadius: 22, borderTopRightRadius: 22 },
  modalTitle: { color: '#3d3320', fontSize: 16, fontWeight: '700', marginBottom: 12 },
  modalInput: { backgroundColor: '#f6f0df', borderRadius: 12, borderWidth: 1, borderColor: '#e0d4b4', paddingHorizontal: 14, paddingVertical: 12, color: '#3d3320', fontSize: 15, minHeight: 60, textAlignVertical: 'top' },
  writeArea: { minHeight: 120, fontSize: 22, lineHeight: 38 },
  lockToggle: { flexDirection: 'row', alignItems: 'center', marginTop: 14, gap: 10 },
  checkbox: { width: 22, height: 22, borderRadius: 6, borderWidth: 1.5, borderColor: '#d8c9a3', alignItems: 'center', justifyContent: 'center' },
  checkboxOn: { backgroundColor: '#c0a058', borderColor: '#c0a058' },
  lockToggleText: { color: '#8a7a5c', fontSize: 12, flex: 1, lineHeight: 17 },
  modalBtnRow: { flexDirection: 'row', gap: 10, marginTop: 18 },
  modalBtn: { flex: 1, paddingVertical: 12, borderRadius: 14, alignItems: 'center' },
  ghost: { borderWidth: 1, borderColor: '#d8c9a3' },
  ghostText: { color: '#9a824e', fontSize: 14 },
  primary: { backgroundColor: '#c0a058' },
  primaryText: { color: '#fff', fontSize: 14, fontWeight: '600' },
});