import axios from 'axios';
import { Audio } from 'expo-av';
import React, { useRef, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  Dimensions,
  KeyboardAvoidingView, Platform,
  ScrollView,
  StatusBar,
  StyleSheet,
  Text, TextInput, TouchableOpacity,
  View
} from 'react-native';

const SERVER_URL = 'https://gojobackend-production-819d.up.railway.app';
const { width, height } = Dimensions.get('window');

// ── 颜色系统 ──────────────────────────────────────────
const C = {
  bg:       '#070d1a',
  card:     '#0d1a2e',
  card2:    '#0f2040',
  border:   '#1a3a5c',
  accent:   '#3b82f6',
  accent2:  '#60a5fa',
  accentDim:'#1d4ed8',
  text:     '#e8f4ff',
  textDim:  '#7ba8d0',
  textMute: '#3d6080',
  userBubble: '#1d4ed8',
  gojoBubble: '#0d1a2e',
  danger:   '#ef4444',
  success:  '#22c55e',
};

const EMOTION_COLORS: Record<string, string> = {
  平静: '#4a90a4', 自信: '#c9a84c', 嘲讽: '#8e6b9e',
  开心: '#3b82f6', 激动: '#e05c5c', 温柔: '#5ba88a',
  认真: '#2563eb', 疑惑: '#7c8fa6', 调皮: '#3b82f6',
  悲伤: '#3a5f7a', 愤怒: '#c0392b',
};

const EMOTION_LABELS: Record<string, string> = {
  平静: '😐', 自信: '😏', 嘲讽: '🙄',
  开心: '😄', 激动: '🔥', 温柔: '🌸',
  认真: '😤', 疑惑: '🤔', 调皮: '😝',
  悲伤: '😔', 愤怒: '😠',
};

interface Message {
  role: 'user' | 'gojo';
  text: string;
  subtitle?: string;
  emotion?: string;
  time?: string;
}

type Screen = 'home' | 'chat' | 'calendar' | 'accounting';

// ── 工具函数 ──────────────────────────────────────────
function now() {
  const d = new Date();
  return `${d.getHours().toString().padStart(2,'0')}:${d.getMinutes().toString().padStart(2,'0')}`;
}

// ════════════════════════════════════════════════════
//  首页
// ════════════════════════════════════════════════════
function HomeScreen({ onNav }: { onNav: (s: Screen) => void }) {
  const streak = 1;

  const cards = [
    { icon: '💬', label: '与悟聊天', desc: '随时开口，他在等你', screen: 'chat' as Screen, color: '#1d4ed8' },
    { icon: '📅', label: '日程安排', desc: '记录你的每一天', screen: 'calendar' as Screen, color: '#0e7490' },
    { icon: '💰', label: '记账本', desc: '掌握收支，不再迷糊', screen: 'accounting' as Screen, color: '#065f46' },
  ];

  return (
    <ScrollView style={styles.homeScroll} contentContainerStyle={styles.homeContent} showsVerticalScrollIndicator={false}>
      {/* 头部 */}
      <View style={styles.homeHeader}>
        <View style={styles.homeAvatarWrap}>
          <View style={styles.homeAvatar}>
            <Text style={styles.homeAvatarText}>悟</Text>
          </View>
          <View style={styles.homeAvatarGlow} />
        </View>
        <Text style={styles.homeName}>五 条 悟</Text>
        <View style={styles.streakBadge}>
          <Text style={styles.streakText}>❤️ 已连续聊天 {streak} 天</Text>
        </View>
        <Text style={styles.homeQuote}>「まあ、僕が最強だから」</Text>
      </View>

      {/* 功能卡片 */}
      <Text style={styles.sectionLabel}>功能入口</Text>
      {cards.map((c) => (
        <TouchableOpacity
          key={c.screen}
          style={[styles.featureCard, { borderLeftColor: c.color }]}
          onPress={() => onNav(c.screen)}
          activeOpacity={0.75}
        >
          <View style={[styles.featureIcon, { backgroundColor: c.color + '22' }]}>
            <Text style={styles.featureIconText}>{c.icon}</Text>
          </View>
          <View style={styles.featureInfo}>
            <Text style={styles.featureLabel}>{c.label}</Text>
            <Text style={styles.featureDesc}>{c.desc}</Text>
          </View>
          <Text style={styles.featureArrow}>›</Text>
        </TouchableOpacity>
      ))}

      {/* 今日一言 */}
      <View style={styles.dailyCard}>
        <Text style={styles.dailyTitle}>今日·五条语录</Text>
        <Text style={styles.dailyQuote}>「つまらない…もっと楽しいことしようよ。」</Text>
        <Text style={styles.dailyTrans}>好无聊…来做点更有意思的事嘛。</Text>
      </View>
    </ScrollView>
  );
}

