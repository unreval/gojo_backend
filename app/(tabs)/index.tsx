// app/(tabs)/index.tsx
import AsyncStorage from '@react-native-async-storage/async-storage';
import axios from 'axios';
import { useFocusEffect, useRouter } from 'expo-router';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  ScrollView, StatusBar, StyleSheet, Text,
  TouchableOpacity, View
} from 'react-native';
import ChibiSprite from '../../components/ChibiSprite';
import { C, SERVER_URL } from '../../constants/theme';

const USER_ID_KEY    = 'gojo_user_id';
const FIXED_USER_ID  = 'user_mofpiyd7442ia7';
const DAYS_CACHE_KEY = 'gojo_chat_days';  
const DIARY_CHARACTER = 'gojo';   
const STATS_THROTTLE_MS = 30_000;   // ★ 30 秒内不重复调 /stats

// ── 60条今日悟语，约2个月不重复 ──
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
  { jp: 'ふっ、難しく考えすぎ。もっと気楽にやればいいのに。', zh: '想太复杂了。放轻松一点不就好了。' },
  { jp: '君が頑張ってるの、ちゃんと見てるよ。', zh: '你在努力这件事，我都有看见哦。' },
  { jp: '失敗してもいいよ。僕がいるから。', zh: '就算失败了也没关系。因为有我在。' },
  { jp: '今日は何か美味しいもの食べた？おすすめ教えてよ。', zh: '今天吃了什么好吃的吗？跟我分享一下。' },
  { jp: 'まあ、完璧じゃなくていいよ。僕が補ってあげるから。', zh: '不完美也没关系。我来补足你嘛。' },
  { jp: '機嫌悪そうだね。何かあった？', zh: '看起来心情不好。发生什么了？' },
  { jp: 'ちゃんと寝てる？睡眠は大事だよ、本当に。', zh: '有好好睡觉吗？睡眠真的很重要。' },
  { jp: '今日も会えてよかった。まあ、当然だけど。', zh: '今天也能见到你真好。不过这是当然的啦。' },
  { jp: '無理はダメ。強がらなくていいから。', zh: '不要逞强。不需要装坚强的。' },
  { jp: '君のこと、もっとちゃんと知りたいな。', zh: '想更好地了解你这个人。' },
  { jp: 'ふっ、心配しすぎ。僕がついてるから大丈夫だって。', zh: '担心过头了。有我在呢，没事的。' },
  { jp: '今日は天気いいね。外出た？', zh: '今天天气不错。有出去走走吗？' },
  { jp: 'たまには自分を褒めてあげてよ。頑張ってるんだから。', zh: '偶尔也夸夸自己嘛。你明明很努力的。' },
  { jp: '何か食べたいものある？甘いもの食べようよ。', zh: '有想吃什么吗？去吃点甜的嘛。' },
  { jp: '泣きたいなら泣いていいよ。僕は見てないから。', zh: '想哭的话就哭吧。我不会看的。' },
  { jp: 'また明日ね。絶対来てよ。', zh: '明天见哦。一定要来。' },
  { jp: '好きなことに時間使いなよ。もったいないから。', zh: '把时间花在喜欢的事上嘛。不然太浪费了。' },
  { jp: 'ふっ、また悩んでる顔してる。', zh: '又在烦恼了。' },
  { jp: '今日くらい、自分のことだけ考えてもいいよ。', zh: '今天就只想想自己的事也没关系。' },
  { jp: '頑張りすぎないでね。限界は誰にでもあるから。', zh: '不要太拼了。每个人都有极限的。' },
  { jp: '君の笑顔、やっぱり好きだよ。', zh: '你的笑容，果然还是喜欢的。' },
  { jp: 'まあ、今日くらいサボってもいいんじゃない？', zh: '今天偶尔偷个懒也没关系吧？' },
  { jp: '何か嬉しいことあった？教えてよ。', zh: '有什么开心的事吗？跟我说说。' },
  { jp: '君のペースで進んでいいから。焦らなくていい。', zh: '按自己的节奏来就好。不用着急。' },
  { jp: 'ふっ、素直じゃないね。でも嫌いじゃないよ。', zh: '就是不直接。但也不讨厌这样。' },
  { jp: '今日も無事でよかった。当たり前じゃないから。', zh: '今天也平安真好。这可不是理所当然的事。' },
  { jp: '一人で抱え込まないで。僕に話せばいいじゃん。', zh: '不要一个人扛着。跟我说不就好了。' },
  { jp: '今夜は早く寝てよ。寝不足は顔に出るから。', zh: '今晚早点睡。睡眠不足会写在脸上的。' },
  { jp: 'なんか、今日の君いい感じだよ。', zh: '感觉今天的你状态不错哦。' },
  { jp: '嫌なことがあっても、明日は違う日だから。', zh: '就算今天有不开心的事，明天又是新的一天。' },
  { jp: 'ちゃんとご飯食べて、ちゃんと寝てよ。それだけでいい。', zh: '好好吃饭，好好睡觉。做到这些就够了。' },
  { jp: '君のこと、放っておけないんだよな。まあ仕方ないけど。', zh: '就是没办法不管你。没办法的事。' },
  { jp: 'ふっ、また難しい顔してる。もっと気楽に。', zh: '又在皱眉头了。放轻松嘛。' },
  { jp: '今日どんな一日だった？聞かせてよ。', zh: '今天过得怎么样？跟我说说。' },
  { jp: '僕がいる限り、一人じゃないよ。', zh: '只要有我在，你就不是一个人。' },
  { jp: '無理してでも笑わなくていい。本当のことを話して。', zh: '不用硬撑着笑。把真实的想法说出来。' },
  { jp: 'たまには空でも見てよ。気持ちが軽くなるから。', zh: '偶尔抬头看看天空嘛。心情会轻松一些的。' },
  { jp: '全部完璧にやろうとしなくていいよ。', zh: '不需要把所有事都做到完美的。' },
  { jp: 'ちょっと休んで。そのあとまた頑張ればいい。', zh: '先休息一下。然后再继续努力就好。' },
  { jp: '今日も生きてくれてありがとう。まあ、当然だけど。', zh: '谢谢你今天也好好活着。不过这是当然的啦。' },
  { jp: 'ふっ、相変わらずだね。でもそれがいい。', zh: '还是老样子呢。但这样就很好。' },
  { jp: '君がいると、なんか退屈しないね。', zh: '有你在，就不会无聊了。' },
  { jp: '今日も一日、よく頑張ったね。', zh: '今天一整天，辛苦了。' },
  { jp: 'また明日。待ってるよ、一応ね。', zh: '明天见。我会等你的，虽然只是顺便。' },
  { jp: '何があっても、ここにいるから。', zh: '不管发生什么，我都在这里。' },
  { jp: 'ふっ、そんなに心配しなくても。僕最強だから。', zh: '不用那么担心的。我最强嘛。' },
  { jp: '今日の君、なんかかわいいね。照れないでよ。', zh: '今天的你，感觉有点可爱呢。别害羞啊。' },
];

