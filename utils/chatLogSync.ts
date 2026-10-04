import type { Message } from '../types/message';

export const CHATLOG_BATCH_SIZE = 100;

export type ChatLogReceiptStatus =
  | 'inserted'
  | 'already_identical'
  | 'metadata_enriched'
  | 'conflict'
  | 'deleted_rejected';

export interface ChatLogReceipt {
  id: string;
  status: ChatLogReceiptStatus;
  reference_status?: 'unavailable';
}

export interface ChatLogMessage {
  client_msg_id: string;
  [key: string]: unknown;
}

const RECEIPT_STATUSES = new Set<ChatLogReceiptStatus>([
  'inserted', 'already_identical', 'metadata_enriched', 'conflict', 'deleted_rejected',
]);

export function confirmedReceipt(receipt: ChatLogReceipt | ChatLogReceiptStatus): boolean {
  const status = typeof receipt === 'string' ? receipt : receipt.status;
  return (status === 'inserted' || status === 'already_identical'
    || status === 'metadata_enriched')
    && (typeof receipt === 'string' || receipt.reference_status !== 'unavailable');
}

export function replyTargetEventId(message: Message): string {
  const target = message.role === 'user'
    ? String(message.replyTo?.source_event_id || message.replyTo?.eventId
      || message.replyTo?.id || '')
    : String(message.replyToSourceEventId || '');
  return target.startsWith('srv_') ? '' : target;
}

export function quotedTimestamp(ts?: string): number | undefined {
  if (!ts) return undefined;
  const utcTs = /[Zz]|[+-]\d{2}:?\d{2}$/.test(ts) ? ts : `${ts}Z`;
  const value = new Date(utcTs).getTime();
  return Number.isFinite(value) ? value : undefined;
}

export function translationRepairIdentity(message: Message): {
  endpoint: 'chat_text' | 'chat_image' | 'delayed_reply';
  sourceEventId: string;
  eventId: string;
} | null {
  const eventId = String(message.eventId || message.id || '');
  const endpoints = [
    ['chat_reply:', 'chat_text'],
    ['image_reply:', 'chat_image'],
    ['delayed_reply:', 'delayed_reply'],
  ] as const;
  for (const [prefix, endpoint] of endpoints) {
    if (!eventId.startsWith(prefix)) continue;
    const lastColon = eventId.lastIndexOf(':');
    const sourceEventId = eventId.slice(prefix.length, lastColon);
    if (sourceEventId && /^\d+$/.test(eventId.slice(lastColon + 1))) {
      return { endpoint, sourceEventId, eventId };
    }
  }
  return null;
}

export function missingTranslation(message: Message): boolean {
  if (message.role !== 'gojo') return false;
  if (typeof message.translationMissing === 'boolean') return message.translationMissing;
  return !String(message.subtitle || '').trim()
    && /[\p{L}\p{N}]/u.test(String(message.text || ''));
}

export function buildQuotePreview(
  message: Message, characterName: string,
): NonNullable<Message['replyTo']> {
  const eventId = message.eventId || (message.id.startsWith('srv_') ? undefined : message.id);
  return {
    id: message.id,
    eventId,
    text: message.text || '',
    subtitle: message.subtitle,
    name: message.senderName || (message.role === 'user' ? '你' : characterName),
    role: message.role,
    timestamp: message.timestamp,
  };
}

/** Every item needs its own server answer; a 200 response alone confirms nothing. */
export async function submitChatLogBatches(
  messages: ChatLogMessage[],
  postBatch: (batch: ChatLogMessage[]) => Promise<{ results?: ChatLogReceipt[] }>,
  onBatch: (batch: ChatLogMessage[], receipts: ChatLogReceipt[]) => Promise<void> | void,
): Promise<void> {
  for (let start = 0; start < messages.length; start += CHATLOG_BATCH_SIZE) {
    const batch = messages.slice(start, start + CHATLOG_BATCH_SIZE);
    const response = await postBatch(batch);
    const receipts = response?.results;
    const expected = new Set(batch.map(message => message.client_msg_id));
    if (!Array.isArray(receipts) || receipts.length !== batch.length || expected.size !== batch.length) {
      throw new Error('chatlog_batch_receipts_incomplete');
    }
    const received = new Set<string>();
    for (const receipt of receipts) {
      if (!receipt || !expected.has(receipt.id) || received.has(receipt.id)
          || !RECEIPT_STATUSES.has(receipt.status)) {
        throw new Error('chatlog_batch_receipts_invalid');
      }
      received.add(receipt.id);
    }
    await onBatch(batch, receipts);
  }
}

/** Server rows win for acknowledged history. Only explicitly pending local rows are added. */
export function mergeServerAndPending<T extends { id: string; timestamp?: number }>(
  server: T[], pending: T[], deletedIds: Set<string>,
): T[] {
  const byId = new Map<string, T>();
  for (const message of server) {
    if (!deletedIds.has(message.id)) byId.set(message.id, message);
  }
  for (const message of pending) {
    if (!deletedIds.has(message.id) && !byId.has(message.id)) byId.set(message.id, message);
  }
  return [...byId.values()].sort((left, right) =>
    (left.timestamp || 0) - (right.timestamp || 0));
}
