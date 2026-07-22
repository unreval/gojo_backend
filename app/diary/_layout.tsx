// app/diary/_layout.tsx
// 去掉 expo-router 默认白色 header（日记页自己画顶部）。
import { Stack } from 'expo-router';
import React from 'react';

export default function DiaryLayout() {
  return <Stack screenOptions={{ headerShown: false }} />;
}