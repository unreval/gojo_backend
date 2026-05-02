// app/(tabs)/chat.tsx — 聊天页（多段连续气泡 + 顺序播放音频）
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
import { C, SERVER_URL, nowTime } from '../../constants/theme';

const { width } = Dimensions.get('window');
const STORAGE_KEY  = 'gojo_messages_v2';
const USER_ID_KEY  = 'gojo_user_id';

// 每条 Gojo 消息之间的延迟（毫秒），模拟"连续打字"
const MSG_DELAY_MS = 800;

export interface Message {
  id: string;
  role: 'user' | 'gojo';
  text: string;
  subtitle?: string;
  time?: string;
}

interface GojoSegment {
  jp: string;
  zh: string;
  audio_b64: string;
}

function generateUserId(): string {
  return 'user_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
}

function sleep(ms: number) {
  return new Promise<void>(resolve => setTimeout(resolve, ms));
}

export default function ChatScreen() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [inputText, setInputText] = useState('');
  const [loading, setLoading] = useState(false);
  const [ready, setReady] = useState(false);
  const [userId, setUserId] = useState<string>('');
  const scrollRef = useRef<ScrollView>(null);
  const currentSoundRef = useRef<Audio.Sound | null>(null);

  useEffect(() => {
    (async () => {
      try {
        await Audio.setAudioModeAsync({
          allowsRecordingIOS: false,
          playsInSilentModeIOS: true,
          staysActiveInBackground: false,
          shouldDuckAndroid: true,
          playThroughEarpieceAndroid: false,
        });

        let uid = await AsyncStorage.getItem(USER_ID_KEY);
        if (!uid) {
          uid = generateUserId();
          await AsyncStorage.setItem(USER_ID_KEY, uid);
        }
        setUserId(uid);

        const saved = await AsyncStorage.getItem(STORAGE_KEY);
        if (saved) setMessages(JSON.parse(saved));
      } catch (e) {
        console.warn('init error', e);
      }
      setReady(true);
    })();

    return () => {
      currentSoundRef.current?.unloadAsync().catch(() => {});
    };
  }, []);

  useEffect(() => {
    if (!ready) return;
    AsyncStorage.setItem(STORAGE_KEY, JSON.stringify(messages)).catch(() => {});
  }, [messages, ready]);

  // 播放一段音频，等播放完成后 resolve
  const playAudioAndWait = async (audio_b64: string): Promise<void> => {
    try {
      if (currentSoundRef.current) {
        await currentSoundRef.current.unloadAsync();
        currentSoundRef.current = null;
      }
    } catch {}

    return new Promise<void>(async (resolve) => {
      try {
        const { sound } = await Audio.Sound.createAsync(
          { uri: `data:audio/mp3;base64,${audio_b64}` },
          { shouldPlay: true, volume: 1.0 }
        );
        currentSoundRef.current = sound;

        sound.setOnPlaybackStatusUpdate((status) => {
          if (status.isLoaded && status.didJustFinish) {
            sound.unloadAsync().catch(() => {});
            if (currentSoundRef.current === sound) currentSoundRef.current = null;
            resolve();
          }
        });
      } catch (e: any) {
        console.error('播放失败', e);
        resolve(); // 失败也继续，不卡住后续消息
      }
    });
  };

  const sendText = async () => {
    const text = inputText.trim();
    if (!text || loading || !userId) return;
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
      const res = await axios.post(`${SERVER_URL}/chat/text`, {
        text,
        user_id: userId,
      });

      // 保存聊天天数到 AsyncStorage，首页可以读取
      if (res.data?.total_days) {
        AsyncStorage.setItem('gojo_chat_days', String(res.data.total_days));
      }

      // 兼容两种返回格式：新版 messages 数组 / 老版单条
      let segments: GojoSegment[] = [];
      if (Array.isArray(res.data?.messages) && res.data.messages.length > 0) {
        segments = res.data.messages;
      } else if (res.data?.jp) {
        segments = [{
          jp: res.data.jp,
          zh: res.data.zh ?? '',
          audio_b64: res.data.audio_b64 ?? '',
        }];
      }

      if (segments.length === 0) {
        Alert.alert('回复异常', '没有收到有效回复');
        return;
      }

      // 思考动画在第一条到达前先关掉
      setLoading(false);

      // 按顺序：插入气泡 → 播放音频 → 等延迟 → 下一条
      for (let i = 0; i < segments.length; i++) {
        const seg = segments[i];
        const gojoMsg: Message = {
          id: `${Date.now()}_${i}`,
          role: 'gojo',
          text: seg.jp,
          subtitle: seg.zh,
          time: nowTime(),
        };
        setMessages(prev => [...prev, gojoMsg]);

        // 播放音频，等播完
        if (seg.audio_b64 && seg.audio_b64.length > 100) {
          await playAudioAndWait(seg.audio_b64);
        }

        // 不是最后一条，加个停顿模拟打字
        if (i < segments.length - 1) {
          await sleep(MSG_DELAY_MS);
        }
      }
    } catch (e: any) {
      console.error('请求失败', e);
      Alert.alert('连接失败', e?.message ?? '请确认服务器正常运行');
    } finally {
      setLoading(false);
    }
  };

  const clearHistory = () =>
    Alert.alert('清空记录', '确认清空所有聊天记录？', [
      { text: '取消', style: 'cancel' },
      {
        text: '清空',
        style: 'destructive',
        onPress: async () => {
          setMessages([]);
          await AsyncStorage.removeItem(STORAGE_KEY);
        },
      },
    ]);

  if (!ready) {
    return (
      <View style={{ flex: 1, backgroundColor: C.bg, alignItems: 'center', justifyContent: 'center' }}>
        <ActivityIndicator color={C.accent} />
      </View>
    );
  }

  return (
    <KeyboardAvoidingView
      style={{ flex: 1, backgroundColor: C.bg }}
      behavior={Platform.OS === 'ios' ? 'padding' : 'height'}
    >
      <StatusBar barStyle="light-content" backgroundColor={C.card} />

      <View style={s.header}>
        <View style={s.avatarSmall}>
          <Text style={s.avatarSmallText}>悟</Text>
        </View>
        <View style={{ flex: 1 }}>
          <Text style={s.headerName}>五条悟</Text>
          <Text style={s.headerSub}>最强的男人</Text>
        </View>
        <TouchableOpacity onPress={clearHistory} style={s.clearBtn}>
          <Text style={s.clearBtnText}>清空</Text>
        </TouchableOpacity>
      </View>

      <ScrollView
        ref={scrollRef}
        style={s.chatArea}
        contentContainerStyle={s.chatContent}
        onContentSizeChange={() => scrollRef.current?.scrollToEnd({ animated: true })}
      >
        {messages.length === 0 && (
          <View style={s.emptyWrap}>
            <Text style={s.emptyEmoji}>👋</Text>
            <Text style={s.emptyText}>跟五条悟说点什么吧</Text>
          </View>
        )}
        {messages.map(msg => (
          <View
            key={msg.id}
            style={[s.msgRow, msg.role === 'user' ? s.msgRowUser : s.msgRowGojo]}
          >
            {msg.role === 'gojo' && (
              <View style={s.msgAvatar}>
                <Text style={s.msgAvatarText}>悟</Text>
              </View>
            )}
            <View style={[s.msgMain, msg.role === 'user' && { alignItems: 'flex-end' }]}>
              {msg.role === 'gojo' && <Text style={s.msgSender}>五条悟</Text>}
              <View style={[s.bubble, msg.role === 'user' ? s.bubbleUser : s.bubbleGojo]}>
                <Text style={[s.bubbleText, msg.role === 'user' && s.bubbleTextUser]}>
                  {msg.text}
                </Text>
                {msg.subtitle && <Text style={s.subtitle}>{msg.subtitle}</Text>}
              </View>
              <Text style={s.msgTime}>{msg.time}</Text>
            </View>
          </View>
        ))}
        {loading && (
          <View style={s.msgRow}>
            <View style={s.msgAvatar}>
              <Text style={s.msgAvatarText}>悟</Text>
            </View>
            <View style={[s.bubble, s.bubbleGojo, { flexDirection: 'row', alignItems: 'center', gap: 8 }]}>
              <ActivityIndicator size="small" color={C.accent} />
              <Text style={{ color: C.textMute, fontSize: 13 }}>思考中...</Text>
            </View>
          </View>
        )}
      </ScrollView>

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
          style={[s.sendBtn, { backgroundColor: loading ? C.textMute : C.accent }]}
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
  header:          { flexDirection:'row', alignItems:'center', backgroundColor:C.card, paddingHorizontal:20, paddingTop:Platform.OS==='ios'?50:40, paddingBottom:14, borderBottomWidth:1, borderBottomColor:C.border },
  avatarSmall:     { width:40, height:40, borderRadius:20, backgroundColor:C.accentDim, alignItems:'center', justifyContent:'center', marginRight:12, borderWidth:1, borderColor:C.accent },
  avatarSmallText: { color:'#fff', fontWeight:'700', fontSize:16 },
  headerName:      { color:C.text, fontSize:16, fontWeight:'600' },
  headerSub:       { color:C.textMute, fontSize:11, marginTop:2 },
  clearBtn:        { paddingHorizontal:10, paddingVertical:6, borderRadius:10, borderWidth:1, borderColor:C.border },
  clearBtnText:    { color:C.textMute, fontSize:12 },
  chatArea:        { flex:1, backgroundColor:C.bg },
  chatContent:     { padding:16, paddingBottom:8, flexGrow:1 },
  emptyWrap:       { flex:1, alignItems:'center', justifyContent:'center', paddingTop:120 },
  emptyEmoji:      { fontSize:48, marginBottom:16 },
  emptyText:       { color:C.textMute, fontSize:15 },
  msgRow:          { flexDirection:'row', marginBottom:16, alignItems:'flex-start' },
  msgRowUser:      { flexDirection:'row-reverse' },
  msgRowGojo:      {},
  msgAvatar:       { width:34, height:34, borderRadius:17, backgroundColor:C.accentDim+'55', alignItems:'center', justifyContent:'center', marginRight:8, borderWidth:1, borderColor:C.border },
  msgAvatarText:   { color:C.accent2, fontSize:13, fontWeight:'700' },
  msgMain:         { maxWidth:width*0.72 },
  msgSender:       { color:C.textMute, fontSize:11, marginBottom:4, marginLeft:2 },
  bubble:          { borderRadius:16, padding:12 },
  bubbleGojo:      { backgroundColor:C.card, borderTopLeftRadius:4, borderLeftWidth:2, borderLeftColor:C.accent },
  bubbleUser:      { backgroundColor:C.userBubble, borderRadius:16, borderTopRightRadius:4 },
  bubbleText:      { color:C.text, fontSize:15, lineHeight:22 },
  bubbleTextUser:  { color:'#fff' },
  subtitle:        { color:C.textDim, fontSize:12, marginTop:6, lineHeight:18, fontStyle:'italic' },
  msgTime:         { color:C.textMute, fontSize:10, marginTop:4, marginHorizontal:2 },
  inputBar:        { flexDirection:'row', alignItems:'flex-end', backgroundColor:C.card, paddingHorizontal:12, paddingVertical:10, borderTopWidth:1, borderTopColor:C.border },
  input:           { flex:1, backgroundColor:C.bg, borderRadius:20, paddingHorizontal:16, paddingVertical:10, color:C.text, fontSize:14, maxHeight:100, borderWidth:1, borderColor:C.border, marginRight:8 },
  sendBtn:         { borderRadius:20, paddingHorizontal:18, paddingVertical:10 },
  sendBtnText:     { color:'#fff', fontWeight:'600', fontSize:14 },
});