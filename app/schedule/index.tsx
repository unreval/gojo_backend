// app/schedule/index.tsx
// 「TA 的一天」—— 角色自己的日程时间轴。
//
// 设计参考用户给的截图:左侧时间刻度 + 竖线节点,右侧卡片(标题/地点/时段/碎碎念)。
// 配色跟全站统一走深蓝,不用参考图那种粉色。
//
// ★ 不用 <Image> 加载 avatar_url —— 那字段是超长 base64,RN 会静默崩溃(日记页踩过)。
//   角色切换用文字 chip。
import axios from 'axios';
import { useFocusEffect, useRouter } from 'expo-router';
import React, { useCallback, useRef, useState } from 'react';
import {
    ActivityIndicator, Alert, Platform, RefreshControl, ScrollView,
    StatusBar, StyleSheet, Text, TouchableOpacity, View,
} from 'react-native';
import { C, SERVER_URL } from '../../constants/theme';

const FIXED_USER_ID = 'user_mofpiyd7442ia7';

interface SchedItem {
  id: number;
  start_time: string;
  end_time: string;
  title: string;
  location: string;
  note: string;
  can_reply: boolean;
}

interface CharacterMeta { id: string; name: string; }

/** 'HH:MM' → 分钟数,用来判断当前落在哪一段 */
function toMin(hhmm: string): number {
  const [h, m] = hhmm.split(':').map(Number);
  return h * 60 + m;
}

/** 判断某个时段是否包含当前时刻(支持跨午夜) */
function isNow(item: SchedItem, nowMin: number): boolean {
  const s = toMin(item.start_time);
  const e = toMin(item.end_time);
  return s <= e ? (nowMin >= s && nowMin < e) : (nowMin >= s || nowMin < e);
}

