// constants/theme.ts — 共享颜色和常量
export const C = {
  bg:        '#070d1a',
  card:      '#0d1a2e',
  card2:     '#0f2040',
  border:    '#1a3a5c',
  accent:    '#3b82f6',
  accent2:   '#60a5fa',
  accentDim: '#1d4ed8',
  text:      '#e8f4ff',
  textDim:   '#7ba8d0',
  textMute:  '#3d6080',
  userBubble:'#1d4ed8',
  income:    '#22c55e',
  expense:   '#ef4444',
};

export const EMOTION_COLORS: Record<string,string> = {
  平静:'#4a90a4', 自信:'#c9a84c', 嘲讽:'#8e6b9e',
  开心:'#3b82f6', 激动:'#e05c5c', 温柔:'#5ba88a',
  认真:'#2563eb', 疑惑:'#7c8fa6', 调皮:'#3b82f6',
  悲伤:'#3a5f7a', 愤怒:'#c0392b',
};

export const EMOTION_LABELS: Record<string,string> = {
  平静:'😐', 自信:'😏', 嘲讽:'🙄', 开心:'😄', 激动:'🔥',
  温柔:'🌸', 认真:'😤', 疑惑:'🤔', 调皮:'😝', 悲伤:'😔', 愤怒:'😠',
};

export const TAG_COLORS: Record<string,string> = {
  约定:'#3b82f6', 学习:'#8b5cf6', 运动:'#22c55e', 工作:'#f59e0b', 其他:'#6b7280',
};

export const TAGS = Object.keys(TAG_COLORS);
export const CATEGORIES = ['餐饮','购物','交通','娱乐','学习','医疗','收入','其他'];
export const WEEKDAYS = ['日','一','二','三','四','五','六'];
export const MONTHS = ['一月','二月','三月','四月','五月','六月','七月','八月','九月','十月','十一月','十二月'];

export const SERVER_URL = 'https://gojobackend-production-819d.up.railway.app';

export function uid() { return Math.random().toString(36).slice(2); }
export function nowTime() {
  const d = new Date();
  return `${d.getHours().toString().padStart(2,'0')}:${d.getMinutes().toString().padStart(2,'0')}`;
}
export function dateKey(y:number, m:number, d:number) {
  return `${y}-${String(m+1).padStart(2,'0')}-${String(d).padStart(2,'0')}`;
}
export function todayStr() {
  const d = new Date();
  return `${d.getMonth()+1}月${d.getDate()}日`;
}