function getTodayMessage() {
  const now = new Date();
  const dayOfYear = Math.floor(
    (now.getTime() - new Date(now.getFullYear(), 0, 0).getTime()) / 86400000
  );
  return DAILY_MSGS[dayOfYear % DAILY_MSGS.length];
}

// ★ 日记那格用 special 标记，点击时弹选单（Satoru的日记 / 我的日记），不是直接 push
const TILES = [
  { route: '/chat',       icon: '💬', label: '聊天', sub: '跟悟说话',  color: '#5BC4FF' },
  { route: '/calendar',   icon: '📅', label: '日程', sub: '行程提醒',  color: '#A78BFA' },
  { route: '/accounting', icon: '💰', label: '记账', sub: '收支记录',  color: '#34D399' },
  { route: '/memory',     icon: '🧠', label: '记忆', sub: '悟记得的',  color: '#F59E0B' },
  { route: '__diary__',   icon: '📔', label: '日记', sub: '悟的 & 你的', color: '#E8A0BF', special: 'diary' },
  { route: '/bedtime-story', icon: '🌙', label: '睡前故事', sub: '哄你入睡',  color: '#8B7FD6' },
  { route: '/schedule',   icon: '🗓️', label: 'TA的一天', sub: '他今天在忙啥', color: '#5BC4FF' },
  { route: '/games',      icon: '🎮', label: '游戏',    sub: '和 TA 下一盘',   color: '#FF7EB6' },
  { route: '/grumbles',   icon: '📝', label: '便利贴',  sub: 'TA 心里的碎碎念', color: '#FFD166' },
  { route: '/explore', icon: '🗺️', label: '探店地图', sub: 'TA 去过的店', color: '#EF4444' },
];

