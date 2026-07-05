// app/(tabs)/settings.tsx
// ★ 设置页 v1 —— 只放"配置"，不放"内容"（记忆以后挪去角色详情页）
//   1. 服务器地址：可编辑 + 保存 + 测试连接（解决 SERVER_URL 写死在代码里的问题）
//   2. 密钥状态：从后端 /config/status 拉取，只读显示（密钥本体在 Zeabur 环境变量里，出于安全不在 app 里编辑）
//   3. 默认角色：选一个角色作为默认，存在本机（后续版本接入电话/主动提醒等功能）
//   4. 关于
import AsyncStorage from '@react-native-async-storage/async-storage';
import axios from 'axios';
import { useFocusEffect } from 'expo-router';
import React, { useCallback, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  Image,
  Platform,
  ScrollView,
  StatusBar,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  View,
} from 'react-native';
import { C, SERVER_URL, setServerUrl } from '../../constants/theme';

const DEFAULT_CHAR_KEY = 'default_character_id';

interface Character {
  id: string;
  name: string;
  avatar_url?: string | null;
}

interface ConfigStatus {
  anthropic_key: boolean;
  tts_key: boolean;
  groq_key: boolean;
  database: boolean;
  default_character: string;
}

export default function SettingsScreen() {
  const [urlInput, setUrlInput]   = useState(SERVER_URL);
  const [testing, setTesting]     = useState(false);
  const [status, setStatus]       = useState<ConfigStatus | null>(null);
  const [chars, setChars]         = useState<Character[]>([]);
  const [defaultChar, setDefaultChar] = useState<string>('gojo');

  const loadAll = async () => {
    setUrlInput(SERVER_URL);
    try {
      const saved = await AsyncStorage.getItem(DEFAULT_CHAR_KEY);
      if (saved) setDefaultChar(saved);
    } catch {}
    try {
      const [sRes, cRes] = await Promise.all([
        axios.get(`${SERVER_URL}/config/status`, { timeout: 8000 }),
        axios.get(`${SERVER_URL}/characters`, { timeout: 8000 }),
      ]);
      setStatus(sRes.data);
      setChars(cRes.data?.characters || []);
    } catch (e: any) {
      console.warn('settings load error', e?.message);
      setStatus(null);
    }
  };

  useFocusEffect(useCallback(() => { loadAll(); }, []));

  // ── 服务器地址 ──
  const saveUrl = async () => {
    const url = urlInput.trim().replace(/\/+$/, '');
    if (!url.startsWith('http')) {
      Alert.alert('地址不对', '要以 http:// 或 https:// 开头');
      return;
    }
    await setServerUrl(url);
    Alert.alert('已保存', '新地址立即生效，建议点一下「测试连接」确认');
    loadAll();
  };

  const testConnection = async () => {
    setTesting(true);
    try {
      const url = urlInput.trim().replace(/\/+$/, '');
      const res = await axios.get(`${url}/characters`, { timeout: 8000 });
      const n = res.data?.characters?.length ?? 0;
      Alert.alert('✅ 连接成功', `服务器正常，共 ${n} 个角色`);
    } catch (e: any) {
      Alert.alert('❌ 连接失败', e?.message ?? '请检查地址和网络');
    } finally {
      setTesting(false);
    }
  };

  // ── 默认角色 ──
  const pickDefault = async (id: string) => {
    setDefaultChar(id);
    try { await AsyncStorage.setItem(DEFAULT_CHAR_KEY, id); } catch {}
  };

  const StatusDot = ({ ok }: { ok: boolean | undefined }) => (
    <View style={[st.dot, { backgroundColor: ok ? '#4ade80' : '#f87171' }]} />
  );

  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
      <StatusBar barStyle="light-content" backgroundColor={C.card} />

      <View style={st.header}>
        <Text style={st.headerTitle}>设置</Text>
        <Text style={st.headerSub}>全局配置放这里，记忆管理以后搬去角色页</Text>
      </View>

      <ScrollView contentContainerStyle={{ padding: 16, paddingBottom: 60 }}>
        {/* ── 服务器 ── */}
        <Text style={st.sectionTitle}>服务器</Text>
        <View style={st.card}>
          <Text style={st.label}>后端地址</Text>
          <TextInput
            style={st.input}
            value={urlInput}
            onChangeText={setUrlInput}
            placeholder="https://your-backend.zeabur.app"
            placeholderTextColor={C.textMute}
            autoCapitalize="none"
            autoCorrect={false}
          />
          <View style={st.btnRow}>
            <TouchableOpacity style={[st.btn, st.btnGhost]} onPress={testConnection} disabled={testing}>
              {testing
                ? <ActivityIndicator size="small" color={C.accent} />
                : <Text style={st.btnGhostText}>测试连接</Text>}
            </TouchableOpacity>
            <TouchableOpacity style={[st.btn, st.btnPrimary]} onPress={saveUrl}>
              <Text style={st.btnPrimaryText}>保存</Text>
            </TouchableOpacity>
          </View>
          <Text style={st.hint}>改完立即生效并保存在本机，重装 app 后需要重新填。</Text>
        </View>

        {/* ── 服务状态 ── */}
        <Text style={st.sectionTitle}>服务状态</Text>
        <View style={st.card}>
          {status ? (
            <>
              <View style={st.statusRow}>
                <StatusDot ok={status.anthropic_key} />
                <Text style={st.statusName}>Anthropic API Key</Text>
                <Text style={st.statusVal}>{status.anthropic_key ? '已配置' : '未配置'}</Text>
              </View>
              <View style={st.statusRow}>
                <StatusDot ok={status.tts_key} />
                <Text style={st.statusName}>TTS（Fish Audio）Key</Text>
                <Text style={st.statusVal}>{status.tts_key ? '已配置' : '未配置'}</Text>
              </View>
              <View style={st.statusRow}>
                <StatusDot ok={status.groq_key} />
                <Text style={st.statusName}>Groq Key（语音电话识别）</Text>
                <Text style={st.statusVal}>{status.groq_key ? '已配置' : '未配置'}</Text>
              </View>
              <View style={st.statusRow}>
                <StatusDot ok={status.database} />
                <Text style={st.statusName}>数据库</Text>
                <Text style={st.statusVal}>{status.database ? '正常' : '异常'}</Text>
              </View>
              <Text style={st.hint}>
                密钥保存在服务器的环境变量里（Zeabur → 你的服务 → Variables），出于安全不在 app 里编辑。
              </Text>
            </>
          ) : (
            <Text style={st.hint}>拿不到状态——先确认上面的服务器地址能连通。</Text>
          )}
        </View>

        {/* ── 默认角色 ── */}
        <Text style={st.sectionTitle}>默认角色</Text>
        <View style={st.card}>
          {chars.length === 0 ? (
            <Text style={st.hint}>还没拉到角色列表</Text>
          ) : (
            chars.map(c => {
              const active = defaultChar === c.id;
              return (
                <TouchableOpacity key={c.id} style={[st.pickRow, active && st.pickRowActive]} onPress={() => pickDefault(c.id)}>
                  <View style={st.pickAvatar}>
                    {c.avatar_url ? (
                      <Image source={{ uri: c.avatar_url }} style={{ width: '100%', height: '100%' }} resizeMode="cover" />
                    ) : (
                      <Text style={st.pickAvatarText}>{c.name?.[0] || '?'}</Text>
                    )}
                  </View>
                  <Text style={st.pickName}>{c.name}</Text>
                  {active && <Text style={st.pickCheck}>✓ 默认</Text>}
                </TouchableOpacity>
              );
            })
          )}
          <Text style={st.hint}>
            当前版本先记录你的选择；电话、主动提醒等功能改为跟随默认角色会在下个版本接入。
          </Text>
        </View>

        {/* ── 关于 ── */}
        <Text style={st.sectionTitle}>关于</Text>
        <View style={st.card}>
          <View style={st.statusRow}>
            <Text style={st.statusName}>当前服务器</Text>
            <Text style={[st.statusVal, { flexShrink: 1 }]} numberOfLines={1}>{SERVER_URL}</Text>
          </View>
          <View style={st.statusRow}>
            <Text style={st.statusName}>平台</Text>
            <Text style={st.statusVal}>{Platform.OS}</Text>
          </View>
        </View>
      </ScrollView>
    </View>
  );
}

