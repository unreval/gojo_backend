// app/games/gomoku.tsx  v5
// 算法快速落子 + 聊天随时可用(不受回合限制) + 战术触发说话
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

const UID = 'user_mofpiyd7442ia7';
const SIZE = 15;
type Cell = 0 | 1 | 2;
const DIRS: [number, number][] = [[1, 0], [0, 1], [1, 1], [1, -1]];

const SW = Dimensions.get('window').width;
const SH = Dimensions.get('window').height;
const BW = Math.min(SW - 32, SH * 0.38);
const BP = 10;
const GW = BW - BP * 2;
const CS = GW / (SIZE - 1);
const SR = CS * 0.42;

const sleep = (ms: number) => new Promise<void>(r => setTimeout(r, ms));
const newBoard = (): Cell[][] => Array.from({ length: SIZE }, () => Array(SIZE).fill(0) as Cell[]);

function checkWin(b: Cell[][], x: number, y: number, c: Cell): boolean {
  for (const [dx, dy] of DIRS) {
    let n = 1, nx = x + dx, ny = y + dy;
    while (nx >= 0 && nx < SIZE && ny >= 0 && ny < SIZE && b[ny][nx] === c) { n++; nx += dx; ny += dy; }
    nx = x - dx; ny = y - dy;
    while (nx >= 0 && nx < SIZE && ny >= 0 && ny < SIZE && b[ny][nx] === c) { n++; nx -= dx; ny -= dy; }
    if (n >= 5) return true;
  }
  return false;
}

// 战术分类:落子后最长连子数
function classify(b: Cell[][], x: number, y: number, c: Cell): 'four' | 'three' | 'normal' {
  let max = 0;
  for (const [dx, dy] of DIRS) {
    let n = 1, nx = x + dx, ny = y + dy;
    while (nx >= 0 && nx < SIZE && ny >= 0 && ny < SIZE && b[ny][nx] === c) { n++; nx += dx; ny += dy; }
    nx = x - dx; ny = y - dy;
    while (nx >= 0 && nx < SIZE && ny >= 0 && ny < SIZE && b[ny][nx] === c) { n++; nx -= dx; ny -= dy; }
    if (n > max) max = n;
  }
  return max >= 4 ? 'four' : max >= 3 ? 'three' : 'normal';
}

function evalSituation(b: Cell[][]): 'winning' | 'losing' | 'even' {
  let uM = 0, aM = 0;
  for (let y = 0; y < SIZE; y++) for (let x = 0; x < SIZE; x++) {
    if (b[y][x] !== 0) continue;
    let uS = 0, aS = 0;
    for (const [dx, dy] of DIRS) {
      for (const color of [1, 2] as Cell[]) {
        let n = 1, oe = 0;
        let nx = x + dx, ny = y + dy;
        while (nx >= 0 && nx < SIZE && ny >= 0 && ny < SIZE && b[ny][nx] === color) { n++; nx += dx; ny += dy; }
        if (nx >= 0 && nx < SIZE && ny >= 0 && ny < SIZE && b[ny][nx] === 0) oe++;
        nx = x - dx; ny = y - dy;
        while (nx >= 0 && nx < SIZE && ny >= 0 && ny < SIZE && b[ny][nx] === color) { n++; nx -= dx; ny -= dy; }
        if (nx >= 0 && nx < SIZE && ny >= 0 && ny < SIZE && b[ny][nx] === 0) oe++;
        const s = n >= 5 ? 100000 : n === 4 && oe === 2 ? 10000 : n === 4 ? 1000 : n === 3 && oe === 2 ? 500 : n === 3 ? 100 : n * 10;
        if (color === 1) uS = Math.max(uS, s); else aS = Math.max(aS, s);
      }
    }
    uM = Math.max(uM, uS); aM = Math.max(aM, aS);
  }
  const d = aM - uM;
  return d > 300 ? 'winning' : d < -300 ? 'losing' : 'even';
}

interface ChatMsg { id: string; role: 'user' | 'char'; jp?: string; zh?: string; text?: string; emotion?: string; audioUri?: string; }

