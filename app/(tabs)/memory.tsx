// app/(tabs)/memory.tsx
// ★ 记忆页 v2 —— 海马体风格保留，按角色分四层：
//   📌 她的事实（shared，全角色共享，按脑区分类）
//   🤝 我们之间（bond between，按角色独立）
//   📖 她告诉过TA的（bond told，剧透与设定）
//   🎭 TA的背景（character_memory，原作设定）
// 交互：顶部切角色 · 点卡片修改 · 长按遗忘 · 背景模块可手动添加
import axios from 'axios';
import { useFocusEffect } from 'expo-router';
import React, { useCallback, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  Image,
  Modal,
  Platform,
  RefreshControl,
  ScrollView,
  StatusBar,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  View,
} from 'react-native';
import { C, SERVER_URL } from '../../constants/theme';

const FIXED_USER_ID = 'user_mofpiyd7442ia7';

interface Character { id: string; name: string; avatar_url?: string | null; }
interface FactMem   { id: number; content: string; category: string; timestamp?: string | null; }
interface BondMem   { id: number; content: string; timestamp?: string | null; }
interface BgMem     { id: number; content: string; category: string; keywords?: string; importance?: number; timestamp?: string | null; }

type ModuleKey = 'facts' | 'between' | 'told' | 'background';

const CATEGORY_ORDER = ['状态', '身份', '喜好', '厌恶', '经历', '关系', '其他'];
const CAT_EMOJI: Record<string, string> = {
  状态: '📌', 身份: '🪪', 喜好: '💗', 厌恶: '🚫', 经历: '🧭', 关系: '🫂', 其他: '🗂',
};
const DOT_COLORS = ['#f472b6', '#60a5fa', '#a78bfa', '#f87171', '#facc15', '#34d399'];

