// 用户表情包面板。列表在进聊天页时拉一次，签名 URL 默认 6 小时，缓存不超过这个时长。
import axios from 'axios';
import React from 'react';
import {
  Image,
  ScrollView,
  StyleSheet,
  Text,
  TouchableOpacity,
  View,
} from 'react-native';
import { C, SERVER_URL } from '../constants/theme';

export type UserSticker = {
  id: string;
  desc: string;
  emotion: string;
  url: string;
};

const STICKER_LIST_TTL_MS = 6 * 60 * 60 * 1000;

let cache: { at: number; items: UserSticker[] } | null = null;
let inflight: Promise<UserSticker[]> | null = null;

function normalize(data: unknown): UserSticker[] {
  if (!Array.isArray(data)) return [];
  const items: UserSticker[] = [];
  for (const raw of data) {
    if (!raw || typeof raw !== 'object') continue;
    const row = raw as Record<string, unknown>;
    const id = String(row.id || '').trim();
    const url = String(row.url || '').trim();
    if (!id || !url) continue;
    items.push({
      id,
      desc: String(row.desc || ''),
      emotion: String(row.emotion || '').trim() || '其他',
      url,
    });
  }
  return items;
}

export async function loadUserStickers(force = false): Promise<UserSticker[]> {
  const fresh = !!(cache && Date.now() - cache.at < STICKER_LIST_TTL_MS);
  if (!force && fresh && cache) return cache.items;
  if (!force && inflight) return inflight;
  const run = (async () => {
    try {
      const res = await axios.get(`${SERVER_URL}/sticker/list`, { timeout: 8000 });
      const items = normalize(res.data);
      cache = { at: Date.now(), items };
      return items;
    } catch (e: any) {
      console.warn('[sticker] list failed', e?.message);
      return cache?.items || [];
    } finally {
      inflight = null;
    }
  })();
  inflight = run;
  return run;
}

export function groupStickers(items: UserSticker[]): { emotion: string; items: UserSticker[] }[] {
  const groups: { emotion: string; items: UserSticker[] }[] = [];
  const index: Record<string, number> = {};
  for (const item of items) {
    const emotion = item.emotion || '其他';
    if (index[emotion] == null) {
      index[emotion] = groups.length;
      groups.push({ emotion, items: [] });
    }
    groups[index[emotion]].items.push(item);
  }
  return groups;
}

export default function StickerPicker({
  items,
  onPick,
  onClose,
  onImageError,
}: {
  items: UserSticker[];
  onPick: (item: UserSticker) => void;
  onClose: () => void;
  onImageError?: () => void;
}) {
  const groups = groupStickers(items);
  return (
    <View style={s.panel}>
      <View style={s.bar}>
        <Text style={s.title}>表情</Text>
        <TouchableOpacity onPress={onClose} hitSlop={{ top: 8, bottom: 8, left: 8, right: 8 }}>
          <Text style={s.close}>关闭</Text>
        </TouchableOpacity>
      </View>
      <ScrollView style={s.scroll} contentContainerStyle={s.content}>
        {groups.map(group => (
          <View key={group.emotion} style={s.group}>
            <Text style={s.emotion}>{group.emotion}</Text>
            <View style={s.grid}>
              {group.items.map(item => (
                <TouchableOpacity
                  key={item.id}
                  style={s.cell}
                  onPress={() => onPick(item)}
                  accessibilityLabel={item.desc || item.id}
                >
                  <Image
                    source={{ uri: item.url }}
                    style={s.thumb}
                    resizeMode="contain"
                    onError={() => onImageError?.()}
                  />
                </TouchableOpacity>
              ))}
            </View>
          </View>
        ))}
      </ScrollView>
    </View>
  );
}

const s = StyleSheet.create({
  panel: {
    backgroundColor: C.card,
    borderTopWidth: 1,
    borderTopColor: C.border,
    maxHeight: 280,
  },
  bar: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: 14,
    paddingTop: 8,
    paddingBottom: 4,
  },
  title: { color: C.textMute, fontSize: 12 },
  close: { color: C.accent2, fontSize: 13 },
  scroll: { flexGrow: 0 },
  content: { paddingHorizontal: 8, paddingBottom: 10 },
  group: { marginTop: 4 },
  emotion: { color: C.textDim, fontSize: 12, marginLeft: 6, marginBottom: 2 },
  grid: { flexDirection: 'row', flexWrap: 'wrap' },
  cell: { width: '25%', padding: 6, alignItems: 'center' },
  thumb: { width: 72, height: 72 },
});
