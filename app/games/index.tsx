// app/games/index.tsx
// 游戏中心:列出所有小游戏,点击进入。
// 五子棋先做完;像素小鸟、翻牌、2048 先占位显示"开发中",后面加。
// 布局参考首页 TILES 那种网格,配色沿用 theme.C。
import { useRouter } from 'expo-router';
import React from 'react';
import {
    Platform, ScrollView, StatusBar, StyleSheet, Text, TouchableOpacity, View,
} from 'react-native';
import { C } from '../../constants/theme';

interface Game {
  id: string;
  icon: string;
  name: string;
  sub: string;
  color: string;
  route: string;
  available: boolean;
}

const GAMES: Game[] = [
  {
    id: 'gomoku',
    icon: '⚫',
    name: '五子棋',
    sub: '和 TA 下一盘,先五连者胜',
    color: '#5BC4FF',
    route: '/games/gomoku',
    available: true,
  },
  {
    id: 'flappy',
    icon: '🐦',
    name: '像素小鸟',
    sub: '轻点屏幕,让小鸟飞起来',
    color: '#F59E0B',
    route: '/games/flappy',
    available: false,
  },
  {
    id: 'match',
    icon: '🎴',
    name: '翻牌配对',
    sub: '记忆力大挑战',
    color: '#A78BFA',
    route: '/games/match',
    available: false,
  },
  {
    id: '2048',
    icon: '🔢',
    name: '2048',
    sub: '合并数字的经典',
    color: '#34D399',
    route: '/games/2048',
    available: false,
  },
];

export default function GamesHomeScreen() {
  const router = useRouter();

  const onPress = (game: Game) => {
    if (!game.available) return;
    router.push(game.route as any);
  };

  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
      <StatusBar barStyle="light-content" backgroundColor={C.card} />

      <View style={s.header}>
        <TouchableOpacity onPress={() => router.back()} style={s.backBtn}>
          <Text style={s.backText}>‹</Text>
        </TouchableOpacity>
        <View style={{ flex: 1 }}>
          <Text style={s.headerTitle}>游戏</Text>
          <Text style={s.headerSub}>陪你打发一会儿</Text>
        </View>
      </View>

      <ScrollView contentContainerStyle={s.container} showsVerticalScrollIndicator={false}>
        <View style={s.grid}>
          {GAMES.map(g => (
            <TouchableOpacity
              key={g.id}
              style={[s.card, !g.available && s.cardDisabled]}
              activeOpacity={g.available ? 0.75 : 1}
              onPress={() => onPress(g)}
            >
              <View style={[s.iconWrap, { backgroundColor: g.color + '33' }]}>
                <Text style={s.icon}>{g.icon}</Text>
              </View>
              <Text style={s.name}>{g.name}</Text>
              <Text style={s.sub}>{g.sub}</Text>
              {!g.available && (
                <View style={s.soonBadge}>
                  <Text style={s.soonText}>开发中</Text>
                </View>
              )}
            </TouchableOpacity>
          ))}
        </View>

        <View style={{ height: 32 }} />
      </ScrollView>
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

  container: { padding: 16 },
  grid: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: 12,
  },
  card: {
    width: '47%',
    aspectRatio: 1,
    backgroundColor: C.card,
    borderRadius: 16,
    padding: 16,
    justifyContent: 'center',
    alignItems: 'center',
    borderWidth: 1,
    borderColor: C.border,
    position: 'relative',
  },
  cardDisabled: { opacity: 0.5 },
  iconWrap: {
    width: 56,
    height: 56,
    borderRadius: 28,
    justifyContent: 'center',
    alignItems: 'center',
    marginBottom: 12,
  },
  icon: { fontSize: 28 },
  name: { color: C.text, fontSize: 15, fontWeight: '600', marginBottom: 4 },
  sub: { color: C.textMute, fontSize: 11, textAlign: 'center' },
  soonBadge: {
    position: 'absolute',
    top: 10,
    right: 10,
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderRadius: 4,
    backgroundColor: C.border,
  },
  soonText: { color: C.textMute, fontSize: 9 },
});