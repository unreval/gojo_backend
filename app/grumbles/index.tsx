// app/grumbles/index.tsx
// 便利贴:AI 在聊天里没说出口的心里话。
//
// 视觉:每条便利贴一张彩色卡片(按情绪配色),稍微歪一下角度堆叠,像手账上贴的。
// 交互:长按撕掉;下拉刷新;打开页面时自动标全部已看(清红点)。
//
// 数据来源:后端 /grumbles(GET) —— 那些"心里话"由 grumble_engine 在每次
// /chat/text 结束后后台生成,用户不会在对话里看到,只在这里看到。
import axios from 'axios';
import { useFocusEffect, useRouter } from 'expo-router';
import React, { useCallback, useState } from 'react';
import {
    ActivityIndicator, Alert, Platform, RefreshControl, ScrollView,
    StatusBar, StyleSheet, Text, TouchableOpacity, View,
} from 'react-native';
import { C, SERVER_URL } from '../../constants/theme';

const FIXED_USER_ID = 'user_mofpiyd7442ia7';

interface Grumble {
  id: number;
  character_id: string;
  content: string;
  emotion: string;
  trigger_snippet: string;
  viewed: boolean;
  created_at: string;
}

// 情绪 → 便利贴底色(马卡龙色系,像真的彩色便签)
const NOTE_COLORS: Record<string, string> = {
  '平静': '#FFF9C4',   // 淡黄
  '调皮': '#FFE0B2',   // 橘色
  '无奈': '#E1E1E1',   // 灰色
  '得意': '#C8E6C9',   // 淡绿
  '嫌弃': '#F5E1F0',   // 淡紫粉
  '心动': '#FFCDD2',   // 淡红
  '感慨': '#D1C4E9',   // 淡紫
  '嘲讽': '#FFCCBC',   // 橘红
  '自嘲': '#DCDCDC',   // 浅灰
  '疑惑': '#B3E5FC',   // 淡蓝
  '开心': '#FFF176',   // 亮黄
  '温柔': '#F8BBD0',   // 粉
  '愤怒': '#FFAB91',   // 深橘
  '悲伤': '#B0BEC5',   // 蓝灰
};

// 情绪 → 便利贴右上角的小 tag(装饰用)
const EMOTION_TAGS: Record<string, string> = {
  '平静': '·', '调皮': 'hh', '无奈': '..', '得意': '哼',
  '嫌弃': 'tsk', '心动': '♡', '感慨': '...', '嘲讽': '呵',
  '自嘲': 'hah', '疑惑': '?', '开心': '!', '温柔': '♡',
  '愤怒': '!!', '悲伤': '..',
};

