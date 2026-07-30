// components/PendingTransactionCard.tsx
// 聊天里检测到消费/收入时弹的确认卡片。
// 账户、分类、金额、描述都可改。用户点"记账"才真正落库。
import axios from 'axios';
import React, { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  StyleSheet,
  Text,
  TextInput,
  TouchableOpacity,
  View,
} from 'react-native';
import { C, CATEGORIES, SERVER_URL } from '../constants/theme';

export interface PendingTransaction {
  type: 'in' | 'out';
  category: string;
  amount: number;
  desc: string;
  account_hint: string;
  date: string | null;      // YYYY-MM-DD
  time: string | null;      // HH:MM or null
}

export interface Account {
  id: number;
  name: string;
  icon: string;
  balance: number;
}

type Status = 'pending' | 'saved' | 'dismissed';

interface Props {
  userId: string;
  transaction: PendingTransaction;
  accounts: Account[];
  initialStatus?: Status;
  onStatusChange?: (status: Status) => void;
  onSaved?: (recordId: number) => void;
}

export default function PendingTransactionCard({
  userId, transaction, accounts, initialStatus = 'pending',
  onStatusChange, onSaved,
}: Props) {
  const [status, setStatus] = useState<Status>(initialStatus);
  const [busy, setBusy] = useState(false);

  // 可编辑字段
  const [amount, setAmount] = useState(String(transaction.amount));
  const [desc, setDesc] = useState(transaction.desc);
  const [category, setCategory] = useState(transaction.category);
  const [accountId, setAccountId] = useState<number | null>(null);

  // 首次挂载:根据 account_hint 猜账户,猜不到就用第一个
  useEffect(() => {
    if (accounts.length === 0) return;
    const hint = (transaction.account_hint || '').trim();
    let matched: Account | undefined;
    if (hint) {
      matched = accounts.find(a => a.name === hint)
             || accounts.find(a => a.name.includes(hint) || hint.includes(a.name));
    }
    setAccountId((matched || accounts[0]).id);
  }, [accounts, transaction.account_hint]);

  const selectedAccount = useMemo(
    () => accounts.find(a => a.id === accountId),
    [accounts, accountId]
  );

  const setStatusInternal = (s: Status) => {
    setStatus(s);
    onStatusChange?.(s);
  };

  const onSave = async () => {
    if (busy || status !== 'pending') return;
    const amt = parseFloat(amount);
    if (!amt || amt <= 0) return Alert.alert('金额有问题', '请输入大于 0 的金额');
    if (!desc.trim()) return Alert.alert('描述不能为空', '简单写一下就行');
    if (!accountId) return Alert.alert('还没选账户', '选一个账户再记');

    setBusy(true);
    try {
      const res = await axios.post(`${SERVER_URL}/accounting/records`, {
        user_id: userId,
        account_id: accountId,
        type: transaction.type,
        category,
        desc: desc.trim(),
        amount: amt,
        date: transaction.date,
        time: transaction.time,
      });
      const newId = res.data?.id;
      setStatusInternal('saved');
      onSaved?.(newId);
    } catch (e: any) {
      Alert.alert('记账失败', e?.response?.data?.error || e?.message || '未知错误');
    } finally {
      setBusy(false);
    }
  };

  const onDismiss = () => {
    if (busy || status !== 'pending') return;
    setStatusInternal('dismissed');
  };

  const dateStr = transaction.date || '今天';
  const timeStr = transaction.time || '';
  const dateTimeStr = timeStr ? `${dateStr} ${timeStr}` : dateStr;

  // ── 已保存/已忽略 状态 ─────────────
  if (status === 'saved') {
    return (
      <View style={[s.card, s.cardDone]}>
        <Text style={s.doneText}>
          ✓ 已记账 · {transaction.type === 'in' ? '+' : '-'}¥{parseFloat(amount).toFixed(2)}
          {selectedAccount ? `  ${selectedAccount.name}` : ''}
        </Text>
      </View>
    );
  }
  if (status === 'dismissed') {
    return (
      <View style={[s.card, s.cardDone]}>
        <Text style={[s.doneText, { color: C.textMute }]}>已忽略</Text>
      </View>
    );
  }

  // ── 待确认 ─────────────
  return (
    <View style={s.card}>
      <View style={s.headerRow}>
        <Text style={s.title}>💰 检测到一笔{transaction.type === 'in' ? '收入' : '支出'}</Text>
      </View>

      {/* 金额 + 描述 */}
      <View style={s.mainRow}>
        <Text style={[s.sign, { color: transaction.type === 'in' ? C.income : C.expense }]}>
          {transaction.type === 'in' ? '+' : '-'}
        </Text>
        <Text style={s.currency}>¥</Text>
        <TextInput
          style={s.amountInput}
          value={amount}
          onChangeText={setAmount}
          keyboardType="decimal-pad"
          placeholder="金额"
          placeholderTextColor={C.textMute}
        />
      </View>

      <TextInput
        style={s.descInput}
        value={desc}
        onChangeText={setDesc}
        placeholder="描述"
        placeholderTextColor={C.textMute}
      />

      {/* 分类 */}
      <Text style={s.label}>分类</Text>
      <View style={s.chipRow}>
        {CATEGORIES.map(c => (
          <TouchableOpacity
            key={c}
            style={[s.chip, category === c && s.chipActive]}
            onPress={() => setCategory(c)}
          >
            <Text style={[s.chipText, category === c && s.chipTextActive]}>{c}</Text>
          </TouchableOpacity>
        ))}
      </View>

      {/* 账户 —— ★ 关键:可改 */}
      <Text style={s.label}>账户</Text>
      {accounts.length === 0 ? (
        <Text style={s.emptyHint}>你还没建账户,先去记账页建一个</Text>
      ) : (
        <View style={s.chipRow}>
          {accounts.map(a => (
            <TouchableOpacity
              key={a.id}
              style={[s.chip, accountId === a.id && s.chipActive]}
              onPress={() => setAccountId(a.id)}
            >
              <Text style={[s.chipText, accountId === a.id && s.chipTextActive]}>
                {a.icon} {a.name}
              </Text>
            </TouchableOpacity>
          ))}
        </View>
      )}

      {/* 日期时间 */}
      <Text style={s.dateHint}>📅 {dateTimeStr}</Text>

      {/* 按钮 */}
      <View style={s.btnRow}>
        <TouchableOpacity style={s.dismissBtn} onPress={onDismiss} disabled={busy}>
          <Text style={s.dismissText}>忽略</Text>
        </TouchableOpacity>
        <TouchableOpacity
          style={[s.saveBtn, (busy || accounts.length === 0) && { opacity: 0.5 }]}
          onPress={onSave}
          disabled={busy || accounts.length === 0}
        >
          <Text style={s.saveText}>{busy ? '记账中...' : '记账'}</Text>
        </TouchableOpacity>
      </View>
    </View>
  );
}

