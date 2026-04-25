// app/(tabs)/chat.tsx — 聊天页（AsyncStorage 持久化，无开场白）
import AsyncStorage from '@react-native-async-storage/async-storage';
import axios from 'axios';
import { Audio } from 'expo-av';
import React, { useEffect, useRef, useState } from 'react';
import {
    ActivityIndicator,
    Alert, Dimensions,
    KeyboardAvoidingView,
    Platform,
    ScrollView,
    StatusBar,
    StyleSheet,
    Text, TextInput, TouchableOpacity,
    View,
} from 'react-native';
import { C, EMOTION_COLORS, EMOTION_LABELS, SERVER_URL, nowTime } from '../../constants/theme';

const { width } = Dimensions.get('window');
const STORAGE_KEY = 'gojo_messages';
const EMOTION_KEY = 'gojo_emotion';

export interface Message {
  id: string;
  role: 'user' | 'gojo';
  text: string;
  subtitle?: string;
  emotion?: string;
  time?: string;
}

export default function ChatScreen() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [inputText, setInputText] = useState('');
  const [loading, setLoading] = useState(false);
  const [currentEmotion, setCurrentEmotion] = useState('调皮');
  const [ready, setReady] = useState(false);
  const scrollRef = useRef<ScrollView>(null);

  // 启动时从本地读取历史消息
  useEffect(() => {
    (async () => {
      try {
        const saved = await AsyncStorage.getItem(STORAGE_KEY);
        const savedEmotion = await AsyncStorage.getItem(EMOTION_KEY);
        if (saved) setMessages(JSON.parse(saved));
        if (savedEmotion) setCurrentEmotion(savedEmotion);
      } catch {}
      setReady(true);
    })();
  }, []);

  // 消息变化时保存
  useEffect(() => {
    if (!ready) return;
    AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(messages)).catch(() => {});
  }, [messages, ready]);

  // emotion 变化时保存
  useEffect(() => {
    AsyncStorage.setItem(EMOTION_KEY, currentEmotion).catch(() => {});
  }, [currentEmotion]);

  const accentColor = EMOTION_COLORS[currentEmotion] || C.accent;

  const sendText = async () => {
    const text = inputText.trim();
    if (!text || loading) return;
    setInputText('');
    const userMsg: Message = {
      id: Date.now().toString(),
      role: 'user',
      text,
      time: nowTime(),
    };
    setMessages(prev => [...prev, userMsg]);
    setLoading(true);
    try {
      const res = await axios.post(`${SERVER_URL}/chat/text`, { text });
      const { emotion, jp, zh, audio_b64 } = res.data;
      setCurrentEmotion(emotion);
      const gojoMsg: Message = {
        id: (Date.now()+1).toString(),
        role: 'gojo',
        text: jp,
        subtitle: zh,
        emotion,
        time: nowTime(),
      };
      setMessages(prev => [...prev, gojoMsg]);
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

  const clearHistory = () => Alert.alert('清空记录', '确认清空所有聊天记录？', [
    { text:'取消', style:'cancel' },
    { text:'清空', style:'destructive', onPress: async () => {
      setMessages([]);
      await AsyncStorage.removeItem(STORAGE_KEY);
    }},
  ]);

  if (!ready) {
    return (
      <View style={{ flex:1, backgroundColor:C.bg, alignItems:'center', justifyContent:'center' }}>
        <ActivityIndicator color={C.accent}/>
      </View>
    );
  }

  return (
    <KeyboardAvoidingView style={{ flex:1, backgroundColor:C.bg }} behavior={Platform.OS==='ios'?'padding':'height'}>
      <StatusBar barStyle="light-content" backgroundColor={C.card}/>

      {/* 头部 */}
      <View style={[s.header, { borderBottomColor: accentColor+'44' }]}>
        <View style={s.avatarSmall}>
          <Text style={s.avatarSmallText}>悟</Text>
        </View>
        <View style={{ flex:1 }}>
          <Text style={s.headerName}>五条悟</Text>
          <Text style={[s.headerEmotion, { color:accentColor }]}>
            {EMOTION_LABELS[currentEmotion]} {currentEmotion}
          </Text>
        </View>
        <TouchableOpacity onPress={clearHistory} style={s.clearBtn}>
          <Text style={s.clearBtnText}>清空</Text>
        </TouchableOpacity>
      </View>

      {/* 消息列表 */}
      <ScrollView
        ref={scrollRef}
        style={s.chatArea}
        contentContainerStyle={s.chatContent}
        onContentSizeChange={() => scrollRef.current?.scrollToEnd({ animated:true })}
      >
        {messages.length === 0 && (
          <View style={s.emptyWrap}>
            <Text style={s.emptyEmoji}>👋</Text>
            <Text style={s.emptyText}>跟五条悟说点什么吧</Text>
          </View>
        )}
        {messages.map(msg => (
          <View key={msg.id} style={[s.msgRow, msg.role==='user' ? s.msgRowUser : s.msgRowGojo]}>
            {msg.role==='gojo' && (
              <View style={[s.msgAvatar, { backgroundColor:(EMOTION_COLORS[msg.emotion||'调皮']||C.accent)+'33' }]}>
                <Text style={s.msgAvatarText}>悟</Text>
              </View>
            )}
            <View style={[s.msgMain, msg.role==='user' && { alignItems:'flex-end' }]}>
              {msg.role==='gojo' && <Text style={s.msgSender}>五条悟</Text>}
              <View style={[
                s.bubble,
                msg.role==='user' ? s.bubbleUser : s.bubbleGojo,
                msg.role==='gojo' && { borderLeftColor: EMOTION_COLORS[msg.emotion||'调皮']||C.accent },
              ]}>
                <Text style={[s.bubbleText, msg.role==='user' && s.bubbleTextUser]}>{msg.text}</Text>
                {msg.subtitle && <Text style={s.subtitle}>{msg.subtitle}</Text>}
                {msg.emotion && (
                  <Text style={[s.emotionTag, { color:EMOTION_COLORS[msg.emotion]||C.accent }]}>
                    {EMOTION_LABELS[msg.emotion]} {msg.emotion}
                  </Text>
                )}
              </View>
              <Text style={s.msgTime}>{msg.time}</Text>
            </View>
          </View>
        ))}
        {loading && (
          <View style={s.msgRow}>
            <View style={s.msgAvatar}><Text style={s.msgAvatarText}>悟</Text></View>
            <View style={[s.bubble, s.bubbleGojo, { flexDirection:'row', alignItems:'center', gap:8 }]}>
              <ActivityIndicator size="small" color={C.accent}/>
              <Text style={{ color:C.textMute, fontSize:13 }}>思考中...</Text>
            </View>
          </View>
        )}
      </ScrollView>

      {/* 输入栏 */}
      <View style={s.inputBar}>
        <TextInput
          style={s.input}
          value={inputText}
          onChangeText={setInputText}
          placeholder="跟五条悟说点什么..."
          placeholderTextColor={C.textMute}
          multiline
        />
        <TouchableOpacity
          style={[s.sendBtn, { backgroundColor: loading ? C.textMute : accentColor }]}
          onPress={sendText}
          disabled={loading}
        >
          <Text style={s.sendBtnText}>发送</Text>
        </TouchableOpacity>
      </View>
    </KeyboardAvoidingView>
  );
}

const s = StyleSheet.create({
  header:         { flexDirection:'row', alignItems:'center', backgroundColor:C.card, paddingHorizontal:20, paddingTop:Platform.OS==='ios'?50:40, paddingBottom:14, borderBottomWidth:1 },
  avatarSmall:    { width:40, height:40, borderRadius:20, backgroundColor:C.accentDim, alignItems:'center', justifyContent:'center', marginRight:12, borderWidth:1, borderColor:C.accent },
  avatarSmallText:{ color:'#fff', fontWeight:'700', fontSize:16 },
  headerName:     { color:C.text, fontSize:16, fontWeight:'600' },
  headerEmotion:  { fontSize:12, marginTop:2 },
  clearBtn:       { paddingHorizontal:10, paddingVertical:6, borderRadius:10, borderWidth:1, borderColor:C.border },
  clearBtnText:   { color:C.textMute, fontSize:12 },
  chatArea:       { flex:1, backgroundColor:C.bg },
  chatContent:    { padding:16, paddingBottom:8, flexGrow:1 },
  emptyWrap:      { flex:1, alignItems:'center', justifyContent:'center', paddingTop:120 },
  emptyEmoji:     { fontSize:48, marginBottom:16 },
  emptyText:      { color:C.textMute, fontSize:15 },
  msgRow:         { flexDirection:'row', marginBottom:16, alignItems:'flex-start' },
  msgRowUser:     { flexDirection:'row-reverse' },
  msgRowGojo:     {},
  msgAvatar:      { width:34, height:34, borderRadius:17, backgroundColor:C.accentDim+'55', alignItems:'center', justifyContent:'center', marginRight:8, borderWidth:1, borderColor:C.border },
  msgAvatarText:  { color:C.accent2, fontSize:13, fontWeight:'700' },
  msgMain:        { maxWidth:width*0.72 },
  msgSender:      { color:C.textMute, fontSize:11, marginBottom:4, marginLeft:2 },
  bubble:         { borderRadius:16, padding:12, borderLeftWidth:2 },
  bubbleGojo:     { backgroundColor:C.card, borderTopLeftRadius:4 },
  bubbleUser:     { backgroundColor:C.userBubble, borderLeftWidth:0, borderRadius:16, borderTopRightRadius:4 },
  bubbleText:     { color:C.text, fontSize:15, lineHeight:22 },
  bubbleTextUser: { color:'#fff' },
  subtitle:       { color:C.textDim, fontSize:12, marginTop:6, lineHeight:18, fontStyle:'italic' },
  emotionTag:     { fontSize:11, marginTop:6 },
  msgTime:        { color:C.textMute, fontSize:10, marginTop:4, marginHorizontal:2 },
  inputBar:       { flexDirection:'row', alignItems:'flex-end', backgroundColor:C.card, paddingHorizontal:12, paddingVertical:10, borderTopWidth:1, borderTopColor:C.border },
  input:          { flex:1, backgroundColor:C.bg, borderRadius:20, paddingHorizontal:16, paddingVertical:10, color:C.text, fontSize:14, maxHeight:100, borderWidth:1, borderColor:C.border, marginRight:8 },
  sendBtn:        { borderRadius:20, paddingHorizontal:18, paddingVertical:10 },
  sendBtnText:    { color:'#fff', fontWeight:'600', fontSize:14 },
});