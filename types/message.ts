// types/message.ts
// 把 Message 类型抽到这里，单聊页、通用聊天页、VoiceCallModal 都从这里 import，
// 避免 VoiceCallModal 反向 import 聊天页造成循环依赖。
export interface Message {
  id: string;
  role: 'user' | 'gojo';
  text: string;
  subtitle?: string;
  time?: string;
  imageUri?: string;
  // 群聊场景才会用到：标记这条消息是哪个角色说的（gojo / geto / ...）
  // 单聊里不用填，默认就是当前聊天页的对方
  senderId?: string;
  senderName?: string;
}