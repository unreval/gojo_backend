import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildQuotePreview, confirmedReceipt, mergeServerAndPending,
  missingTranslation, quotedTimestamp, replyTargetEventId, submitChatLogBatches,
  translationRepairIdentity,
} from '../utils/chatLogSync.ts';

const messages = (count) => Array.from({ length: count }, (_, index) => ({
  client_msg_id: `message-${index}`,
  text: `原文 ${index}`,
}));

test('500 messages are sent in five complete batches', async () => {
  const sent = [];
  const confirmed = new Set();
  await submitChatLogBatches(messages(500), async batch => {
    sent.push(batch.map(message => message.client_msg_id));
    return { results: batch.map(message => ({
      id: message.client_msg_id, status: 'inserted',
    })) };
  }, (_batch, receipts) => receipts.forEach(receipt => confirmed.add(receipt.id)));
  assert.deepEqual(sent.map(batch => batch.length), [100, 100, 100, 100, 100]);
  assert.equal(confirmed.size, 500);
  assert.equal(new Set(sent.flat()).size, 500);
});

test('failure after a confirmed batch retries only unconfirmed messages', async () => {
  const all = messages(500);
  const pending = new Set(all.map(message => message.client_msg_id));
  let request = 0;
  await assert.rejects(submitChatLogBatches(all, async batch => {
    request += 1;
    if (request === 2) throw new Error('offline');
    return { results: batch.map(message => ({
      id: message.client_msg_id, status: 'inserted',
    })) };
  }, (_batch, receipts) => receipts.forEach(receipt => pending.delete(receipt.id))), /offline/);
  assert.equal(pending.size, 400);
  const retried = [];
  await submitChatLogBatches(all.filter(message => pending.has(message.client_msg_id)), async batch => {
    retried.push(batch.length);
    return { results: batch.map(message => ({
      id: message.client_msg_id, status: 'already_identical',
    })) };
  }, (_batch, receipts) => receipts.forEach(receipt => pending.delete(receipt.id)));
  assert.deepEqual(retried, [100, 100, 100, 100]);
  assert.equal(pending.size, 0);
});

test('an incomplete 200 response confirms no items in that batch', async () => {
  let callbacks = 0;
  await assert.rejects(submitChatLogBatches(messages(2), async () => ({
    results: [{ id: 'message-0', status: 'inserted' }],
  }), () => { callbacks += 1; }), /receipts_incomplete/);
  assert.equal(callbacks, 0);
});

test('server history merges only pending local records and never deleted ones', () => {
  const server = [{ id: 'acknowledged', timestamp: 1, text: 'server' }];
  const pending = [
    { id: 'new', timestamp: 3, text: 'local pending' },
    { id: 'deleted', timestamp: 2, text: 'stale local' },
    { id: 'acknowledged', timestamp: 1, text: 'stale cache' },
  ];
  assert.deepEqual(mergeServerAndPending(server, pending, new Set(['deleted'])), [
    server[0], pending[0],
  ]);
});

test('quote preview keeps source identity, original, translation, speaker and time', () => {
  const quoted = buildQuotePreview({
    id: 'assistant-event', eventId: 'assistant-event', role: 'gojo',
    text: '僕が言った', subtitle: '我说过', timestamp: 123456789,
  }, '五条悟');
  assert.deepEqual(quoted, {
    id: 'assistant-event', eventId: 'assistant-event',
    text: '僕が言った', subtitle: '我说过',
    name: '五条悟', role: 'gojo', timestamp: 123456789,
  });
  assert.equal(replyTargetEventId({
    id: 'new-user-event', role: 'user', text: '你说的啊', replyTo: quoted,
  }), 'assistant-event');
  assert.equal(replyTargetEventId({
    id: 'new-assistant-event', role: 'gojo', text: 'うん',
    replyTo: quoted, replyToSourceEventId: 'new-user-event',
  }), 'new-user-event');
  assert.equal(replyTargetEventId({
    id: 'new-user-event', role: 'user', text: '你说的啊',
    replyTo: { id: 'srv_12', text: 'old unknown event' },
  }), '');
});

test('conflict and tombstone receipts never count as confirmed', () => {
  assert.equal(confirmedReceipt('inserted'), true);
  assert.equal(confirmedReceipt('already_identical'), true);
  assert.equal(confirmedReceipt('metadata_enriched'), true);
  assert.equal(confirmedReceipt('conflict'), false);
  assert.equal(confirmedReceipt('deleted_rejected'), false);
  assert.equal(confirmedReceipt({ id: 'u1', status: 'inserted',
    reference_status: 'unavailable' }), false);
  assert.equal(confirmedReceipt({ id: 'u1', status: 'metadata_enriched' }), true);
});

test('reenter uses canonical quote source even when display ID is synthetic', () => {
  assert.equal(replyTargetEventId({
    id: 'new-user', role: 'user', text: '你说的啊',
    replyTo: { id: 'srv_19', source_event_id: 'old-gojo',
      text: '原话', ts: '2026-10-04T08:00:00Z' },
  }), 'old-gojo');
  assert.equal(quotedTimestamp('2026-10-04T08:00:00'),
    Date.parse('2026-10-04T08:00:00Z'));
  assert.equal(quotedTimestamp('2026-10-04T16:00:00+08:00'),
    Date.parse('2026-10-04T08:00:00Z'));
});

test('translation repair identifies one bubble without regenerating its turn', () => {
  assert.deepEqual(translationRepairIdentity({
    id: 'chat_reply:source-1:0', role: 'gojo', text: 'おはよう',
  }), { endpoint: 'chat_text', sourceEventId: 'source-1',
    eventId: 'chat_reply:source-1:0' });
  assert.deepEqual(translationRepairIdentity({
    id: 'delayed_reply:42:1', role: 'gojo', text: 'おはよう',
  }), { endpoint: 'delayed_reply', sourceEventId: '42',
    eventId: 'delayed_reply:42:1' });
  assert.equal(missingTranslation({ id: 'one', role: 'gojo', text: 'おはよう' }), true);
  assert.equal(missingTranslation({ id: 'emoji', role: 'gojo', text: '🥺' }), false);
  assert.equal(missingTranslation({ id: 'done', role: 'gojo', text: 'おはよう',
    subtitle: '早上好' }), false);
});
