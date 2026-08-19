// app/games/gomoku.tsx
// 五子棋 v2 —— 双人对战(你 vs 角色) + 边玩边聊天
//
// 布局(参考用户截图):
//   ┌─────────────────────────┐
//   │ header  ‹  五子棋 vs Xxx │
//   ├─────────────────────────┤
//   │ 比分栏 [你] VS [TA]     │
//   ├─────────────────────────┤
//   │                         │
//   │       15×15 棋盘        │
//   │                         │
//   ├─────────────────────────┤
//   │ [悔棋] [重开]           │
//   ├─────────────────────────┤
//   │ 聊天气泡区 (可滚)       │
//   │  角色: へえ、いいとこ… │
//   │  你: 你先手不厚道       │
//   ├─────────────────────────┤
//   │ [输入框]         [发送] │
//   └─────────────────────────┘
//
// AI 说话时机(见 route_game.py 的 event 定义):
//   - 进入页面: game_start  必说
//   - AI 落子: 关键节点必说(活三/活四) + 普通步骤 25% 概率
//   - 玩家落子: 关键节点必说(压过来了) + 普通步骤 15% 概率
//   - 用户主动说话: 必回
//   - 胜负: 必说
//
// 消息输入的 IME 截断修复(和主聊天页同源问题):
//   · 用 useRef 双写 inputText,发送时从 ref 读最新值(state 可能落后一次 IME commit)
//   · 发送前 Keyboard.dismiss() 强制中文输入法 commit 未确认的候选
//   · sleep 80ms 让 state 追上来再读
import AsyncStorage from '@react-native-async-storage/async-storage';
import axios from 'axios';
import { Audio } from 'expo-av';
import * as FileSystem from 'expo-file-system/legacy';
import { useFocusEffect, useRouter } from 'expo-router';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
    Alert, Dimensions,
    Keyboard,
    KeyboardAvoidingView,
    Platform,
    ScrollView, StatusBar, StyleSheet, Text, TextInput,
    TouchableOpacity, View,
} from 'react-native';
import { C, SERVER_URL } from '../../constants/theme';

const FIXED_USER_ID = 'user_mofpiyd7442ia7';
const DEFAULT_CHAR_KEY = 'default_character_id';

// ─────────────────── 棋盘常量 ───────────────────

const SIZE = 15;
type Cell = 0 | 1 | 2;                 // 0=空 1=玩家(黑) 2=AI(白)

const SCREEN_W = Dimensions.get('window').width;
const SCREEN_H = Dimensions.get('window').height;
// 棋盘不能占满,不然聊天区没空间 —— 限制上限
const BOARD_MAX = Math.min(SCREEN_W - 32, SCREEN_H * 0.45);
const BOARD_PAD = 12;
const BOARD_W = BOARD_MAX;
const GRID_W = BOARD_W - BOARD_PAD * 2;
const CELL = GRID_W / (SIZE - 1);
const STONE_R = CELL * 0.42;

const DIRS: [number, number][] = [[1, 0], [0, 1], [1, 1], [1, -1]];

// ─────────────────── 棋型 / 判定 ───────────────────

const newBoard = (): Cell[][] =>
  Array.from({ length: SIZE }, () => Array(SIZE).fill(0) as Cell[]);

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

/** 位置评分:如果 color 下在这里,能形成的最大威胁。用于 AI 决策 + 局势评估。 */
function evalCell(b: Cell[][], x: number, y: number, color: Cell): number {
  let score = 0;
  for (const [dx, dy] of DIRS) {
    let count = 1;
    let openEnds = 0;
    let nx = x + dx, ny = y + dy;
    while (nx >= 0 && nx < SIZE && ny >= 0 && ny < SIZE && b[ny][nx] === color) { count++; nx += dx; ny += dy; }
    if (nx >= 0 && nx < SIZE && ny >= 0 && ny < SIZE && b[ny][nx] === 0) openEnds++;
    nx = x - dx; ny = y - dy;
    while (nx >= 0 && nx < SIZE && ny >= 0 && ny < SIZE && b[ny][nx] === color) { count++; nx -= dx; ny -= dy; }
    if (nx >= 0 && nx < SIZE && ny >= 0 && ny < SIZE && b[ny][nx] === 0) openEnds++;

    if (count >= 5) score += 100000;
    else if (count === 4 && openEnds === 2) score += 10000;
    else if (count === 4 && openEnds === 1) score += 1000;
    else if (count === 3 && openEnds === 2) score += 500;
    else if (count === 3 && openEnds === 1) score += 100;
    else if (count === 2 && openEnds === 2) score += 50;
    else if (count === 2 && openEnds === 1) score += 10;
    else score += count;
  }
  return score;
}

