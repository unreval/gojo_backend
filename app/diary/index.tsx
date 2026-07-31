// app/diary/index.tsx
// 日记首页:列出所有角色的日记本 + 我的日记本。
// ★ v3 (safe): 不再用 <Image> 加载 avatar_url —— 那个字段是 80000+ 字符的 base64 data URI,
//   RN 的 Image 组件在某些 Android 版本会静默崩溃(整个屏幕白屏)。
//   改用 emoji + 颜色区分,不影响功能。
// ★ 每一步都加了 console.log,以后再出问题打开 adb logcat 或 Metro 就能立刻定位
import axios from 'axios';
import { useFocusEffect, useRouter } from 'expo-router';
import React, { useCallback, useState } from 'react';
import {
  ActivityIndicator, Platform, ScrollView, StatusBar,
  StyleSheet, Text, TouchableOpacity, View,
} from 'react-native';
import { C, SERVER_URL } from '../../constants/theme';

const FIXED_USER_ID = 'user_mofpiyd7442ia7';

interface CharacterMeta {
  id: string;
  name: string;
  bookTitle?: string;    // 日记本名字(从 /diary/book/<id> 拉)
}

// ★ 根据 character id 稳定映射一个 emoji,视觉区分角色
function pickAvatar(id: string): string {
  const emojis = ['📔', '📕', '📗', '📘', '📙', '📓', '📒', '🖊'];
  let hash = 0;
  for (let i = 0; i < id.length; i++) hash = (hash * 31 + id.charCodeAt(i)) & 0x7fffffff;
  return emojis[hash % emojis.length];
}

const AVATAR_BORDER_COLORS = ['#3b82f6', '#60a5fa', '#A78BFA', '#F59E0B', '#E8A0BF', '#34D399'];

export default function DiaryHomeScreen() {
  const router = useRouter();
  const [characters, setCharacters] = useState<CharacterMeta[]>([]);
  const [myTitle, setMyTitle] = useState('我的日记');
  const [loading, setLoading] = useState(true);

  const load = async () => {
    console.log('[diary/index] load() start');
    try {
      // 1. 先拉全部角色
      console.log('[diary/index] fetching /characters_all');
      const charsRes = await axios.get(`${SERVER_URL}/characters_all`, { timeout: 8000 });
      const allChars: any[] = charsRes.data?.characters || [];
      console.log(`[diary/index] got ${allChars.length} characters`);

      // 2. 我的日记本名字
      try {
        const mineRes = await axios.get(`${SERVER_URL}/diary/book/user`, {
          params: { user_id: FIXED_USER_ID }, timeout: 8000,
        });
        if (mineRes.data?.title) setMyTitle(mineRes.data.title);
      } catch (e: any) {
        console.warn('[diary/index] my book title fetch failed', e?.message);
      }

      // 3. 每个角色的日记本名字(并发,失败也不阻断)
      const withTitles: CharacterMeta[] = await Promise.all(
        allChars.map(async (c) => {
          const base = {
            id: c.id,
            name: c.name || c.id,
          };
          try {
            const bookRes = await axios.get(`${SERVER_URL}/diary/book/${c.id}`, {
              params: { user_id: FIXED_USER_ID }, timeout: 8000,
            });
            return { ...base, bookTitle: bookRes.data?.title || `${base.name} 的日记` };
          } catch {
            return { ...base, bookTitle: `${base.name} 的日记` };
          }
        })
      );
      console.log('[diary/index] all book titles resolved');
      setCharacters(withTitles);
    } catch (e: any) {
      console.warn('[diary/index] load failed:', e?.message);
    }
    console.log('[diary/index] load() done');
  };

  useFocusEffect(useCallback(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      await load();
      if (!cancelled) setLoading(false);
    })();
    return () => { cancelled = true; };
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
          <Text style={s.headerSub}>他们偶尔写 · 你也写 · 彼此偷看</Text>
        </View>
      </View>

      {loading ? (
        <View style={s.center}><ActivityIndicator color={C.accent} /></View>
      ) : (
        <ScrollView contentContainerStyle={{ padding: 16 }}>
          {/* 每个角色一本 —— emoji 头像,不再吃 base64 图片 */}
          {characters.map((c, i) => {
            const color = AVATAR_BORDER_COLORS[i % AVATAR_BORDER_COLORS.length];
            return (
              <TouchableOpacity
                key={c.id}
                activeOpacity={0.8}
                style={s.row}
                onPress={() => {
                  console.log(`[diary/index] tap character ${c.id}`);
                  router.push(`/diary/${c.id}` as any);
                }}
              >
                <View style={[s.avatar, { borderColor: color, backgroundColor: color + '22' }]}>
                  <Text style={s.avatarText}>{pickAvatar(c.id)}</Text>
                </View>
                <View style={{ flex: 1, marginLeft: 12 }}>
                  <Text style={s.rowTitle} numberOfLines={1}>{c.bookTitle || `${c.name} 的日记`}</Text>
                  <Text style={s.rowSub} numberOfLines={1}>{c.name} 写的心里话 · 你可以留言</Text>
                </View>
                <Text style={s.arrow}>›</Text>
              </TouchableOpacity>
            );
          })}

          {characters.length === 0 && (
            <View style={{ paddingVertical: 30, alignItems: 'center' }}>
              <Text style={{ color: C.textMute, fontSize: 12 }}>还没有角色</Text>
            </View>
          )}

          {/* 我的日记 */}
          <TouchableOpacity
            activeOpacity={0.8}
            style={s.row}
            onPress={() => {
              console.log('[diary/index] tap mine');
              router.push('/diary/mine' as any);
            }}
          >
            <View style={[s.avatar, { borderColor: '#60a5fa', backgroundColor: '#60a5fa22' }]}>
              <Text style={s.avatarText}>🖊</Text>
            </View>
            <View style={{ flex: 1, marginLeft: 12 }}>
              <Text style={s.rowTitle} numberOfLines={1}>{myTitle}</Text>
              <Text style={s.rowSub} numberOfLines={1}>你写的 · 他们会偷看,留下访客记号</Text>
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
  header: {
    flexDirection: 'row', alignItems: 'center', backgroundColor: C.card,
    paddingHorizontal: 12, paddingTop: Platform.OS === 'ios' ? 50 : 40,
    paddingBottom: 12, borderBottomWidth: 1, borderBottomColor: C.border,
  },
  backBtn: { paddingHorizontal: 6, paddingVertical: 4 },
  backText: { color: C.text, fontSize: 30, lineHeight: 32, fontWeight: '300' },
  headerTitle: { color: C.text, fontSize: 17, fontWeight: '700' },
  headerSub: { color: C.textMute, fontSize: 11, marginTop: 2 },
  center: { flex: 1, alignItems: 'center', justifyContent: 'center' },

  row: {
    flexDirection: 'row', alignItems: 'center', backgroundColor: C.card,
    borderRadius: 16, padding: 14, marginBottom: 12,
    borderWidth: 1, borderColor: C.border,
  },
  avatar: {
    width: 48, height: 48, borderRadius: 24,
    alignItems: 'center', justifyContent: 'center',
    borderWidth: 1.5,
  },
  avatarText: { fontSize: 22 },
  rowTitle: { color: C.text, fontSize: 16, fontWeight: '600' },
  rowSub: { color: C.textMute, fontSize: 12, marginTop: 3 },
  arrow: { color: C.textMute, fontSize: 22, marginLeft: 8 },
  hint: { color: C.textMute, fontSize: 11, textAlign: 'center', marginTop: 8 },
});