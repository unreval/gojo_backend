// app/(tabs)/_layout.tsx
import { Tabs } from 'expo-router';
import { Platform } from 'react-native';
import { C } from '../../constants/theme';

export default function TabLayout() {
  return (
    <Tabs screenOptions={{
      headerShown: false,
      tabBarStyle: {
        backgroundColor: C.card,
        borderTopColor: C.border,
        borderTopWidth: 1,
        paddingBottom: Platform.OS === 'ios' ? 24 : 8,
        paddingTop: 8,
        height: Platform.OS === 'ios' ? 84 : 60,
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
    </Tabs>
  );
}