function aiMove(b: Cell[][]): [number, number] {
  let best: [number, number] | null = null;
  let bestScore = -1;
  for (let y = 0; y < SIZE; y++) {
    for (let x = 0; x < SIZE; x++) {
      if (b[y][x] !== 0) continue;
      let hasNeighbor = false;
      for (let dy = -2; dy <= 2 && !hasNeighbor; dy++) {
        for (let dx = -2; dx <= 2 && !hasNeighbor; dx++) {
          const nx = x + dx, ny = y + dy;
          if (nx >= 0 && nx < SIZE && ny >= 0 && ny < SIZE && b[ny][nx] !== 0) hasNeighbor = true;
        }
      }
      if (!hasNeighbor) continue;
      const atk = evalCell(b, x, y, 2);
      const def = evalCell(b, x, y, 1);
      const score = atk + def * 0.9;
      if (score > bestScore) { bestScore = score; best = [x, y]; }
    }
  }
  return best ?? [Math.floor(SIZE / 2), Math.floor(SIZE / 2)];
}

/** 判断某一步的战术等级 —— 用来决定要不要触发 AI 说话。
 *  基于"落子后周围最长连子数"这个简单指标(不考虑活/眠,够用了)。
 *  返回 'four'(冲/活四) | 'three'(眠/活三) | 'normal'
 */
function classifyMove(b: Cell[][], x: number, y: number, color: Cell): 'four' | 'three' | 'normal' {
  let maxLine = 0;
  for (const [dx, dy] of DIRS) {
    let count = 1;
    let nx = x + dx, ny = y + dy;
    while (nx >= 0 && nx < SIZE && ny >= 0 && ny < SIZE && b[ny][nx] === color) { count++; nx += dx; ny += dy; }
    nx = x - dx; ny = y - dy;
    while (nx >= 0 && nx < SIZE && ny >= 0 && ny < SIZE && b[ny][nx] === color) { count++; nx -= dx; ny -= dy; }
    if (count > maxLine) maxLine = count;
  }
  if (maxLine >= 4) return 'four';
  if (maxLine >= 3) return 'three';
  return 'normal';
}

/** 粗略局势判断:比较双方"下一步能造成的最大威胁" */
function evaluateSituation(b: Cell[][]): 'winning' | 'losing' | 'even' {
  let userMax = 0, aiMax = 0;
  for (let y = 0; y < SIZE; y++) {
    for (let x = 0; x < SIZE; x++) {
      if (b[y][x] !== 0) continue;
      const u = evalCell(b, x, y, 1);
      const a = evalCell(b, x, y, 2);
      if (u > userMax) userMax = u;
      if (a > aiMax) aiMax = a;
    }
  }
  const diff = aiMax - userMax;
  if (diff > 300) return 'winning';
  if (diff < -300) return 'losing';
  return 'even';
}

// ─────────────────── 类型 ───────────────────

interface ChatMsg {
  id: string;
  role: 'user' | 'char';
  jp?: string;
  zh?: string;
  text?: string;       // 只有 user 有
  emotion?: string;
  audioUri?: string;   // 本地保存路径,复播用
}

interface CharacterMeta { id: string; name: string; }

// ─────────────────── 组件 ───────────────────

