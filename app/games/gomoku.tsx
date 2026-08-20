// app/games/gomoku.tsx
// 五子棋 v4 —— AI 用 LLM 决定落子
//
// 改动:
// 1. AI 不再用前端算法,改走后端 /game/gomoku/move
//    → LLM 看棋盘 + 对局聊天记录,按人设性格决定下哪里
//    → 用户撒娇/耍赖 → AI 可能"看心情"下弱一点
//    → LLM 返回无效位置 → 后端自动 fallback 到算法
// 2. AI 落子时可能顺便说话(move 接口返回 say 字段),省一轮请求
// 3. 游戏结束时自动保存有趣瞬间到记忆
// 4. 键盘:手动 Keyboard.addListener,不用 KeyboardAvoidingView
import AsyncStorage from '@react-native-async-storage/async-storage';
import axios from 'axios';
import { Audio } from 'expo-av';
import * as FileSystem from 'expo-file-system/legacy';
import { useFocusEffect, useRouter } from 'expo-router';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
    Alert, Dimensions, Keyboard, Platform,
    ScrollView, StatusBar, StyleSheet, Text, TextInput,
    TouchableOpacity, View,
} from 'react-native';
import { C, SERVER_URL } from '../../constants/theme';

const FIXED_USER_ID = 'user_mofpiyd7442ia7';
const DEFAULT_CHAR_KEY = 'default_character_id';
const SIZE = 15;
type Cell = 0 | 1 | 2;

const SCREEN_W = Dimensions.get('window').width;
const SCREEN_H = Dimensions.get('window').height;
const BOARD_MAX = Math.min(SCREEN_W - 32, SCREEN_H * 0.40);
const BOARD_PAD = 10;
const BOARD_W = BOARD_MAX;
const GRID_W = BOARD_W - BOARD_PAD * 2;
const CELL_SIZE = GRID_W / (SIZE - 1);
const STONE_R = CELL_SIZE * 0.42;

function sleep(ms: number) { return new Promise<void>(r => setTimeout(r, ms)); }
const newBoard = (): Cell[][] => Array.from({ length: SIZE }, () => Array(SIZE).fill(0) as Cell[]);

// 只保留 checkWin(前端需要即时判定胜负,不等后端)
const DIRS: [number, number][] = [[1, 0], [0, 1], [1, 1], [1, -1]];
function checkWin(b: Cell[][], x: number, y: number, color: Cell): boolean {
  for (const [dx, dy] of DIRS) {
    let count = 1;
    let nx = x + dx, ny = y + dy;
    while (nx >= 0 && nx < SIZE && ny >= 0 && ny < SIZE && b[ny][nx] === color) { count++; nx += dx; ny += dy; }
    nx = x - dx; ny = y - dy;
    while (nx >= 0 && nx < SIZE && ny >= 0 && ny < SIZE && b[ny][nx] === color) { count++; nx -= dx; ny -= dy; }
    if (count >= 5) return true;
  }
  return false;
}

interface ChatMsg {
  id: string;
  role: 'user' | 'char';
  jp?: string; zh?: string; text?: string;
  emotion?: string; audioUri?: string;
}

