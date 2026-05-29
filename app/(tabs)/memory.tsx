// app/(tabs)/memory.tsx — 海马体神经元风格记忆管理页（增强版）
import AsyncStorage from '@react-native-async-storage/async-storage';
import axios from 'axios';
import React, { useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  Animated,
  Dimensions,
  Easing,
  Modal,
  Platform,
  Pressable,
  ScrollView,
  StatusBar,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  View,
} from 'react-native';
import ChibiSprite from '../../components/ChibiSprite';
import { C, SERVER_URL } from '../../constants/theme';

const USER_ID_KEY = 'gojo_user_id';
const { width } = Dimensions.get('window');

// 海马体脑区分类
const MEMORY_CATEGORIES: Record<string, { icon: string; color: string; brainRegion: string }> = {
  '喜好': { icon: '💜', color: '#F5A0C0', brainRegion: '愉悦核' },
  '厌恶': { icon: '💔', color: '#F87171', brainRegion: '杏仁核' },
  '身份': { icon: '👤', color: '#A78BFA', brainRegion: '前额叶' },
  '状态': { icon: '📌', color: '#5BC4FF', brainRegion: '海马回' },
  '经历': { icon: '📖', color: '#FFD700', brainRegion: '颞叶' },
  '关系': { icon: '🤝', color: '#34D399', brainRegion: '镜像区' },
  '其他': { icon: '✨', color: '#9CA3AF', brainRegion: '联想区' },
};

interface LongMemory {
  id: number;
  content: string;
  category: string;
  timestamp: string;
}

// ────── 跳动的神经元节点 ──────
function PulsingNode({ color, size = 8, delay = 0 }: { color: string; size?: number; delay?: number }) {
  const scale = useRef(new Animated.Value(0.6)).current;
  const opacity = useRef(new Animated.Value(0.3)).current;

  useEffect(() => {
    const pulse = () => {
      Animated.loop(
        Animated.sequence([
          Animated.parallel([
            Animated.timing(scale,   { toValue: 1.4, duration: 1200, easing: Easing.out(Easing.quad), useNativeDriver: true }),
            Animated.timing(opacity, { toValue: 0.9, duration: 1200, easing: Easing.out(Easing.quad), useNativeDriver: true }),
          ]),
          Animated.parallel([
            Animated.timing(scale,   { toValue: 0.6, duration: 1200, easing: Easing.in(Easing.quad),  useNativeDriver: true }),
            Animated.timing(opacity, { toValue: 0.3, duration: 1200, easing: Easing.in(Easing.quad),  useNativeDriver: true }),
          ]),
        ])
      ).start();
    };
    const t = setTimeout(pulse, delay);
    return () => clearTimeout(t);
  }, []);

  return (
    <Animated.View
      style={{
        width: size, height: size, borderRadius: size / 2,
        backgroundColor: color, transform: [{ scale }], opacity,
        shadowColor: color, shadowOffset: { width: 0, height: 0 }, shadowOpacity: 0.9, shadowRadius: size, elevation: 4,
      }}
    />
  );
}

// ────── 缓慢呼吸的大脑（空状态用）──────
function BreathingBrain() {
  const scale = useRef(new Animated.Value(1)).current;

  useEffect(() => {
    Animated.loop(
      Animated.sequence([
        Animated.timing(scale, { toValue: 1.08, duration: 2200, easing: Easing.inOut(Easing.sin), useNativeDriver: true }),
        Animated.timing(scale, { toValue: 1.0,  duration: 2200, easing: Easing.inOut(Easing.sin), useNativeDriver: true }),
      ])
    ).start();
  }, []);

  return (
    <Animated.Text style={[s.emptyEmoji, { transform: [{ scale }] }]}>🧠</Animated.Text>
  );
}

