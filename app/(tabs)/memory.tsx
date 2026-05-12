// app/(tabs)/memory.tsx — 记忆管理页（连接后端PostgreSQL）
import AsyncStorage from '@react-native-async-storage/async-storage';
import axios from 'axios';
import React, { useEffect, useState } from 'react';
import {
    ActivityIndicator,
    Alert,
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

// 记忆分类配置
const MEMORY_CATEGORIES: Record<string, { icon: string; color: string }> = {
  '喜好': { icon: '💜', color: '#F5A0C0' },
  '厌恶': { icon: '💔', color: '#F87171' },
  '身份': { icon: '👤', color: '#A78BFA' },
  '状态': { icon: '📌', color: '#5BC4FF' },
  '经历': { icon: '📖', color: '#FFD700' },
  '关系': { icon: '🤝', color: '#34D399' },
  '其他': { icon: '📝', color: '#9CA3AF' },
};

interface LongMemory {
  id: number;
  content: string;
  category: string;
  timestamp: string;
}

export default function MemoryScreen() {
  const [userId, setUserId] = useState('');
  const [memories, setMemories] = useState<LongMemory[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  // 编辑弹窗
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

  // 获取所有长期记忆
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

  // 删除记忆
  const deleteMemory = (mem: LongMemory) => {
    Alert.alert('删除记忆', `确认让悟忘记「${mem.content}」？`, [
      { text: '取消', style: 'cancel' },
      {
        text: '删除',
        style: 'destructive',
        onPress: async () => {
          try {
            await axios.delete(`${SERVER_URL}/long_memory/${mem.id}`);
            setMemories(prev => prev.filter(m => m.id !== mem.id));
          } catch (e) {
            Alert.alert('删除失败', '请检查网络');
          }
        },
      },
    ]);
  };

  // 打开编辑弹窗
  const openEdit = (mem: LongMemory) => {
    setEditId(mem.id);
    setEditContent(mem.content);
    setEditCategory(mem.category || '其他');
    setEditModal(true);
  };

  // 保存编辑
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
          <Text style={s.headerTitle}>悟的记忆</Text>
          <ChibiSprite pose="tiny" />
        </View>
        <TouchableOpacity onPress={refresh} style={s.refreshBtn}>
          <Text style={s.refreshText}>{refreshing ? '...' : '刷新'}</Text>
        </TouchableOpacity>
      </View>

      <Text style={s.headerHint}>悟从聊天中记住的事情　长按删除 · 点击修改</Text>

      {/* 记忆列表 */}
      <ScrollView style={s.list} contentContainerStyle={{ paddingBottom: 40 }}>
        {memories.length === 0 && (
          <View style={s.emptyWrap}>
            <Text style={s.emptyEmoji}>🧠</Text>
            <Text style={s.emptyText}>悟还没有记住任何事情</Text>
            <Text style={s.emptyHint}>多跟悟聊天，他会自动记住重要的事</Text>
          </View>
        )}

        {Object.entries(grouped).map(([category, mems]) => {
          const config = MEMORY_CATEGORIES[category] || MEMORY_CATEGORIES['其他'];
          return (
            <View key={category} style={s.categorySection}>
              {/* 分类标题 */}
              <View style={s.categoryHeader}>
                <Text style={{ fontSize: 16 }}>{config.icon}</Text>
                <Text style={[s.categoryTitle, { color: config.color }]}>{category}</Text>
                <Text style={s.categoryCount}>{mems.length}</Text>
              </View>

              {/* 该分类下的记忆 */}
              {mems.map(mem => (
                <TouchableOpacity
                  key={mem.id}
                  style={s.memoryCard}
                  onPress={() => openEdit(mem)}
                  onLongPress={() => deleteMemory(mem)}
                  activeOpacity={0.7}
                >
                  <View style={[s.memoryDot, { backgroundColor: config.color }]} />
                  <View style={s.memoryInfo}>
                    <Text style={s.memoryContent}>{mem.content}</Text>
                    <Text style={s.memoryTime}>
                      {mem.timestamp ? new Date(mem.timestamp).toLocaleDateString('zh-CN') : ''}
                    </Text>
                  </View>
                  <Text style={s.editArrow}>›</Text>
                </TouchableOpacity>
              ))}
            </View>
          );
        })}
      </ScrollView>

      {/* 编辑弹窗 */}
      <Modal visible={editModal} transparent animationType="slide">
        <Pressable style={s.modalOverlay} onPress={() => setEditModal(false)}>
          <Pressable style={s.modalContent} onPress={e => e.stopPropagation()}>
            <Text style={s.modalTitle}>修改记忆</Text>

            <TextInput
              style={s.modalInput}
              value={editContent}
              onChangeText={setEditContent}
              placeholder="记忆内容..."
              placeholderTextColor={C.textMute}
              multiline
              autoFocus
            />

            <Text style={s.modalLabel}>分类</Text>
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
  headerHint:   { color: C.textMute, fontSize: 11, paddingHorizontal: 20, marginBottom: 12 },
  refreshBtn:   { paddingHorizontal: 12, paddingVertical: 6, borderRadius: 10, borderWidth: 1, borderColor: C.border },
  refreshText:  { color: C.textMute, fontSize: 12 },

  list:         { flex: 1, paddingHorizontal: 16 },
  emptyWrap:    { alignItems: 'center', paddingTop: 80 },
  emptyEmoji:   { fontSize: 48, marginBottom: 16 },
  emptyText:    { color: C.textMute, fontSize: 15, marginBottom: 6 },
  emptyHint:    { color: C.textDim, fontSize: 12 },

  categorySection: { marginBottom: 20 },
  categoryHeader:  { flexDirection: 'row', alignItems: 'center', gap: 6, marginBottom: 10, paddingLeft: 4 },
  categoryTitle:   { fontSize: 14, fontWeight: '600' },
  categoryCount:   { color: C.textMute, fontSize: 11, marginLeft: 4 },

  memoryCard:   { flexDirection: 'row', alignItems: 'center', backgroundColor: C.card, borderRadius: 14, padding: 14, marginBottom: 8, borderWidth: 1, borderColor: C.border },
  memoryDot:    { width: 4, height: 24, borderRadius: 2, marginRight: 12 },
  memoryInfo:   { flex: 1 },
  memoryContent:{ color: C.text, fontSize: 14, lineHeight: 20 },
  memoryTime:   { color: C.textDim, fontSize: 11, marginTop: 4 },
  editArrow:    { color: C.textMute, fontSize: 20, paddingLeft: 8 },

  modalOverlay: { flex: 1, backgroundColor: 'rgba(0,0,0,0.6)', justifyContent: 'flex-end' },
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