export default function GomokuScreen() {
  const router = useRouter();
  const [board, setBoard] = useState<Cell[][]>(newBoard());
  const [turn, setTurn] = useState<Cell>(1);
  const [gameOver, setGameOver] = useState(false);
  const [winner, setWinner] = useState<Cell | 0>(0);
  const [history, setHistory] = useState<{ x: number; y: number; color: Cell }[]>([]);
  const aiThinkingRef = useRef(false);

  const [characterId, setCharacterId] = useState('gojo');
  const [charName, setCharName] = useState('对手');

  const [chatMsgs, setChatMsgs] = useState<ChatMsg[]>([]);
  const [inputText, setInputText] = useState('');
  const inputTextRef = useRef('');
  const [sendingChat, setSendingChat] = useState(false);
  const chatScrollRef = useRef<ScrollView>(null);
  const talkInFlightRef = useRef(false);
  const currentSoundRef = useRef<Audio.Sound | null>(null);
  const gameStartedRef = useRef(false);
  const [keyboardHeight, setKeyboardHeight] = useState(0);

  // 键盘监听
  useEffect(() => {
    const screenH = Dimensions.get('window').height;
    const showEvt = Platform.OS === 'ios' ? 'keyboardWillShow' : 'keyboardDidShow';
    const hideEvt = Platform.OS === 'ios' ? 'keyboardWillHide' : 'keyboardDidHide';
    const showSub = Keyboard.addListener(showEvt, e => {
      const screenY = e.endCoordinates.screenY ?? 0;
      const reportedH = e.endCoordinates.height ?? 0;
      setKeyboardHeight(screenY > 0 ? Math.max(screenH - screenY, reportedH) : reportedH);
      setTimeout(() => chatScrollRef.current?.scrollToEnd({ animated: true }), 50);
    });
    const hideSub = Keyboard.addListener(hideEvt, () => setKeyboardHeight(0));
    return () => { showSub.remove(); hideSub.remove(); };
  }, []);

  // 初始化
  useFocusEffect(useCallback(() => {
    let cancelled = false;
    (async () => {
      try {
        const saved = await AsyncStorage.getItem(DEFAULT_CHAR_KEY);
        const cid = saved || 'gojo';
        if (cancelled) return;
        setCharacterId(cid);
        try {
          const res = await axios.get(`${SERVER_URL}/characters_all`, { timeout: 5000 });
          const found = (res.data?.characters || []).find((c: any) => c.id === cid);
          if (found && !cancelled) setCharName(found.name);
        } catch {}
        if (!gameStartedRef.current) {
          gameStartedRef.current = true;
          setTimeout(() => triggerAITalk('game_start', 0, undefined, cid), 500);
        }
      } catch {}
    })();
    return () => { cancelled = true; };
  }, []));

  useEffect(() => () => { currentSoundRef.current?.unloadAsync().catch(() => {}); }, []);

  // 音频
  const playAudioB64 = async (b64: string, msgId: string): Promise<string | null> => {
    if (!b64 || b64.length < 100) return null;
    try {
      const dir = `${FileSystem.cacheDirectory}gomoku_audio/`;
      await FileSystem.makeDirectoryAsync(dir, { intermediates: true }).catch(() => {});
      const uri = `${dir}${msgId}.mp3`;
      await FileSystem.writeAsStringAsync(uri, b64, { encoding: FileSystem.EncodingType.Base64 });
      if (currentSoundRef.current) try { await currentSoundRef.current.unloadAsync(); } catch {}
      const { sound } = await Audio.Sound.createAsync({ uri }, { shouldPlay: true });
      currentSoundRef.current = sound;
      return uri;
    } catch { return null; }
  };

  const replayAudio = async (uri: string) => {
    try {
      if (currentSoundRef.current) try { await currentSoundRef.current.unloadAsync(); } catch {}
      const { sound } = await Audio.Sound.createAsync({ uri }, { shouldPlay: true });
      currentSoundRef.current = sound;
    } catch {}
  };

  // 添加聊天消息(带音频处理)
  const addCharMsg = async (say: any) => {
    if (!say || !say.jp) return;
    const msgId = `${Date.now()}_char_${Math.random().toString(36).slice(2, 6)}`;
    const audioUri = await playAudioB64(say.audio_b64 || '', msgId);
    setChatMsgs(prev => [...prev, {
      id: msgId, role: 'char', jp: say.jp, zh: say.zh,
      emotion: say.emotion, audioUri: audioUri || undefined,
    }]);
    setTimeout(() => chatScrollRef.current?.scrollToEnd({ animated: true }), 100);
  };

  // AI 说话(不带落子:game_start/user_chat/win)
  const triggerAITalk = async (event: string, moveCount: number, userText?: string, cid?: string) => {
    if (talkInFlightRef.current && event !== 'user_chat') return;
    talkInFlightRef.current = true;
    try {
      const res = await axios.post(`${SERVER_URL}/game/gomoku/talk`, {
        user_id: FIXED_USER_ID, character_id: cid || characterId,
        event, move_count: moveCount, user_text: userText,
      }, { timeout: 20000 });
      if (res.data?.say) {
        await addCharMsg(res.data);
      }
    } catch (e: any) { console.warn('[gomoku talk]', e?.message); }
    finally { talkInFlightRef.current = false; }
  };

  // 获取对局聊天记录(喂给 LLM 看上下文)
  const getChatHistory = () => {
    return chatMsgs.slice(-10).map(m => ({
      role: m.role === 'user' ? 'user' : 'char',
      text: m.role === 'user' ? (m.text || '') : (m.zh || m.jp || ''),
    }));
  };

  // 玩家落子
  const onIntersectionPress = (x: number, y: number) => {
    if (gameOver || turn !== 1 || aiThinkingRef.current) return;
    if (board[y][x] !== 0) return;
    const b = board.map(r => [...r]) as Cell[][];
    b[y][x] = 1;
    setBoard(b); setHistory(prev => [...prev, { x, y, color: 1 }]);
    if (checkWin(b, x, y, 1)) {
      setGameOver(true); setWinner(1);
      triggerAITalk('user_win', history.length + 1);
      saveGameMemory('user_win', history.length + 1);
      return;
    }
    setTurn(2);
  };

  // ★ AI 回合:走后端 LLM 决定落子
  useEffect(() => {
    if (turn !== 2 || gameOver) return;
    aiThinkingRef.current = true;

    const doAIMove = async () => {
      try {
        const last = history[history.length - 1];
        const res = await axios.post(`${SERVER_URL}/game/gomoku/move`, {
          user_id: FIXED_USER_ID,
          character_id: characterId,
          board,
          last_move: last ? [last.x, last.y] : null,
          chat_history: getChatHistory(),
        }, { timeout: 30000 });

        const { x, y, say } = res.data;

        // 落子
        const b = board.map(r => [...r]) as Cell[][];
        b[y][x] = 2;
        setBoard(b);
        setHistory(prev => [...prev, { x, y, color: 2 }]);

        // 说话(如果 LLM 顺便说了)
        if (say) await addCharMsg(say);

        // 检查胜负
        if (checkWin(b, x, y, 2)) {
          setGameOver(true); setWinner(2);
          // 如果 move 里没说话,单独触发胜利说话
          if (!say) triggerAITalk('ai_win', history.length + 1);
          saveGameMemory('ai_win', history.length + 1);
        } else {
          setTurn(1);
        }
      } catch (e: any) {
        console.warn('[gomoku AI move]', e?.message);
        // 网络失败:让玩家重新操作
        setTurn(1);
      } finally {
        aiThinkingRef.current = false;
      }
    };

    // 稍微延迟,不然太快了没感觉
    const t = setTimeout(doAIMove, 600);
    return () => clearTimeout(t);
  }, [turn]);

  // 游戏结束保存记忆
  const saveGameMemory = async (result: string, moveCount: number) => {
    try {
      // 选有趣的对话片段
      const highlights = chatMsgs
        .filter(m => m.text || m.zh)
        .slice(-6)
        .map(m => ({
          role: m.role === 'user' ? 'user' : 'char',
          text: m.role === 'user' ? (m.text || '') : (m.zh || ''),
        }));

      await axios.post(`${SERVER_URL}/game/gomoku/save_memory`, {
        user_id: FIXED_USER_ID,
        character_id: characterId,
        result, move_count: moveCount,
        chat_highlights: highlights,
      }, { timeout: 15000 });
    } catch {}
  };

  // 胜负弹窗
  useEffect(() => {
    if (!gameOver || !winner) return;
    const msg = winner === 1 ? '你赢了 🎉' : `${charName} 赢了`;
    setTimeout(() => Alert.alert('对局结束', msg, [
      { text: '再来一局', onPress: restart },
      { text: '看棋盘', style: 'cancel' },
    ]), 500);
  }, [gameOver, winner]);

  const restart = () => {
    setBoard(newBoard()); setTurn(1); setGameOver(false); setWinner(0);
    setHistory([]); aiThinkingRef.current = false; setChatMsgs([]);
    setTimeout(() => triggerAITalk('game_start', 0), 300);
  };

  const undo = () => {
    if (history.length < 2 || gameOver) return;
    const nh = [...history];
    const b = board.map(r => [...r]) as Cell[][];
    for (let i = 0; i < 2 && nh.length > 0; i++) { const l = nh.pop()!; b[l.y][l.x] = 0; }
    setBoard(b); setHistory(nh); setTurn(1);
  };

  const sendChat = async () => {
    if (sendingChat) return;
    Keyboard.dismiss();
    await sleep(80);
    const text = (inputTextRef.current || inputText).trim();
    if (!text) return;
    setInputText(''); inputTextRef.current = '';
    setChatMsgs(prev => [...prev, { id: `${Date.now()}_user`, role: 'user', text }]);
    setTimeout(() => chatScrollRef.current?.scrollToEnd({ animated: true }), 100);
    setSendingChat(true);
    await triggerAITalk('user_chat', history.length, text);
    setSendingChat(false);
  };

  // ── 渲染 ──
  const stars: [number, number][] = [[3, 3], [3, 11], [11, 3], [11, 11], [7, 7]];
  const last = history[history.length - 1];
  const status = gameOver
    ? (winner === 1 ? '你赢了 🎉' : `${charName} 赢了`)
    : (turn === 1 ? '你的回合(黑)' : `${charName} 在想...`);

  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
      <StatusBar barStyle="light-content" backgroundColor={C.card} />
      <View style={st.header}>
        <TouchableOpacity onPress={() => router.back()} style={st.backBtn}>
          <Text style={st.backText}>‹</Text>
        </TouchableOpacity>
        <View style={{ flex: 1 }}>
          <Text style={st.title}>五子棋 · vs {charName}</Text>
          <Text style={st.sub}>{status}</Text>
        </View>
      </View>

      <View style={st.scoreRow}>
        <View style={st.pill}><View style={[st.dot, { backgroundColor: '#111' }]} /><Text style={st.pillText}>你</Text></View>
        <Text style={st.vs}>VS</Text>
        <View style={st.pill}><View style={[st.dot, { backgroundColor: '#fafafa', borderWidth: 1, borderColor: '#555' }]} /><Text style={st.pillText}>{charName}</Text></View>
      </View>

      <View style={st.boardWrap}>
        <View style={{ width: BOARD_W, height: BOARD_W, backgroundColor: '#e6c98f', borderRadius: 6, padding: BOARD_PAD }}>
          <View style={{ width: GRID_W, height: GRID_W }}>
            {Array.from({ length: SIZE }).map((_, i) => <View key={`h${i}`} style={{ position: 'absolute', left: 0, top: i * CELL_SIZE - 0.5, width: GRID_W, height: 1, backgroundColor: '#666' }} />)}
            {Array.from({ length: SIZE }).map((_, i) => <View key={`v${i}`} style={{ position: 'absolute', left: i * CELL_SIZE - 0.5, top: 0, width: 1, height: GRID_W, backgroundColor: '#666' }} />)}
            {stars.map(([sx, sy], i) => <View key={`s${i}`} style={{ position: 'absolute', left: sx * CELL_SIZE - 3, top: sy * CELL_SIZE - 3, width: 6, height: 6, borderRadius: 3, backgroundColor: '#666' }} />)}
            {board.map((row, y) => row.map((c, x) => c === 0 ? null : (
              <View key={`p${x}_${y}`} pointerEvents="none" style={{
                position: 'absolute', left: x * CELL_SIZE - STONE_R, top: y * CELL_SIZE - STONE_R,
                width: STONE_R * 2, height: STONE_R * 2, borderRadius: STONE_R,
                backgroundColor: c === 1 ? '#111' : '#fafafa',
                borderWidth: c === 2 ? 1 : 0, borderColor: '#555',
                shadowColor: '#000', shadowOffset: { width: 1, height: 1 }, shadowOpacity: 0.3, shadowRadius: 1, elevation: 2,
              }} />
            )))}
            {last && <View pointerEvents="none" style={{ position: 'absolute', left: last.x * CELL_SIZE - 3, top: last.y * CELL_SIZE - 3, width: 6, height: 6, borderRadius: 3, backgroundColor: '#e53935' }} />}
            {Array.from({ length: SIZE }).map((_, y) => Array.from({ length: SIZE }).map((_, x) => (
              <TouchableOpacity key={`t${x}_${y}`} activeOpacity={0.4} onPress={() => onIntersectionPress(x, y)}
                style={{ position: 'absolute', left: x * CELL_SIZE - CELL_SIZE / 2, top: y * CELL_SIZE - CELL_SIZE / 2, width: CELL_SIZE, height: CELL_SIZE }} />
            )))}
          </View>
        </View>
      </View>

      <View style={st.btnRow}>
        <TouchableOpacity style={st.btn} onPress={undo} disabled={history.length < 2 || gameOver}>
          <Text style={[st.btnText, (history.length < 2 || gameOver) && st.btnDisabled]}>悔棋</Text>
        </TouchableOpacity>
        <TouchableOpacity style={[st.btn, st.btnPrimary]} onPress={restart}>
          <Text style={[st.btnText, { color: '#fff' }]}>重新开始</Text>
        </TouchableOpacity>
      </View>

      <View style={st.chatArea}>
        <ScrollView ref={chatScrollRef} style={{ flex: 1 }} contentContainerStyle={st.chatContent}
          onContentSizeChange={() => chatScrollRef.current?.scrollToEnd({ animated: true })}>
          {chatMsgs.length === 0 && <Text style={st.chatEmpty}>{charName} 会在下棋时开口</Text>}
          {chatMsgs.map(m => m.role === 'user' ? (
            <View key={m.id} style={st.userBubbleWrap}>
              <View style={st.userBubble}><Text style={st.userBubbleText}>{m.text}</Text></View>
            </View>
          ) : (
            <View key={m.id} style={st.charBubbleWrap}>
              <View style={st.charBubble}>
                <Text style={st.charJp}>{m.jp}</Text>
                {m.zh ? <Text style={st.charZh}>{m.zh}</Text> : null}
                {m.audioUri && <TouchableOpacity onPress={() => replayAudio(m.audioUri!)} style={st.replayBtn}><Text style={st.replayText}>🔊 点击重播</Text></TouchableOpacity>}
              </View>
            </View>
          ))}
        </ScrollView>
        <View style={[st.inputBar, { marginBottom: keyboardHeight }]}>
          <TextInput style={st.input} value={inputText}
            onChangeText={t => { setInputText(t); inputTextRef.current = t; }}
            placeholder={sendingChat ? '等 TA 回复...' : `跟 ${charName} 说...`}
            placeholderTextColor={C.textMute} editable={!sendingChat}
            onSubmitEditing={sendChat} blurOnSubmit={false} returnKeyType="send" multiline />
          <TouchableOpacity style={[st.sendBtn, { backgroundColor: (!sendingChat && inputText.trim()) ? C.accent : C.textMute + '55' }]}
            onPress={sendChat} disabled={sendingChat || !inputText.trim()}>
            <Text style={st.sendBtnText}>发送</Text>
          </TouchableOpacity>
        </View>
      </View>
    </View>
  );
}