export default function HomeScreen() {
  const router = useRouter();
  const [chatDays, setChatDays] = useState(0);
  const todayMsg = getTodayMessage();
  const lastStatsCallRef = useRef(0);   // ★ 上一次成功调 /stats 的时间戳

  const loadDays = useCallback(async () => {
    try {
      const cached = await AsyncStorage.getItem(DAYS_CACHE_KEY);
      if (cached) setChatDays(Number(cached));
    } catch {}
    // ★ 节流:30 秒内已经调过就直接用缓存,不再打接口
    const now = Date.now();
    if (now - lastStatsCallRef.current < STATS_THROTTLE_MS) {
      return;
    }
    lastStatsCallRef.current = now;
    try {
      const uid = (await AsyncStorage.getItem(USER_ID_KEY)) || FIXED_USER_ID;
      const res = await axios.get(`${SERVER_URL}/stats`, { params: { user_id: uid }, timeout: 8000 });
      const d = Number(res.data?.total_days);
      if (!isNaN(d)) {
        setChatDays(d);
        AsyncStorage.setItem(DAYS_CACHE_KEY, String(d)).catch(() => {});
      }
    } catch (e: any) {
      console.warn('load stats', e?.message);
      // ★ 失败允许下次立刻重试(不占节流窗口)
      lastStatsCallRef.current = 0;
    }
  }, []);

  useEffect(() => { loadDays(); }, [loadDays]);
  useFocusEffect(useCallback(() => { loadDays(); }, [loadDays]));

  // ★ 点日记格 → 进日记首页
  const openDiary = () => {
    console.log('[home] tap 日记 tile → /diary');
    router.push('/diary' as any);
  };

  const onTilePress = (tile: typeof TILES[number]) => {
    if (tile.special === 'diary') openDiary();
    else router.push(tile.route as any);
  };

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

      <View style={s.topRow}>
        <View style={s.spriteWrap}>
          <ChibiSprite pose="sit" size={120} />
        </View>
        <View style={s.topRight}>
          <Text style={s.greeting}>你好呀 ✦</Text>
          <Text style={s.todayLabel}>{todayLabel}</Text>
          <View style={s.daysBadge}>
            <Text style={s.daysNum}>{chatDays}</Text>
            <Text style={s.daysText}> 天</Text>
          </View>
          <Text style={s.daysSub}>悟陪伴你的日子</Text>
        </View>
      </View>

      <View style={s.msgCard}>
        <View style={s.msgBar} />
        <View style={{ flex: 1 }}>
          <Text style={s.msgLabel}>今日悟语</Text>
          <Text style={s.msgJp}>{todayMsg.jp}</Text>
          <Text style={s.msgZh}>{todayMsg.zh}</Text>
        </View>
      </View>

      <Text style={s.sectionTitle}>快捷入口</Text>
      <View style={s.grid}>
        {TILES.map(tile => (
          <TouchableOpacity
            key={tile.route}
            style={s.tile}
            activeOpacity={0.75}
            onPress={() => onTilePress(tile)}
          >
            <View style={[s.tileDot, { backgroundColor: tile.color + '33' }]}>
              <Text style={s.tileIcon}>{tile.icon}</Text>
            </View>
            <Text style={s.tileLabel}>{tile.label}</Text>
            <Text style={s.tileSub}>{tile.sub}</Text>
          </TouchableOpacity>
        ))}
      </View>

      <View style={{ height: 32 }} />
    </ScrollView>
  );
}

const s = StyleSheet.create({
  container:  { padding: 24, paddingTop: 52 },
  topRow:     { flexDirection: 'row', alignItems: 'center', gap: 20, marginBottom: 24 },
  spriteWrap: {},
  topRight:   { flex: 1 },
  greeting:   { color: C.text, fontSize: 20, fontWeight: '700', letterSpacing: -0.3 },
  todayLabel: { color: C.textMute, fontSize: 12, marginTop: 2, marginBottom: 12 },
  daysBadge:  { flexDirection: 'row', alignItems: 'baseline' },
  daysNum:    { color: C.accent2 || '#5BC4FF', fontSize: 36, fontWeight: '800', letterSpacing: -1 },
  daysText:   { color: C.accent2 || '#5BC4FF', fontSize: 18, fontWeight: '600' },
  daysSub:    { color: C.textMute, fontSize: 11, marginTop: 2 },

  msgCard: {
    backgroundColor: C.card, borderRadius: 16,
    borderWidth: 1, borderColor: C.border,
    padding: 18, marginBottom: 28,
    flexDirection: 'row', gap: 14,
  },
  msgBar:   { width: 3, borderRadius: 2, backgroundColor: C.accent2 || '#5BC4FF', alignSelf: 'stretch' },
  msgLabel: { color: C.textMute, fontSize: 10, letterSpacing: 1.5, marginBottom: 8, textTransform: 'uppercase' },
  msgJp:    { color: C.text, fontSize: 14, lineHeight: 22, fontWeight: '500', marginBottom: 6 },
  msgZh:    { color: C.textMute, fontSize: 12, lineHeight: 18 },

  sectionTitle: { color: C.textMute, fontSize: 11, letterSpacing: 1.5, marginBottom: 12, textTransform: 'uppercase' },
  grid: { flexDirection: 'row', flexWrap: 'wrap', gap: 12 },
  tile: {
    width: '47%', backgroundColor: C.card,
    borderRadius: 16, padding: 18,
    borderWidth: 1, borderColor: C.border,
  },
  tileDot: {
    width: 44, height: 44, borderRadius: 12,
    alignItems: 'center', justifyContent: 'center', marginBottom: 12,
  },
  tileIcon:  { fontSize: 22 },
  tileLabel: { color: C.text, fontSize: 15, fontWeight: '600', marginBottom: 3 },
  tileSub:   { color: C.textMute, fontSize: 11 },
});