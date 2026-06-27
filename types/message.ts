// types/message.ts
// 把 Message 类型抽到这里，单聊页、通用聊天页、VoiceCallModal 都从这里 import，
// 避免 VoiceCallModal 反向 import 聊天页造成循环依赖。
export interface Message {
  id: string;
  role: 'user' | 'gojo';
  text: string;
  subtitle?: string;
  time?: string;
  timestamp?: number;  // epoch ms，用于时间分隔条
  imageUri?: string;
  senderId?: string;
  senderName?: string;
}