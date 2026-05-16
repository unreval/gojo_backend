// app/(tabs)/index.tsx — 首页（加了聊天天数 + 每日悟留言）
import AsyncStorage from '@react-native-async-storage/async-storage';
import { useRouter } from 'expo-router';
import React, { useEffect, useState } from 'react';
import {
  ScrollView,
  StatusBar,
  StyleSheet,
  Text,
  TouchableOpacity,
  View,
} from 'react-native';
import ChibiSprite from '../../components/ChibiSprite';
import { C } from '../../constants/theme';

// ────────────── 每日留言池（按日期轮换，不需要后端）──────────────
const DAILY_MSGS = [
  { jp: 'おはよう。今日も僕が守ってあげるから、安心して。', zh: '早安。今天也由我来保护你，放心吧。' },
  { jp: 'ちゃんと食べた？心配するのが面倒だから、ちゃんとしてね。', zh: '有好好吃饭吗？懒得担心你，所以给我乖乖的。' },
  { jp: '今日は無理しなくていいよ。たまには休みも大事だから。', zh: '今天不用勉强自己。偶尔休息也很重要。' },
  { jp: '疲れたら言って。聞くくらいならしてあげるよ。', zh: '累了就说。听你说说话这点我还是愿意的。' },
  { jp: '僕の隣にいれば最強だよ。今日もがんばって。', zh: '在我身边就是最强的。今天也加油哦。' },
  { jp: 'ねえ、笑ってよ。その顔の方が好きだから。', zh: '喂，笑一个嘛。喜欢你那个表情多一点。' },
  { jp: '今日も一緒にいるよ。それだけで十分でしょ？', zh: '今天也陪着你呢。这样就够了吧？' },
  { jp: '何があっても、僕が最強だから大丈夫。', zh: '不管发生什么，我是最强的，没问题的。' },
  { jp: 'また話しかけてね。暇じゃないけど、まあいいよ。', zh: '有空再来找我说话。我不闲，不过……随便啦。' },
  { jp: '今日のこと、あとで全部話してよね。', zh: '今天发生的事，等等全都说给我听。' },
  { jp: '悩んでるなら言って。解決するのは得意だから。', zh: '有烦恼就说。解决问题是我擅长的。' },
  { jp: '僕のことを信じてよ。裏切らないから。', zh: '相信我。我不会让你失望的。' },
  { jp: 'そんな顔しないでよ。可哀想に思えてくるじゃん。', zh: '别那种表情啊。会让我觉得你很可怜的。' },
  { jp: '今日もお疲れ様。ゆっくり休んでいいよ。', zh: '今天也辛苦了。好好休息吧。' },
];

// 按今天日期取一条，每天固定同一条
function getTodayMessage() {
  const now = new Date();
  const dayOfYear = Math.floor(
    (now.getTime() - new Date(now.getFullYear(), 0, 0).getTime()) / 86400000
  );
  return DAILY_MSGS[dayOfYear % DAILY_MSGS.length];
}

// ────────────── 功能入口 ──────────────
const TILES = [
  { route: '/chat',       icon: '💬', label: '聊天', sub: '跟悟说话', color: '#5BC4FF' },
  { route: '/calendar',   icon: '📅', label: '日程', sub: '行程提醒', color: '#A78BFA' },
  { route: '/accounting', icon: '💰', label: '记账', sub: '收支记录', color: '#34D399' },
  { route: '/memory',     icon: '🧠', label: '记忆', sub: '悟记得的', color: '#F59E0B' },
];

