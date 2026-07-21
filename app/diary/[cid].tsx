// app/diary/[cid].tsx
// 「看他的日记」页：读某个角色（cid）不定期写的日记，可以在某篇下留言。
//   ★ 不对称核心：你读他日记【不留痕】；只有你留言，他之后才会"发现"你看过。
//   入口：从聊天页头部的 📔 按钮进来（router.push('/diary/' + chatId)）。
import axios from 'axios';
import { useLocalSearchParams, useRouter } from 'expo-router';
import React, { useCallback, useState } from 'react';
import { useFocusEffect } from 'expo-router';
import {
  ActivityIndicator, Alert, Modal, Platform, RefreshControl,
  ScrollView, StatusBar, StyleSheet, Text, TextInput, TouchableOpacity, View,
} from 'react-native';
import { C, SERVER_URL } from '../../constants/theme';

const FIXED_USER_ID = 'user_mofpiyd7442ia7';

interface DiaryComment { id: number; content: string; created_at: string | null; }
interface CharDiary {
  id: number;
  content: string;
  emotion: string;
  created_at: string | null;
  comments: DiaryComment[];
}

const EMOJI: Record<string, string> = {
  平静: '😌', 温柔: '🫧', 调皮: '😏', 认真: '🤨',
  开心: '😄', 疑惑: '🤔', 悲伤: '🥀', 自信: '😎',
};

