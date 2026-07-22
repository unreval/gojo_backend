// app/diary/[cid].tsx —— 放进项目时去掉前缀，文件名就叫 [cid].tsx
// 「他的日记」：手写信纸质感 + 日记本名字（他自己取，你也能改）+ 留言。
//   ★ 不对称：你读他日记不留痕；只有你留言，他之后才会"发现"。
import axios from 'axios';
import { useFonts } from 'expo-font';
import { useFocusEffect, useLocalSearchParams, useRouter } from 'expo-router';
import React, { useCallback, useState } from 'react';
import {
  ActivityIndicator, Alert,
  Modal, Platform, RefreshControl,
  ScrollView, StatusBar, StyleSheet, Text, TextInput, TouchableOpacity, View
} from 'react-native';
import { SERVER_URL } from '../../constants/theme';

const FIXED_USER_ID = 'user_mofpiyd7442ia7';
const HAND = 'LongCang';   // ★ 手写字体名，须与 assets/fonts/LongCang-Regular.ttf 对应

interface DiaryComment { id: number; content: string; created_at: string | null; }
interface CharDiary {
  id: number; content: string; emotion: string;
  created_at: string | null; comments: DiaryComment[];
}

const EMOJI: Record<string, string> = {
  平静: '😌', 温柔: '🫧', 调皮: '😏', 认真: '🤨',
  开心: '😄', 疑惑: '🤔', 悲伤: '🥀', 自信: '😎',
};

