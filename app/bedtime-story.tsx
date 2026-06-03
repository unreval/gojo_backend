// BedtimeStoryScreen.tsx —— 独立的「睡前故事」页面
// 调用后端 /story/generate，顺序播放每段音频，显示中文字幕。
// 不经过聊天、不写记忆。
import React, { useState, useRef, useCallback } from 'react';
import { View, Text, TouchableOpacity, ActivityIndicator, StyleSheet } from 'react-native';
import { Audio } from 'expo-av';
import axios from 'axios';

// ★ 改成你自己的后端地址（和聊天用的同一个）
const SERVER_URL = 'https://gojobackend-production-819d.up.railway.app';
const CHARACTER_ID = 'gojo';

type Segment = { jp: string; zh: string; audio_b64: string };

export default function BedtimeStoryScreen() {
  const [loading, setLoading] = useState(false);
  const [segments, setSegments] = useState<Segment[]>([]);
  const [index, setIndex] = useState(-1);
  const [playing, setPlaying] = useState(false);
  const soundRef = useRef<Audio.Sound | null>(null);
  const cancelRef = useRef(false);

  const unload = useCallback(async () => {
    if (soundRef.current) {
      try { await soundRef.current.unloadAsync(); } catch {}
      soundRef.current = null;
    }
  }, []);

  // 播放一段 base64 音频，等它放完再 resolve
  // ★ 如果你语音通话里已有验证过的 playAudio，建议直接换成那个，更稳
  const playOne = useCallback(async (b64: string) => {
    if (!b64 || b64.length < 100) return;
    try {
      await unload();
      const { sound } = await Audio.Sound.createAsync(
        { uri: 'data:audio/mp3;base64,' + b64 },
        { shouldPlay: true },
      );
      soundRef.current = sound;
      await new Promise<void>((resolve) => {
        sound.setOnPlaybackStatusUpdate((st: any) => {
          if (st.didJustFinish || st.error) resolve();
        });
      });
    } catch (e) {
      console.warn('playOne error', e);
    }
  }, [unload]);

  const playFrom = useCallback(async (segs: Segment[], start: number) => {
    cancelRef.current = false;
    setPlaying(true);
    for (let i = start; i < segs.length; i++) {
      if (cancelRef.current) break;
      setIndex(i);
      await playOne(segs[i].audio_b64);
    }
    await unload();
    setPlaying(false);
  }, [playOne, unload]);

  const start = useCallback(async () => {
    setLoading(true);
    setSegments([]);
    setIndex(-1);
    try {
      const res = await axios.post(`${SERVER_URL}/story/generate`, { character_id: CHARACTER_ID });
      const segs: Segment[] = Array.isArray(res.data?.segments) ? res.data.segments : [];
      setSegments(segs);
      setLoading(false);
      if (segs.length) playFrom(segs, 0);
    } catch (e) {
      console.warn('story error', e);
      setLoading(false);
    }
  }, [playFrom]);

  const stop = useCallback(async () => {
    cancelRef.current = true;
    await unload();
    setPlaying(false);
  }, [unload]);

  const current = index >= 0 && index < segments.length ? segments[index] : null;

  return (
    <View style={styles.container}>
      <Text style={styles.title}>🌙 睡前故事</Text>

      <View style={styles.subtitleBox}>
        {loading ? (
          <>
            <ActivityIndicator color="#cbb3ff" size="large" />
            <Text style={styles.hint}>悟在想故事…</Text>
          </>
        ) : current ? (
          <>
            <Text style={styles.zh}>{current.zh || '…'}</Text>
            <Text style={styles.jp}>{current.jp}</Text>
          </>
        ) : (
          <Text style={styles.hint}>点下面的按钮，让悟讲个故事哄你睡觉</Text>
        )}
      </View>

      {!playing ? (
        <TouchableOpacity style={styles.btn} onPress={start} disabled={loading}>
          <Text style={styles.btnText}>{loading ? '准备中…' : '开始讲故事'}</Text>
        </TouchableOpacity>
      ) : (
        <TouchableOpacity style={[styles.btn, styles.stopBtn]} onPress={stop}>
          <Text style={styles.btnText}>停止</Text>
        </TouchableOpacity>
      )}

      {segments.length > 0 && (
        <Text style={styles.progress}>{Math.max(index + 1, 0)} / {segments.length}</Text>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: '#0e0b1a', alignItems: 'center', justifyContent: 'center', padding: 24 },
  title: { color: '#e8dcff', fontSize: 22, fontWeight: '600', marginBottom: 32 },
  subtitleBox: { minHeight: 170, justifyContent: 'center', alignItems: 'center', paddingHorizontal: 12 },
  zh: { color: '#f3ecff', fontSize: 20, lineHeight: 30, textAlign: 'center', marginBottom: 10 },
  jp: { color: '#9a86c4', fontSize: 14, textAlign: 'center' },
  hint: { color: '#7d7399', fontSize: 15, textAlign: 'center', lineHeight: 24, marginTop: 12 },
  btn: { backgroundColor: '#6c4fd6', paddingVertical: 14, paddingHorizontal: 40, borderRadius: 28, marginTop: 40 },
  stopBtn: { backgroundColor: '#4a4060' },
  btnText: { color: '#fff', fontSize: 16, fontWeight: '600' },
  progress: { color: '#6b6385', fontSize: 13, marginTop: 18 },
});