const st = StyleSheet.create({
  header: {
    paddingHorizontal: 20,
    paddingTop: Platform.OS === 'ios' ? 56 : 44,
    paddingBottom: 14,
    backgroundColor: C.card,
    borderBottomWidth: 1,
    borderBottomColor: C.border,
  },
  headerTitle: { color: C.text, fontSize: 22, fontWeight: '700' },
  headerSub:   { color: C.textMute, fontSize: 12, marginTop: 4 },

  sectionTitle: { color: C.textDim, fontSize: 12, fontWeight: '600', marginBottom: 8, marginLeft: 4, marginTop: 18, letterSpacing: 1 },
  card: {
    backgroundColor: 'rgba(255,255,255,0.04)',
    borderRadius: 16, borderWidth: 1, borderColor: C.border,
    padding: 14,
  },
  label: { color: C.textDim, fontSize: 12, marginBottom: 6 },
  input: {
    backgroundColor: C.bg, borderRadius: 12,
    borderWidth: 1, borderColor: C.border,
    paddingHorizontal: 14, paddingVertical: 10,
    color: C.text, fontSize: 13,
  },
  btnRow: { flexDirection: 'row', gap: 10, marginTop: 12 },
  btn: { flex: 1, paddingVertical: 11, borderRadius: 12, alignItems: 'center', justifyContent: 'center' },
  btnGhost: { borderWidth: 1, borderColor: C.border },
  btnGhostText: { color: C.accent2, fontSize: 13, fontWeight: '600' },
  btnPrimary: { backgroundColor: C.accent },
  btnPrimaryText: { color: '#fff', fontSize: 13, fontWeight: '600' },
  hint: { color: C.textMute, fontSize: 11, marginTop: 10, lineHeight: 16 },

  statusRow: { flexDirection: 'row', alignItems: 'center', paddingVertical: 8, gap: 8 },
  dot: { width: 8, height: 8, borderRadius: 4 },
  statusName: { color: C.text, fontSize: 13, flex: 1 },
  statusVal: { color: C.textMute, fontSize: 12 },

  pickRow: {
    flexDirection: 'row', alignItems: 'center',
    paddingVertical: 9, paddingHorizontal: 10,
    borderRadius: 12, marginBottom: 6, gap: 10,
    backgroundColor: 'rgba(255,255,255,0.03)',
  },
  pickRowActive: { backgroundColor: C.accent + '22', borderWidth: 1, borderColor: C.accent + '55' },
  pickAvatar: { width: 32, height: 32, borderRadius: 16, backgroundColor: C.accentDim, alignItems: 'center', justifyContent: 'center', overflow: 'hidden' },
  pickAvatarText: { color: '#fff', fontSize: 13, fontWeight: '700' },
  pickName: { color: C.text, fontSize: 14, flex: 1 },
  pickCheck: { color: C.accent2, fontSize: 12, fontWeight: '600' },
});