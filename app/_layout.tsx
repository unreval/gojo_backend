import { DarkTheme, DefaultTheme, ThemeProvider } from '@react-navigation/native';
import axios from 'axios';
import Constants from 'expo-constants';
import * as Device from 'expo-device';
import * as Notifications from 'expo-notifications';
import { Stack } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { useEffect } from 'react';
import { Platform } from 'react-native';
import 'react-native-reanimated';

import { useColorScheme } from '@/hooks/use-color-scheme';
import { SERVER_URL } from '../constants/theme';

export const unstable_settings = {
  anchor: '(tabs)',
};

const FIXED_USER_ID = 'user_mofpiyd7442ia7';

// 收到推送时怎么显示（前台也弹）
Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowAlert: true,
    shouldPlaySound: true,
    shouldSetBadge: false,
  }),
});

// ★ 注册推送：拿到这台手机的 push token，发给后端存起来
async function registerForPush() {
  try {
    if (!Device.isDevice) return; // 模拟器不支持推送

    // Android 建个通知渠道（不然通知不响/不弹）
    if (Platform.OS === 'android') {
      await Notifications.setNotificationChannelAsync('default', {
        name: '五条悟的消息',
        importance: Notifications.AndroidImportance.MAX,
        vibrationPattern: [0, 250, 250, 250],
        sound: 'default',
      });
    }

    // 要权限
    const { status: existing } = await Notifications.getPermissionsAsync();
    let finalStatus = existing;
    if (existing !== 'granted') {
      const { status } = await Notifications.requestPermissionsAsync();
      finalStatus = status;
    }
    if (finalStatus !== 'granted') {
      console.warn('[push] 用户没给通知权限');
      return;
    }

    // 拿 Expo push token（需要 projectId）
    const projectId =
      Constants?.expoConfig?.extra?.eas?.projectId ??
      Constants?.easConfig?.projectId;
    const tokenResp = await Notifications.getExpoPushTokenAsync(
      projectId ? { projectId } : undefined
    );
    const token = tokenResp.data;
    console.log('[push] token:', token);

    // 发给后端
    await axios.post(`${SERVER_URL}/push/register`, {
      user_id: FIXED_USER_ID,
      token,
    });
    console.log('[push] 已注册到后端');
  } catch (e: any) {
    console.warn('[push] 注册失败:', e?.message);
  }
}

export default function RootLayout() {
  const colorScheme = useColorScheme();

  useEffect(() => {
    registerForPush();
  }, []);

  return (
    <ThemeProvider value={colorScheme === 'dark' ? DarkTheme : DefaultTheme}>
      <Stack>
        <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
        <Stack.Screen name="chat" options={{ headerShown: false }} />
        <Stack.Screen name="diary" options={{ headerShown: false }} />
        <Stack.Screen name="modal" options={{ presentation: 'modal', title: 'Modal' }} />
        <Stack.Screen name="bedtime-story" options={{ title: '睡前故事' }} />
      </Stack>
      <StatusBar style="auto" />
    </ThemeProvider>
  );
}