export default function GomokuScreen() {
  const router = useRouter();
  const [board, setBoard] = useState<Cell[][]>(newBoard());
  const [turn, setTurn] = useState<Cell>(1);
  const [gameOver, setGameOver] = useState(false);
  const [winner, setWinner] = useState<Cell | 0>(0);
  const [history, setHistory] = useState<{ x: number; y: number; color: Cell }[]>([]);
  const aiRef = useRef(false);
  const [charId, setCharId] = useState('gojo');
  const [charName, setCharName] = useState('对手');
  const [msgs, setMsgs] = useState<ChatMsg[]>([]);
  const [inp, setInp] = useState('');
  const inpRef = useRef('');
  const [sending, setSending] = useState(false);
  const scrollRef = useRef<ScrollView>(null);
  const talkRef = useRef(false);
  const sndRef = useRef<Audio.Sound | null>(null);
  const startedRef = useRef(false);
  const [kbH, setKbH] = useState(0);

  // 键盘
  useEffect(() => {
    const h = Dimensions.get('window').height;
    const sE = Platform.OS === 'ios' ? 'keyboardWillShow' : 'keyboardDidShow';
    const hE = Platform.OS === 'ios' ? 'keyboardWillHide' : 'keyboardDidHide';
    const s1 = Keyboard.addListener(sE, e => {
      const sY = e.endCoordinates.screenY ?? 0;
      const rH = e.endCoordinates.height ?? 0;
      setKbH(sY > 0 ? Math.max(h - sY, rH) : rH);
      setTimeout(() => scrollRef.current?.scrollToEnd({ animated: true }), 50);
    });
    const s2 = Keyboard.addListener(hE, () => setKbH(0));
    return () => { s1.remove(); s2.remove(); };
  }, []);

  useFocusEffect(useCallback(() => {
    let c = false;
    (async () => {
      const saved = await AsyncStorage.getItem('default_character_id').catch(() => null);
      const cid = saved || 'gojo';
      if (c) return;
      setCharId(cid);
      try {
        const r = await axios.get(`${SERVER_URL}/characters_all`, { timeout: 5000 });
        const f = (r.data?.characters || []).find((x: any) => x.id === cid);
        if (f && !c) setCharName(f.name);
      } catch {}
      if (!startedRef.current) {
        startedRef.current = true;
        setTimeout(() => talk('game_start', 0, 'even', undefined, cid), 500);
      }
    })();
    return () => { c = true; };
  }, []));

  useEffect(() => () => { sndRef.current?.unloadAsync().catch(() => {}); }, []);

  const playB64 = async (b64: string, id: string): Promise<string | null> => {
    if (!b64 || b64.length < 100) return null;
    try {
      const dir = `${FileSystem.cacheDirectory}gomoku_audio/`;
      await FileSystem.makeDirectoryAsync(dir, { intermediates: true }).catch(() => {});
      const uri = `${dir}${id}.mp3`;
      await FileSystem.writeAsStringAsync(uri, b64, { encoding: FileSystem.EncodingType.Base64 });
      if (sndRef.current) try { await sndRef.current.unloadAsync(); } catch {}
      const { sound } = await Audio.Sound.createAsync({ uri }, { shouldPlay: true });
      sndRef.current = sound;
      return uri;
    } catch { return null; }
  };

  const replay = async (uri: string) => {
    try {
      if (sndRef.current) try { await sndRef.current.unloadAsync(); } catch {}
      const { sound } = await Audio.Sound.createAsync({ uri }, { shouldPlay: true });
      sndRef.current = sound;
    } catch {}
  };

  const addChar = async (data: any) => {
    if (!data?.jp) return;
    const id = `${Date.now()}_c_${Math.random().toString(36).slice(2, 5)}`;
    const au = await playB64(data.audio_b64 || '', id);
    setMsgs(p => [...p, { id, role: 'char', jp: data.jp, zh: data.zh, emotion: data.emotion, audioUri: au || undefined }]);
    setTimeout(() => scrollRef.current?.scrollToEnd({ animated: true }), 100);
  };

  // ★ 说话:独立于回合,随时可调
  const talk = async (event: string, mc: number, sit?: string, ut?: string, cid?: string) => {
    if (talkRef.current && event !== 'user_chat') return;
    talkRef.current = true;
    try {
      const r = await axios.post(`${SERVER_URL}/game/gomoku/talk`, {
        user_id: UID, character_id: cid || charId,
        event, move_count: mc, situation: sit || 'even', user_text: ut,
      }, { timeout: 20000 });
      if (r.data?.say) await addChar(r.data);
    } catch (e: any) { console.warn('[talk]', e?.message); }
    finally { talkRef.current = false; }
  };

  // 玩家落子
  const tap = (x: number, y: number) => {
    if (gameOver || turn !== 1 || aiRef.current) return;
    if (board[y][x] !== 0) return;
    const b = board.map(r => [...r]) as Cell[][];
    b[y][x] = 1;
    setBoard(b); setHistory(p => [...p, { x, y, color: 1 }]);
    const mc = history.length + 1;
    if (checkWin(b, x, y, 1)) {
      setGameOver(true); setWinner(1);
      talk('user_win', mc, 'losing');
      saveMem('user_win', mc);
      return;
    }
    const tc = classify(b, x, y, 1);
    if (tc === 'four') talk('user_attack_four', mc, evalSituation(b));
    else if (tc === 'three') talk('user_attack_three', mc, evalSituation(b));
    else if (Math.random() < 0.12) talk('user_normal', mc, evalSituation(b));
    setTurn(2);
  };

  // ★ AI 回合:算法(快) + 战术触发说话
  useEffect(() => {
    if (turn !== 2 || gameOver) return;
    aiRef.current = true;
    const t = setTimeout(async () => {
      try {
        // 走后端算法(50ms 级别)
        const r = await axios.post(`${SERVER_URL}/game/gomoku/move`, { board }, { timeout: 5000 });
        const { x, y } = r.data;
        const b = board.map(r2 => [...r2]) as Cell[][];
        b[y][x] = 2;
        setBoard(b); setHistory(p => [...p, { x, y, color: 2 }]);
        const mc = history.length + 1;
        if (checkWin(b, x, y, 2)) {
          setGameOver(true); setWinner(2);
          talk('ai_win', mc, 'winning');
          saveMem('ai_win', mc);
        } else {
          const tc = classify(b, x, y, 2);
          if (tc === 'four') talk('ai_attack_four', mc, evalSituation(b));
          else if (tc === 'three') talk('ai_attack_three', mc, evalSituation(b));
          else if (Math.random() < 0.20) talk('ai_normal', mc, evalSituation(b));
          setTurn(1);
        }
      } catch {
        setTurn(1); // 网络失败就跳过
      } finally { aiRef.current = false; }
    }, 400);
    return () => clearTimeout(t);
  }, [turn]);

  useEffect(() => {
    if (!gameOver || !winner) return;
    setTimeout(() => Alert.alert('对局结束', winner === 1 ? '你赢了 🎉' : `${charName} 赢了`, [
      { text: '再来', onPress: restart }, { text: '看棋盘', style: 'cancel' },
    ]), 500);
  }, [gameOver, winner]);

  const restart = () => {
    setBoard(newBoard()); setTurn(1); setGameOver(false); setWinner(0);
    setHistory([]); aiRef.current = false; setMsgs([]);
    setTimeout(() => talk('game_start', 0, 'even'), 300);
  };

  const undo = () => {
    if (history.length < 2 || gameOver) return;
    const nh = [...history]; const b = board.map(r => [...r]) as Cell[][];
    for (let i = 0; i < 2 && nh.length; i++) { const l = nh.pop()!; b[l.y][l.x] = 0; }
    setBoard(b); setHistory(nh); setTurn(1);
  };

  // ★ 聊天:随时可发,不受回合限制
  const send = async () => {
    if (sending) return;
    Keyboard.dismiss(); await sleep(80);
    const t = (inpRef.current || inp).trim();
    if (!t) return;
    setInp(''); inpRef.current = '';
    setMsgs(p => [...p, { id: `${Date.now()}_u`, role: 'user', text: t }]);
    setTimeout(() => scrollRef.current?.scrollToEnd({ animated: true }), 100);
    setSending(true);
    await talk('user_chat', history.length, evalSituation(board), t);
    setSending(false);
  };

  const saveMem = async (result: string, mc: number) => {
    try {
      const hl = msgs.filter(m => m.text || m.zh).slice(-6).map(m => ({
        role: m.role === 'user' ? 'user' : 'char',
        text: m.role === 'user' ? (m.text || '') : (m.zh || ''),
      }));
      await axios.post(`${SERVER_URL}/game/gomoku/save_memory`, {
        user_id: UID, character_id: charId, result, move_count: mc, chat_highlights: hl,
      }, { timeout: 15000 });
    } catch {}
  };

  const stars: [number, number][] = [[3, 3], [3, 11], [11, 3], [11, 11], [7, 7]];
  const last = history[history.length - 1];
  const st8 = gameOver ? (winner === 1 ? '你赢了 🎉' : `${charName} 赢了`) : (turn === 1 ? '你的回合(黑)' : `${charName} 在想...`);

  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
      <StatusBar barStyle="light-content" backgroundColor={C.card} />
      <View style={s.hdr}><TouchableOpacity onPress={() => router.back()} style={s.back}><Text style={s.backT}>‹</Text></TouchableOpacity><View style={{ flex: 1 }}><Text style={s.title}>五子棋 · vs {charName}</Text><Text style={s.sub}>{st8}</Text></View></View>
      <View style={s.score}><View style={s.pill}><View style={[s.dot, { backgroundColor: '#111' }]} /><Text style={s.pT}>你</Text></View><Text style={s.vs}>VS</Text><View style={s.pill}><View style={[s.dot, { backgroundColor: '#fafafa', borderWidth: 1, borderColor: '#555' }]} /><Text style={s.pT}>{charName}</Text></View></View>
      <View style={s.bw}>
        <View style={{ width: BW, height: BW, backgroundColor: '#e6c98f', borderRadius: 6, padding: BP }}>
          <View style={{ width: GW, height: GW }}>
            {Array.from({ length: SIZE }).map((_, i) => <View key={`h${i}`} style={{ position: 'absolute', left: 0, top: i * CS - 0.5, width: GW, height: 1, backgroundColor: '#666' }} />)}
            {Array.from({ length: SIZE }).map((_, i) => <View key={`v${i}`} style={{ position: 'absolute', left: i * CS - 0.5, top: 0, width: 1, height: GW, backgroundColor: '#666' }} />)}
            {stars.map(([sx, sy], i) => <View key={`s${i}`} style={{ position: 'absolute', left: sx * CS - 3, top: sy * CS - 3, width: 6, height: 6, borderRadius: 3, backgroundColor: '#666' }} />)}
            {board.map((row, y) => row.map((c, x) => c === 0 ? null : <View key={`p${x}_${y}`} pointerEvents="none" style={{ position: 'absolute', left: x * CS - SR, top: y * CS - SR, width: SR * 2, height: SR * 2, borderRadius: SR, backgroundColor: c === 1 ? '#111' : '#fafafa', borderWidth: c === 2 ? 1 : 0, borderColor: '#555', shadowColor: '#000', shadowOffset: { width: 1, height: 1 }, shadowOpacity: 0.3, shadowRadius: 1, elevation: 2 }} />))}
            {last && <View pointerEvents="none" style={{ position: 'absolute', left: last.x * CS - 3, top: last.y * CS - 3, width: 6, height: 6, borderRadius: 3, backgroundColor: '#e53935' }} />}
            {Array.from({ length: SIZE }).map((_, y) => Array.from({ length: SIZE }).map((_, x) => <TouchableOpacity key={`t${x}_${y}`} activeOpacity={0.4} onPress={() => tap(x, y)} style={{ position: 'absolute', left: x * CS - CS / 2, top: y * CS - CS / 2, width: CS, height: CS }} />))}
          </View>
        </View>
      </View>
      <View style={s.btns}><TouchableOpacity style={s.btn} onPress={undo} disabled={history.length < 2 || gameOver}><Text style={[s.btnT, (history.length < 2 || gameOver) && s.btnD]}>悔棋</Text></TouchableOpacity><TouchableOpacity style={[s.btn, s.btnP]} onPress={restart}><Text style={[s.btnT, { color: '#fff' }]}>重新开始</Text></TouchableOpacity></View>
      <View style={s.chat}>
        <ScrollView ref={scrollRef} style={{ flex: 1 }} contentContainerStyle={s.cc} onContentSizeChange={() => scrollRef.current?.scrollToEnd({ animated: true })}>
          {msgs.length === 0 && <Text style={s.empty}>{charName} 会在下棋时开口,你也可以随时跟 TA 说话</Text>}
          {msgs.map(m => m.role === 'user' ? (
            <View key={m.id} style={s.uw}><View style={s.ub}><Text style={s.ut}>{m.text}</Text></View></View>
          ) : (
            <View key={m.id} style={s.cw}><View style={s.cb}>
              <Text style={s.cj}>{m.jp}</Text>
              {m.zh ? <Text style={s.cz}>{m.zh}</Text> : null}
              {m.audioUri && <TouchableOpacity onPress={() => replay(m.audioUri!)} style={s.rp}><Text style={s.rt}>🔊 重播</Text></TouchableOpacity>}
            </View></View>
          ))}
        </ScrollView>
        <View style={[s.bar, { marginBottom: kbH }]}>
          <TextInput style={s.inp} value={inp} onChangeText={t => { setInp(t); inpRef.current = t; }}
            placeholder={sending ? '等 TA 回...' : `跟 ${charName} 说...`}
            placeholderTextColor={C.textMute} editable={!sending}
            onSubmitEditing={send} blurOnSubmit={false} returnKeyType="send" multiline />
          <TouchableOpacity style={[s.snd, { backgroundColor: (!sending && inp.trim()) ? C.accent : C.textMute + '55' }]}
            onPress={send} disabled={sending || !inp.trim()}>
            <Text style={s.sndT}>发送</Text>
          </TouchableOpacity>
        </View>
      </View>
    </View>
  );
}