// ────── 大脑可视化（顶部头图）──────
function BrainViz({ totalMemories, categoryCount }: { totalMemories: number; categoryCount: number }) {
  return (
    <View style={s.brainViz}>
      <View style={s.brainNetwork}>
        <View style={s.brainCenter}>
          <Text style={s.brainEmoji}>🧠</Text>
        </View>
        <View style={[s.satelliteNode, { top: 6, left: 30 }]}>
          <PulsingNode color="#F5A0C0" size={6} delay={0} />
        </View>
        <View style={[s.satelliteNode, { top: 14, right: 24 }]}>
          <PulsingNode color="#5BC4FF" size={7} delay={300} />
        </View>
        <View style={[s.satelliteNode, { bottom: 8, left: 18 }]}>
          <PulsingNode color="#FFD700" size={5} delay={600} />
        </View>
        <View style={[s.satelliteNode, { bottom: 14, right: 32 }]}>
          <PulsingNode color="#34D399" size={6} delay={900} />
        </View>
        <View style={[s.satelliteNode, { top: 36, left: 6 }]}>
          <PulsingNode color="#A78BFA" size={5} delay={450} />
        </View>
        <View style={[s.satelliteNode, { top: 40, right: 10 }]}>
          <PulsingNode color="#F87171" size={6} delay={750} />
        </View>
      </View>

      <View style={s.brainStats}>
        <View style={s.statItem}>
          <Text style={s.statNumber}>{totalMemories}</Text>
          <Text style={s.statLabel}>神经元</Text>
        </View>
        <View style={s.statDivider} />
        <View style={s.statItem}>
          <Text style={s.statNumber}>{categoryCount}</Text>
          <Text style={s.statLabel}>脑区</Text>
        </View>
      </View>
    </View>
  );
}

// 判断记忆是否是"新生"（24 小时内）
function isFreshMemory(timestamp: string): boolean {
  if (!timestamp) return false;
  const now = Date.now();
  const memTime = new Date(timestamp).getTime();
  return (now - memTime) < 24 * 60 * 60 * 1000;
}

