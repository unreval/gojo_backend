// app/diary/mine.tsx
// 「我的日记」页：你写日记，选给他看/私密（私密带密码=剧情机关，他猜对能解锁）。
//   ★ 不对称核心：他偷看你日记【必留访客记号】——每篇下方显示"他 X月X日 03:14 看过"。
//     若记号标了🔓，就是他"猜对密码"解开了你上锁的私密篇（大事件）。
//   入口：从聊天页头部 📔 旁边，或首页入口 → router.push('/diary/mine')。
import axios from 'axios';
import { useRouter, useFocusEffect } from 'expo-router';
import React, { useCallback, useState } from 'react';
import {
  ActivityIndicator, Alert, Modal, Platform, RefreshControl,
  ScrollView, StatusBar, StyleSheet, Text, TextInput, TouchableOpacity, View,
} from 'react-native';
import { C, SERVER_URL } from '../../constants/theme';

const FIXED_USER_ID = 'user_mofpiyd7442ia7';

interface Visit { character_id: string; unlocked: boolean; visited_at: string | null; }
interface UserDiary {
  id: number;
  content: string;
  visibility: 'open' | 'locked';
  has_password: boolean;
  created_at: string | null;
  visits: Visit[];
}

function fmt(ts: string | null): string {
  if (!ts) return '';
  const d = new Date(ts.replace(' ', 'T') + (ts.includes('Z') ? '' : 'Z'));
  if (isNaN(d.getTime())) return ts.slice(5, 16);
  const mo = d.getMonth() + 1, da = d.getDate();
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${mo}月${da}日 ${hh}:${mm}`;
}

export default function MyDiaryScreen() {
  const router = useRouter();
  const [diaries, setDiaries] = useState<UserDiary[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  // 写日记模态
  const [showWrite, setShowWrite] = useState(false);
  const [content, setContent] = useState('');
  const [locked, setLocked] = useState(false);
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const load = async () => {
    try {
      const res = await axios.get(`${SERVER_URL}/diary/user`, { params: { user_id: FIXED_USER_ID } });
      setDiaries(res.data?.diaries || []);
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
        user_id: FIXED_USER_ID,
        content: content.trim(),
        visibility: locked ? 'locked' : 'open',
        password: locked ? password.trim() : null,
      });
      setContent(''); setPassword(''); setLocked(false); setShowWrite(false);
      await load();
    } catch (e: any) {
      Alert.alert('发布失败', e?.message ?? '请重试');
    } finally { setSubmitting(false); }
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

  // 改可见性 / 密码
  const changeLock = (d: UserDiary) => {
    if (d.visibility === 'locked') {
      Alert.alert('这篇是私密的', '要改成给他看，还是改密码？', [
        { text: '改成给他看', onPress: async () => {
          await axios.post(`${SERVER_URL}/diary/user/${d.id}/password`, { user_id: FIXED_USER_ID, password: '' });
          await load();
        }},
        { text: '改密码', onPress: () => promptNewPassword(d) },
        { text: '取消', style: 'cancel' },
      ]);
    } else {
      Alert.alert('这篇他能看到', '要把它设成私密（上锁）吗？', [
        { text: '设为私密', onPress: () => promptNewPassword(d) },
        { text: '取消', style: 'cancel' },
      ]);
    }
  };

  const [pwModalFor, setPwModalFor] = useState<UserDiary | null>(null);
  const [newPw, setNewPw] = useState('');
  const promptNewPassword = (d: UserDiary) => { setPwModalFor(d); setNewPw(''); };
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

  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
      <StatusBar barStyle="light-content" backgroundColor={C.card} />
      <View style={s.header}>
        <TouchableOpacity onPress={() => router.back()} style={s.backBtn}>
          <Text style={s.backText}>‹</Text>
        </TouchableOpacity>
        <View style={{ flex: 1 }}>
          <Text style={s.headerTitle}>我的日记</Text>
          <Text style={s.headerSub}>他会偷看 · 你能看到他几点翻过</Text>
        </View>
        <TouchableOpacity style={s.writeBtn} onPress={() => setShowWrite(true)}>
          <Text style={s.writeBtnText}>✎ 写</Text>
        </TouchableOpacity>
      </View>

      {loading ? (
        <View style={s.center}><ActivityIndicator color={C.accent} /></View>
      ) : (
        <ScrollView
          contentContainerStyle={{ padding: 16, paddingBottom: 40 }}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={C.accent} />}
        >
          {diaries.length === 0 && (
            <View style={s.empty}>
              <Text style={s.emptyEmoji}>🖊</Text>
              <Text style={s.emptyText}>还没写日记。{'\n'}写一篇，看他会不会偷偷来看。</Text>
            </View>
          )}

          {diaries.map(d => (
            <View key={d.id} style={s.card}>
              <View style={s.cardTop}>
                <Text style={s.lockTag}>
                  {d.visibility === 'locked' ? '🔒 私密' : '👁 他能看'}
                </Text>
                <Text style={s.date}>{fmt(d.created_at)}</Text>
              </View>
              <Text style={s.content}>{d.content}</Text>

              {/* 访客记号 */}
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
                  <Text style={[s.actionText, { color: '#f43f5e' }]}>删除</Text>
                </TouchableOpacity>
              </View>
            </View>
          ))}
        </ScrollView>
      )}

      {/* 写日记模态 */}
      <Modal visible={showWrite} animationType="slide" transparent statusBarTranslucent>
        <View style={s.modalBackdrop}>
          <View style={s.modalCard}>
            <Text style={s.modalTitle}>写日记</Text>
            <TextInput
              style={s.modalInput}
              value={content}
              onChangeText={setContent}
              placeholder="今天想记点什么…"
              placeholderTextColor={C.textMute}
              multiline
              autoFocus
            />
            <TouchableOpacity style={s.lockToggle} onPress={() => setLocked(v => !v)}>
              <View style={[s.checkbox, locked && s.checkboxOn]}>
                <Text style={{ color: locked ? '#fff' : 'transparent', fontSize: 13 }}>✓</Text>
              </View>
              <Text style={s.lockToggleText}>
                设为私密（上锁）—— 他默认看不到，除非他"猜对密码"
              </Text>
            </TouchableOpacity>
            {locked && (
              <TextInput
                style={[s.modalInput, { minHeight: 0, marginTop: 10 }]}
                value={password}
                onChangeText={setPassword}
                placeholder="给这篇设个密码"
                placeholderTextColor={C.textMute}
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

      {/* 改密码模态 */}
      <Modal visible={!!pwModalFor} animationType="fade" transparent statusBarTranslucent>
        <View style={s.modalBackdrop}>
          <View style={s.modalCard}>
            <Text style={s.modalTitle}>设置密码</Text>
            <TextInput
              style={[s.modalInput, { minHeight: 0 }]}
              value={newPw}
              onChangeText={setNewPw}
              placeholder="输入新密码"
              placeholderTextColor={C.textMute}
              autoFocus
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
  header: { flexDirection: 'row', alignItems: 'center', backgroundColor: C.card, paddingHorizontal: 12, paddingTop: Platform.OS === 'ios' ? 50 : 40, paddingBottom: 12, borderBottomWidth: 1, borderBottomColor: C.border, gap: 6 },
  backBtn: { paddingHorizontal: 6, paddingVertical: 4 },
  backText: { color: C.text, fontSize: 30, lineHeight: 32, fontWeight: '300' },
  headerTitle: { color: C.text, fontSize: 17, fontWeight: '700' },
  headerSub: { color: C.textMute, fontSize: 11, marginTop: 2 },
  writeBtn: { paddingHorizontal: 12, paddingVertical: 7, borderRadius: 12, backgroundColor: C.accent },
  writeBtnText: { color: '#fff', fontSize: 13, fontWeight: '600' },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  empty: { alignItems: 'center', paddingTop: 100 },
  emptyEmoji: { fontSize: 44, marginBottom: 14 },
  emptyText: { color: C.textMute, fontSize: 14, textAlign: 'center', lineHeight: 22 },

  card: { backgroundColor: C.card, borderRadius: 16, padding: 16, marginBottom: 14, borderLeftWidth: 3, borderLeftColor: C.accent2 },
  cardTop: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 },
  lockTag: { color: C.textDim, fontSize: 12, fontWeight: '600' },
  date: { color: C.textMute, fontSize: 12 },
  content: { color: C.text, fontSize: 15, lineHeight: 24 },

  visitBox: { marginTop: 12, paddingTop: 10, borderTopWidth: 1, borderTopColor: C.border, gap: 5 },
  visitText: { color: C.accent2, fontSize: 12 },
  visitUnlocked: { color: '#f59e0b', fontWeight: '700' },

  actionRow: { flexDirection: 'row', gap: 18, marginTop: 12 },
  actionText: { color: C.textMute, fontSize: 12 },

  modalBackdrop: { flex: 1, backgroundColor: 'rgba(0,0,0,0.55)', justifyContent: 'flex-end' },
  modalCard: { backgroundColor: C.card, paddingHorizontal: 20, paddingTop: 18, paddingBottom: 30, borderTopLeftRadius: 22, borderTopRightRadius: 22, borderTopWidth: 1, borderColor: C.border },
  modalTitle: { color: C.text, fontSize: 16, fontWeight: '700', marginBottom: 12 },
  modalInput: { backgroundColor: C.bg, borderRadius: 12, borderWidth: 1, borderColor: C.border, paddingHorizontal: 14, paddingVertical: 12, color: C.text, fontSize: 14, minHeight: 100, textAlignVertical: 'top' },
  lockToggle: { flexDirection: 'row', alignItems: 'center', marginTop: 14, gap: 10 },
  checkbox: { width: 22, height: 22, borderRadius: 6, borderWidth: 1.5, borderColor: C.border, alignItems: 'center', justifyContent: 'center' },
  checkboxOn: { backgroundColor: C.accent, borderColor: C.accent },
  lockToggleText: { color: C.textDim, fontSize: 12, flex: 1, lineHeight: 17 },
  modalBtnRow: { flexDirection: 'row', gap: 10, marginTop: 18 },
  modalBtn: { flex: 1, paddingVertical: 12, borderRadius: 14, alignItems: 'center' },
  ghost: { borderWidth: 1, borderColor: C.border },
  ghostText: { color: C.textMute, fontSize: 14 },
  primary: { backgroundColor: C.accent },
  primaryText: { color: '#fff', fontSize: 14, fontWeight: '600' },
});
