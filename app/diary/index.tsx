// app/diary/index.tsx
// 日记首页：点首页「日记」进来，列出两本日记（我的 / Satoru的），深色卡片、和聊天列表统一。
//   点卡片 → 进各自的日记本。
import axios from 'axios';
import { useFocusEffect, useRouter } from 'expo-router';
import React, { useCallback, useState } from 'react';
import {
    ActivityIndicator, Image, Platform, ScrollView, StatusBar,
    StyleSheet, Text, TouchableOpacity, View,
} from 'react-native';
import { C, SERVER_URL } from '../../constants/theme';

const FIXED_USER_ID = 'user_mofpiyd7442ia7';
const DIARY_CHARACTER = 'gojo';

export default function DiaryHomeScreen() {
  const router = useRouter();
  const [myTitle, setMyTitle] = useState('我的日记');
  const [hisTitle, setHisTitle] = useState('Satoru 的日记');
  const [charAvatar, setCharAvatar] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = async () => {
    try {
      const [mine, his, ch] = await Promise.all([
        axios.get(`${SERVER_URL}/diary/book/user`, { params: { user_id: FIXED_USER_ID } }),
        axios.get(`${SERVER_URL}/diary/book/${DIARY_CHARACTER}`, { params: { user_id: FIXED_USER_ID } }),
        axios.get(`${SERVER_URL}/characters/${DIARY_CHARACTER}`).catch(() => null),
      ]);
      if (mine?.data?.title) setMyTitle(mine.data.title);
      if (his?.data?.title) setHisTitle(his.data.title);
      if (ch?.data?.avatar_url) setCharAvatar(ch.data.avatar_url);
    } catch (e: any) { console.warn('diary home', e?.message); }
  };

  useFocusEffect(useCallback(() => {
    (async () => { setLoading(true); await load(); setLoading(false); })();
  }, []));

  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
      <StatusBar barStyle="light-content" backgroundColor={C.card} />
      <View style={s.header}>
        <TouchableOpacity onPress={() => router.back()} style={s.backBtn}>
          <Text style={s.backText}>‹</Text>
        </TouchableOpacity>
        <View style={{ flex: 1 }}>
          <Text style={s.headerTitle}>日记</Text>
          <Text style={s.headerSub}>他偶尔写 · 你也写 · 彼此偷看</Text>
        </View>
      </View>

      {loading ? (
        <View style={s.center}><ActivityIndicator color={C.accent} /></View>
      ) : (
        <ScrollView contentContainerStyle={{ padding: 16 }}>
          {/* Satoru 的日记 */}
          <TouchableOpacity
            activeOpacity={0.8}
            style={s.row}
            onPress={() => router.push(`/diary/${DIARY_CHARACTER}` as any)}
          >
            <View style={[s.avatar, { borderColor: C.accent, overflow: 'hidden' }]}>
              {charAvatar ? (
                <Image source={{ uri: charAvatar }} style={{ width: '100%', height: '100%' }} resizeMode="cover" />
              ) : (
                <Text style={s.avatarText}>📔</Text>
              )}
            </View>
            <View style={{ flex: 1, marginLeft: 12 }}>
              <Text style={s.rowTitle} numberOfLines={1}>{hisTitle}</Text>
              <Text style={s.rowSub} numberOfLines={1}>他写的心里话 · 你可以留言</Text>
            </View>
            <Text style={s.arrow}>›</Text>
          </TouchableOpacity>

          {/* 我的日记 */}
          <TouchableOpacity
            activeOpacity={0.8}
            style={s.row}
            onPress={() => router.push('/diary/mine' as any)}
          >
            <View style={[s.avatar, { borderColor: C.accent2, overflow: 'hidden' }]}>
              <Text style={s.avatarText}>🖊</Text>
            </View>
            <View style={{ flex: 1, marginLeft: 12 }}>
              <Text style={s.rowTitle} numberOfLines={1}>{myTitle}</Text>
              <Text style={s.rowSub} numberOfLines={1}>你写的 · 他会偷看，留下访客记号</Text>
            </View>
            <Text style={s.arrow}>›</Text>
          </TouchableOpacity>

          <Text style={s.hint}>日记本的名字可以在各自里面点标题改</Text>
        </ScrollView>
      )}
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

  row: { flexDirection: 'row', alignItems: 'center', backgroundColor: C.card, borderRadius: 16, padding: 14, marginBottom: 12, borderWidth: 1, borderColor: C.border },
  avatar: { width: 48, height: 48, borderRadius: 24, alignItems: 'center', justifyContent: 'center', borderWidth: 1.5, backgroundColor: C.accentDim + '33' },
  avatarText: { fontSize: 22 },
  rowTitle: { color: C.text, fontSize: 16, fontWeight: '600' },
  rowSub: { color: C.textMute, fontSize: 12, marginTop: 3 },
  arrow: { color: C.textMute, fontSize: 22, marginLeft: 8 },
  hint: { color: C.textMute, fontSize: 11, textAlign: 'center', marginTop: 8 },
});