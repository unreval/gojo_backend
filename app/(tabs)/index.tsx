import axios from 'axios';
import { Audio } from 'expo-av';
import React, { useRef, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  KeyboardAvoidingView, Platform,
  ScrollView, StyleSheet,
  Text, TextInput, TouchableOpacity,
  View,
} from 'react-native';

const SERVER_URL = 'https://gojobackend-production-819d.up.railway.app';

const EMOTION_COLORS: Record<string, string> = {
  平静: '#7B8FA1', 自信: '#C9A84C', 嘲讽: '#8E6B9E',
  开心: '#F0A500', 激动: '#E05C5C', 温柔: '#85C1AE',
  认真: '#3A7CA5', 疑惑: '#B0A090', 调皮: '#E8875A',
  悲伤: '#5B7FA6', 愤怒: '#C0392B',
};

const EMOTION_LABELS: Record<string, string> = {
  平静: '😐 平静', 自信: '😏 自信', 嘲讽: '🙄 嘲讽',
  开心: '😄 开心', 激动: '🔥 激动', 温柔: '🌸 温柔',
  认真: '😤 认真', 疑惑: '🤔 疑惑', 调皮: '😝 调皮',
  悲伤: '😔 悲伤', 愤怒: '😠 愤怒',
};

interface Message {
  role: 'user' | 'gojo';
  text: string;
  subtitle?: string;
  emotion?: string;
}

export default function App() {
  const [messages, setMessages] = useState<Message[]>([
    { role: 'gojo', text: 'やあ。僕が来てあげたよ。', subtitle: '嘿，我来了哦。', emotion: '调皮' },
  ]);
  const [inputText, setInputText] = useState('');
  const [loading, setLoading] = useState(false);
  const [currentEmotion, setCurrentEmotion] = useState('调皮');
  const scrollRef = useRef<ScrollView>(null);

  const sendText = async () => {
    const text = inputText.trim();
    if (!text || loading) return;
    setInputText('');
    setMessages(prev => [...prev, { role: 'user', text }]);
    setLoading(true);
    try {
      const res = await axios.post(`${SERVER_URL}/chat/text`, { text });
      const { emotion, jp, zh, audio_b64 } = res.data;
      setCurrentEmotion(emotion);
      setMessages(prev => [...prev, { role: 'gojo', text: jp, subtitle: zh, emotion }]);
      if (audio_b64) {
        try {
          await Audio.setAudioModeAsync({ playsInSilentModeIOS: true });
          const { sound } = await Audio.Sound.createAsync(
            { uri: `data:audio/mp3;base64,${audio_b64}` }
          );
          await sound.playAsync();
        } catch (audioErr) {
          console.log('音频播放失败', audioErr);
        }
      }
    } catch (e) {
      Alert.alert('连接失败', '请确认电脑服务器已启动');
    } finally {
      setLoading(false);
    }
  };

  return (
    <KeyboardAvoidingView style={styles.container} behavior={Platform.OS === 'ios' ? 'padding' : 'height'} keyboardVerticalOffset={80}>
      <View style={[styles.header, { backgroundColor: EMOTION_COLORS[currentEmotion] || '#3A7CA5' }]}>
        <Text style={styles.headerTitle}>五条悟</Text>
        <Text style={styles.headerEmotion}>{EMOTION_LABELS[currentEmotion] || '😐 平静'}</Text>
      </View>
      <ScrollView
        ref={scrollRef}
        style={styles.chatArea}
        contentContainerStyle={styles.chatContent}
        onContentSizeChange={() => scrollRef.current?.scrollToEnd({ animated: true })}>
        {messages.map((msg, idx) => (
          <View key={idx} style={[styles.bubble, msg.role === 'user' ? styles.userBubble : styles.gojoBubble]}>
            {msg.role === 'gojo' && <Text style={styles.gojoName}>五条悟</Text>}
            <Text style={msg.role === 'user' ? styles.userText : styles.gojoText}>{msg.text}</Text>
            {msg.subtitle && <Text style={styles.subtitle}>{msg.subtitle}</Text>}
            {msg.emotion && <Text style={styles.emotionTag}>{EMOTION_LABELS[msg.emotion]}</Text>}
          </View>
        ))}
        {loading && (
          <View style={styles.gojoBubble}>
            <ActivityIndicator color="#3A7CA5" />
            <Text style={styles.subtitle}>五条悟思考中...</Text>
          </View>
        )}
      </ScrollView>
      <View style={styles.inputArea}>
        <TextInput
          style={styles.input}
          value={inputText}
          onChangeText={setInputText}
          placeholder="跟五条悟说点什么..."
          placeholderTextColor="#999"
          onSubmitEditing={sendText}
          returnKeyType="send"
        />
        <TouchableOpacity
          style={[styles.sendBtn, loading && styles.sendBtnDisabled]}
          onPress={sendText}
          disabled={loading}>
          <Text style={styles.sendBtnText}>发送</Text>
        </TouchableOpacity>
      </View>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#0F0F1A' },
  header: { paddingTop: 50, paddingBottom: 16, paddingHorizontal: 20, alignItems: 'center' },
  headerTitle: { fontSize: 22, fontWeight: 'bold', color: '#fff', letterSpacing: 4 },
  headerEmotion: { fontSize: 14, color: 'rgba(255,255,255,0.8)', marginTop: 4 },
  chatArea: { flex: 1 },
  chatContent: { padding: 16, gap: 12 },
  bubble: { maxWidth: '80%', borderRadius: 16, padding: 12, marginVertical: 4 },
  userBubble: { alignSelf: 'flex-end', backgroundColor: '#3A7CA5' },
  gojoBubble: { alignSelf: 'flex-start', backgroundColor: '#1E1E2E', borderWidth: 1, borderColor: '#3A7CA5' },
  gojoName: { fontSize: 11, color: '#3A7CA5', marginBottom: 4 },
  gojoText: { fontSize: 16, color: '#E8E8F0', lineHeight: 24 },
  userText: { fontSize: 16, color: '#fff', lineHeight: 24 },
  subtitle: { fontSize: 12, color: '#888', marginTop: 6, fontStyle: 'italic' },
  emotionTag: { fontSize: 11, color: '#666', marginTop: 4 },
  inputArea: { flexDirection: 'row', padding: 12, backgroundColor: '#1A1A2E', gap: 8 },
  input: { flex: 1, backgroundColor: '#2A2A3E', borderRadius: 24, paddingHorizontal: 16, paddingVertical: 10, color: '#fff', fontSize: 15 },
  sendBtn: { backgroundColor: '#3A7CA5', borderRadius: 24, paddingHorizontal: 20, justifyContent: 'center' },
  sendBtnDisabled: { backgroundColor: '#555' },
  sendBtnText: { color: '#fff', fontWeight: 'bold' },
});