// 后端 naive UTC 时间串 → 本地相对时间
function parseTs(ts?: string | null): number | null {
  if (!ts) return null;
  const iso = ts.includes('T') ? ts : ts.replace(' ', 'T');
  const d = new Date(iso.endsWith('Z') || iso.includes('+') ? iso : iso + 'Z');
  const t = d.getTime();
  return isNaN(t) ? null : t;
}
function relTime(ts?: string | null): string {
  const t = parseTs(ts);
  if (!t) return '';
  const diff = Date.now() - t;
  const min = Math.floor(diff / 60000);
  if (min < 1) return '刚刚';
  if (min < 60) return `${min}分钟前`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}小时前`;
  const day = Math.floor(hr / 24);
  if (day < 7) return `${day}天前`;
  const d = new Date(t);
  return `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()}`;
}
// ★ 新生 = 今天（本地日历日）新增的，不再是"24小时内"——
//   否则昨晚生成的记忆到今天早上还挂着"新生"，看着混乱。
const isNewborn = (ts?: string | null) => {
  const t = parseTs(ts);
  return t != null && new Date(t).toDateString() === new Date().toDateString();
};
const isToday = (ts?: string | null) => {
  const t = parseTs(ts);
  return t != null && new Date(t).toDateString() === new Date().toDateString();
};

export default function MemoryScreen() {
  const [chars, setChars] = useState<Character[]>([]);
  const [charId, setCharId] = useState<string>('gojo');
  const [facts, setFacts] = useState<FactMem[]>([]);
  const [between, setBetween] = useState<BondMem[]>([]);
  const [told, setTold] = useState<BondMem[]>([]);
  const [background, setBackground] = useState<BgMem[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [collapsed, setCollapsed] = useState<Record<ModuleKey, boolean>>({
    facts: false, between: false, told: false, background: true,
  });
  // 编辑/新增弹窗
  const [editing, setEditing] = useState<{ module: ModuleKey; id?: number; content: string; category?: string } | null>(null);
  const [saving, setSaving] = useState(false);

  const loadChars = async () => {
    try {
      const res = await axios.get(`${SERVER_URL}/characters`);
      const list: Character[] = res.data?.characters || [];
      setChars(list);
      if (list.length && !list.find(c => c.id === charId)) setCharId(list[0].id);
      return list;
    } catch { return []; }
  };

  const loadMemories = async (cid: string) => {
    try {
      const [fRes, bRes, tRes, gRes] = await Promise.all([
        axios.get(`${SERVER_URL}/long_memory?user_id=${FIXED_USER_ID}&character_id=${cid}`),
        axios.get(`${SERVER_URL}/bond_memory?user_id=${FIXED_USER_ID}&character_id=${cid}&kind=between`),
        axios.get(`${SERVER_URL}/bond_memory?user_id=${FIXED_USER_ID}&character_id=${cid}&kind=told`),
        axios.get(`${SERVER_URL}/character_memory?character_id=${cid}`),
      ]);
      setFacts(fRes.data?.memories || []);
      setBetween(bRes.data?.memories || []);
      setTold(tRes.data?.memories || []);
      setBackground(gRes.data?.memories || []);
    } catch (e: any) {
      console.warn('load memories error', e?.message);
    }
  };

  useFocusEffect(useCallback(() => {
    (async () => {
      setLoading(true);
      await loadChars();
      await loadMemories(charId);
      setLoading(false);
    })();
  }, [charId]));

  const onRefresh = async () => {
    setRefreshing(true);
    await loadMemories(charId);
    setRefreshing(false);
  };

  const curChar = chars.find(c => c.id === charId);
  const charName = curChar?.name || charId;
  const neurons = facts.length + between.length + told.length + background.length;
  const brainZones = new Set([...facts.map(f => f.category || '其他'), '羁绊', '认知', '背景']).size;
  const todayNew = [...facts, ...between, ...told].filter(m => isToday(m.timestamp)).length;

  // ── 遗忘（删除）──
  const forget = (module: ModuleKey, id: number, content: string) => {
    Alert.alert('遗忘这段记忆？', content.length > 40 ? content.slice(0, 40) + '…' : content, [
      { text: '取消', style: 'cancel' },
      {
        text: '遗忘', style: 'destructive',
        onPress: async () => {
          try {
            const url =
              module === 'facts' ? `${SERVER_URL}/long_memory/${id}` :
              module === 'background' ? `${SERVER_URL}/character_memory/${id}` :
              `${SERVER_URL}/bond_memory/${id}`;
            await axios.delete(url);
            await loadMemories(charId);
          } catch (e: any) {
            Alert.alert('遗忘失败', e?.message ?? '请重试');
          }
        },
      },
    ]);
  };

  // ── 保存（修改 / 新增）──
  const saveEdit = async () => {
    if (!editing) return;
    const content = editing.content.trim();
    if (!content) { Alert.alert('内容不能为空'); return; }
    setSaving(true);
    try {
      if (editing.module === 'facts' && editing.id != null) {
        await axios.put(`${SERVER_URL}/long_memory/${editing.id}`, { content, category: editing.category });
      } else if (editing.module === 'background') {
        if (editing.id != null) {
          await axios.put(`${SERVER_URL}/character_memory/${editing.id}`, { content });
        } else {
          await axios.post(`${SERVER_URL}/character_memory`, { character_id: charId, content, category: '其他' });
        }
      } else if (editing.id != null) {
        await axios.put(`${SERVER_URL}/bond_memory/${editing.id}`, { content });
      }
      setEditing(null);
      await loadMemories(charId);
    } catch (e: any) {
      Alert.alert('保存失败', e?.message ?? '请重试');
    } finally { setSaving(false); }
  };

  const toggle = (k: ModuleKey) => setCollapsed(p => ({ ...p, [k]: !p[k] }));

  // ── 单张记忆卡 ──
  const MemCard = ({ module, id, content, meta, ts }: {
    module: ModuleKey; id: number; content: string; meta?: string; ts?: string | null;
  }) => (
    <TouchableOpacity
      activeOpacity={0.8}
      style={s.card}
      onPress={() => setEditing({
        module, id, content,
        category: module === 'facts' ? (facts.find(f => f.id === id)?.category || '其他') : undefined,
      })}
      onLongPress={() => forget(module, id, content)}
      delayLongPress={400}
    >
      <View style={s.cardDot} />
      <View style={{ flex: 1 }}>
        <View style={{ flexDirection: 'row', alignItems: 'flex-start' }}>
          <Text style={s.cardText}>{content}</Text>
          {isNewborn(ts) && <View style={s.newborn}><Text style={s.newbornText}>新生</Text></View>}
        </View>
        <Text style={s.cardMeta}>
          {relTime(ts)}{meta ? `  ${meta}` : ''}  突触 #{id}
        </Text>
      </View>
      <Text style={s.cardArrow}>›</Text>
    </TouchableOpacity>
  );

  // ── 模块头 ──
  const SectionHead = ({ k, emoji, title, sub, count, extra }: {
    k: ModuleKey; emoji: string; title: string; sub: string; count: number; extra?: React.ReactNode;
  }) => (
    <TouchableOpacity style={s.sectionHead} onPress={() => toggle(k)} activeOpacity={0.7}>
      <View style={s.sectionDot} />
      <Text style={s.sectionEmoji}>{emoji}</Text>
      <Text style={s.sectionTitle}>{title}</Text>
      <Text style={s.sectionSub}> · {sub}</Text>
      <View style={{ flex: 1 }} />
      {extra}
      <View style={s.countPill}><Text style={s.countPillText}>{count}</Text></View>
      <Text style={s.collapseArrow}>{collapsed[k] ? '▸' : '▾'}</Text>
    </TouchableOpacity>
  );

  // 她的事实按脑区分组
  const factsByCat: [string, FactMem[]][] = CATEGORY_ORDER
    .map(cat => [cat, facts.filter(f => (f.category || '其他') === cat)] as [string, FactMem[]])
    .filter(([, arr]) => arr.length > 0);

  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
      <StatusBar barStyle="light-content" backgroundColor={C.card} />

      {/* 顶部 */}
      <View style={s.header}>
        <View style={{ flex: 1 }}>
          <View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}>
            <Text style={s.headerTitle}>{charName}的海马体</Text>
            {curChar?.avatar_url ? (
              <Image source={{ uri: curChar.avatar_url }} style={s.headerAvatar} />
            ) : null}
          </View>
          <Text style={s.headerSub}>每一颗神经元都是一段记忆 · 长按遗忘 · 点击修改</Text>
        </View>
        <TouchableOpacity style={s.refreshBtn} onPress={onRefresh}>
          <Text style={s.refreshText}>刷新</Text>
        </TouchableOpacity>
      </View>

      {/* 角色切换 */}
      <View style={s.charBar}>
        <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ paddingHorizontal: 14, gap: 8 }}>
          {chars.map(c => {
            const active = c.id === charId;
            return (
              <TouchableOpacity key={c.id} style={[s.charChip, active && s.charChipActive]} onPress={() => setCharId(c.id)}>
                <View style={s.charChipAvatar}>
                  {c.avatar_url
                    ? <Image source={{ uri: c.avatar_url }} style={{ width: '100%', height: '100%' }} resizeMode="cover" />
                    : <Text style={s.charChipAvatarText}>{c.name?.[0] || '?'}</Text>}
                </View>
                <Text style={[s.charChipName, active && { color: C.text }]}>{c.name}</Text>
              </TouchableOpacity>
            );
          })}
        </ScrollView>
      </View>

      {loading ? (
        <View style={{ flex: 1, alignItems: 'center', justifyContent: 'center' }}>
          <ActivityIndicator color={C.accent} />
        </View>
      ) : (
        <ScrollView
          contentContainerStyle={{ padding: 16, paddingBottom: 60 }}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={C.accent} />}
        >
          {/* 大脑卡片 */}
          <View style={s.brainCard}>
            <View style={s.brainOrbit}>
              {DOT_COLORS.map((col, i) => {
                const angle = (i / DOT_COLORS.length) * Math.PI * 2;
                return (
                  <View key={i} style={[s.orbitDot, {
                    backgroundColor: col,
                    left: 90 + Math.cos(angle) * 82 - 5,
                    top: 90 + Math.sin(angle) * 82 - 5,
                  }]} />
                );
              })}
              <View style={s.brainCircle}><Text style={{ fontSize: 44 }}>🧠</Text></View>
            </View>
            <View style={s.brainStats}>
              <View style={s.statCol}>
                <Text style={s.statNum}>{neurons}</Text>
                <Text style={s.statLabel}>神经元</Text>
              </View>
              <View style={s.statDivider} />
              <View style={s.statCol}>
                <Text style={s.statNum}>{brainZones}</Text>
                <Text style={s.statLabel}>脑区</Text>
              </View>
            </View>
          </View>

          {todayNew > 0 && (
            <View style={s.todayBar}>
              <View style={s.sectionDot} />
              <Text style={s.todayText}>今日新生成 {todayNew} 颗神经元 ✨</Text>
            </View>
          )}

          {/* 模块一：她的事实 */}
          <SectionHead k="facts" emoji="📌" title="她的事实" sub="全角色共享" count={facts.length} />
          {!collapsed.facts && factsByCat.map(([cat, arr]) => (
            <View key={cat}>
              <Text style={s.catHead}>{CAT_EMOJI[cat] || '🗂'} {cat} · 海马回</Text>
              {arr.map(m => <MemCard key={m.id} module="facts" id={m.id} content={m.content} ts={m.timestamp} />)}
            </View>
          ))}
          {!collapsed.facts && facts.length === 0 && <Text style={s.emptyText}>还没有关于她的记忆</Text>}

          {/* 模块二：我们之间 */}
          <SectionHead k="between" emoji="🤝" title="我们之间" sub={`她和${charName}的共同记忆`} count={between.length} />
          {!collapsed.between && between.map(m => (
            <MemCard key={m.id} module="between" id={m.id} content={m.content} ts={m.timestamp} />
          ))}
          {!collapsed.between && between.length === 0 && <Text style={s.emptyText}>还没有共同的记忆，去创造一些吧</Text>}

          {/* 模块三：她告诉过TA的 */}
          <SectionHead k="told" emoji="📖" title={`她告诉过${charName}的`} sub="剧透与设定" count={told.length} />
          {!collapsed.told && told.map(m => (
            <MemCard key={m.id} module="told" id={m.id} content={m.content} ts={m.timestamp} />
          ))}
          {!collapsed.told && told.length === 0 && <Text style={s.emptyText}>她还没有告诉{charName}什么特别的事</Text>}

          {/* 模块四：TA的背景 */}
          <SectionHead
            k="background" emoji="🎭" title={`${charName}的背景`} sub="原作设定"
            count={background.length}
            extra={
              <TouchableOpacity
                style={s.addBtn}
                onPress={() => setEditing({ module: 'background', content: '' })}
              >
                <Text style={s.addBtnText}>＋</Text>
              </TouchableOpacity>
            }
          />
          {!collapsed.background && background.map(m => (
            <MemCard
              key={m.id} module="background" id={m.id} content={m.content}
              meta={`${m.category || '其他'}${m.importance != null ? ` · 权重${m.importance}` : ''}`}
              ts={m.timestamp}
            />
          ))}
          {!collapsed.background && background.length === 0 && <Text style={s.emptyText}>没有背景记忆</Text>}
        </ScrollView>
      )}

      {/* 编辑 / 新增弹窗 */}
      {editing && (
        <Modal visible transparent animationType="fade" statusBarTranslucent>
          <View style={s.modalBackdrop}>
            <View style={s.modalCard}>
              <Text style={s.modalTitle}>
                {editing.id != null ? '修改这颗神经元' : '新增背景记忆'}
              </Text>
              <TextInput
                style={s.modalInput}
                value={editing.content}
                onChangeText={t => setEditing(p => p ? { ...p, content: t } : p)}
                multiline
                placeholder="记忆内容…"
                placeholderTextColor={C.textMute}
              />
              {editing.module === 'facts' && (
                <View style={s.catPickRow}>
                  {CATEGORY_ORDER.map(cat => (
                    <TouchableOpacity
                      key={cat}
                      style={[s.catPick, editing.category === cat && s.catPickActive]}
                      onPress={() => setEditing(p => p ? { ...p, category: cat } : p)}
                    >
                      <Text style={[s.catPickText, editing.category === cat && { color: '#fff' }]}>{cat}</Text>
                    </TouchableOpacity>
                  ))}
                </View>
              )}
              <View style={s.modalBtnRow}>
                <TouchableOpacity style={[s.modalBtn, s.modalBtnGhost]} onPress={() => setEditing(null)} disabled={saving}>
                  <Text style={s.modalBtnGhostText}>取消</Text>
                </TouchableOpacity>
                <TouchableOpacity style={[s.modalBtn, s.modalBtnPrimary]} onPress={saveEdit} disabled={saving}>
                  {saving ? <ActivityIndicator color="#fff" /> : <Text style={s.modalBtnPrimaryText}>保存</Text>}
                </TouchableOpacity>
              </View>
            </View>
          </View>
        </Modal>
      )}
    </View>
  );
}

const s = StyleSheet.create({
  header: {
    flexDirection: 'row', alignItems: 'flex-end',
    paddingHorizontal: 20, paddingTop: Platform.OS === 'ios' ? 56 : 44, paddingBottom: 12,
    backgroundColor: C.card, borderBottomWidth: 1, borderBottomColor: C.border,
  },
  headerTitle:  { color: C.text, fontSize: 22, fontWeight: '700' },
  headerAvatar: { width: 30, height: 30, borderRadius: 15, backgroundColor: C.bg },
  headerSub:    { color: C.textMute, fontSize: 11, marginTop: 4 },
  refreshBtn: {
    paddingHorizontal: 14, paddingVertical: 8, borderRadius: 12,
    borderWidth: 1, borderColor: C.border,
  },
  refreshText: { color: C.textMute, fontSize: 13 },

  charBar: { backgroundColor: C.card, paddingVertical: 8, borderBottomWidth: 1, borderBottomColor: C.border },
  charChip: {
    flexDirection: 'row', alignItems: 'center', gap: 6,
    paddingHorizontal: 10, paddingVertical: 6, borderRadius: 18,
    backgroundColor: 'rgba(255,255,255,0.04)', borderWidth: 1, borderColor: C.border,
  },
  charChipActive: { borderColor: C.accent, backgroundColor: C.accent + '22' },
  charChipAvatar: {
    width: 24, height: 24, borderRadius: 12, overflow: 'hidden',
    backgroundColor: C.accentDim, alignItems: 'center', justifyContent: 'center',
  },
  charChipAvatarText: { color: '#fff', fontSize: 11, fontWeight: '700' },
  charChipName: { color: C.textMute, fontSize: 13, fontWeight: '600' },

  brainCard: {
    backgroundColor: 'rgba(255,255,255,0.03)',
    borderRadius: 22, borderWidth: 1, borderColor: C.border,
    alignItems: 'center', paddingVertical: 22, marginBottom: 14,
  },
  brainOrbit: { width: 180, height: 180 },
  orbitDot:   { position: 'absolute', width: 10, height: 10, borderRadius: 5, opacity: 0.9 },
  brainCircle: {
    position: 'absolute', left: 90 - 46, top: 90 - 46,
    width: 92, height: 92, borderRadius: 46,
    backgroundColor: '#2a2440', borderWidth: 2, borderColor: '#6d5fa8',
    alignItems: 'center', justifyContent: 'center',
  },
  brainStats:  { flexDirection: 'row', alignItems: 'center', marginTop: 10 },
  statCol:     { alignItems: 'center', paddingHorizontal: 28 },
  statNum:     { color: C.text, fontSize: 34, fontWeight: '800' },
  statLabel:   { color: C.textMute, fontSize: 13, marginTop: 2 },
  statDivider: { width: 1, height: 44, backgroundColor: C.border },

  todayBar: {
    flexDirection: 'row', alignItems: 'center', gap: 10,
    backgroundColor: 'rgba(255,255,255,0.03)',
    borderRadius: 14, borderWidth: 1, borderColor: C.border,
    paddingHorizontal: 14, paddingVertical: 12, marginBottom: 6,
  },
  todayText: { color: C.accent2, fontSize: 14, fontWeight: '600' },

  sectionHead: { flexDirection: 'row', alignItems: 'center', marginTop: 18, marginBottom: 8 },
  sectionDot:  { width: 10, height: 10, borderRadius: 5, backgroundColor: C.accent2 },
  sectionEmoji:{ fontSize: 15, marginLeft: 8 },
  sectionTitle:{ color: C.text, fontSize: 16, fontWeight: '700', marginLeft: 4 },
  sectionSub:  { color: C.textMute, fontSize: 12, fontStyle: 'italic' },
  countPill: {
    borderWidth: 1, borderColor: C.border, borderRadius: 12,
    paddingHorizontal: 10, paddingVertical: 2, marginLeft: 8,
  },
  countPillText: { color: C.textDim, fontSize: 12, fontWeight: '600' },
  collapseArrow: { color: C.textMute, fontSize: 14, marginLeft: 8 },
  addBtn: {
    width: 24, height: 24, borderRadius: 12, borderWidth: 1, borderColor: C.accent + '66',
    alignItems: 'center', justifyContent: 'center', backgroundColor: C.accent + '22',
  },
  addBtnText: { color: C.accent2, fontSize: 14, fontWeight: '700', lineHeight: 16 },

  catHead: { color: C.textDim, fontSize: 12, marginTop: 8, marginBottom: 6, marginLeft: 4 },

  card: {
    flexDirection: 'row', alignItems: 'center', gap: 10,
    backgroundColor: 'rgba(255,255,255,0.03)',
    borderRadius: 16, borderWidth: 1, borderColor: C.border,
    borderLeftWidth: 3, borderLeftColor: C.accent + '99',
    paddingHorizontal: 12, paddingVertical: 12, marginBottom: 8,
  },
  cardDot:  { width: 8, height: 8, borderRadius: 4, backgroundColor: C.accent2 + 'aa' },
  cardText: { color: C.text, fontSize: 14, lineHeight: 20, flex: 1 },
  cardMeta: { color: C.textMute, fontSize: 11, marginTop: 5 },
  cardArrow:{ color: C.textMute, fontSize: 18 },
  newborn: {
    backgroundColor: C.accent2 + '33', borderRadius: 10,
    paddingHorizontal: 8, paddingVertical: 2, marginLeft: 6,
  },
  newbornText: { color: C.accent2, fontSize: 10, fontWeight: '700' },
  emptyText: { color: C.textMute, fontSize: 12, paddingVertical: 10, paddingLeft: 4 },

  modalBackdrop: {
    flex: 1, backgroundColor: 'rgba(0,0,0,0.6)',
    alignItems: 'center', justifyContent: 'center', padding: 24,
  },
  modalCard: {
    width: '100%', backgroundColor: 'rgba(13,26,46,0.97)',
    borderRadius: 20, borderWidth: 1, borderColor: C.border, padding: 18,
  },
  modalTitle: { color: C.text, fontSize: 16, fontWeight: '700', marginBottom: 12 },
  modalInput: {
    backgroundColor: C.bg, borderRadius: 12, borderWidth: 1, borderColor: C.border,
    paddingHorizontal: 14, paddingVertical: 10, color: C.text, fontSize: 14,
    minHeight: 90, textAlignVertical: 'top',
  },
  catPickRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 6, marginTop: 10 },
  catPick: {
    paddingHorizontal: 12, paddingVertical: 6, borderRadius: 12,
    borderWidth: 1, borderColor: C.border,
  },
  catPickActive: { backgroundColor: C.accent, borderColor: C.accent },
  catPickText:   { color: C.textMute, fontSize: 12 },
  modalBtnRow: { flexDirection: 'row', gap: 10, marginTop: 14 },
  modalBtn: { flex: 1, paddingVertical: 12, borderRadius: 14, alignItems: 'center' },
  modalBtnGhost: { borderWidth: 1, borderColor: C.border },
  modalBtnGhostText: { color: C.textMute, fontSize: 14 },
  modalBtnPrimary: { backgroundColor: C.accent },
  modalBtnPrimaryText: { color: '#fff', fontSize: 14, fontWeight: '600' },
});