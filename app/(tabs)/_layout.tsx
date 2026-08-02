// app/(tabs)/_layout.tsx
// ★ tab bar 底部适配三键/手势条:用 useSafeAreaInsets 拿真实高度,
//   不再 hardcode(旧版本 Android 分支写死 8px,三键 48px 完全罩不住)
import { Tabs } from 'expo-router';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { C } from '../../constants/theme';

export default function TabLayout() {
  const insets = useSafeAreaInsets();
  const bottomInset = insets.bottom;   // 三键机 ~48, 手势机 ~20, 无按键 0

  return (
    <Tabs screenOptions={{
      headerShown: false,
      tabBarStyle: {
        backgroundColor: C.card,
        borderTopColor: C.border,
        borderTopWidth: 1,
        // 底部空间 = 安全区 + 常规内边距;height 也把安全区加进去,不然内容会被裁掉
        paddingBottom: bottomInset + 8,
        paddingTop: 8,
        height: 60 + bottomInset,
      },
      tabBarActiveTintColor: C.accent2,
      tabBarInactiveTintColor: C.textMute,
      tabBarLabelStyle: { fontSize: 10 },
    }}>
      <Tabs.Screen name="index"      options={{ title:'首页', tabBarIcon:()=>null, tabBarLabel:'🏠 首页' }}/>
      <Tabs.Screen name="chat"       options={{ title:'聊天', tabBarIcon:()=>null, tabBarLabel:'💬 聊天' }}/>
      <Tabs.Screen name="calendar"   options={{ title:'日程', tabBarIcon:()=>null, tabBarLabel:'📅 日程' }}/>
      <Tabs.Screen name="accounting" options={{ title:'记账', tabBarIcon:()=>null, tabBarLabel:'💰 记账' }}/>
      <Tabs.Screen name="memory"     options={{ title:'记忆', tabBarIcon:()=>null, tabBarLabel:'🧠 记忆' }}/>
      <Tabs.Screen name="settings"   options={{ title:'设置', tabBarIcon:()=>null, tabBarLabel:'⚙️ 设置' }}/>
    </Tabs>
  );
}