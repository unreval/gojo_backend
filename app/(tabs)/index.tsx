// app/(tabs)/index.tsx — 首页
import { useRouter } from 'expo-router';
import React from 'react';
import { ScrollView, StatusBar, StyleSheet, Text, TouchableOpacity, View } from 'react-native';
import { C } from '../../constants/theme';

export default function HomeScreen() {
  const router = useRouter();

  const cards = [
    { icon:'💬', label:'与悟聊天',  desc:'随时开口，他在等你',   path:'/chat',       color:'#1d4ed8' },
    { icon:'📅', label:'日程安排',  desc:'记录你的每一天',       path:'/calendar',   color:'#0e7490' },
    { icon:'💰', label:'记账本',    desc:'掌握收支，不再迷糊',   path:'/accounting', color:'#065f46' },
  ];

  return (
    <ScrollView style={s.scroll} contentContainerStyle={s.content} showsVerticalScrollIndicator={false}>
      <StatusBar barStyle="light-content" backgroundColor={C.bg}/>

      {/* 头部 */}
      <View style={s.header}>
        <View style={s.avatarWrap}>
          <View style={s.avatar}><Text style={s.avatarText}>悟</Text></View>
          <View style={s.avatarGlow}/>
        </View>
        <Text style={s.name}>五 条 悟</Text>
        <View style={s.streak}><Text style={s.streakText}>❤️ 已连续聊天 1 天</Text></View>
        <Text style={s.quote}>「まあ、僕が最強だから」</Text>
      </View>

      {/* 功能卡片 */}
      <Text style={s.sectionLabel}>功能入口</Text>
      {cards.map(c => (
        <TouchableOpacity key={c.path} style={[s.card, { borderLeftColor: c.color }]}
          onPress={() => router.push(c.path as any)} activeOpacity={0.75}>
          <View style={[s.cardIcon, { backgroundColor: c.color + '22' }]}>
            <Text style={s.cardIconText}>{c.icon}</Text>
          </View>
          <View style={s.cardInfo}>
            <Text style={s.cardLabel}>{c.label}</Text>
            <Text style={s.cardDesc}>{c.desc}</Text>
          </View>
          <Text style={s.cardArrow}>›</Text>
        </TouchableOpacity>
      ))}

      {/* 今日语录 */}
      <View style={s.daily}>
        <Text style={s.dailyTitle}>今日·五条语录</Text>
        <Text style={s.dailyQuote}>「つまらない…もっと楽しいことしようよ。」</Text>
        <Text style={s.dailyTrans}>好无聊…来做点更有意思的事嘛。</Text>
      </View>
    </ScrollView>
  );
}

const s = StyleSheet.create({
  scroll:       { flex:1, backgroundColor:C.bg },
  content:      { paddingBottom:32 },
  header:       { alignItems:'center', paddingTop:60, paddingBottom:32, paddingHorizontal:24 },
  avatarWrap:   { position:'relative', marginBottom:16 },
  avatar:       { width:90, height:90, borderRadius:45, backgroundColor:C.accentDim, alignItems:'center', justifyContent:'center', borderWidth:2, borderColor:C.accent },
  avatarGlow:   { position:'absolute', top:-6, left:-6, right:-6, bottom:-6, borderRadius:51, borderWidth:1, borderColor:C.accent+'44' },
  avatarText:   { fontSize:36, color:'#fff', fontWeight:'700' },
  name:         { fontSize:26, color:C.text, fontWeight:'300', letterSpacing:8, marginBottom:10 },
  streak:       { backgroundColor:C.accent+'22', borderWidth:1, borderColor:C.accent+'55', borderRadius:20, paddingHorizontal:16, paddingVertical:6, marginBottom:12 },
  streakText:   { color:C.accent2, fontSize:13 },
  quote:        { color:C.textDim, fontSize:13, fontStyle:'italic' },
  sectionLabel: { color:C.textMute, fontSize:11, letterSpacing:2, marginLeft:20, marginBottom:8, marginTop:8 },
  card:         { flexDirection:'row', alignItems:'center', backgroundColor:C.card, marginHorizontal:16, marginBottom:12, borderRadius:16, padding:16, borderLeftWidth:3 },
  cardIcon:     { width:48, height:48, borderRadius:12, alignItems:'center', justifyContent:'center', marginRight:14 },
  cardIconText: { fontSize:22 },
  cardInfo:     { flex:1 },
  cardLabel:    { color:C.text, fontSize:15, fontWeight:'600', marginBottom:3 },
  cardDesc:     { color:C.textDim, fontSize:12 },
  cardArrow:    { color:C.textMute, fontSize:24 },
  daily:        { margin:16, backgroundColor:C.card2, borderRadius:16, padding:20, borderWidth:1, borderColor:C.border },
  dailyTitle:   { color:C.accent2, fontSize:12, marginBottom:10, letterSpacing:1 },
  dailyQuote:   { color:C.text, fontSize:15, marginBottom:6, fontStyle:'italic' },
  dailyTrans:   { color:C.textDim, fontSize:12 },
});