// ════════════════════════════════════════════════════
//  聊天页
// ════════════════════════════════════════════════════
function ChatScreen() {
  const [messages, setMessages] = useState<Message[]>([
    { role: 'gojo', text: 'やあ。僕が来てあげたよ。', subtitle: '嘿，我来了哦。', emotion: '调皮', time: now() },
  ]);
  const [inputText, setInputText] = useState('');
  const [loading, setLoading] = useState(false);
  const [currentEmotion, setCurrentEmotion] = useState('调皮');
  const scrollRef = useRef<ScrollView>(null);

  const accentColor = EMOTION_COLORS[currentEmotion] || C.accent;

  const sendText = async () => {
    const text = inputText.trim();
    if (!text || loading) return;
    setInputText('');
    const t = now();
    setMessages(prev => [...prev, { role: 'user', text, time: t }]);
    setLoading(true);
    try {
      const res = await axios.post(`${SERVER_URL}/chat/text`, { text });
      const { emotion, jp, zh, audio_b64 } = res.data;
      setCurrentEmotion(emotion);
      setMessages(prev => [...prev, { role: 'gojo', text: jp, subtitle: zh, emotion, time: now() }]);
      if (audio_b64) {
        try {
          const { sound } = await Audio.Sound.createAsync({ uri: `data:audio/mp3;base64,${audio_b64}` });
          await sound.playAsync();
        } catch {}
      }
    } catch {
      Alert.alert('连接失败', '请确认服务器正常运行');
    } finally {
      setLoading(false);
    }
  };

  return (
    <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === 'ios' ? 'padding' : 'height'}>
      {/* 聊天头部 */}
      <View style={[styles.chatHeader, { borderBottomColor: accentColor + '44' }]}>
        <View style={styles.chatAvatarSmall}>
          <Text style={styles.chatAvatarSmallText}>悟</Text>
        </View>
        <View>
          <Text style={styles.chatHeaderName}>五条悟</Text>
          <Text style={[styles.chatHeaderEmotion, { color: accentColor }]}>
            {EMOTION_LABELS[currentEmotion]} {currentEmotion}
          </Text>
        </View>
      </View>

      {/* 消息列表 */}
      <ScrollView
        ref={scrollRef}
        style={styles.chatArea}
        contentContainerStyle={styles.chatContent}
        onContentSizeChange={() => scrollRef.current?.scrollToEnd({ animated: true })}
      >
        {messages.map((msg, idx) => (
          <View key={idx} style={[styles.msgRow, msg.role === 'user' ? styles.msgRowUser : styles.msgRowGojo]}>
            {msg.role === 'gojo' && (
              <View style={[styles.msgAvatar, { backgroundColor: EMOTION_COLORS[msg.emotion || '调皮'] + '33' }]}>
                <Text style={styles.msgAvatarText}>悟</Text>
              </View>
            )}
            <View style={styles.msgMain}>
              {msg.role === 'gojo' && (
                <Text style={styles.msgSender}>五条悟</Text>
              )}
              <View style={[
                styles.bubble,
                msg.role === 'user' ? styles.bubbleUser : styles.bubbleGojo,
                msg.role === 'gojo' && { borderLeftColor: EMOTION_COLORS[msg.emotion || '调皮'] }
              ]}>
                <Text style={[styles.bubbleText, msg.role === 'user' && styles.bubbleTextUser]}>
                  {msg.text}
                </Text>
                {msg.subtitle && (
                  <Text style={styles.subtitle}>{msg.subtitle}</Text>
                )}
                {msg.emotion && (
                  <Text style={[styles.emotionTag, { color: EMOTION_COLORS[msg.emotion] }]}>
                    {EMOTION_LABELS[msg.emotion]} {msg.emotion}
                  </Text>
                )}
              </View>
              <Text style={styles.msgTime}>{msg.time}</Text>
            </View>
          </View>
        ))}
        {loading && (
          <View style={styles.msgRow}>
            <View style={styles.msgAvatar}><Text style={styles.msgAvatarText}>悟</Text></View>
            <View style={[styles.bubble, styles.bubbleGojo, styles.loadingBubble]}>
              <ActivityIndicator size="small" color={C.accent} />
              <Text style={styles.loadingText}>思考中...</Text>
            </View>
          </View>
        )}
      </ScrollView>

      {/* 输入栏 */}
      <View style={styles.inputBar}>
        <TextInput
          style={styles.input}
          value={inputText}
          onChangeText={setInputText}
          placeholder="跟五条悟说点什么..."
          placeholderTextColor={C.textMute}
          multiline
          onSubmitEditing={sendText}
        />
        <TouchableOpacity
          style={[styles.sendBtn, { backgroundColor: loading ? C.textMute : accentColor }]}
          onPress={sendText}
          disabled={loading}
        >
          <Text style={styles.sendBtnText}>发送</Text>
        </TouchableOpacity>
      </View>
    </KeyboardAvoidingView>
  );
}