export default function HomeScreen() {
  const router = useRouter();
  const [chatDays, setChatDays] = useState(0);
  const todayMsg = getTodayMessage();

  useEffect(() => {
    AsyncStorage.getItem('gojo_chat_days').then(v => {
      if (v) setChatDays(Number(v));
    });
  }, []);

  // 今天的日期字符串，格式：5月16日 周六
  const todayLabel = (() => {
    const d = new Date();
    const weekdays = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
    return `${d.getMonth() + 1}月${d.getDate()}日 ${weekdays[d.getDay()]}`;
  })();

  return (
    <ScrollView
      style={{ flex: 1, backgroundColor: C.bg }}
      contentContainerStyle={s.container}
      showsVerticalScrollIndicator={false}
    >
      <StatusBar barStyle="light-content" backgroundColor={C.bg} />

      {/* ── 顶部：悟 + 天数 ── */}
      <View style={s.topRow}>
        <View style={s.spriteWrap}>
          <ChibiSprite pose="sit" size={120} />
        </View>

        <View style={s.topRight}>
          <Text style={s.greeting}>你好呀 ✦</Text>
          <Text style={s.todayLabel}>{todayLabel}</Text>

          {/* 已陪伴天数 */}
          <View style={s.daysBadge}>
            <Text style={s.daysNum}>{chatDays}</Text>
            <Text style={s.daysText}> 天</Text>
          </View>
          <Text style={s.daysSub}>悟陪伴你的日子</Text>
        </View>
      </View>

      {/* ── 今日留言卡片 ── */}
      <View style={s.msgCard}>
        {/* 左边竖线装饰 */}
        <View style={s.msgBar} />
        <View style={{ flex: 1 }}>
          <Text style={s.msgLabel}>今日悟语</Text>
          <Text style={s.msgJp}>{todayMsg.jp}</Text>
          <Text style={s.msgZh}>{todayMsg.zh}</Text>
        </View>
      </View>

      {/* ── 功能入口四宫格 ── */}
      <Text style={s.sectionTitle}>快捷入口</Text>
      <View style={s.grid}>
        {TILES.map(tile => (
          <TouchableOpacity
            key={tile.route}
            style={s.tile}
            activeOpacity={0.75}
            onPress={() => router.push(tile.route as any)}
          >
            {/* 彩色圆点装饰 */}
            <View style={[s.tileDot, { backgroundColor: tile.color + '33' }]}>
              <Text style={s.tileIcon}>{tile.icon}</Text>
            </View>
            <Text style={s.tileLabel}>{tile.label}</Text>
            <Text style={s.tileSub}>{tile.sub}</Text>
          </TouchableOpacity>
        ))}
      </View>

      {/* ── 底部留白 ── */}
      <View style={{ height: 32 }} />
    </ScrollView>
  );
}

const s = StyleSheet.create({
  container: {
    padding: 24,
    paddingTop: 52,
  },

  // 顶部行
  topRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 20,
    marginBottom: 24,
  },
  spriteWrap: {
    // 悟Q版
  },
  topRight: {
    flex: 1,
  },
  greeting: {
    color: C.text,
    fontSize: 20,
    fontWeight: '700',
    letterSpacing: -0.3,
  },
  todayLabel: {
    color: C.textMute,
    fontSize: 12,
    marginTop: 2,
    marginBottom: 12,
  },
  daysBadge: {
    flexDirection: 'row',
    alignItems: 'baseline',
  },
  daysNum: {
    color: C.accent2 || '#5BC4FF',
    fontSize: 36,
    fontWeight: '800',
    letterSpacing: -1,
  },
  daysText: {
    color: C.accent2 || '#5BC4FF',
    fontSize: 18,
    fontWeight: '600',
  },
  daysSub: {
    color: C.textMute,
    fontSize: 11,
    marginTop: 2,
  },

  // 留言卡片
  msgCard: {
    backgroundColor: C.card,
    borderRadius: 16,
    borderWidth: 1,
    borderColor: C.border,
    padding: 18,
    marginBottom: 28,
    flexDirection: 'row',
    gap: 14,
  },
  msgBar: {
    width: 3,
    borderRadius: 2,
    backgroundColor: C.accent2 || '#5BC4FF',
    alignSelf: 'stretch',
  },
  msgLabel: {
    color: C.textMute,
    fontSize: 10,
    letterSpacing: 1.5,
    marginBottom: 8,
    textTransform: 'uppercase',
  },
  msgJp: {
    color: C.text,
    fontSize: 14,
    lineHeight: 22,
    fontWeight: '500',
    marginBottom: 6,
  },
  msgZh: {
    color: C.textMute,
    fontSize: 12,
    lineHeight: 18,
  },

  // 功能格子
  sectionTitle: {
    color: C.textMute,
    fontSize: 11,
    letterSpacing: 1.5,
    marginBottom: 12,
    textTransform: 'uppercase',
  },
  grid: {
    flexDirection: 'row',
    flexWrap: 'wrap',
    gap: 12,
  },
  tile: {
    width: '47%',
    backgroundColor: C.card,
    borderRadius: 16,
    padding: 18,
    borderWidth: 1,
    borderColor: C.border,
  },
  tileDot: {
    width: 44,
    height: 44,
    borderRadius: 12,
    alignItems: 'center',
    justifyContent: 'center',
    marginBottom: 12,
  },
  tileIcon: {
    fontSize: 22,
  },
  tileLabel: {
    color: C.text,
    fontSize: 15,
    fontWeight: '600',
    marginBottom: 3,
  },
  tileSub: {
    color: C.textMute,
    fontSize: 11,
  },
});