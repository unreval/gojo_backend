import { DarkTheme, DefaultTheme, ThemeProvider } from '@react-navigation/native';
import axios from 'axios';
import Constants from 'expo-constants';
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

Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowAlert: true,
    shouldPlaySound: true,
    shouldSetBadge: false,
  }),
});

// 把排查信息发回后端，这样看 Zeabur 日志就知道推送注册走到哪一步、为什么停
async function pushDebug(step: string) {
  try {
    await axios.post(`${SERVER_URL}/push/debug`, { user_id: FIXED_USER_ID, step });
  } catch {}
}

async function registerForPush() {
  await pushDebug('开始注册');
  try {
    // 建渠道单独包起来：就算失败也不能挡住后面"要权限"那步
    if (Platform.OS === 'android') {
      try {
        await Notifications.setNotificationChannelAsync('default', {
          name: '五条悟的消息',
          importance: Notifications.AndroidImportance.MAX,
          vibrationPattern: [0, 250, 250, 250],
          sound: 'default',
        });
        await pushDebug('渠道已建');
      } catch (ce: any) {
        await pushDebug('建渠道失败(不影响):' + (ce?.message || String(ce)).slice(0, 80));
      }
    }

    await pushDebug('准备要权限');
    const { status: existing } = await Notifications.getPermissionsAsync();
    let finalStatus = existing;
    await pushDebug('当前权限=' + existing);
    if (existing !== 'granted') {
      const { status } = await Notifications.requestPermissionsAsync();
      finalStatus = status;
      await pushDebug('请求后权限=' + status);
    }
    if (finalStatus !== 'granted') {
      await pushDebug('没给权限，停止');
      return;
    }

    const projectId =
      (Constants as any)?.expoConfig?.extra?.eas?.projectId ??
      (Constants as any)?.easConfig?.projectId;
    await pushDebug('projectId=' + String(projectId));

    const tokenResp = await Notifications.getExpoPushTokenAsync(
      projectId ? { projectId } : undefined
    );
    const token = tokenResp.data;
    await pushDebug('拿到token=' + token.slice(0, 30));

    await axios.post(`${SERVER_URL}/push/register`, {
      user_id: FIXED_USER_ID,
      token,
    });
    await pushDebug('注册成功');
  } catch (e: any) {
    const msg = (e?.message || String(e));
    await pushDebug('出错A:' + msg.slice(0, 150));
    await pushDebug('出错B:' + msg.slice(150, 300));
    await pushDebug('出错码:' + String(e?.code || 'none'));
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