export default function ScheduleScreen() {
  const router = useRouter();
  const [chars, setChars] = useState<CharacterMeta[]>([]);
  const [activeId, setActiveId] = useState<string>('gojo');
  const [items, setItems] = useState<SchedItem[]>([]);
  const [serverNow, setServerNow] = useState<string>('');
  const [dateStr, setDateStr] = useState<string>('');
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const scrollRef = useRef<ScrollView>(null);

  const loadChars = async () => {
    try {
      const res = await axios.get(`${SERVER_URL}/characters_all`, { timeout: 8000 });
      const list: CharacterMeta[] = (res.data?.characters || [])
        .map((c: any) => ({ id: c.id, name: c.name || c.id }));
      setChars(list);
      if (list.length && !list.find(c => c.id === activeId)) setActiveId(list[0].id);
    } catch (e: any) {
      console.warn('[schedule] 拉角色失败', e?.message);
    }
  };

  const loadSchedule = async (cid: string) => {
    try {
      // 后端在当天没数据时会现场生成,可能要几秒
      const res = await axios.get(`${SERVER_URL}/schedule`, {
        params: { character_id: cid, user_id: FIXED_USER_ID },
        timeout: 45000,
      });
      setItems(res.data?.items || []);
      setServerNow(res.data?.now || '');
      setDateStr(res.data?.date || '');
    } catch (e: any) {
      console.warn('[schedule] 拉日程失败', e?.message);
      setItems([]);
    }
  };

  useFocusEffect(useCallback(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      await loadChars();
      if (!cancelled) await loadSchedule(activeId);
      if (!cancelled) setLoading(false);
    })();
    return () => { cancelled = true; };
  }, [activeId]));

  const onRefresh = async () => {
    setRefreshing(true);
    await loadSchedule(activeId);
    setRefreshing(false);
  };

  const regenerate = () => {
    Alert.alert('重新安排今天?', '会让他重新想一遍今天怎么过,现在这份会被覆盖。', [
      { text: '取消', style: 'cancel' },
      {
        text: '重新安排', onPress: async () => {
          setRegenerating(true);
          try {
            await axios.post(`${SERVER_URL}/schedule/generate`, {
              character_id: activeId, user_id: FIXED_USER_ID, force: true,
            }, { timeout: 60000 });
            await loadSchedule(activeId);
          } catch (e: any) {
            Alert.alert('生成失败', e?.message ?? '再试一次');
          }
          setRegenerating(false);
        }
      },
    ]);
  };

  const nowMin = serverNow ? toMin(serverNow) : -1;
  const current = items.find(it => isNow(it, nowMin));
  const activeName = chars.find(c => c.id === activeId)?.name || activeId;

  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
      <StatusBar barStyle="light-content" backgroundColor={C.card} />

      {/* 顶栏 */}
      <View style={s.header}>
        <TouchableOpacity onPress={() => router.back()} style={s.backBtn}>
          <Text style={s.backText}>‹</Text>
        </TouchableOpacity>
        <View style={{ flex: 1 }}>
          <Text style={s.headerTitle}>{activeName} 的一天</Text>
          <Text style={s.headerSub}>{dateStr}{serverNow ? ` · 现在 ${serverNow}` : ''}</Text>
        </View>
        <TouchableOpacity onPress={regenerate} style={s.regenBtn} disabled={regenerating}>
          {regenerating
            ? <ActivityIndicator size="small" color={C.accent2} />
            : <Text style={s.regenText}>↻</Text>}
        </TouchableOpacity>
      </View>

      {/* 角色切换 */}
      {chars.length > 1 && (
        <ScrollView horizontal showsHorizontalScrollIndicator={false}
                    style={s.chipBar} contentContainerStyle={{ paddingHorizontal: 12, gap: 8 }}>
          {chars.map(c => {
            const on = c.id === activeId;
            return (
              <TouchableOpacity key={c.id} onPress={() => setActiveId(c.id)}
                                style={[s.chip, on && s.chipOn]} activeOpacity={0.8}>
                <Text style={[s.chipText, on && s.chipTextOn]}>{c.name}</Text>
              </TouchableOpacity>
            );
          })}
        </ScrollView>
      )}

      {/* 此刻在做什么 */}
      {current && (
        <View style={s.nowCard}>
          <View style={[s.nowDot, { backgroundColor: current.can_reply ? C.income : '#F59E0B' }]} />
          <View style={{ flex: 1 }}>
            <Text style={s.nowLabel}>
              此刻 · {current.can_reply ? '有空搭理你' : '走不开,只会已读'}
            </Text>
            <Text style={s.nowTitle} numberOfLines={1}>{current.title}</Text>
            {!!current.location && <Text style={s.nowLoc}>{current.location}</Text>}
          </View>
          <Text style={s.nowTime}>~{current.end_time}</Text>
        </View>
      )}

      {loading ? (
        <View style={s.center}>
          <ActivityIndicator color={C.accent} />
          <Text style={s.loadingHint}>他还在想今天怎么过…</Text>
        </View>
      ) : items.length === 0 ? (
        <View style={s.center}>
          <Text style={s.emptyText}>今天还没安排</Text>
          <TouchableOpacity onPress={regenerate} style={s.emptyBtn}>
            <Text style={s.emptyBtnText}>让他排一下</Text>
          </TouchableOpacity>
        </View>
      ) : (
        <ScrollView
          ref={scrollRef}
          contentContainerStyle={{ paddingVertical: 16, paddingRight: 16 }}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh}
                                          tintColor={C.accent2} />}
        >
          {items.map((it, idx) => {
            const now = isNow(it, nowMin);
            const past = nowMin >= 0 && !now && toMin(it.end_time) <= nowMin
                         && toMin(it.start_time) <= toMin(it.end_time);
            return (
              <View key={it.id ?? idx} style={s.row}>
                {/* 左侧时间轴 */}
                <View style={s.timeCol}>
                  <Text style={[s.timeText, now && s.timeTextNow, past && s.dim]}>
                    {it.start_time}
                  </Text>
                </View>
                <View style={s.lineCol}>
                  {idx !== 0 && <View style={[s.line, past && s.lineDim]} />}
                  <View style={[
                    s.node,
                    now && s.nodeNow,
                    past && s.nodeDim,
                  ]} />
                  {idx !== items.length - 1 && <View style={[s.line, { flex: 1 }, past && s.lineDim]} />}
                </View>

                {/* 右侧卡片 */}
                <View style={[s.card, now && s.cardNow, past && s.cardPast]}>
                  <Text style={[s.title, past && s.dim]} numberOfLines={2}>{it.title}</Text>
                  {!!it.location && (
                    <Text style={[s.loc, past && s.dim]} numberOfLines={1}>{it.location}</Text>
                  )}
                  <View style={s.metaRow}>
                    <Text style={[s.range, now && s.rangeNow, past && s.dim]}>
                      {it.start_time} - {it.end_time}
                    </Text>
                    <View style={[
                      s.statusDot,
                      { backgroundColor: it.can_reply ? C.income : '#F59E0B' },
                      past && { opacity: 0.4 },
                    ]} />
                    {!it.can_reply && (
                      <Text style={[s.busyTag, past && s.dim]}>走不开</Text>
                    )}
                  </View>
                  {!!it.note && (
                    <>
                      <View style={s.divider} />
                      <Text style={[s.note, past && s.dim]}>{it.note}</Text>
                    </>
                  )}
                </View>
              </View>
            );
          })}

          <Text style={s.footHint}>
            标橙的时段他走不开,消息只会显示已读 · 忙完会回你
          </Text>
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
  regenBtn: {
    width: 34, height: 34, borderRadius: 17, alignItems: 'center',
    justifyContent: 'center', borderWidth: 1, borderColor: C.border,
  },
  regenText: { color: C.accent2, fontSize: 18 },

  chipBar: { flexGrow: 0, paddingVertical: 10, backgroundColor: C.card },
  chip: {
    paddingHorizontal: 14, paddingVertical: 6, borderRadius: 16,
    borderWidth: 1, borderColor: C.border, backgroundColor: C.bg,
  },
  chipOn: { borderColor: C.accent, backgroundColor: C.accent + '22' },
  chipText: { color: C.textMute, fontSize: 13 },
  chipTextOn: { color: C.accent2, fontWeight: '600' },

  nowCard: {
    flexDirection: 'row', alignItems: 'center', gap: 10,
    marginHorizontal: 16, marginTop: 12, padding: 14,
    backgroundColor: C.card2, borderRadius: 14,
    borderWidth: 1, borderColor: C.accent + '55',
  },
  nowDot: { width: 8, height: 8, borderRadius: 4 },
  nowLabel: { color: C.accent2, fontSize: 10, letterSpacing: 1, marginBottom: 3 },
  nowTitle: { color: C.text, fontSize: 15, fontWeight: '600' },
  nowLoc: { color: C.textMute, fontSize: 11, marginTop: 2 },
  nowTime: { color: C.textDim, fontSize: 12 },

  center: { flex: 1, alignItems: 'center', justifyContent: 'center', gap: 12 },
  loadingHint: { color: C.textMute, fontSize: 12 },
  emptyText: { color: C.textMute, fontSize: 13 },
  emptyBtn: {
    paddingHorizontal: 20, paddingVertical: 10, borderRadius: 20,
    backgroundColor: C.accent,
  },
  emptyBtnText: { color: '#fff', fontSize: 14, fontWeight: '600' },

  row: { flexDirection: 'row', alignItems: 'stretch' },
  timeCol: { width: 52, alignItems: 'flex-end', paddingRight: 8, paddingTop: 12 },
  timeText: { color: C.textMute, fontSize: 11, fontWeight: '600' },
  timeTextNow: { color: C.accent2 },
  lineCol: { width: 20, alignItems: 'center' },
  line: { width: 1.5, backgroundColor: C.border, height: 14 },
  lineDim: { opacity: 0.4 },
  node: {
    width: 9, height: 9, borderRadius: 5, marginVertical: 2,
    backgroundColor: C.bg, borderWidth: 2, borderColor: C.border,
  },
  nodeNow: { borderColor: C.accent2, backgroundColor: C.accent2, width: 11, height: 11, borderRadius: 6 },
  nodeDim: { opacity: 0.4 },

  card: {
    flex: 1, backgroundColor: C.card, borderRadius: 14, padding: 14,
    marginBottom: 12, borderWidth: 1, borderColor: C.border,
  },
  cardNow: { borderColor: C.accent, backgroundColor: C.card2 },
  cardPast: { opacity: 0.55 },
  title: { color: C.text, fontSize: 15, fontWeight: '700', lineHeight: 21 },
  loc: { color: C.textMute, fontSize: 12, marginTop: 3 },
  metaRow: { flexDirection: 'row', alignItems: 'center', gap: 7, marginTop: 8 },
  range: { color: C.textDim, fontSize: 12, fontWeight: '600' },
  rangeNow: { color: C.accent2 },
  statusDot: { width: 7, height: 7, borderRadius: 4 },
  busyTag: { color: '#F59E0B', fontSize: 10, fontWeight: '600' },
  divider: { height: 1, backgroundColor: C.border, marginTop: 10, marginBottom: 8 },
  note: { color: C.textMute, fontSize: 12, lineHeight: 18, fontStyle: 'italic' },
  dim: { opacity: 0.6 },

  footHint: {
    color: C.textMute, fontSize: 11, textAlign: 'center',
    marginTop: 8, marginBottom: 24, paddingHorizontal: 32, lineHeight: 17,
  },
});