const s = StyleSheet.create({
  hdr: { flexDirection: 'row', alignItems: 'center', paddingHorizontal: 12, paddingTop: Platform.OS === 'ios' ? 50 : 40, paddingBottom: 8, borderBottomWidth: 1, borderBottomColor: C.border, backgroundColor: C.card },
  back: { paddingHorizontal: 6, paddingVertical: 4 },
  backT: { color: C.text, fontSize: 30, lineHeight: 32, fontWeight: '300' },
  title: { color: C.text, fontSize: 16, fontWeight: '700' },
  sub: { color: C.textMute, fontSize: 11, marginTop: 2 },
  score: { flexDirection: 'row', alignItems: 'center', justifyContent: 'center', paddingVertical: 6, gap: 10 },
  pill: { flexDirection: 'row', alignItems: 'center', backgroundColor: C.card, paddingHorizontal: 10, paddingVertical: 4, borderRadius: 16, gap: 6 },
  dot: { width: 14, height: 14, borderRadius: 7 },
  pT: { color: C.text, fontSize: 13 },
  vs: { color: C.textMute, fontSize: 11 },
  bw: { alignItems: 'center', paddingVertical: 4 },
  btns: { flexDirection: 'row', paddingHorizontal: 16, gap: 10, marginVertical: 4 },
  btn: { flex: 1, paddingVertical: 8, borderRadius: 10, backgroundColor: C.card, alignItems: 'center', borderWidth: 1, borderColor: C.border },
  btnP: { backgroundColor: C.accent, borderColor: C.accent },
  btnT: { color: C.text, fontSize: 13, fontWeight: '500' },
  btnD: { color: C.textMute },
  chat: { flex: 1, borderTopWidth: 1, borderTopColor: C.border, backgroundColor: 'rgba(0,0,0,0.15)' },
  cc: { padding: 10, paddingBottom: 4, flexGrow: 1 },
  empty: { color: C.textMute, fontSize: 12, textAlign: 'center', marginTop: 12, fontStyle: 'italic' },
  uw: { alignItems: 'flex-end', marginBottom: 6 },
  ub: { backgroundColor: C.accent, borderRadius: 14, borderBottomRightRadius: 4, paddingHorizontal: 12, paddingVertical: 7, maxWidth: '80%' },
  ut: { color: '#fff', fontSize: 14, lineHeight: 20 },
  cw: { alignItems: 'flex-start', marginBottom: 6 },
  cb: { backgroundColor: C.card, borderRadius: 14, borderBottomLeftRadius: 4, paddingHorizontal: 12, paddingVertical: 7, maxWidth: '85%', borderWidth: 1, borderColor: C.border },
  cj: { color: C.text, fontSize: 14, lineHeight: 20 },
  cz: { color: C.textMute, fontSize: 12, marginTop: 3, fontStyle: 'italic' },
  rp: { marginTop: 5 },
  rt: { color: C.accent2, fontSize: 11 },
  bar: { flexDirection: 'row', alignItems: 'flex-end', paddingHorizontal: 10, paddingVertical: 6, borderTopWidth: 1, borderTopColor: C.border, backgroundColor: C.card, gap: 8 },
  inp: { flex: 1, backgroundColor: C.bg, borderWidth: 1, borderColor: C.border, borderRadius: 18, paddingHorizontal: 14, paddingVertical: 7, color: C.text, fontSize: 14, maxHeight: 80, minHeight: 34 },
  snd: { paddingHorizontal: 14, paddingVertical: 8, borderRadius: 18 },
  sndT: { color: '#fff', fontSize: 13, fontWeight: '600' },
});