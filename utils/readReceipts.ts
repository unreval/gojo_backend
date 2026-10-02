import type { Message } from '../types/message';

export interface ReadReceipt {
  source_event_id: string;
  seen_at: string;
  seen_via: 'immediate' | 'phone_check';
  phone_check_id: number | null;
}

export function unreadSourceEventIds(messages: Message[], limit = 100): string[] {
  const ids = messages
    .filter(m => m.role === 'user' && !m.readAt)
    .map(m => String(m.sourceEventId || m.id || '').trim())
    .filter(Boolean);
  return [...new Set(ids)].slice(-limit);
}

export function mergeReadReceipts(messages: Message[], receipts: ReadReceipt[]): Message[] {
  const seenAtById = new Map<string, number>();
  for (const receipt of receipts) {
    const seenAt = Date.parse(receipt.seen_at);
    if (receipt.source_event_id && Number.isFinite(seenAt)) {
      seenAtById.set(receipt.source_event_id, seenAt);
    }
  }
  return messages.map(m => {
    if (m.role !== 'user' || m.readAt) return m;
    const sourceEventId = m.sourceEventId || m.id;
    const readAt = seenAtById.get(sourceEventId);
    return readAt === undefined ? m : { ...m, readAt };
  });
}