const s = StyleSheet.create({
  card: {
    backgroundColor: C.card,
    borderRadius: 16,
    padding: 14,
    marginVertical: 6,
    borderWidth: 1,
    borderColor: C.accent + '55',
    borderLeftWidth: 3,
    borderLeftColor: C.accent,
  },
  cardDone: {
    padding: 12,
    borderLeftColor: C.income,
    borderColor: C.border,
  },
  doneText: { color: C.text, fontSize: 13, fontWeight: '600' },
  headerRow: { marginBottom: 10 },
  title: { color: C.text, fontSize: 14, fontWeight: '600' },
  mainRow: { flexDirection: 'row', alignItems: 'center', marginBottom: 8 },
  sign: { fontSize: 22, fontWeight: '700', marginRight: 2 },
  currency: { color: C.textDim, fontSize: 18, marginRight: 4 },
  amountInput: {
    flex: 1, color: C.text, fontSize: 22, fontWeight: '700',
    paddingVertical: 4, borderBottomWidth: 1, borderBottomColor: C.border,
  },
  descInput: {
    backgroundColor: C.bg, borderRadius: 10,
    paddingHorizontal: 12, paddingVertical: 8,
    color: C.text, fontSize: 14, borderWidth: 1, borderColor: C.border,
    marginBottom: 10,
  },
  label: { color: C.textMute, fontSize: 11, letterSpacing: 1, marginBottom: 6, marginTop: 6 },
  chipRow: { flexDirection: 'row', flexWrap: 'wrap', gap: 6 },
  chip: {
    borderRadius: 14, paddingHorizontal: 10, paddingVertical: 5,
    borderWidth: 1, borderColor: C.border, backgroundColor: C.bg,
  },
  chipActive: { backgroundColor: C.accent + '33', borderColor: C.accent },
  chipText: { color: C.textDim, fontSize: 12 },
  chipTextActive: { color: C.accent2, fontWeight: '600' },
  emptyHint: { color: C.textMute, fontSize: 12, fontStyle: 'italic' },
  dateHint: { color: C.textDim, fontSize: 11, marginTop: 10, marginBottom: 12 },
  btnRow: { flexDirection: 'row', gap: 10 },
  dismissBtn: {
    flex: 1, paddingVertical: 10, borderRadius: 10,
    borderWidth: 1, borderColor: C.border, alignItems: 'center',
  },
  dismissText: { color: C.textDim, fontSize: 13, fontWeight: '600' },
  saveBtn: {
    flex: 2, paddingVertical: 10, borderRadius: 10,
    backgroundColor: C.accent, alignItems: 'center',
  },
  saveText: { color: '#fff', fontSize: 13, fontWeight: '700' },
});
