// app/(tabs)/chat.tsx
// 替换原来的"直接进单聊"。改成微信式会话列表：
//   上面是角色单聊（gojo / geto / ...，从 GET /characters 拉）
//   下面是群（从 GET /groups 拉）
// 点某个 → router.push('/chat/' + id)，进通用聊天页 app/chat/[id].tsx
//
// 视觉：毛玻璃风格（expo-blur），跟项目暗色主题搭。
// 顶部有 ➕ 新建群按钮：弹一个简易选角色面板，建完自动刷新列表。
import axios from 'axios';
import { BlurView } from 'expo-blur';
import { useFocusEffect, useRouter } from 'expo-router';
import React, { useCallback, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
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

interface Character {
  id: string;
  name: string;
  name_en?: string;
  avatar_url?: string | null;
  voice_id?: string;
  greeting?: string;
}

interface Group {
  id: number;
  name: string;
  avatar_url?: string | null;
  member_names: string[];
  last_message?: string;
}

export default function ChatListScreen() {
  const router = useRouter();
  const [chars, setChars]       = useState<Character[]>([]);
  const [groups, setGroups]     = useState<Group[]>([]);
  const [loading, setLoading]   = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [showCreate, setShowCreate] = useState(false);

  const load = async () => {
    try {
      const [cRes, gRes] = await Promise.all([
        axios.get(`${SERVER_URL}/characters`),
        axios.get(`${SERVER_URL}/groups?user_id=${FIXED_USER_ID}`),
      ]);
      setChars(cRes.data?.characters || []);
      setGroups(gRes.data?.groups || []);
    } catch (e: any) {
      console.warn('load list error', e?.message);
    }
  };

  useFocusEffect(useCallback(() => {
    (async () => {
      setLoading(true);
      await load();
      setLoading(false);
    })();
  }, []));

  const onRefresh = async () => {
    setRefreshing(true);
    await load();
    setRefreshing(false);
  };

  const openCharacter = (id: string) => router.push(`/chat/${id}` as any);
  const openGroup     = (id: number)  => router.push(`/chat/group_${id}` as any);

  // 头像首字
  const firstChar = (name: string) => (name?.[0] || '?');

  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
      <StatusBar barStyle="light-content" backgroundColor={C.card} />

      {/* 顶部标题栏 */}
      <View style={s.header}>
        <View style={{ flex: 1 }}>
          <Text style={s.headerTitle}>消息</Text>
          <Text style={s.headerSub}>跟谁聊？</Text>
        </View>
        <TouchableOpacity style={s.addBtn} onPress={() => setShowCreate(true)}>
          <Text style={s.addBtnText}>➕ 新建群</Text>
        </TouchableOpacity>
      </View>

      {loading ? (
        <View style={s.loadingWrap}>
          <ActivityIndicator color={C.accent} />
        </View>
      ) : (
        <ScrollView
          style={{ flex: 1 }}
          contentContainerStyle={{ padding: 16, paddingBottom: 40 }}
          refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={C.accent} />}
        >
          {/* ── 角色单聊 ── */}
          <Text style={s.sectionTitle}>角色单聊</Text>
          {chars.length === 0 && (
            <Text style={s.emptyText}>还没有角色，先去后端建一个吧</Text>
          )}
          {chars.map(c => (
            <GlassRow
              key={c.id}
              title={c.name}
              subtitle={c.id === 'gojo' ? '最强的男人' : (c.name_en || '点击进入聊天')}
              firstChar={firstChar(c.name)}
              accentColor={c.id === 'gojo' ? C.accent : C.accent2}
              onPress={() => openCharacter(c.id)}
            />
          ))}

          {/* ── 群聊 ── */}
          <Text style={[s.sectionTitle, { marginTop: 24 }]}>群聊</Text>
          {groups.length === 0 && (
            <Text style={s.emptyText}>还没有群，点右上角「➕ 新建群」试试</Text>
          )}
          {groups.map(g => (
            <GlassRow
              key={g.id}
              title={g.name}
              subtitle={g.member_names?.length
                ? `群成员：${g.member_names.join('、')}`
                : '空群'}
              lastMsg={g.last_message}
              firstChar={firstChar(g.name)}
              accentColor={C.accent2}
              isGroup
              onPress={() => openGroup(g.id)}
            />
          ))}
        </ScrollView>
      )}

      {/* 新建群模态 */}
      {showCreate && (
        <CreateGroupModal
          characters={chars}
          onClose={() => setShowCreate(false)}
          onCreated={async () => {
            setShowCreate(false);
            await load();
          }}
        />
      )}
    </View>
  );
}

