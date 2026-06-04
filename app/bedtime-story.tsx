// app/bedtime-story.tsx —— 独立的「睡前故事」页面（两段式）
// 第一步：/story/generate 秒回故事文字
// 第二步：每段播之前才调 /story/tts 合成语音，并提前预取下一段，播放不卡顿
// 不经过聊天、不写记忆。
import axios from 'axios';
import { Audio } from 'expo-av';
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ActivityIndicator, StyleSheet, Text, TouchableOpacity, View } from 'react-native';
// ★ 和聊天用同一个后端地址
import { SERVER_URL } from '../constants/theme';

const CHARACTER_ID = 'gojo';

type Segment = { jp: string; zh: string };

export default function BedtimeStoryScreen() {
  const [loading, setLoading] = useState(false);   // 第一步：生成文字中
  const [segments, setSegments] = useState<Segment[]>([]);
  const [index, setIndex] = useState(-1);
  const [playing, setPlaying] = useState(false);

  const emotionRef = useRef('平静');
  const audioCacheRef = useRef<Record<number, string>>({});  // 每段语音缓存（按需合成）
  const soundRef = useRef<Audio.Sound | null>(null);
  const cancelRef = useRef(false);

  useEffect(() => {
    return () => { soundRef.current?.unloadAsync().catch(() => {}); };
  }, []);

  const unload = useCallback(async () => {
    if (soundRef.current) {
      try { await soundRef.current.unloadAsync(); } catch {}
      soundRef.current = null;
    }
  }, []);

  // 取第 i 段的语音（已取过就用缓存；没取过就向 /story/tts 要）
  const fetchAudio = useCallback(async (i: number, segs: Segment[]): Promise<string> => {
    if (audioCacheRef.current[i] !== undefined) return audioCacheRef.current[i];
    try {
      const res = await axios.post(
        `${SERVER_URL}/story/tts`,
        { text: segs[i].jp, emotion: emotionRef.current, character_id: CHARACTER_ID },
        { timeout: 30000 },
      );
      const b64 = res.data?.audio_b64 || '';
      audioCacheRef.current[i] = b64;
      return b64;
    } catch (e) {
      console.warn('story tts error', e);
      audioCacheRef.current[i] = '';
      return '';
    }
  }, []);

  // 播一段 base64 音频，放完再继续
  const playOne = useCallback(async (b64: string) => {
    if (!b64 || b64.length < 100) return;
    try {
      await unload();
      const { sound } = await Audio.Sound.createAsync(
        { uri: 'data:audio/mp3;base64,' + b64 },
        { shouldPlay: true, volume: 1.0 },
      );
      soundRef.current = sound;
      await new Promise<void>((resolve) => {
        sound.setOnPlaybackStatusUpdate((st: any) => {
          if (st.isLoaded && (st.didJustFinish || st.error)) resolve();
        });
      });
    } catch (e) {
      console.warn('playOne error', e);
    }
  }, [unload]);

  const playFrom = useCallback(async (segs: Segment[], startIdx: number) => {
    cancelRef.current = false;
    setPlaying(true);
    for (let i = startIdx; i < segs.length; i++) {
      if (cancelRef.current) break;
      setIndex(i);
      const b64 = await fetchAudio(i, segs);          // 确保当前段语音就绪
      if (cancelRef.current) break;
      if (i + 1 < segs.length) fetchAudio(i + 1, segs); // 一边播当前段，一边预取下一段（不等待）
      await playOne(b64);
    }
    await unload();
    setPlaying(false);
  }, [fetchAudio, playOne, unload]);

  const start = useCallback(async () => {
    setLoading(true);
    setSegments([]);
    setIndex(-1);
    audioCacheRef.current = {};
    try {
      const res = await axios.post(
        `${SERVER_URL}/story/generate`,
        { character_id: CHARACTER_ID },
        { timeout: 60000 },
      );
      const segs: Segment[] = Array.isArray(res.data?.segments) ? res.data.segments : [];
      emotionRef.current = res.data?.emotion || '平静';
      setSegments(segs);
      setLoading(false);
      if (segs.length) playFrom(segs, 0);
    } catch (e) {
      console.warn('story generate error', e);
      setLoading(false);
    }
  }, [playFrom]);

  const stop = useCallback(async () => {
    cancelRef.current = true;
    await unload();
    setPlaying(false);
  }, [unload]);

  // 字幕：当前段没有中文（被切出来的后半句）时，往前找最近一句有中文的，保持字幕稳定
  const subtitleZh = (() => {
    for (let i = index; i >= 0; i--) {
      if (segments[i]?.zh) return segments[i].zh;
    }
    return '';
  })();
  const currentJp = index >= 0 && index < segments.length ? segments[index].jp : '';

  return (
    <View style={styles.container}>
      <Text style={styles.title}>🌙 睡前故事</Text>

      <View style={styles.subtitleBox}>
        {loading ? (
          <>
            <ActivityIndicator color="#cbb3ff" size="large" />
            <Text style={styles.hint}>悟在想故事…</Text>
          </>
        ) : index >= 0 ? (
          <>
            <Text style={styles.zh}>{subtitleZh || '…'}</Text>
            <Text style={styles.jp}>{currentJp}</Text>
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