function fmt(ts: string | null): string {
  if (!ts) return '';
  const d = new Date(ts.replace(' ', 'T') + (ts.includes('Z') ? '' : 'Z'));
  if (isNaN(d.getTime())) return ts.slice(5, 16);
  return `${d.getMonth() + 1}月${d.getDate()}日 ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

export default function HisDiaryScreen() {
  const { cid: rawCid } = useLocalSearchParams<{ cid: string }>();
  const cid = (rawCid || 'gojo') as string;
  const router = useRouter();

  const [fontsLoaded] = useFonts({ [HAND]: require('../../assets/fonts/LongCang-Regular.ttf') });

  const [bookTitle, setBookTitle] = useState('');
  const [diaries, setDiaries] = useState<CharDiary[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const [commentFor, setCommentFor] = useState<CharDiary | null>(null);
  const [commentText, setCommentText] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const [renaming, setRenaming] = useState(false);
  const [newTitle, setNewTitle] = useState('');

  const load = async () => {
    try {
      const [bookRes, listRes] = await Promise.all([
        axios.get(`${SERVER_URL}/diary/book/${cid}`, { params: { user_id: FIXED_USER_ID } }),
        axios.get(`${SERVER_URL}/diary/char/${cid}`, { params: { user_id: FIXED_USER_ID } }),
      ]);
      setBookTitle(bookRes.data?.title || `${cid}的日记`);
      setDiaries(listRes.data?.diaries || []);
    } catch (e: any) { console.warn('load his diary', e?.message); }
  };

  useFocusEffect(useCallback(() => {
    (async () => { setLoading(true); await load(); setLoading(false); })();
  }, [cid]));

  const onRefresh = async () => { setRefreshing(true); await load(); setRefreshing(false); };

  const submitComment = async () => {
    if (!commentFor || !commentText.trim()) return;
    setSubmitting(true);
    try {
      await axios.post(`${SERVER_URL}/diary/char/${commentFor.id}/comment`, {
        user_id: FIXED_USER_ID, content: commentText.trim(),
      });
      setCommentText(''); setCommentFor(null);
      await load();
      Alert.alert('留言了', '他之后会发现你看过这篇…', [{ text: '好' }]);
    } catch (e: any) { Alert.alert('留言失败', e?.message ?? '请重试'); }
    finally { setSubmitting(false); }
  };

  const submitRename = async () => {
    if (!newTitle.trim()) return;
    try {
      await axios.put(`${SERVER_URL}/diary/book/${cid}`, { user_id: FIXED_USER_ID, title: newTitle.trim() });
      setRenaming(false); setNewTitle('');
      await load();
    } catch (e: any) { Alert.alert('改名失败', e?.message ?? '请重试'); }
  };

  const hand = fontsLoaded ? { fontFamily: HAND } : {};

  return (
    <View style={{ flex: 1, backgroundColor: '#e8ddc7' }}>
      <StatusBar barStyle="dark-content" backgroundColor="#e8ddc7" />

      {/* 顶部：书名（点一下改名） */}
      <View style={s.header}>
        <TouchableOpacity onPress={() => router.back()} style={s.backBtn}>
          <Text style={s.backText}>‹</Text>
        </TouchableOpacity>
        <TouchableOpacity
          style={{ flex: 1, alignItems: 'center' }}
          onPress={() => { setNewTitle(bookTitle); setRenaming(true); }}
          activeOpacity={0.7}
        >
          <Text style={[s.bookTitle, hand]} numberOfLines={1}>{bookTitle || '　'}</Text>
          <Text style={s.bookHint}>点标题可改名</Text>
        </TouchableOpacity>
        <View style={{ width: 34 }} />
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
              <Text style={s.emptyEmoji}>📔</Text>
              <Text style={[s.emptyText, hand]}>他还没写日记。{'\n'}他想写才写，等等看吧。</Text>
            </View>
          )}

          {diaries.map(d => (
            <View key={d.id} style={s.paper}>
              <View style={s.paperTop}>
                <Text style={s.emotion}>{EMOJI[d.emotion] || '🖊'}</Text>
                <Text style={s.date}>{fmt(d.created_at)}</Text>
              </View>
              <Text style={[s.diaryText, hand]}>{d.content}</Text>

              {d.comments.length > 0 && (
                <View style={s.commentBox}>
                  {d.comments.map(c => (
                    <View key={c.id} style={s.commentItem}>
                      <Text style={s.commentLabel}>你留言 · {fmt(c.created_at)}</Text>
                      <Text style={[s.commentText, hand]}>{c.content}</Text>
                    </View>
                  ))}
                </View>
              )}

              <TouchableOpacity style={s.commentBtn} onPress={() => { setCommentFor(d); setCommentText(''); }}>
                <Text style={s.commentBtnText}>
                  {d.comments.length > 0 ? '＋ 再留一句' : '💬 留言（他会发现你看过）'}
                </Text>
              </TouchableOpacity>
            </View>
          ))}
        </ScrollView>
      )}

      {/* 留言 */}
      <Modal visible={!!commentFor} animationType="slide" transparent statusBarTranslucent>
        <View style={s.modalBackdrop}>
          <View style={s.modalCard}>
            <Text style={s.modalTitle}>给这篇日记留言</Text>
            {commentFor && <Text style={[s.modalQuote, hand]} numberOfLines={2}>「{commentFor.content}」</Text>}
            <TextInput
              style={s.modalInput} value={commentText} onChangeText={setCommentText}
              placeholder="写点什么给他看到…" placeholderTextColor="#b3a48a" multiline autoFocus
            />
            <View style={s.modalBtnRow}>
              <TouchableOpacity style={[s.modalBtn, s.ghost]} onPress={() => setCommentFor(null)} disabled={submitting}>
                <Text style={s.ghostText}>取消</Text>
              </TouchableOpacity>
              <TouchableOpacity style={[s.modalBtn, s.primary]} onPress={submitComment} disabled={submitting || !commentText.trim()}>
                {submitting ? <ActivityIndicator color="#fff" /> : <Text style={s.primaryText}>留言</Text>}
              </TouchableOpacity>
            </View>
          </View>
        </View>
      </Modal>

      {/* 改名 */}
      <Modal visible={renaming} animationType="fade" transparent statusBarTranslucent>
        <View style={s.modalBackdrop}>
          <View style={s.modalCard}>
            <Text style={s.modalTitle}>给这本日记改个名</Text>
            <TextInput
              style={s.modalInput} value={newTitle} onChangeText={setNewTitle}
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
    </View>
  );
}

const s = StyleSheet.create({
  header: { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 12, paddingTop: Platform.OS === 'ios' ? 52 : 42, paddingBottom: 12 },
  backBtn: { width: 34, paddingVertical: 4 },
  backText: { color: '#5a4d33', fontSize: 30, lineHeight: 32, fontWeight: '300' },
  bookTitle: { color: '#3d3320', fontSize: 26, letterSpacing: 2 },
  bookHint: { color: '#a5946f', fontSize: 10, marginTop: 1 },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  empty: { alignItems: 'center', paddingTop: 90 },
  emptyEmoji: { fontSize: 44, marginBottom: 14 },
  emptyText: { color: '#8a7a5c', fontSize: 22, textAlign: 'center', lineHeight: 34 },

  // 信纸
  paper: {
    backgroundColor: '#fffdf5', borderRadius: 8, padding: 18, marginBottom: 16,
    borderWidth: 1, borderColor: '#e6dcc2',
    shadowColor: '#000', shadowOpacity: 0.08, shadowRadius: 6, shadowOffset: { width: 0, height: 2 }, elevation: 2,
  },
  paperTop: { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 },
  emotion: { fontSize: 18 },
  date: { color: '#a99a76', fontSize: 12 },
  diaryText: { color: '#3d3320', fontSize: 22, lineHeight: 40 },

  commentBox: { marginTop: 14, paddingTop: 10, borderTopWidth: 1, borderTopColor: '#efe7d2', gap: 8 },
  commentItem: { backgroundColor: '#f6f0df', borderRadius: 8, padding: 10 },
  commentLabel: { color: '#c08a3e', fontSize: 11, marginBottom: 3 },
  commentText: { color: '#6b5c3e', fontSize: 19, lineHeight: 28 },

  commentBtn: { marginTop: 12, alignSelf: 'flex-start', paddingHorizontal: 12, paddingVertical: 7, borderRadius: 12, borderWidth: 1, borderColor: '#d8c9a3', backgroundColor: '#f3ecd8' },
  commentBtnText: { color: '#9a824e', fontSize: 12, fontWeight: '600' },

  modalBackdrop: { flex: 1, backgroundColor: 'rgba(0,0,0,0.4)', justifyContent: 'flex-end' },
  modalCard: { backgroundColor: '#fffdf5', paddingHorizontal: 20, paddingTop: 18, paddingBottom: 30, borderTopLeftRadius: 22, borderTopRightRadius: 22 },
  modalTitle: { color: '#3d3320', fontSize: 16, fontWeight: '700', marginBottom: 10 },
  modalQuote: { color: '#8a7a5c', fontSize: 18, marginBottom: 12, lineHeight: 26 },
  modalInput: { backgroundColor: '#f6f0df', borderRadius: 12, borderWidth: 1, borderColor: '#e0d4b4', paddingHorizontal: 14, paddingVertical: 12, color: '#3d3320', fontSize: 15, minHeight: 60, textAlignVertical: 'top' },
  modalBtnRow: { flexDirection: 'row', gap: 10, marginTop: 16 },
  modalBtn: { flex: 1, paddingVertical: 12, borderRadius: 14, alignItems: 'center' },
  ghost: { borderWidth: 1, borderColor: '#d8c9a3' },
  ghostText: { color: '#9a824e', fontSize: 14 },
  primary: { backgroundColor: '#c0a058' },
  primaryText: { color: '#fff', fontSize: 14, fontWeight: '600' },
});