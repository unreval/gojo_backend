// app/(tabs)/index.tsx — 主页
import { useRouter } from 'expo-router';
import React from 'react';
import { StatusBar, StyleSheet, Text, TouchableOpacity, View } from 'react-native';
import ChibiSprite from '../../components/ChibiSprite';
import { C } from '../../constants/theme';

const TILES = [
  { route: '/chat',       icon: '💬', label: '聊天', sub: '跟悟说话', color: '#5BC4FF' },
  { route: '/calendar',   icon: '📅', label: '日历', sub: '行程提醒', color: '#A78BFA' },
  { route: '/accounting', icon: '💰', label: '记账', sub: '收支记录', color: '#34D399' },
  { route: '/memory',     icon: '🧠', label: '记忆', sub: '悟记得的', color: '#F59E0B' },
];

export default function HomeScreen() {
  const router = useRouter();

  return (
    <View style={s.container}>
      <StatusBar barStyle="light-content" backgroundColor={C.bg} />

      {/* Q版悟 */}
      <View style={s.spriteWrap}>
        <ChibiSprite pose="sit" size={160} />
      </View>

      {/* 标题 */}
      <Text style={s.title}>五条悟</Text>
      <Text style={s.subtitle}>僕が最強だから</Text>

      {/* 四宫格入口 */}
      <View style={s.grid}>
        {TILES.map(tile => (
          <TouchableOpacity
            key={tile.route}
            style={s.tile}
            activeOpacity={0.7}
            onPress={() => router.push(tile.route as any)}
          >
            <Text style={s.tileIcon}>{tile.icon}</Text>
            <Text style={s.tileLabel}>{tile.label}</Text>
            <Text style={s.tileSub}>{tile.sub}</Text>
          </TouchableOpacity>
        ))}
      </View>
    </View>
  );
}

const s = StyleSheet.create({
  container:  { flex: 1, backgroundColor: C.bg, alignItems: 'center', justifyContent: 'center', padding: 24 },
  spriteWrap: { marginBottom: 20 },
  title:      { color: C.text, fontSize: 28, fontWeight: '700', letterSpacing: -0.5 },
  subtitle:   { color: C.textMute, fontSize: 13, letterSpacing: 1, marginTop: 6, marginBottom: 32 },
  grid:       { flexDirection: 'row', flexWrap: 'wrap', justifyContent: 'center', gap: 12, width: '100%', maxWidth: 300 },
  tile:       { width: '47%', backgroundColor: C.card, borderRadius: 16, padding: 20, borderWidth: 1, borderColor: C.border },
  tileIcon:   { fontSize: 28, marginBottom: 8 },
  tileLabel:  { color: C.text, fontSize: 16, fontWeight: '600', marginBottom: 4 },
  tileSub:    { color: C.textMute, fontSize: 11 },
});