// ─────────────────────────────────────
//  毛玻璃行：单聊 / 群聊 公用
// ─────────────────────────────────────
function GlassRow({
  title, subtitle, lastMsg, firstChar, accentColor, isGroup, onPress,
}: {
  title: string;
  subtitle: string;
  lastMsg?: string;
  firstChar: string;
  accentColor: string;
  isGroup?: boolean;
  onPress: () => void;
}) {
  return (
    <TouchableOpacity activeOpacity={0.7} onPress={onPress} style={s.rowWrap}>
      <BlurView intensity={30} tint="dark" style={s.rowBlur}>
        <View style={[s.rowInner, { borderColor: accentColor + '33' }]}>
          {/* 头像 */}
          <View style={[s.avatar, { backgroundColor: accentColor + '22', borderColor: accentColor }]}>
            <Text style={[s.avatarText, { color: accentColor }]}>
              {isGroup ? '群' : firstChar}
            </Text>
          </View>
          {/* 文本区 */}
          <View style={{ flex: 1, marginLeft: 12 }}>
            <Text style={s.rowTitle} numberOfLines={1}>{title}</Text>
            <Text style={s.rowSub} numberOfLines={1}>{subtitle}</Text>
            {lastMsg ? (
              <Text style={s.rowLast} numberOfLines={1}>{lastMsg}</Text>
            ) : null}
          </View>
          <Text style={s.arrow}>›</Text>
        </View>
      </BlurView>
    </TouchableOpacity>
  );
}

