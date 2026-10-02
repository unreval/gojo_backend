import assert from 'node:assert/strict';
import test from 'node:test';
import { mergeReadReceipts, unreadSourceEventIds } from '../utils/readReceipts.ts';

const unread = () => ['A', 'B', 'C'].map(id => ({
  id, sourceEventId: id, role: 'user', text: id,
}));
const receipt = (id, at) => ({
  source_event_id: id, seen_at: at,
  seen_via: 'phone_check', phone_check_id: 1,
});

test('only exact source events gain readAt', () => {
  const first = mergeReadReceipts(unread(), [receipt('A', '2026-09-18T10:00:00Z')]);
  assert.deepEqual(first.map(m => Boolean(m.readAt)), [true, false, false]);
  const second = mergeReadReceipts(first, [receipt('B', '2026-09-18T10:05:00Z')]);
  assert.deepEqual(second.map(m => Boolean(m.readAt)), [true, true, false]);
  assert.equal(second[0].readAt, first[0].readAt);
  assert.deepEqual(unreadSourceEventIds(second), ['C']);
});

test('unrelated proactive bubbles cannot imply a user read receipt', () => {
  const messages = [...unread(), {
    id: 'proactive:life_share:9', role: 'gojo', text: '甘いものを食べた',
  }];
  const merged = mergeReadReceipts(messages, []);
  assert.deepEqual(merged.slice(0, 3).map(m => m.readAt),
                   [undefined, undefined, undefined]);
});

test('query batch includes at most the latest 100 unread source IDs', () => {
  const messages = Array.from({ length: 101 }, (_, i) => ({
    id: `event-${i}`, role: 'user', text: '',
  }));
  const ids = unreadSourceEventIds(messages);
  assert.equal(ids.length, 100);
  assert.equal(ids[0], 'event-1');
  assert.equal(ids.at(-1), 'event-100');
});
