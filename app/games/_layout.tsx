// app/games/_layout.tsx
// 和 chat/_layout.tsx、diary/_layout.tsx 一样:关掉默认 stack header,页面自己画顶部。
import { Stack } from 'expo-router';

export default function GamesLayout() {
  return <Stack screenOptions={{ headerShown: false }} />;
}