// ════════════════════════════════════════════════════
//  日程页（占位）
// ════════════════════════════════════════════════════
function CalendarScreen() {
  const items = [
    { date: '04.25', title: '和悟一起看星星', time: '21:00 - 22:00', tag: '约定' },
    { date: '04.26', title: '复习期末考', time: '14:00 - 18:00', tag: '学习' },
    { date: '04.28', title: '健身', time: '全天', tag: '运动' },
  ];
  const tagColors: Record<string, string> = { 约定: '#3b82f6', 学习: '#8b5cf6', 运动: '#22c55e', 其他: '#f59e0b' };

  return (
    <ScrollView style={styles.pageScroll} contentContainerStyle={styles.pageContent}>
      <Text style={styles.pageTitle}>📅 我的日程</Text>
      <View style={styles.timeline}>
        {items.map((item, i) => (
          <View key={i} style={styles.timelineRow}>
            <View style={styles.timelineLeft}>
              <Text style={styles.timelineDate}>{item.date}</Text>
              <View style={styles.timelineDot} />
              {i < items.length - 1 && <View style={styles.timelineLine} />}
            </View>
            <View style={styles.timelineCard}>
              <View style={{ flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' }}>
                <Text style={styles.timelineTitle}>{item.title}</Text>
                <View style={[styles.timelineTag, { backgroundColor: (tagColors[item.tag] || '#f59e0b') + '33' }]}>
                  <Text style={[styles.timelineTagText, { color: tagColors[item.tag] || '#f59e0b' }]}>{item.tag}</Text>
                </View>
              </View>
              <Text style={styles.timelineTime}>{item.time}</Text>
            </View>
          </View>
        ))}
      </View>
      <TouchableOpacity style={styles.addBtn}>
        <Text style={styles.addBtnText}>＋ 添加日程</Text>
      </TouchableOpacity>
    </ScrollView>
  );
}

// ════════════════════════════════════════════════════
//  记账页（占位）
// ════════════════════════════════════════════════════
function AccountingScreen() {
  const records = [
    { type: 'out', category: '餐饮', desc: '珍珠奶茶', amount: -18, date: '今天' },
    { type: 'out', category: '购物', desc: '新裙子', amount: -256, date: '今天' },
    { type: 'in',  category: '收入', desc: '生活费', amount: 1500, date: '昨天' },
    { type: 'out', category: '交通', desc: '地铁', amount: -4.2, date: '昨天' },
  ];

  const totalIn = records.filter(r => r.type === 'in').reduce((s, r) => s + r.amount, 0);
  const totalOut = records.filter(r => r.type === 'out').reduce((s, r) => s + r.amount, 0);

  return (
    <ScrollView style={styles.pageScroll} contentContainerStyle={styles.pageContent}>
      <Text style={styles.pageTitle}>💰 记账本</Text>

      {/* 统计卡片 */}
      <View style={styles.statsRow}>
        <View style={[styles.statsCard, { borderColor: '#22c55e44' }]}>
          <Text style={styles.statsLabel}>本月收入</Text>
          <Text style={[styles.statsAmount, { color: '#22c55e' }]}>+¥{totalIn}</Text>
        </View>
        <View style={[styles.statsCard, { borderColor: '#ef444444' }]}>
          <Text style={styles.statsLabel}>本月支出</Text>
          <Text style={[styles.statsAmount, { color: '#ef4444' }]}>-¥{Math.abs(totalOut)}</Text>
        </View>
      </View>

      {/* 记录列表 */}
      <Text style={styles.sectionLabel}>最近记录</Text>
      {records.map((r, i) => (
        <View key={i} style={styles.recordRow}>
          <View style={[styles.recordIcon, { backgroundColor: r.type === 'in' ? '#22c55e22' : '#ef444422' }]}>
            <Text>{r.type === 'in' ? '📥' : '📤'}</Text>
          </View>
          <View style={{ flex: 1 }}>
            <Text style={styles.recordDesc}>{r.desc}</Text>
            <Text style={styles.recordCategory}>{r.category} · {r.date}</Text>
          </View>
          <Text style={[styles.recordAmount, { color: r.type === 'in' ? '#22c55e' : '#ef4444' }]}>
            {r.amount > 0 ? '+' : ''}¥{r.amount}
          </Text>
        </View>
      ))}

      <TouchableOpacity style={styles.addBtn}>
        <Text style={styles.addBtnText}>＋ 添加记录</Text>
      </TouchableOpacity>
    </ScrollView>
  );
}

// ════════════════════════════════════════════════════
//  主 App
// ════════════════════════════════════════════════════
export default function App() {
  const [screen, setScreen] = useState<Screen>('home');

  const tabs: { key: Screen; icon: string; label: string }[] = [
    { key: 'home',       icon: '🏠', label: '首页' },
    { key: 'chat',       icon: '💬', label: '聊天' },
    { key: 'calendar',   icon: '📅', label: '日程' },
    { key: 'accounting', icon: '💰', label: '记账' },
  ];

  return (
    <View style={styles.root}>
      <StatusBar barStyle="light-content" backgroundColor={C.bg} />

      {/* 内容区 */}
      <View style={{ flex: 1 }}>
        {screen === 'home'       && <HomeScreen onNav={setScreen} />}
        {screen === 'chat'       && <ChatScreen />}
        {screen === 'calendar'   && <CalendarScreen />}
        {screen === 'accounting' && <AccountingScreen />}
      </View>

      {/* 底部导航 */}
      <View style={styles.tabBar}>
        {tabs.map(t => (
          <TouchableOpacity
            key={t.key}
            style={styles.tabItem}
            onPress={() => setScreen(t.key)}
          >
            <Text style={styles.tabIcon}>{t.icon}</Text>
            <Text style={[styles.tabLabel, screen === t.key && styles.tabLabelActive]}>
              {t.label}
            </Text>
            {screen === t.key && <View style={styles.tabActiveDot} />}
          </TouchableOpacity>
        ))}
      </View>
    </View>
  );
}

// ════════════════════════════════════════════════════
//  样式
// ════════════════════════════════════════════════════
const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: C.bg },

  // ── 首页 ──
  homeScroll: { flex: 1, backgroundColor: C.bg },
  homeContent: { paddingBottom: 32 },
  homeHeader: { alignItems: 'center', paddingTop: 60, paddingBottom: 32, paddingHorizontal: 24 },
  homeAvatarWrap: { position: 'relative', marginBottom: 16 },
  homeAvatar: {
    width: 90, height: 90, borderRadius: 45,
    backgroundColor: C.accentDim,
    alignItems: 'center', justifyContent: 'center',
    borderWidth: 2, borderColor: C.accent,
  },
  homeAvatarGlow: {
    position: 'absolute', top: -6, left: -6, right: -6, bottom: -6,
    borderRadius: 51, borderWidth: 1, borderColor: C.accent + '44',
  },
  homeAvatarText: { fontSize: 36, color: '#fff', fontWeight: '700' },
  homeName: { fontSize: 26, color: C.text, fontWeight: '300', letterSpacing: 8, marginBottom: 10 },
  streakBadge: {
    backgroundColor: C.accent + '22', borderWidth: 1, borderColor: C.accent + '55',
    borderRadius: 20, paddingHorizontal: 16, paddingVertical: 6, marginBottom: 12,
  },
  streakText: { color: C.accent2, fontSize: 13 },
  homeQuote: { color: C.textDim, fontSize: 13, fontStyle: 'italic' },

  featureCard: {
    flexDirection: 'row', alignItems: 'center',
    backgroundColor: C.card, marginHorizontal: 16, marginBottom: 12,
    borderRadius: 16, padding: 16,
    borderLeftWidth: 3, borderLeftColor: C.accent,
  },
  featureIcon: {
    width: 48, height: 48, borderRadius: 12,
    alignItems: 'center', justifyContent: 'center', marginRight: 14,
  },
  featureIconText: { fontSize: 22 },
  featureInfo: { flex: 1 },
  featureLabel: { color: C.text, fontSize: 15, fontWeight: '600', marginBottom: 3 },
  featureDesc: { color: C.textDim, fontSize: 12 },
  featureArrow: { color: C.textMute, fontSize: 24 },

  dailyCard: {
    margin: 16, backgroundColor: C.card2,
    borderRadius: 16, padding: 20,
    borderWidth: 1, borderColor: C.border,
  },
  dailyTitle: { color: C.accent2, fontSize: 12, marginBottom: 10, letterSpacing: 1 },
  dailyQuote: { color: C.text, fontSize: 15, marginBottom: 6, fontStyle: 'italic' },
  dailyTrans: { color: C.textDim, fontSize: 12 },

  sectionLabel: { color: C.textMute, fontSize: 11, letterSpacing: 2, marginLeft: 20, marginBottom: 8, marginTop: 8 },

  // ── 聊天 ──
  chatHeader: {
    flexDirection: 'row', alignItems: 'center',
    backgroundColor: C.card, paddingHorizontal: 20,
    paddingTop: Platform.OS === 'ios' ? 50 : 40, paddingBottom: 14,
    borderBottomWidth: 1,
  },
  chatAvatarSmall: {
    width: 40, height: 40, borderRadius: 20,
    backgroundColor: C.accentDim, alignItems: 'center', justifyContent: 'center',
    marginRight: 12, borderWidth: 1, borderColor: C.accent,
  },
  chatAvatarSmallText: { color: '#fff', fontWeight: '700', fontSize: 16 },
  chatHeaderName: { color: C.text, fontSize: 16, fontWeight: '600' },
  chatHeaderEmotion: { fontSize: 12, marginTop: 2 },

  chatArea: { flex: 1, backgroundColor: C.bg },
  chatContent: { padding: 16, paddingBottom: 8 },

  msgRow: { flexDirection: 'row', marginBottom: 16, alignItems: 'flex-start' },
  msgRowUser: { flexDirection: 'row-reverse' },
  msgRowGojo: {},
  msgAvatar: {
    width: 34, height: 34, borderRadius: 17,
    backgroundColor: C.accentDim + '55',
    alignItems: 'center', justifyContent: 'center',
    marginRight: 8, borderWidth: 1, borderColor: C.border,
  },
  msgAvatarText: { color: C.accent2, fontSize: 13, fontWeight: '700' },
  msgMain: { maxWidth: width * 0.72 },
  msgSender: { color: C.textMute, fontSize: 11, marginBottom: 4, marginLeft: 2 },

  bubble: {
    borderRadius: 16, padding: 12,
    borderLeftWidth: 2,
  },
  bubbleGojo: {
    backgroundColor: C.card,
    borderLeftColor: C.accent,
    borderTopLeftRadius: 4,
  },
  bubbleUser: {
    backgroundColor: C.userBubble,
    borderLeftWidth: 0,
    borderTopRightRadius: 4,
    borderRadius: 16,
  },
  bubbleText: { color: C.text, fontSize: 15, lineHeight: 22 },
  bubbleTextUser: { color: '#fff' },
  subtitle: { color: C.textDim, fontSize: 12, marginTop: 6, lineHeight: 18, fontStyle: 'italic' },
  emotionTag: { fontSize: 11, marginTop: 6 },
  msgTime: { color: C.textMute, fontSize: 10, marginTop: 4, marginLeft: 2 },

  loadingBubble: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  loadingText: { color: C.textMute, fontSize: 13 },

  inputBar: {
    flexDirection: 'row', alignItems: 'flex-end',
    backgroundColor: C.card, paddingHorizontal: 12,
    paddingVertical: 10, borderTopWidth: 1, borderTopColor: C.border,
  },
  input: {
    flex: 1, backgroundColor: C.bg,
    borderRadius: 20, paddingHorizontal: 16, paddingVertical: 10,
    color: C.text, fontSize: 14, maxHeight: 100,
    borderWidth: 1, borderColor: C.border, marginRight: 8,
  },
  sendBtn: {
    backgroundColor: C.accent, borderRadius: 20,
    paddingHorizontal: 18, paddingVertical: 10,
  },
  sendBtnText: { color: '#fff', fontWeight: '600', fontSize: 14 },

  // ── 通用页面 ──
  pageScroll: { flex: 1, backgroundColor: C.bg },
  pageContent: { padding: 20, paddingTop: Platform.OS === 'ios' ? 60 : 50, paddingBottom: 40 },
  pageTitle: { color: C.text, fontSize: 22, fontWeight: '700', marginBottom: 24 },

  // ── 日程 ──
  timeline: { marginBottom: 20 },
  timelineRow: { flexDirection: 'row', marginBottom: 20 },
  timelineLeft: { width: 56, alignItems: 'center' },
  timelineDate: { color: C.accent2, fontSize: 12, fontWeight: '600', marginBottom: 6 },
  timelineDot: { width: 10, height: 10, borderRadius: 5, backgroundColor: C.accent },
  timelineLine: { width: 2, flex: 1, backgroundColor: C.border, marginTop: 4 },
  timelineCard: {
    flex: 1, backgroundColor: C.card,
    borderRadius: 14, padding: 14, marginLeft: 12,
    borderWidth: 1, borderColor: C.border,
  },
  timelineTitle: { color: C.text, fontSize: 14, fontWeight: '600' },
  timelineTime: { color: C.textDim, fontSize: 12, marginTop: 4 },
  timelineTag: { borderRadius: 8, paddingHorizontal: 8, paddingVertical: 3 },
  timelineTagText: { fontSize: 11, fontWeight: '600' },

  // ── 记账 ──
  statsRow: { flexDirection: 'row', gap: 12, marginBottom: 24 },
  statsCard: {
    flex: 1, backgroundColor: C.card,
    borderRadius: 14, padding: 16,
    borderWidth: 1,
  },
  statsLabel: { color: C.textDim, fontSize: 12, marginBottom: 8 },
  statsAmount: { fontSize: 22, fontWeight: '700' },
  recordRow: {
    flexDirection: 'row', alignItems: 'center',
    backgroundColor: C.card, borderRadius: 14,
    padding: 14, marginBottom: 10,
    borderWidth: 1, borderColor: C.border,
  },
  recordIcon: { width: 40, height: 40, borderRadius: 10, alignItems: 'center', justifyContent: 'center', marginRight: 12 },
  recordDesc: { color: C.text, fontSize: 14, fontWeight: '500' },
  recordCategory: { color: C.textDim, fontSize: 12, marginTop: 2 },
  recordAmount: { fontSize: 16, fontWeight: '700' },

  // ── 通用按钮 ──
  addBtn: {
    backgroundColor: C.accent + '22', borderWidth: 1, borderColor: C.accent + '55',
    borderRadius: 14, padding: 14, alignItems: 'center', marginTop: 8,
  },
  addBtnText: { color: C.accent2, fontWeight: '600', fontSize: 14 },

  // ── 底部导航 ──
  tabBar: {
    flexDirection: 'row',
    backgroundColor: C.card,
    borderTopWidth: 1, borderTopColor: C.border,
    paddingBottom: Platform.OS === 'ios' ? 24 : 8,
    paddingTop: 8,
  },
  tabItem: { flex: 1, alignItems: 'center', paddingVertical: 4 },
  tabIcon: { fontSize: 20, marginBottom: 2 },
  tabLabel: { fontSize: 10, color: C.textMute },
  tabLabelActive: { color: C.accent2, fontWeight: '600' },
  tabActiveDot: {
    position: 'absolute', bottom: -2,
    width: 4, height: 4, borderRadius: 2,
    backgroundColor: C.accent,
  },
});