export default function GrumblesScreen() {
  const router = useRouter();
  const [grumbles, setGrumbles] = useState<Grumble[]>([]);
  const [charNames, setCharNames] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const load = async () => {
    try {
      const [gres, cres] = await Promise.all([
        axios.get(`${SERVER_URL}/grumbles`, {
          params: { user_id: FIXED_USER_ID },
          timeout: 8000,
        }),
        axios.get(`${SERVER_URL}/characters_all`, { timeout: 8000 })
          .catch(() => ({ data: { characters: [] } })),
      ]);
      setGrumbles(gres.data?.grumbles || []);
      const nameMap: Record<string, string> = {};
      for (const c of (cres.data?.characters || [])) {
        if (c?.id) nameMap[c.id] = c.name || c.id;
      }
      setCharNames(nameMap);

      // 打开页面就一次性标已看(清首页红点)。失败不阻断。
      try {
        await axios.post(`${SERVER_URL}/grumbles/mark_viewed`, {
          user_id: FIXED_USER_ID,
        });
      } catch {}
    } catch (e: any) {
      console.warn('[grumbles] load failed:', e?.message);
    }
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

  const onRefresh = async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  };

  const deleteOne = (id: number) => {
    Alert.alert('撕掉这张便利贴?', '', [
      { text: '取消', style: 'cancel' },
      {
        text: '撕掉',
        style: 'destructive',
        onPress: async () => {
          try {
            await axios.delete(`${SERVER_URL}/grumbles/${id}`, {
              params: { user_id: FIXED_USER_ID },
            });
            setGrumbles(prev => prev.filter(g => g.id !== id));
          } catch (e: any) {
            Alert.alert('删除失败', e?.message ?? '');
          }
        },
      },
    ]);
  };

  const formatTime = (ts: string): string => {
    if (!ts) return '';
    // 后端返回 timestamp 可能带/不带时区,统一按 UTC 解析后转本地
    const withZ = /[Zz]|[+-]\d{2}:?\d{2}$/.test(ts) ? ts : ts + 'Z';
    const d = new Date(withZ);
    if (isNaN(d.getTime())) return ts.slice(5, 16);
    const now = new Date();
    const isToday = d.toDateString() === now.toDateString();
    const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    if (isToday) return `今天 ${hm}`;
    return `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
  };

  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
      <StatusBar barStyle="light-content" backgroundColor={C.card} />

      <View style={s.header}>
        <TouchableOpacity onPress={() => router.back()} style={s.backBtn}>
          <Text style={s.backText}>‹</Text>
        </TouchableOpacity>
        <View style={{ flex: 1 }}>
          <Text style={s.headerTitle}>便利贴</Text>
          <Text style={s.headerSub}>TA 没说出口的心里话</Text>
        </View>
      </View>

      {loading ? (
        <View style={s.center}>
          <ActivityIndicator color={C.accent2} />
        </View>
      ) : grumbles.length === 0 ? (
        <View style={s.center}>
          <Text style={s.emptyIcon}>📝</Text>
          <Text style={s.emptyText}>还没有便利贴</Text>
          <Text style={s.emptySub}>聊得多了,TA 心里的碎碎念会自动贴上来</Text>
        </View>
      ) : (
        <ScrollView
          contentContainerStyle={s.container}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={C.accent2} />}
          showsVerticalScrollIndicator={false}
        >
          {grumbles.map((g, idx) => {
            const bg = NOTE_COLORS[g.emotion] || NOTE_COLORS['平静'];
            // 隔一张往相反方向歪一下,更像手账
            const rotate = (idx % 2 === 0 ? -1.2 : 1.5);
            const charName = charNames[g.character_id] || g.character_id;
            const tag = EMOTION_TAGS[g.emotion] || '·';
            return (
              <TouchableOpacity
                key={g.id}
                activeOpacity={0.92}
                onLongPress={() => deleteOne(g.id)}
                style={[
                  s.note,
                  { backgroundColor: bg, transform: [{ rotate: `${rotate}deg` }] },
                ]}
              >
                <View style={s.noteHead}>
                  <Text style={s.noteMeta}>{charName} · {g.emotion}</Text>
                  <Text style={s.noteMeta}>{formatTime(g.created_at)}</Text>
                </View>
                <Text style={s.noteBody}>{g.content}</Text>
                {g.trigger_snippet ? (
                  <Text style={s.noteTrigger}>关于:「{g.trigger_snippet}」</Text>
                ) : null}
                <Text style={s.noteTag}>{tag}</Text>
              </TouchableOpacity>
            );
          })}
          <Text style={s.footer}>长按便利贴可以撕掉 · 共 {grumbles.length} 张</Text>
          <View style={{ height: 32 }} />
        </ScrollView>
      )}
    </View>
  );
}

const s = StyleSheet.create({
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    paddingHorizontal: 12,
    paddingTop: Platform.OS === 'ios' ? 50 : 40,
    paddingBottom: 12,
    borderBottomWidth: 1,
    borderBottomColor: C.border,
    backgroundColor: C.card,
  },
  backBtn: { paddingHorizontal: 6, paddingVertical: 4 },
  backText: { color: C.text, fontSize: 30, lineHeight: 32, fontWeight: '300' },
  headerTitle: { color: C.text, fontSize: 17, fontWeight: '700' },
  headerSub: { color: C.textMute, fontSize: 11, marginTop: 2 },

  center: { flex: 1, justifyContent: 'center', alignItems: 'center', padding: 32 },
  emptyIcon: { fontSize: 48, marginBottom: 12 },
  emptyText: { color: C.text, fontSize: 16, marginBottom: 6 },
  emptySub: { color: C.textMute, fontSize: 12, textAlign: 'center' },

  container: { padding: 16, paddingTop: 24, paddingBottom: 20 },
  note: {
    borderRadius: 4,
    padding: 14,
    paddingRight: 32,   // 给右上角小 tag 留位
    marginBottom: 20,
    shadowColor: '#000',
    shadowOffset: { width: 2, height: 3 },
    shadowOpacity: 0.18,
    shadowRadius: 4,
    elevation: 3,
    position: 'relative',
  },
  noteHead: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    marginBottom: 8,
  },
  noteMeta: { color: '#666', fontSize: 10 },
  noteBody: { color: '#333', fontSize: 15, lineHeight: 22 },
  noteTrigger: {
    color: '#888', fontSize: 11,
    marginTop: 8, fontStyle: 'italic',
  },
  noteTag: {
    position: 'absolute',
    top: 6, right: 10,
    color: '#999', fontSize: 18, fontWeight: '700',
  },

  footer: {
    color: C.textMute, fontSize: 11,
    textAlign: 'center', marginTop: 8,
  },
});