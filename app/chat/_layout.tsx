// app/chat/_layout.tsx
// 关掉默认 stack header，避免聊天页顶部出现"chat/[id]"那一栏。
// 每个聊天页内部自己有标题栏（返回键 + 角色名 + 清空 等）。
import { Stack } from 'expo-router';

export default function ChatStackLayout() {
  return <Stack screenOptions={{ headerShown: false }} />;
}