const st = StyleSheet.create({
  header: { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 12, paddingTop: Platform.OS === 'ios' ? 50 : 40, paddingBottom: 8, borderBottomWidth: 1, borderBottomColor: C.border, backgroundColor: C.card },
  backBtn: { paddingHorizontal: 6, paddingVertical: 4 },
  backText: { color: C.text, fontSize: 30, lineHeight: 32, fontWeight: '300' },
  title: { color: C.text, fontSize: 16, fontWeight: '700' },
  sub: { color: C.textMute, fontSize: 11, marginTop: 2 },
  scoreRow: { flexDirection: 'row', alignItems: 'center', justifyContent: 'center', paddingVertical: 6, gap: 10 },
  pill: { flexDirection: 'row', alignItems: 'center', backgroundColor: C.card, paddingHorizontal: 10, paddingVertical: 4, borderRadius: 16, gap: 6 },
  dot: { width: 14, height: 14, borderRadius: 7 },
  pillText: { color: C.text, fontSize: 13 },
  vs: { color: C.textMute, fontSize: 11 },
  boardWrap: { alignItems: 'center', paddingVertical: 4 },
  btnRow: { flexDirection: 'row', paddingHorizontal: 16, gap: 10, marginTop: 4, marginBottom: 4 },
  btn: { flex: 1, paddingVertical: 8, borderRadius: 10, backgroundColor: C.card, alignItems: 'center', borderWidth: 1, borderColor: C.border },
  btnPrimary: { backgroundColor: C.accent, borderColor: C.accent },
  btnText: { color: C.text, fontSize: 13, fontWeight: '500' },
  btnDisabled: { color: C.textMute },
  chatArea: { flex: 1, borderTopWidth: 1, borderTopColor: C.border, backgroundColor: 'rgba(0,0,0,0.15)' },
  chatContent: { padding: 10, paddingBottom: 4, flexGrow: 1 },
  chatEmpty: { color: C.textMute, fontSize: 12, textAlign: 'center', marginTop: 12, fontStyle: 'italic' },
  userBubbleWrap: { alignItems: 'flex-end', marginBottom: 6 },
  userBubble: { backgroundColor: C.accent, borderRadius: 14, borderBottomRightRadius: 4, paddingHorizontal: 12, paddingVertical: 7, maxWidth: '80%' },
  userBubbleText: { color: '#fff', fontSize: 14, lineHeight: 20 },
  charBubbleWrap: { alignItems: 'flex-start', marginBottom: 6 },
  charBubble: { backgroundColor: C.card, borderRadius: 14, borderBottomLeftRadius: 4, paddingHorizontal: 12, paddingVertical: 7, maxWidth: '85%', borderWidth: 1, borderColor: C.border },
  charJp: { color: C.text, fontSize: 14, lineHeight: 20 },
  charZh: { color: C.textMute, fontSize: 12, marginTop: 3, fontStyle: 'italic' },
  replayBtn: { marginTop: 5 },
  replayText: { color: C.accent2, fontSize: 11 },
  inputBar: { flexDirection: 'row', alignItems: 'flex-end', paddingHorizontal: 10, paddingVertical: 6, borderTopWidth: 1, borderTopColor: C.border, backgroundColor: C.card, gap: 8 },
  input: { flex: 1, backgroundColor: C.bg, borderWidth: 1, borderColor: C.border, borderRadius: 18, paddingHorizontal: 14, paddingVertical: 7, color: C.text, fontSize: 14, maxHeight: 80, minHeight: 34 },
  sendBtn: { paddingHorizontal: 14, paddingVertical: 8, borderRadius: 18 },
  sendBtnText: { color: '#fff', fontSize: 13, fontWeight: '600' },
});