// ─────────────────────────────────────
//  新建群 模态
// ─────────────────────────────────────
function CreateGroupModal({
  characters, onClose, onCreated,
}: {
  characters: Character[];
  onClose: () => void;
  onCreated: () => void;
}) {
  const [name, setName] = useState('');
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [owner, setOwner] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const togglePick = (id: string) => {
    const next = new Set(picked);
    if (next.has(id)) {
      next.delete(id);
      if (owner === id) setOwner(null);
    } else {
      next.add(id);
      if (!owner) setOwner(id);
    }
    setPicked(next);
  };

  const submit = async () => {
    if (!name.trim()) {
      Alert.alert('提示', '群名不能为空');
      return;
    }
    if (picked.size === 0) {
      Alert.alert('提示', '至少选一个角色进群');
      return;
    }
    setSubmitting(true);
    try {
      await axios.post(`${SERVER_URL}/group`, {
        name: name.trim(),
        user_id: FIXED_USER_ID,
        member_ids: Array.from(picked),
        owner_role_id: owner,
      });
      onCreated();
    } catch (e: any) {
      Alert.alert('建群失败', e?.message ?? '请重试');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal visible animationType="slide" transparent statusBarTranslucent>
      <View style={s.modalBackdrop}>
        <BlurView intensity={40} tint="dark" style={s.modalCard}>
          <View style={s.modalInner}>
            <Text style={s.modalTitle}>新建群聊</Text>

            <Text style={s.modalLabel}>群名</Text>
            <TextInput
              style={s.modalInput}
              value={name}
              onChangeText={setName}
              placeholder="例如：悟和夏油"
              placeholderTextColor={C.textMute}
            />

            <Text style={[s.modalLabel, { marginTop: 14 }]}>拉进群的角色（可多选）</Text>
            <ScrollView style={{ maxHeight: 240, marginTop: 6 }}>
              {characters.map(c => {
                const isPicked = picked.has(c.id);
                const isOwner  = owner === c.id;
                return (
                  <TouchableOpacity
                    key={c.id}
                    style={[s.pickRow, isPicked && s.pickRowActive]}
                    onPress={() => togglePick(c.id)}
                    onLongPress={() => { if (isPicked) setOwner(c.id); }}
                  >
                    <View style={[s.checkBox, isPicked && s.checkBoxActive]}>
                      <Text style={{ color: isPicked ? '#fff' : 'transparent', fontSize: 12 }}>✓</Text>
                    </View>
                    <Text style={s.pickName}>{c.name}</Text>
                    {isOwner && <Text style={s.ownerBadge}>群主角色</Text>}
                  </TouchableOpacity>
                );
              })}
            </ScrollView>
            <Text style={s.hintText}>长按某个已选角色，可把他设为"群主角色"（@没人时默认他回）</Text>

            <View style={s.modalBtnRow}>
              <TouchableOpacity style={[s.modalBtn, s.modalBtnGhost]} onPress={onClose} disabled={submitting}>
                <Text style={s.modalBtnGhostText}>取消</Text>
              </TouchableOpacity>
              <TouchableOpacity style={[s.modalBtn, s.modalBtnPrimary]} onPress={submit} disabled={submitting}>
                {submitting
                  ? <ActivityIndicator color="#fff" />
                  : <Text style={s.modalBtnPrimaryText}>建群</Text>}
              </TouchableOpacity>
            </View>
          </View>
        </BlurView>
      </View>
    </Modal>
  );
}

const s = StyleSheet.create({
  header: {
    flexDirection: 'row',
    alignItems: 'flex-end',
    paddingHorizontal: 20,
    paddingTop: Platform.OS === 'ios' ? 56 : 44,
    paddingBottom: 14,
    backgroundColor: C.card,
    borderBottomWidth: 1,
    borderBottomColor: C.border,
  },
  headerTitle: { color: C.text, fontSize: 22, fontWeight: '700' },
  headerSub:   { color: C.textMute, fontSize: 12, marginTop: 4 },
  addBtn: {
    paddingHorizontal: 12, paddingVertical: 8,
    borderRadius: 14, borderWidth: 1, borderColor: C.accent + '66',
    backgroundColor: C.accent + '22',
  },
  addBtnText: { color: C.accent2, fontSize: 13, fontWeight: '600' },

  loadingWrap: { flex: 1, alignItems: 'center', justifyContent: 'center' },

  sectionTitle: {
    color: C.textDim,
    fontSize: 12,
    fontWeight: '600',
    marginBottom: 10,
    marginLeft: 4,
    letterSpacing: 1,
  },
  emptyText: { color: C.textMute, fontSize: 13, paddingVertical: 16, textAlign: 'center' },

  rowWrap: {
    marginBottom: 12,
    borderRadius: 18,
    overflow: 'hidden',
  },
  rowBlur: { borderRadius: 18, overflow: 'hidden' },
  rowInner: {
    flexDirection: 'row',
    alignItems: 'center',
    padding: 14,
    borderRadius: 18,
    borderWidth: 1,
    backgroundColor: 'rgba(255,255,255,0.04)',
  },
  avatar: {
    width: 48, height: 48, borderRadius: 24,
    alignItems: 'center', justifyContent: 'center',
    borderWidth: 1.5,
  },
  avatarText: { fontSize: 18, fontWeight: '700' },
  rowTitle:   { color: C.text, fontSize: 16, fontWeight: '600' },
  rowSub:     { color: C.textMute, fontSize: 12, marginTop: 3 },
  rowLast:    { color: C.textDim, fontSize: 12, marginTop: 4, fontStyle: 'italic' },
  arrow:      { color: C.textMute, fontSize: 22, marginLeft: 8 },

  // 模态
  modalBackdrop: {
    flex: 1,
    backgroundColor: 'rgba(0,0,0,0.55)',
    justifyContent: 'flex-end',
  },
  modalCard: {
    overflow: 'hidden',
    borderTopLeftRadius: 24, borderTopRightRadius: 24,
  },
  modalInner: {
    backgroundColor: 'rgba(13,26,46,0.92)',
    paddingHorizontal: 22, paddingTop: 18, paddingBottom: 30,
    borderTopLeftRadius: 24, borderTopRightRadius: 24,
    borderTopWidth: 1, borderColor: C.border,
  },
  modalTitle:   { color: C.text, fontSize: 18, fontWeight: '700', marginBottom: 14 },
  modalLabel:   { color: C.textDim, fontSize: 12, marginBottom: 6, marginLeft: 2 },
  modalInput: {
    backgroundColor: C.bg, borderRadius: 12,
    borderWidth: 1, borderColor: C.border,
    paddingHorizontal: 14, paddingVertical: 10,
    color: C.text, fontSize: 14,
  },
  pickRow: {
    flexDirection: 'row', alignItems: 'center',
    paddingVertical: 10, paddingHorizontal: 10,
    borderRadius: 10, marginBottom: 6,
    backgroundColor: 'rgba(255,255,255,0.03)',
  },
  pickRowActive: { backgroundColor: C.accent + '22' },
  checkBox: {
    width: 22, height: 22, borderRadius: 6,
    borderWidth: 1.5, borderColor: C.border,
    alignItems: 'center', justifyContent: 'center',
    marginRight: 10,
  },
  checkBoxActive: { backgroundColor: C.accent, borderColor: C.accent },
  pickName: { color: C.text, fontSize: 14, flex: 1 },
  ownerBadge: {
    color: C.accent2, fontSize: 11,
    backgroundColor: C.accent + '22',
    paddingHorizontal: 8, paddingVertical: 3, borderRadius: 8,
  },
  hintText: { color: C.textMute, fontSize: 11, marginTop: 8, lineHeight: 16 },

  modalBtnRow: { flexDirection: 'row', gap: 10, marginTop: 18 },
  modalBtn: { flex: 1, paddingVertical: 12, borderRadius: 14, alignItems: 'center' },
  modalBtnGhost: { borderWidth: 1, borderColor: C.border },
  modalBtnGhostText: { color: C.textMute, fontSize: 14 },
  modalBtnPrimary: { backgroundColor: C.accent },
  modalBtnPrimaryText: { color: '#fff', fontSize: 14, fontWeight: '600' },
});