// 友好时间显示
function friendlyTime(timestamp: string): string {
  if (!timestamp) return '';
  const date = new Date(timestamp);
  const now = new Date();
  const diff = (now.getTime() - date.getTime()) / 1000;

  if (diff < 60) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}小时前`;
  if (diff < 172800) return '昨天';
  if (diff < 604800) return `${Math.floor(diff / 86400)}天前`;
  return date.toLocaleDateString('zh-CN');
}

export default function MemoryScreen() {
  const [userId, setUserId] = useState('');
  const [memories, setMemories] = useState<LongMemory[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const [editModal, setEditModal] = useState(false);
  const [editId, setEditId] = useState<number | null>(null);
  const [editContent, setEditContent] = useState('');
  const [editCategory, setEditCategory] = useState('其他');

  useEffect(() => {
    (async () => {
      const uid = await AsyncStorage.getItem(USER_ID_KEY);
      if (uid) {
        setUserId(uid);
        await fetchMemories(uid);
      }
      setLoading(false);
    })();
  }, []);

  const fetchMemories = async (uid: string) => {
    try {
      const res = await axios.get(`${SERVER_URL}/long_memory`, { params: { user_id: uid } });
      if (res.data?.memories) {
        setMemories(res.data.memories);
      }
    } catch (e) {
      console.warn('获取记忆失败', e);
    }
  };

  const refresh = async () => {
    setRefreshing(true);
    await fetchMemories(userId);
    setRefreshing(false);
  };

  const deleteMemory = (mem: LongMemory) => {
    Alert.alert('遗忘这段记忆', `确认让悟忘记「${mem.content}」？`, [
      { text: '取消', style: 'cancel' },
      {
        text: '遗忘',
        style: 'destructive',
        onPress: async () => {
          try {
            await axios.delete(`${SERVER_URL}/long_memory/${mem.id}`);
            setMemories(prev => prev.filter(m => m.id !== mem.id));
          } catch (e) {
            Alert.alert('遗忘失败', '请检查网络');
          }
        },
      },
    ]);
  };

  const openEdit = (mem: LongMemory) => {
    setEditId(mem.id);
    setEditContent(mem.content);
    setEditCategory(mem.category || '其他');
    setEditModal(true);
  };

  const saveEdit = async () => {
    if (!editContent.trim() || editId === null) return;
    try {
      await axios.put(`${SERVER_URL}/long_memory/${editId}`, {
        content: editContent.trim(),
        category: editCategory,
      });
      setMemories(prev =>
        prev.map(m => m.id === editId ? { ...m, content: editContent.trim(), category: editCategory } : m)
      );
      setEditModal(false);
    } catch (e) {
      Alert.alert('修改失败', '请检查网络');
    }
  };

  // 按分类分组
  const grouped: Record<string, LongMemory[]> = {};
  memories.forEach(m => {
    const cat = m.category || '其他';
    if (!grouped[cat]) grouped[cat] = [];
    grouped[cat].push(m);
  });

  // 按记忆数量排序
  const sortedCategories = Object.entries(grouped).sort((a, b) => b[1].length - a[1].length);

  // 统计今日新增
  const todayNewCount = memories.filter(m => isFreshMemory(m.timestamp)).length;

  if (loading) {
    return (
      <View style={{ flex: 1, backgroundColor: C.bg, alignItems: 'center', justifyContent: 'center' }}>
        <ActivityIndicator color={C.accent} />
      </View>
    );
  }

  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
      <StatusBar barStyle="light-content" backgroundColor={C.bg} />

      {/* 顶栏 */}
      <View style={s.header}>
        <View style={s.headerLeft}>
          <Text style={s.headerTitle}>悟的海马体</Text>
          <ChibiSprite pose="tiny" />
        </View>
        <TouchableOpacity onPress={refresh} style={s.refreshBtn}>
          <Text style={s.refreshText}>{refreshing ? '...' : '刷新'}</Text>
        </TouchableOpacity>
      </View>

      <Text style={s.headerHint}>每一颗神经元都是一段记忆 · 长按遗忘 · 点击修改</Text>

      <ScrollView style={s.list} contentContainerStyle={{ paddingBottom: 40 }}>
        {/* ★ 大脑可视化 + 今日新增提示 */}
        {memories.length > 0 && (
          <>
            <BrainViz totalMemories={memories.length} categoryCount={Object.keys(grouped).length} />
            {todayNewCount > 0 && (
              <View style={s.freshBanner}>
                <View style={s.freshDot} />
                <Text style={s.freshBannerText}>今日新生成 {todayNewCount} 颗神经元 ✨</Text>
              </View>
            )}
          </>
        )}

        {memories.length === 0 && (
          <View style={s.emptyWrap}>
            <BreathingBrain />
            <Text style={s.emptyText}>海马体一片空白</Text>
            <Text style={s.emptyHint}>多跟悟聊天，他会自动形成神经元记忆</Text>
          </View>
        )}

        {/* 按分类显示 */}
        {sortedCategories.map(([category, mems]) => {
          const config = MEMORY_CATEGORIES[category] || MEMORY_CATEGORIES['其他'];
          return (
            <View key={category} style={s.categorySection}>
              {/* 脑区标题 */}
              <View style={s.categoryHeader}>
                <PulsingNode color={config.color} size={10} delay={Math.random() * 1000} />
                <Text style={{ fontSize: 16, marginLeft: 6 }}>{config.icon}</Text>
                <Text style={[s.categoryTitle, { color: config.color }]}>{category}</Text>
                <Text style={[s.brainRegionLabel, { color: config.color + '88' }]}>· {config.brainRegion}</Text>
                <View style={{ flex: 1 }} />
                <View style={[s.countBadge, { borderColor: config.color + '55' }]}>
                  <Text style={[s.countText, { color: config.color }]}>{mems.length}</Text>
                </View>
              </View>

              <View style={[s.connectionLine, { backgroundColor: config.color + '22' }]} />

              {/* 神经元卡片 */}
              {mems.map((mem) => {
                const isFresh = isFreshMemory(mem.timestamp);
                return (
                  <TouchableOpacity
                    key={mem.id}
                    style={[
                      s.neuronCard,
                      { borderLeftColor: config.color },
                      isFresh && { backgroundColor: config.color + '0A', borderColor: config.color + '33' },
                    ]}
                    onPress={() => openEdit(mem)}
                    onLongPress={() => deleteMemory(mem)}
                    activeOpacity={0.65}
                  >
                    <View style={s.neuronHead}>
                      <View style={[s.neuronCore, { backgroundColor: config.color }]} />
                      <View style={[s.neuronGlow, { backgroundColor: config.color + '22' }]} />
                    </View>

                    <View style={s.neuronContent}>
                      <View style={s.neuronTopRow}>
                        <Text style={s.neuronText} numberOfLines={3}>{mem.content}</Text>
                        {isFresh && (
                          <View style={[s.freshTag, { backgroundColor: config.color }]}>
                            <Text style={s.freshTagText}>新生</Text>
                          </View>
                        )}
                      </View>
                      <View style={s.neuronMeta}>
                        <Text style={s.neuronTime}>{friendlyTime(mem.timestamp)}</Text>
                        <Text style={[s.neuronTag, { color: config.color + 'AA' }]}>突触 #{mem.id}</Text>
                      </View>
                    </View>
                    <Text style={s.editArrow}>›</Text>
                  </TouchableOpacity>
                );
              })}
            </View>
          );
        })}
      </ScrollView>

      {/* 编辑弹窗 */}
      <Modal visible={editModal} transparent animationType="slide">
        <Pressable style={s.modalOverlay} onPress={() => setEditModal(false)}>
          <Pressable style={s.modalContent} onPress={e => e.stopPropagation()}>
            <Text style={s.modalTitle}>编辑神经元</Text>

            <TextInput
              style={s.modalInput}
              value={editContent}
              onChangeText={setEditContent}
              placeholder="记忆内容..."
              placeholderTextColor={C.textMute}
              multiline
              autoFocus
            />

            <Text style={s.modalLabel}>归属脑区</Text>
            <View style={s.catRow}>
              {Object.entries(MEMORY_CATEGORIES).map(([cat, cfg]) => (
                <TouchableOpacity
                  key={cat}
                  style={[
                    s.catChip,
                    editCategory === cat && { backgroundColor: cfg.color + '33', borderColor: cfg.color },
                  ]}
                  onPress={() => setEditCategory(cat)}
                >
                  <Text style={{ fontSize: 14 }}>{cfg.icon}</Text>
                  <Text style={[s.catChipText, editCategory === cat && { color: cfg.color }]}>{cat}</Text>
                </TouchableOpacity>
              ))}
            </View>

            <View style={s.modalActions}>
              <TouchableOpacity style={s.cancelBtn} onPress={() => setEditModal(false)}>
                <Text style={s.cancelText}>取消</Text>
              </TouchableOpacity>
              <TouchableOpacity
                style={[s.saveBtn, { opacity: editContent.trim() ? 1 : 0.4 }]}
                onPress={saveEdit}
                disabled={!editContent.trim()}
              >
                <Text style={s.saveText}>保存</Text>
              </TouchableOpacity>
            </View>
          </Pressable>
        </Pressable>
      </Modal>
    </View>
  );
}

const s = StyleSheet.create({
  header:       { flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', paddingHorizontal: 20, paddingTop: Platform.OS === 'ios' ? 54 : 44, paddingBottom: 8, backgroundColor: C.bg },
  headerLeft:   { flexDirection: 'row', alignItems: 'center', gap: 10 },
  headerTitle:  { color: C.text, fontSize: 22, fontWeight: '600' },
  headerHint:   { color: C.textMute, fontSize: 11, paddingHorizontal: 20, marginBottom: 8 },
  refreshBtn:   { paddingHorizontal: 12, paddingVertical: 6, borderRadius: 10, borderWidth: 1, borderColor: C.border },
  refreshText:  { color: C.textMute, fontSize: 12 },

  list:         { flex: 1, paddingHorizontal: 16 },

  // 大脑可视化
  brainViz:        { alignItems: 'center', marginVertical: 16, paddingVertical: 20, backgroundColor: 'rgba(255,255,255,0.03)', borderRadius: 20, borderWidth: 1, borderColor: 'rgba(255,255,255,0.06)' },
  brainNetwork:    { width: 130, height: 100, alignItems: 'center', justifyContent: 'center', position: 'relative' },
  brainCenter:     { width: 64, height: 64, borderRadius: 32, backgroundColor: 'rgba(167,139,250,0.15)', borderWidth: 1.5, borderColor: 'rgba(167,139,250,0.4)', alignItems: 'center', justifyContent: 'center', shadowColor: '#A78BFA', shadowOffset: { width: 0, height: 0 }, shadowOpacity: 0.6, shadowRadius: 14, elevation: 8 },
  brainEmoji:      { fontSize: 28 },
  satelliteNode:   { position: 'absolute' },

  brainStats:      { flexDirection: 'row', alignItems: 'center', marginTop: 14, gap: 24 },
  statItem:        { alignItems: 'center' },
  statNumber:      { color: C.text, fontSize: 22, fontWeight: '700', letterSpacing: 1 },
  statLabel:       { color: C.textMute, fontSize: 11, marginTop: 2, letterSpacing: 1 },
  statDivider:     { width: 1, height: 28, backgroundColor: C.border },

  // ★ 今日新增横幅
  freshBanner:     { flexDirection: 'row', alignItems: 'center', backgroundColor: 'rgba(91,196,255,0.08)', borderWidth: 1, borderColor: 'rgba(91,196,255,0.2)', borderRadius: 12, paddingHorizontal: 14, paddingVertical: 10, marginBottom: 16, gap: 10 },
  freshDot:        { width: 8, height: 8, borderRadius: 4, backgroundColor: '#5BC4FF', shadowColor: '#5BC4FF', shadowOffset: { width: 0, height: 0 }, shadowOpacity: 0.8, shadowRadius: 6, elevation: 4 },
  freshBannerText: { color: '#5BC4FF', fontSize: 13, fontWeight: '500' },

  emptyWrap:    { alignItems: 'center', paddingTop: 100 },
  emptyEmoji:   { fontSize: 64, marginBottom: 22, opacity: 0.75 },
  emptyText:    { color: C.textMute, fontSize: 16, marginBottom: 8 },
  emptyHint:    { color: C.textDim, fontSize: 12 },

  // 分类
  categorySection:    { marginBottom: 22 },
  categoryHeader:     { flexDirection: 'row', alignItems: 'center', marginBottom: 6, paddingLeft: 4 },
  categoryTitle:      { fontSize: 15, fontWeight: '600', marginLeft: 6, letterSpacing: 0.5 },
  brainRegionLabel:   { fontSize: 11, marginLeft: 6, fontStyle: 'italic' },
  countBadge:         { paddingHorizontal: 8, paddingVertical: 2, borderRadius: 10, borderWidth: 1 },
  countText:          { fontSize: 11, fontWeight: '600' },
  connectionLine:     { height: 1, marginBottom: 8, marginLeft: 18 },

  // 神经元卡片
  neuronCard:   { flexDirection: 'row', alignItems: 'center', backgroundColor: C.card, borderRadius: 14, padding: 12, paddingLeft: 16, marginBottom: 8, borderLeftWidth: 3, borderWidth: 1, borderColor: 'rgba(255,255,255,0.04)' },
  neuronHead:   { width: 22, height: 22, alignItems: 'center', justifyContent: 'center', marginRight: 10 },
  neuronCore:   { width: 8, height: 8, borderRadius: 4, zIndex: 2 },
  neuronGlow:   { position: 'absolute', width: 20, height: 20, borderRadius: 10 },
  neuronContent:{ flex: 1 },
  neuronTopRow: { flexDirection: 'row', alignItems: 'flex-start', justifyContent: 'space-between' },
  neuronText:   { color: C.text, fontSize: 14, lineHeight: 20, flex: 1 },
  // ★ 新生标签
  freshTag:     { paddingHorizontal: 6, paddingVertical: 2, borderRadius: 8, marginLeft: 8 },
  freshTagText: { color: '#fff', fontSize: 9, fontWeight: '700', letterSpacing: 0.5 },
  neuronMeta:   { flexDirection: 'row', alignItems: 'center', gap: 10, marginTop: 4 },
  neuronTime:   { color: C.textDim, fontSize: 10 },
  neuronTag:    { fontSize: 10, fontWeight: '500', letterSpacing: 0.5 },
  editArrow:    { color: C.textMute, fontSize: 20, paddingLeft: 8 },

  // 弹窗
  modalOverlay: { flex: 1, backgroundColor: 'rgba(0,0,0,0.65)', justifyContent: 'flex-end' },
  modalContent: { backgroundColor: C.card, borderTopLeftRadius: 24, borderTopRightRadius: 24, padding: 24, paddingBottom: Platform.OS === 'ios' ? 40 : 24 },
  modalTitle:   { color: C.text, fontSize: 18, fontWeight: '600', marginBottom: 16 },
  modalInput:   { backgroundColor: C.bg, borderRadius: 12, paddingHorizontal: 16, paddingVertical: 12, color: C.text, fontSize: 14, borderWidth: 1, borderColor: C.border, marginBottom: 16, minHeight: 60, textAlignVertical: 'top' },
  modalLabel:   { color: C.textMute, fontSize: 12, marginBottom: 8, letterSpacing: 1 },

  catRow:       { flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginBottom: 20 },
  catChip:      { flexDirection: 'row', alignItems: 'center', gap: 4, paddingHorizontal: 12, paddingVertical: 6, borderRadius: 16, borderWidth: 1, borderColor: C.border },
  catChipText:  { color: C.textMute, fontSize: 12 },

  modalActions: { flexDirection: 'row', gap: 12 },
  cancelBtn:    { flex: 1, paddingVertical: 14, borderRadius: 14, borderWidth: 1, borderColor: C.border, alignItems: 'center' },
  cancelText:   { color: C.textMute, fontSize: 15 },
  saveBtn:      { flex: 1, paddingVertical: 14, borderRadius: 14, backgroundColor: C.accent, alignItems: 'center' },
  saveText:     { color: '#fff', fontSize: 15, fontWeight: '600' },
});