export default function GomokuScreen() {
  const router = useRouter();

  // 棋盘
  const [board, setBoard] = useState<Cell[][]>(newBoard());
  const [turn, setTurn] = useState<Cell>(1);
  const [gameOver, setGameOver] = useState(false);
  const [winner, setWinner] = useState<Cell | 0>(0);
  const [history, setHistory] = useState<{ x: number; y: number; color: Cell }[]>([]);
  const aiThinkingRef = useRef(false);

  // 角色
  const [characterId, setCharacterId] = useState<string>('gojo');
  const [charName, setCharName] = useState('对手');

  // 聊天
  const [chatMsgs, setChatMsgs] = useState<ChatMsg[]>([]);
  const [inputText, setInputText] = useState('');
  const inputTextRef = useRef('');          // ★ IME 双写:发送时从 ref 读最新值
  const [sendingChat, setSendingChat] = useState(false);
  const chatScrollRef = useRef<ScrollView>(null);
  const talkInFlightRef = useRef(false);    // 防止 AI 说话请求叠加
  const currentSoundRef = useRef<Audio.Sound | null>(null);
  const gameStartedRef = useRef(false);     // 只在第一次挂载时触发 game_start

  // ── 初始化:拿默认角色 + 触发开局说话 ──
  useFocusEffect(useCallback(() => {
    let cancelled = false;
    (async () => {
      try {
        const saved = await AsyncStorage.getItem(DEFAULT_CHAR_KEY);
        const cid = saved || 'gojo';
        if (cancelled) return;
        setCharacterId(cid);

        // 拿角色名
        try {
          const res = await axios.get(`${SERVER_URL}/characters_all`, { timeout: 5000 });
          const chars: CharacterMeta[] = res.data?.characters || [];
          const found = chars.find(c => c.id === cid);
          if (found && !cancelled) setCharName(found.name);
        } catch {}

        // 只在第一次进游戏时触发开局白 —— useFocusEffect 会重复调用,加个 ref 拦一下
        if (!gameStartedRef.current) {
          gameStartedRef.current = true;
          setTimeout(() => triggerAITalk('game_start', 0, 'even', undefined, cid), 300);
        }
      } catch (e: any) {
        console.warn('[gomoku] init', e?.message);
      }
    })();
    return () => { cancelled = true; };
  }, []));

  // 卸载时停音频
  useEffect(() => {
    return () => {
      if (currentSoundRef.current) {
        currentSoundRef.current.unloadAsync().catch(() => {});
        currentSoundRef.current = null;
      }
    };
  }, []);

  // ── 音频:base64 保存到临时文件后播 ──
  const playAudioB64 = async (b64: string, msgId: string): Promise<string | null> => {
    if (!b64 || b64.length < 100) return null;
    try {
      const dir = `${FileSystem.cacheDirectory}gomoku_audio/`;
      await FileSystem.makeDirectoryAsync(dir, { intermediates: true }).catch(() => {});
      const uri = `${dir}${msgId}.mp3`;
      await FileSystem.writeAsStringAsync(uri, b64, { encoding: FileSystem.EncodingType.Base64 });

      if (currentSoundRef.current) {
        try { await currentSoundRef.current.unloadAsync(); } catch {}
        currentSoundRef.current = null;
      }
      const { sound } = await Audio.Sound.createAsync({ uri }, { shouldPlay: true });
      currentSoundRef.current = sound;
      return uri;
    } catch (e: any) {
      console.warn('[gomoku audio]', e?.message);
      return null;
    }
  };

  const replayAudio = async (uri: string) => {
    try {
      if (currentSoundRef.current) {
        try { await currentSoundRef.current.unloadAsync(); } catch {}
        currentSoundRef.current = null;
      }
      const { sound } = await Audio.Sound.createAsync({ uri }, { shouldPlay: true });
      currentSoundRef.current = sound;
    } catch (e: any) {
      console.warn('[gomoku replay]', e?.message);
    }
  };

  // ── 触发 AI 说话 ──
  //   cid 显式传入以避免闭包捕获旧值(game_start 时 characterId 还没 setState 到)
  const triggerAITalk = async (
    event: string,
    moveCount: number,
    situation: 'winning' | 'losing' | 'even',
    userText?: string,
    cid?: string,
  ) => {
    // user_chat 优先级最高,其他情况如果有请求在飞就跳过(避免话叠话)
    if (talkInFlightRef.current && event !== 'user_chat') return;
    talkInFlightRef.current = true;
    try {
      const res = await axios.post(`${SERVER_URL}/game/gomoku/talk`, {
        user_id: FIXED_USER_ID,
        character_id: cid || characterId,
        event,
        move_count: moveCount,
        ai_color: 'white',                // AI 执白,玩家执黑先手
        situation,
        user_text: userText,
      }, { timeout: 20000 });

      if (!res.data?.say) return;

      const msgId = `${Date.now()}_char_${Math.random().toString(36).slice(2, 6)}`;
      const audioUri = await playAudioB64(res.data.audio_b64 || '', msgId);

      const msg: ChatMsg = {
        id: msgId,
        role: 'char',
        jp: res.data.jp,
        zh: res.data.zh,
        emotion: res.data.emotion,
        audioUri: audioUri || undefined,
      };
      setChatMsgs(prev => [...prev, msg]);
      setTimeout(() => chatScrollRef.current?.scrollToEnd({ animated: true }), 100);
    } catch (e: any) {
      console.warn('[gomoku talk]', e?.message);
    } finally {
      talkInFlightRef.current = false;
    }
  };

  // ── 落子 ──
  const placeAndCheck = (
    x: number, y: number, color: Cell,
  ): { newBoard: Cell[][]; win: boolean; tactical: 'four' | 'three' | 'normal' } => {
    const b = board.map(row => [...row]) as Cell[][];
    b[y][x] = color;
    const win = checkWin(b, x, y, color);
    const tactical = classifyMove(b, x, y, color);
    return { newBoard: b, win, tactical };
  };

  const onIntersectionPress = (x: number, y: number) => {
    if (gameOver || turn !== 1 || aiThinkingRef.current) return;
    if (board[y][x] !== 0) return;

    const { newBoard: nb, win, tactical } = placeAndCheck(x, y, 1);
    setBoard(nb);
    setHistory(prev => [...prev, { x, y, color: 1 }]);
    const nextCount = history.length + 1;

    if (win) {
      setGameOver(true); setWinner(1);
      triggerAITalk('user_win', nextCount, 'losing');
      return;
    }

    // 根据战术等级决定 AI 要不要说话
    if (tactical === 'four') {
      triggerAITalk('user_attack_four', nextCount, evaluateSituation(nb));
    } else if (tactical === 'three') {
      triggerAITalk('user_attack_three', nextCount, evaluateSituation(nb));
    } else if (Math.random() < 0.15) {
      triggerAITalk('user_normal', nextCount, evaluateSituation(nb));
    }

    setTurn(2);
  };

  // AI 回合
  useEffect(() => {
    if (turn !== 2 || gameOver) return;
    aiThinkingRef.current = true;
    const t = setTimeout(() => {
      const [x, y] = aiMove(board);
      const b = board.map(row => [...row]) as Cell[][];
      b[y][x] = 2;
      setBoard(b);
      setHistory(prev => [...prev, { x, y, color: 2 }]);
      const nextCount = history.length + 1;

      const win = checkWin(b, x, y, 2);
      if (win) {
        setGameOver(true); setWinner(2);
        triggerAITalk('ai_win', nextCount, 'winning');
        aiThinkingRef.current = false;
        return;
      }

      const tactical = classifyMove(b, x, y, 2);
      if (tactical === 'four') {
        triggerAITalk('ai_attack_four', nextCount, evaluateSituation(b));
      } else if (tactical === 'three') {
        triggerAITalk('ai_attack_three', nextCount, evaluateSituation(b));
      } else if (Math.random() < 0.25) {
        triggerAITalk('ai_normal', nextCount, evaluateSituation(b));
      }

      setTurn(1);
      aiThinkingRef.current = false;
    }, 500);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [turn]);

  // 胜负提示
  useEffect(() => {
    if (gameOver && winner) {
      const msg = winner === 1 ? '你赢了 🎉' : `${charName} 赢了`;
      setTimeout(() => Alert.alert('对局结束', msg, [
        { text: '再来一局', onPress: restart },
        { text: '看棋盘', style: 'cancel' },
      ]), 500);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gameOver, winner]);

  // ── 操作按钮 ──
  const restart = () => {
    setBoard(newBoard());
    setTurn(1);
    setGameOver(false);
    setWinner(0);
    setHistory([]);
    aiThinkingRef.current = false;
    setChatMsgs([]);
    // 再触发一次开局
    setTimeout(() => triggerAITalk('game_start', 0, 'even'), 300);
  };

  const undo = () => {
    if (history.length < 2 || gameOver) return;
    const newHistory = [...history];
    const b = board.map(row => [...row]) as Cell[][];
    for (let i = 0; i < 2 && newHistory.length > 0; i++) {
      const last = newHistory.pop()!;
      b[last.y][last.x] = 0;
    }
    setBoard(b);
    setHistory(newHistory);
    setTurn(1);
  };

  // ── 发送聊天(带 IME 修复) ──
  const sendChat = async () => {
    if (sendingChat) return;

    // ★★ IME 截断修复(问题 2):
    //   中文输入法在 composition 状态下(拼音候选还没选字确认),
    //   TextInput state 里可能没接住最后一次 keystroke。
    //   先 Keyboard.dismiss() 强制 IME commit,再 sleep 80ms 让 state 追上,
    //   最后从 ref 读最新值 —— ref 会在 onChangeText 里同步更新。
    Keyboard.dismiss();
    await new Promise<void>(r => setTimeout(r, 80));

    const text = (inputTextRef.current || inputText).trim();
    if (!text) return;

    setInputText('');
    inputTextRef.current = '';

    const userMsg: ChatMsg = {
      id: `${Date.now()}_user`,
      role: 'user',
      text,
    };
    setChatMsgs(prev => [...prev, userMsg]);
    setTimeout(() => chatScrollRef.current?.scrollToEnd({ animated: true }), 100);

    setSendingChat(true);
    await triggerAITalk('user_chat', history.length, evaluateSituation(board), text);
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

      <KeyboardAvoidingView
        style={{ flex: 1 }}
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
        keyboardVerticalOffset={0}
      >
        {/* header */}
        <View style={st.header}>
          <TouchableOpacity onPress={() => router.back()} style={st.backBtn}>
            <Text style={st.backText}>‹</Text>
          </TouchableOpacity>
          <View style={{ flex: 1 }}>
            <Text style={st.title}>五子棋 · vs {charName}</Text>
            <Text style={st.sub}>{status}</Text>
          </View>
        </View>

        {/* 比分栏 */}
        <View style={st.scoreRow}>
          <View style={st.pill}>
            <View style={[st.dot, { backgroundColor: '#111' }]} />
            <Text style={st.pillText}>你</Text>
          </View>
          <Text style={st.vs}>VS</Text>
          <View style={st.pill}>
            <View style={[st.dot, { backgroundColor: '#fafafa', borderWidth: 1, borderColor: '#555' }]} />
            <Text style={st.pillText}>{charName}</Text>
          </View>
        </View>

        {/* 棋盘 */}
        <View style={st.boardWrap}>
          <View style={{
            width: BOARD_W, height: BOARD_W,
            backgroundColor: '#e6c98f',
            borderRadius: 6, padding: BOARD_PAD,
          }}>
            <View style={{ width: GRID_W, height: GRID_W }}>
              {/* 横竖线 */}
              {Array.from({ length: SIZE }).map((_, i) => (
                <View key={`h${i}`} style={{
                  position: 'absolute', left: 0, top: i * CELL - 0.5,
                  width: GRID_W, height: 1, backgroundColor: '#666',
                }} />
              ))}
              {Array.from({ length: SIZE }).map((_, i) => (
                <View key={`v${i}`} style={{
                  position: 'absolute', left: i * CELL - 0.5, top: 0,
                  width: 1, height: GRID_W, backgroundColor: '#666',
                }} />
              ))}
              {/* 星位 */}
              {stars.map(([sx, sy], i) => (
                <View key={`s${i}`} style={{
                  position: 'absolute',
                  left: sx * CELL - 3, top: sy * CELL - 3,
                  width: 6, height: 6, borderRadius: 3, backgroundColor: '#666',
                }} />
              ))}
              {/* 棋子 */}
              {board.map((row, y) => row.map((c, x) => {
                if (c === 0) return null;
                return (
                  <View key={`p${x}_${y}`} pointerEvents="none" style={{
                    position: 'absolute',
                    left: x * CELL - STONE_R, top: y * CELL - STONE_R,
                    width: STONE_R * 2, height: STONE_R * 2,
                    borderRadius: STONE_R,
                    backgroundColor: c === 1 ? '#111' : '#fafafa',
                    borderWidth: c === 2 ? 1 : 0, borderColor: '#555',
                    shadowColor: '#000', shadowOffset: { width: 1, height: 1 },
                    shadowOpacity: 0.3, shadowRadius: 1, elevation: 2,
                  }} />
                );
              }))}
              {/* 最后一手红点 */}
              {last && (
                <View pointerEvents="none" style={{
                  position: 'absolute',
                  left: last.x * CELL - 3, top: last.y * CELL - 3,
                  width: 6, height: 6, borderRadius: 3, backgroundColor: '#e53935',
                }} />
              )}
              {/* 点击热区 */}
              {Array.from({ length: SIZE }).map((_, y) =>
                Array.from({ length: SIZE }).map((_, x) => (
                  <TouchableOpacity
                    key={`t${x}_${y}`}
                    activeOpacity={0.4}
                    onPress={() => onIntersectionPress(x, y)}
                    style={{
                      position: 'absolute',
                      left: x * CELL - CELL / 2, top: y * CELL - CELL / 2,
                      width: CELL, height: CELL,
                    }}
                  />
                ))
              )}
            </View>
          </View>
        </View>

        {/* 操作按钮 */}
        <View style={st.btnRow}>
          <TouchableOpacity style={st.btn} onPress={undo} disabled={history.length < 2 || gameOver}>
            <Text style={[st.btnText, (history.length < 2 || gameOver) && st.btnDisabled]}>悔棋</Text>
          </TouchableOpacity>
          <TouchableOpacity style={[st.btn, st.btnPrimary]} onPress={restart}>
            <Text style={[st.btnText, { color: '#fff' }]}>重新开始</Text>
          </TouchableOpacity>
        </View>

        {/* 聊天区 —— flex: 1 撑满剩余空间 */}
        <View style={st.chatArea}>
          <ScrollView
            ref={chatScrollRef}
            style={{ flex: 1 }}
            contentContainerStyle={st.chatContent}
            onContentSizeChange={() => chatScrollRef.current?.scrollToEnd({ animated: true })}
          >
            {chatMsgs.length === 0 && (
              <Text style={st.chatEmpty}>{charName} 会在下棋时开口</Text>
            )}
            {chatMsgs.map(m => {
              if (m.role === 'user') {
                return (
                  <View key={m.id} style={st.userBubbleWrap}>
                    <View style={st.userBubble}>
                      <Text style={st.userBubbleText}>{m.text}</Text>
                    </View>
                  </View>
                );
              }
              return (
                <View key={m.id} style={st.charBubbleWrap}>
                  <View style={st.charBubble}>
                    <Text style={st.charJp}>{m.jp}</Text>
                    {m.zh ? <Text style={st.charZh}>{m.zh}</Text> : null}
                    {m.audioUri ? (
                      <TouchableOpacity onPress={() => replayAudio(m.audioUri!)} style={st.replayBtn}>
                        <Text style={st.replayText}>🔊 点击重播</Text>
                      </TouchableOpacity>
                    ) : null}
                  </View>
                </View>
              );
            })}
          </ScrollView>

          {/* 输入框 */}
          <View style={st.inputBar}>
            <TextInput
              style={st.input}
              value={inputText}
              onChangeText={(t) => {
                setInputText(t);
                inputTextRef.current = t;   // ★ 双写:发送时从 ref 读,防 IME 截断
              }}
              placeholder={sendingChat ? '等 TA 回复...' : `跟 ${charName} 说...`}
              placeholderTextColor={C.textMute}
              editable={!sendingChat}
              onSubmitEditing={sendChat}
              blurOnSubmit={false}
              returnKeyType="send"
              multiline
            />
            <TouchableOpacity
              style={[st.sendBtn, {
                backgroundColor: (!sendingChat && inputText.trim()) ? C.accent : C.textMute + '55',
              }]}
              onPress={sendChat}
              disabled={sendingChat || !inputText.trim()}
            >
              <Text style={st.sendBtnText}>发送</Text>
            </TouchableOpacity>
          </View>
        </View>
      </KeyboardAvoidingView>
    </View>
  );
}

const st = StyleSheet.create({
  header: {
    flexDirection: 'row', alignItems: 'center',
    paddingHorizontal: 12,
    paddingTop: Platform.OS === 'ios' ? 50 : 40,
    paddingBottom: 10,
    borderBottomWidth: 1, borderBottomColor: C.border,
    backgroundColor: C.card,
  },
  backBtn: { paddingHorizontal: 6, paddingVertical: 4 },
  backText: { color: C.text, fontSize: 30, lineHeight: 32, fontWeight: '300' },
  title: { color: C.text, fontSize: 16, fontWeight: '700' },
  sub: { color: C.textMute, fontSize: 11, marginTop: 2 },

  scoreRow: {
    flexDirection: 'row', alignItems: 'center', justifyContent: 'center',
    paddingVertical: 8, gap: 10,
  },
  pill: {
    flexDirection: 'row', alignItems: 'center',
    backgroundColor: C.card,
    paddingHorizontal: 10, paddingVertical: 5,
    borderRadius: 16, gap: 6,
  },
  dot: { width: 14, height: 14, borderRadius: 7 },
  pillText: { color: C.text, fontSize: 13 },
  vs: { color: C.textMute, fontSize: 11 },

  boardWrap: { alignItems: 'center', paddingVertical: 6 },

  btnRow: {
    flexDirection: 'row',
    paddingHorizontal: 16, gap: 10, marginTop: 6, marginBottom: 8,
  },
  btn: {
    flex: 1, paddingVertical: 10, borderRadius: 10,
    backgroundColor: C.card, alignItems: 'center',
    borderWidth: 1, borderColor: C.border,
  },
  btnPrimary: { backgroundColor: C.accent, borderColor: C.accent },
  btnText: { color: C.text, fontSize: 13, fontWeight: '500' },
  btnDisabled: { color: C.textMute },

  chatArea: {
    flex: 1,
    borderTopWidth: 1, borderTopColor: C.border,
    backgroundColor: 'rgba(0,0,0,0.15)',
  },
  chatContent: { padding: 12, paddingBottom: 8, flexGrow: 1 },
  chatEmpty: {
    color: C.textMute, fontSize: 12, textAlign: 'center',
    marginTop: 16, fontStyle: 'italic',
  },

  userBubbleWrap: { alignItems: 'flex-end', marginBottom: 8 },
  userBubble: {
    backgroundColor: C.accent,
    borderRadius: 14, borderBottomRightRadius: 4,
    paddingHorizontal: 12, paddingVertical: 8,
    maxWidth: '80%',
  },
  userBubbleText: { color: '#fff', fontSize: 14, lineHeight: 20 },

  charBubbleWrap: { alignItems: 'flex-start', marginBottom: 8 },
  charBubble: {
    backgroundColor: C.card,
    borderRadius: 14, borderBottomLeftRadius: 4,
    paddingHorizontal: 12, paddingVertical: 8,
    maxWidth: '85%',
    borderWidth: 1, borderColor: C.border,
  },
  charJp: { color: C.text, fontSize: 14, lineHeight: 20 },
  charZh: { color: C.textMute, fontSize: 12, marginTop: 4, fontStyle: 'italic' },
  replayBtn: { marginTop: 6 },
  replayText: { color: C.accent2, fontSize: 11 },

  inputBar: {
    flexDirection: 'row', alignItems: 'flex-end',
    paddingHorizontal: 10, paddingVertical: 8,
    borderTopWidth: 1, borderTopColor: C.border,
    backgroundColor: C.card,
    gap: 8,
  },
  input: {
    flex: 1,
    backgroundColor: C.bg,
    borderWidth: 1, borderColor: C.border,
    borderRadius: 18,
    paddingHorizontal: 14, paddingVertical: 8,
    color: C.text, fontSize: 14,
    maxHeight: 100, minHeight: 36,
  },
  sendBtn: {
    paddingHorizontal: 14, paddingVertical: 9,
    borderRadius: 18,
  },
  sendBtnText: { color: '#fff', fontSize: 13, fontWeight: '600' },
});