function fmt(ts: string | null): string {
  if (!ts) return '';
  const d = new Date(ts.replace(' ', 'T') + (ts.includes('Z') ? '' : 'Z'));
  if (isNaN(d.getTime())) return ts.slice(5, 16);
  const mo = d.getMonth() + 1, da = d.getDate();
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${mo}月${da}日 ${hh}:${mm}`;
}

export default function HisDiaryScreen() {
  const { cid: rawCid } = useLocalSearchParams<{ cid: string }>();
  const cid = (rawCid || 'gojo') as string;
  const router = useRouter();

  const [diaries, setDiaries] = useState<CharDiary[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [commentFor, setCommentFor] = useState<CharDiary | null>(null);
  const [commentText, setCommentText] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const load = async () => {
    try {
      const res = await axios.get(`${SERVER_URL}/diary/char/${cid}`, {
        params: { user_id: FIXED_USER_ID },
      });
      setDiaries(res.data?.diaries || []);
    } catch (e: any) {
      console.warn('load his diary', e?.message);
    }
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
      setCommentText('');
      setCommentFor(null);
      await load();
      Alert.alert('留言了', '他之后会发现你看过这篇…', [{ text: '好' }]);
    } catch (e: any) {
      Alert.alert('留言失败', e?.message ?? '请重试');
    } finally { setSubmitting(false); }
  };

  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
      <StatusBar barStyle="light-content" backgroundColor={C.card} />
      <View style={s.header}>
        <TouchableOpacity onPress={() => router.back()} style={s.backBtn}>
          <Text style={s.backText}>‹</Text>
        </TouchableOpacity>
        <View style={{ flex: 1 }}>
          <Text style={s.headerTitle}>他的日记</Text>
          <Text style={s.headerSub}>他偶尔会写点心里话 · 你可以留言</Text>
        </View>
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
              <Text style={s.emptyEmoji}>📔</Text>
              <Text style={s.emptyText}>他还没写日记。{'\n'}他想写才写，等等看吧。</Text>
            </View>
          )}

          {diaries.map(d => (
            <View key={d.id} style={s.card}>
              <View style={s.cardTop}>
                <Text style={s.emotion}>{EMOJI[d.emotion] || '🖊'}</Text>
                <Text style={s.date}>{fmt(d.created_at)}</Text>
              </View>
              <Text style={s.content}>{d.content}</Text>

              {d.comments.length > 0 && (
                <View style={s.commentBox}>
                  {d.comments.map(c => (
                    <View key={c.id} style={s.commentItem}>
                      <Text style={s.commentLabel}>你留言 · {fmt(c.created_at)}</Text>
                      <Text style={s.commentText}>{c.content}</Text>
                    </View>
                  ))}
                </View>
              )}

              <TouchableOpacity
                style={s.commentBtn}
                onPress={() => { setCommentFor(d); setCommentText(''); }}
              >
                <Text style={s.commentBtnText}>
                  {d.comments.length > 0 ? '＋ 再留一句' : '💬 留言（他会发现你看过）'}
                </Text>
              </TouchableOpacity>
            </View>
          ))}
        </ScrollView>
      )}

      <Modal visible={!!commentFor} animationType="slide" transparent statusBarTranslucent>
        <View style={s.modalBackdrop}>
          <View style={s.modalCard}>
            <Text style={s.modalTitle}>给这篇日记留言</Text>
            {commentFor && <Text style={s.modalQuote} numberOfLines={2}>「{commentFor.content}」</Text>}
            <TextInput
              style={s.modalInput}
              value={commentText}
              onChangeText={setCommentText}
              placeholder="写点什么给他看到…"
              placeholderTextColor={C.textMute}
              multiline
              autoFocus
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
    </View>
  );
}

const s = StyleSheet.create({
  header: { flexDirection: 'row', alignItems: 'center', backgroundColor: C.card, paddingHorizontal: 12, paddingTop: Platform.OS === 'ios' ? 50 : 40, paddingBottom: 12, borderBottomWidth: 1, borderBottomColor: C.border },
  backBtn: { paddingHorizontal: 6, paddingVertical: 4 },
  backText: { color: C.text, fontSize: 30, lineHeight: 32, fontWeight: '300' },
  headerTitle: { color: C.text, fontSize: 17, fontWeight: '700' },
  headerSub: { color: C.textMute, fontSize: 11, marginTop: 2 },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  empty: { alignItems: 'center', paddingTop: 100 },
  emptyEmoji: { fontSize: 44, marginBottom: 14 },
  emptyText: { color: C.textMute, fontSize: 14, textAlign: 'center', lineHeight: 22 },

  card: { backgroundColor: C.card, borderRadius: 16, padding: 16, marginBottom: 14, borderLeftWidth: 3, borderLeftColor: C.accent },
  cardTop: { flexDirection: 'row', alignItems: 'center', marginBottom: 10, gap: 8 },
  emotion: { fontSize: 18 },
  date: { color: C.textMute, fontSize: 12 },
  content: { color: C.text, fontSize: 15, lineHeight: 24 },

  commentBox: { marginTop: 12, paddingTop: 10, borderTopWidth: 1, borderTopColor: C.border, gap: 8 },
  commentItem: { backgroundColor: C.bg, borderRadius: 10, padding: 10 },
  commentLabel: { color: C.accent2, fontSize: 11, marginBottom: 3 },
  commentText: { color: C.textDim, fontSize: 13, lineHeight: 19 },

  commentBtn: { marginTop: 12, alignSelf: 'flex-start', paddingHorizontal: 12, paddingVertical: 7, borderRadius: 12, borderWidth: 1, borderColor: C.accent + '55', backgroundColor: C.accent + '18' },
  commentBtnText: { color: C.accent2, fontSize: 12, fontWeight: '600' },

  modalBackdrop: { flex: 1, backgroundColor: 'rgba(0,0,0,0.55)', justifyContent: 'flex-end' },
  modalCard: { backgroundColor: C.card, paddingHorizontal: 20, paddingTop: 18, paddingBottom: 30, borderTopLeftRadius: 22, borderTopRightRadius: 22, borderTopWidth: 1, borderColor: C.border },
  modalTitle: { color: C.text, fontSize: 16, fontWeight: '700', marginBottom: 10 },
  modalQuote: { color: C.textMute, fontSize: 12, fontStyle: 'italic', marginBottom: 12, lineHeight: 18 },
  modalInput: { backgroundColor: C.bg, borderRadius: 12, borderWidth: 1, borderColor: C.border, paddingHorizontal: 14, paddingVertical: 12, color: C.text, fontSize: 14, minHeight: 80, textAlignVertical: 'top' },
  modalBtnRow: { flexDirection: 'row', gap: 10, marginTop: 16 },
  modalBtn: { flex: 1, paddingVertical: 12, borderRadius: 14, alignItems: 'center' },
  ghost: { borderWidth: 1, borderColor: C.border },
  ghostText: { color: C.textMute, fontSize: 14 },
  primary: { backgroundColor: C.accent },
  primaryText: { color: '#fff', fontSize: 14, fontWeight: '600' },
});
