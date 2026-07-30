// app/(tabs)/accounting.tsx — 记账页(账户系统 + 后端持久化 + 转账 + 五条悟短评)
import AsyncStorage from '@react-native-async-storage/async-storage';
import axios from 'axios';
import { useFocusEffect } from 'expo-router';
import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  ActivityIndicator, Alert, KeyboardAvoidingView, Modal, Platform,
  Pressable, ScrollView, StatusBar, StyleSheet, Text, TextInput,
  TouchableOpacity, View,
} from 'react-native';
import ChibiSprite from '../../components/ChibiSprite';
import { C, CATEGORIES, SERVER_URL } from '../../constants/theme';

const USER_ID_KEY  = 'gojo_user_id';
const INSIGHT_CACHE_KEY = 'gojo_accounting_insight_v1';
const INSIGHT_TTL_MS    = 30 * 60 * 1000;   // 30 分钟内不重复请求

// ══════════════════════════════════════════════
//  类型
// ══════════════════════════════════════════════
interface Account {
  id: number;
  name: string;
  icon: string;
  initial_balance: number;
  balance: number;
  total_income: number;
  total_expense: number;
  sort_order: number;
}
interface Record {
  id: number;
  account_id: number;
  account_name: string;
  account_icon: string;
  type: 'in' | 'out';
  category: string;
  desc: string;
  amount: number;
  date: string | null;
  time: string | null;
  is_transfer: boolean;
  transfer_id: string | null;
}

const todayISO = () => new Date().toISOString().slice(0, 10);
const nowHHMM  = () => {
  const d = new Date();
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
};

// ══════════════════════════════════════════════
export default function AccountingScreen() {
  const [userId, setUserId] = useState<string>('default');
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [records, setRecords]   = useState<Record[]>([]);
  const [filterAccountId, setFilterAccountId] = useState<number | null>(null); // null = 全部
  const [loading, setLoading] = useState(true);

  // 五条悟短评
  const [insight, setInsight] = useState<{ zh: string; jp: string } | null>(null);
  const [insightLoading, setInsightLoading] = useState(false);

  // 各种 modal
  const [showAddModal, setShowAddModal]           = useState(false);
  const [showAccountsModal, setShowAccountsModal] = useState(false);
  const [showFirstRunHint, setShowFirstRunHint]   = useState(false);

  // ── 拉取用户 ID ──
  useEffect(() => {
    AsyncStorage.getItem(USER_ID_KEY).then(v => { if (v) setUserId(v); });
  }, []);

  // ── 每次进 tab 刷新数据 + 五条悟短评 ──
  useFocusEffect(useCallback(() => {
    (async () => {
      await loadData(userId);
      loadInsight(userId).catch(() => {});
    })();
  }, [userId]));

  const loadData = async (uid: string) => {
    setLoading(true);
    try {
      const [aRes, rRes] = await Promise.all([
        axios.get(`${SERVER_URL}/accounts?user_id=${uid}`),
        axios.get(`${SERVER_URL}/accounting/records?user_id=${uid}`),
      ]);
      const accs = aRes.data?.accounts || [];
      setAccounts(accs);
      setRecords(rRes.data?.records || []);
      if (accs.length === 0) setShowFirstRunHint(true);
    } catch (e: any) {
      console.warn('load accounting data:', e?.message);
    } finally {
      setLoading(false);
    }
  };

  const loadInsight = async (uid: string) => {
    // 先看缓存
    try {
      const raw = await AsyncStorage.getItem(INSIGHT_CACHE_KEY);
      if (raw) {
        const c = JSON.parse(raw);
        if (c.ts && Date.now() - c.ts < INSIGHT_TTL_MS && c.userId === uid) {
          setInsight({ zh: c.zh, jp: c.jp });
          return;
        }
      }
    } catch {}

    setInsightLoading(true);
    try {
      const res = await axios.get(`${SERVER_URL}/accounting/insights?user_id=${uid}`);
      const data = res.data;
      if (data?.zh) {
        setInsight({ zh: data.zh, jp: data.jp || '' });
        await AsyncStorage.setItem(INSIGHT_CACHE_KEY, JSON.stringify({
          zh: data.zh, jp: data.jp, ts: Date.now(), userId: uid,
        }));
      }
    } catch {} finally { setInsightLoading(false); }
  };

  const refreshInsight = async () => {
    await AsyncStorage.removeItem(INSIGHT_CACHE_KEY);
    await loadInsight(userId);
  };

  // ── 派生数据 ──
  const filteredRecords = useMemo(() => {
    if (filterAccountId === null) return records;
    return records.filter(r => r.account_id === filterAccountId);
  }, [records, filterAccountId]);

  const totalBalance = accounts.reduce((s, a) => s + a.balance, 0);

  // 分类支出(排除转账)
  const catStats: { [k: string]: number } = {};
  filteredRecords
    .filter(r => r.type === 'out' && !r.is_transfer)
    .forEach(r => { catStats[r.category] = (catStats[r.category] || 0) + r.amount; });
  const catList = Object.entries(catStats).sort((a, b) => b[1] - a[1]);
  const catTotal = Object.values(catStats).reduce((s, v) => s + v, 0);

  // 收支合计
  const totalIn  = filteredRecords.filter(r => r.type === 'in'  && !r.is_transfer).reduce((s, r) => s + r.amount, 0);
  const totalOut = filteredRecords.filter(r => r.type === 'out' && !r.is_transfer).reduce((s, r) => s + r.amount, 0);

  // ── 删除记录 ──
  const onDeleteRecord = (r: Record) => {
    Alert.alert('删除记录', `确认删除「${r.desc}」?`, [
      { text: '取消', style: 'cancel' },
      { text: '删除', style: 'destructive', onPress: async () => {
        try {
          await axios.delete(`${SERVER_URL}/accounting/records/${r.id}`);
          await loadData(userId);
        } catch { Alert.alert('删除失败'); }
      }},
    ]);
  };

  // ══════════════════════════════════════════════
  //  渲染
  // ══════════════════════════════════════════════
  if (loading) {
    return (
      <View style={{ flex: 1, backgroundColor: C.bg, alignItems: 'center', justifyContent: 'center' }}>
        <ActivityIndicator color={C.accent} />
      </View>
    );
  }

  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
      <StatusBar barStyle="light-content" backgroundColor={C.bg} />

      <ScrollView contentContainerStyle={s.content} showsVerticalScrollIndicator={false}>
        <View style={s.headerRow}>
          <Text style={s.pageTitle}>💰 记账本</Text>
          <TouchableOpacity onPress={() => setShowAccountsModal(true)} style={s.manageBtn}>
            <Text style={s.manageBtnText}>账户管理</Text>
          </TouchableOpacity>
        </View>

        {/* 五条悟短评 */}
        <TouchableOpacity onPress={refreshInsight} activeOpacity={0.7} style={s.insightBox}>
          <ChibiSprite pose="peek" size={44} />
          <View style={{ flex: 1, marginLeft: 10 }}>
            {insightLoading ? (
              <Text style={s.insightText}>...</Text>
            ) : insight ? (
              <>
                <Text style={s.insightText}>{insight.zh}</Text>
                {!!insight.jp && <Text style={s.insightJp}>{insight.jp}</Text>}
              </>
            ) : (
              <Text style={[s.insightText, { color: C.textMute }]}>(点一下让他说点什么)</Text>
            )}
          </View>
        </TouchableOpacity>

        {/* 余额卡 + 账户切换 */}
        <View style={s.balanceCard}>
          <Text style={s.balanceLabel}>
            {filterAccountId === null ? '全部账户余额' : accounts.find(a => a.id === filterAccountId)?.name || ''}
          </Text>
          <Text style={[s.balanceAmount, { color: totalBalance >= 0 ? C.income : C.expense }]}>
            ¥{(filterAccountId === null
                ? totalBalance
                : (accounts.find(a => a.id === filterAccountId)?.balance ?? 0)
              ).toFixed(2)}
          </Text>
          <View style={s.balanceRow}>
            <View style={s.balanceItem}>
              <Text style={s.balanceItemLabel}>收入</Text>
              <Text style={[s.balanceItemVal, { color: C.income }]}>+¥{totalIn.toFixed(0)}</Text>
            </View>
            <View style={s.balanceDivider} />
            <View style={s.balanceItem}>
              <Text style={s.balanceItemLabel}>支出</Text>
              <Text style={[s.balanceItemVal, { color: C.expense }]}>-¥{totalOut.toFixed(0)}</Text>
            </View>
          </View>
        </View>

        {/* 账户 pills */}
        <ScrollView horizontal showsHorizontalScrollIndicator={false} style={s.pillsRow} contentContainerStyle={s.pillsInner}>
          <TouchableOpacity
            style={[s.pill, filterAccountId === null && s.pillActive]}
            onPress={() => setFilterAccountId(null)}
          >
            <Text style={[s.pillText, filterAccountId === null && s.pillTextActive]}>全部</Text>
          </TouchableOpacity>
          {accounts.map(a => (
            <TouchableOpacity
              key={a.id}
              style={[s.pill, filterAccountId === a.id && s.pillActive]}
              onPress={() => setFilterAccountId(a.id)}
            >
              <Text style={[s.pillText, filterAccountId === a.id && s.pillTextActive]}>
                {a.icon} {a.name}
              </Text>
              <Text style={s.pillBalance}>¥{a.balance.toFixed(0)}</Text>
            </TouchableOpacity>
          ))}
          <TouchableOpacity style={[s.pill, s.pillAdd]} onPress={() => setShowAccountsModal(true)}>
            <Text style={s.pillAddText}>＋</Text>
          </TouchableOpacity>
        </ScrollView>

        {/* 首次没账户的提示 */}
        {accounts.length === 0 && showFirstRunHint && (
          <View style={s.emptyStateCard}>
            <Text style={s.emptyStateTitle}>还没有账户</Text>
            <Text style={s.emptyStateText}>先建一个账户,聊天记账才能开始工作</Text>
            <TouchableOpacity style={s.emptyStateBtn} onPress={() => setShowAccountsModal(true)}>
              <Text style={s.emptyStateBtnText}>建第一个账户</Text>
            </TouchableOpacity>
          </View>
        )}

        {/* 分类支出 */}
        {catList.length > 0 && (
          <>
            <Text style={s.sectionLabel}>支出分类</Text>
            <View style={s.catCard}>
              {catList.map(([cat, amt]) => (
                <View key={cat} style={s.catRow}>
                  <Text style={s.catLabel}>{cat}</Text>
                  <View style={s.catBarWrap}>
                    <View style={[s.catBar, { width: `${Math.min((amt / catTotal) * 100, 100)}%` as any }]} />
                  </View>
                  <Text style={s.catAmt}>¥{amt.toFixed(0)}</Text>
                </View>
              ))}
            </View>
          </>
        )}

        {/* 明细 */}
        <View style={s.recordHeader}>
          <Text style={s.sectionLabel}>明细记录</Text>
          <TouchableOpacity style={s.addMiniBtn} onPress={() => setShowAddModal(true)}
            disabled={accounts.length === 0}>
            <Text style={[s.addMiniBtnText, accounts.length === 0 && { opacity: 0.4 }]}>＋ 添加</Text>
          </TouchableOpacity>
        </View>
        {filteredRecords.length === 0 && (
          <Text style={s.emptyText}>还没有记录{accounts.length === 0 ? '' : ',快来记一笔吧～'}</Text>
        )}
        {filteredRecords.map(r => (
          <TouchableOpacity key={r.id} style={s.recordRow} onLongPress={() => onDeleteRecord(r)}>
            <View style={[s.recordIcon, {
              backgroundColor: r.is_transfer ? C.accent + '22'
                : (r.type === 'in' ? C.income + '22' : C.expense + '22')
            }]}>
              <Text style={{ fontSize: 16 }}>
                {r.is_transfer ? '🔄' : r.type === 'in' ? '📥' : '📤'}
              </Text>
            </View>
            <View style={{ flex: 1 }}>
              <Text style={s.recordDesc}>{r.desc}</Text>
              <Text style={s.recordCat}>
                {r.category} · {r.account_name}
                {r.date ? ` · ${r.date}` : ''}
                {r.time ? ` ${r.time}` : ''}
              </Text>
            </View>
            <Text style={[s.recordAmt, {
              color: r.is_transfer ? C.accent2 : (r.type === 'in' ? C.income : C.expense)
            }]}>
              {r.type === 'in' ? '+' : '-'}¥{r.amount.toFixed(2)}
            </Text>
          </TouchableOpacity>
        ))}
      </ScrollView>

      {/* ═══ 添加记录 modal ═══ */}
      <AddRecordModal
        visible={showAddModal}
        onClose={() => setShowAddModal(false)}
        accounts={accounts}
        userId={userId}
        defaultAccountId={filterAccountId ?? accounts[0]?.id ?? null}
        onSaved={() => { setShowAddModal(false); loadData(userId); }}
      />

      {/* ═══ 账户管理 modal ═══ */}
      <AccountsManageModal
        visible={showAccountsModal}
        onClose={() => setShowAccountsModal(false)}
        accounts={accounts}
        userId={userId}
        onChanged={() => loadData(userId)}
      />
    </View>
  );
}

// ══════════════════════════════════════════════
//  添加记录/收入/转账 modal
// ══════════════════════════════════════════════
type AddMode = 'out' | 'in' | 'transfer';
function AddRecordModal({
  visible, onClose, accounts, userId, defaultAccountId, onSaved,
}: {
  visible: boolean; onClose: () => void;
  accounts: Account[]; userId: string;
  defaultAccountId: number | null; onSaved: () => void;
}) {
  const [mode, setMode] = useState<AddMode>('out');
  const [amount, setAmount] = useState('');
  const [desc, setDesc]     = useState('');
  const [category, setCategory] = useState<string>('餐饮');
  const [accountId, setAccountId] = useState<number | null>(defaultAccountId);
  const [toAccountId, setToAccountId] = useState<number | null>(null); // 转账目标
  const [date, setDate] = useState(todayISO());
  const [time, setTime] = useState(nowHHMM());
  const [busy, setBusy] = useState(false);

  // 每次打开重置
  useEffect(() => {
    if (visible) {
      setMode('out');
      setAmount('');
      setDesc('');
      setCategory('餐饮');
      setAccountId(defaultAccountId ?? accounts[0]?.id ?? null);
      const other = accounts.find(a => a.id !== (defaultAccountId ?? accounts[0]?.id));
      setToAccountId(other?.id ?? null);
      setDate(todayISO());
      setTime(nowHHMM());
    }
  }, [visible, defaultAccountId, accounts]);

  const canSubmit = (() => {
    const amt = parseFloat(amount);
    if (!amt || amt <= 0) return false;
    if (mode === 'transfer') {
      return !!accountId && !!toAccountId && accountId !== toAccountId;
    }
    return !!accountId && !!desc.trim();
  })();

  const onSubmit = async () => {
    if (!canSubmit || busy) return;
    const amt = parseFloat(amount);
    setBusy(true);
    try {
      if (mode === 'transfer') {
        await axios.post(`${SERVER_URL}/accounting/transfer`, {
          user_id: userId,
          from_account_id: accountId,
          to_account_id: toAccountId,
          amount: amt,
          desc: desc.trim() || '转账',
          date, time,
        });
      } else {
        await axios.post(`${SERVER_URL}/accounting/records`, {
          user_id: userId,
          account_id: accountId,
          type: mode,
          category: mode === 'in' ? (category === '收入' ? '收入' : category) : category,
          desc: desc.trim(),
          amount: amt,
          date, time,
        });
      }
      onSaved();
    } catch (e: any) {
      Alert.alert('保存失败', e?.response?.data?.error || e?.message || '未知错误');
    } finally { setBusy(false); }
  };

  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose}>
      <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined} style={s.overlay}>
        <Pressable style={{ flex: 1 }} onPress={onClose} />
        <View style={s.modalBox}>
          <Text style={s.modalTitle}>添加记录</Text>

          {/* 支出/收入/转账 切换 */}
          <View style={s.modeSwitch}>
            {([
              { key: 'out',      label: '支出', color: C.expense },
              { key: 'in',       label: '收入', color: C.income  },
              { key: 'transfer', label: '转账', color: C.accent  },
            ] as const).map(m => (
              <TouchableOpacity key={m.key}
                style={[s.modeBtn, mode === m.key && { backgroundColor: m.color }]}
                onPress={() => setMode(m.key)}>
                <Text style={[s.modeText, mode === m.key && { color: '#fff' }]}>{m.label}</Text>
              </TouchableOpacity>
            ))}
          </View>

          <TextInput style={s.modalInput}
            placeholder="金额" placeholderTextColor={C.textMute}
            value={amount} onChangeText={setAmount}
            keyboardType="decimal-pad" />

          {mode !== 'transfer' && (
            <TextInput style={s.modalInput}
              placeholder="描述(如:奶茶)" placeholderTextColor={C.textMute}
              value={desc} onChangeText={setDesc} />
          )}

          {mode !== 'transfer' && mode === 'out' && (
            <>
              <Text style={s.modalLabel}>分类</Text>
              <View style={s.chipRow}>
                {CATEGORIES.filter(c => c !== '收入').map(c => (
                  <TouchableOpacity key={c}
                    style={[s.chip, category === c && s.chipActive]}
                    onPress={() => setCategory(c)}>
                    <Text style={[s.chipText, category === c && s.chipTextActive]}>{c}</Text>
                  </TouchableOpacity>
                ))}
              </View>
            </>
          )}

          <Text style={s.modalLabel}>{mode === 'transfer' ? '转出账户' : '账户'}</Text>
          <View style={s.chipRow}>
            {accounts.map(a => (
              <TouchableOpacity key={a.id}
                style={[s.chip, accountId === a.id && s.chipActive]}
                onPress={() => setAccountId(a.id)}>
                <Text style={[s.chipText, accountId === a.id && s.chipTextActive]}>
                  {a.icon} {a.name}
                </Text>
              </TouchableOpacity>
            ))}
          </View>

          {mode === 'transfer' && (
            <>
              <Text style={s.modalLabel}>转入账户</Text>
              <View style={s.chipRow}>
                {accounts.filter(a => a.id !== accountId).map(a => (
                  <TouchableOpacity key={a.id}
                    style={[s.chip, toAccountId === a.id && s.chipActive]}
                    onPress={() => setToAccountId(a.id)}>
                    <Text style={[s.chipText, toAccountId === a.id && s.chipTextActive]}>
                      {a.icon} {a.name}
                    </Text>
                  </TouchableOpacity>
                ))}
              </View>
              <TextInput style={s.modalInput}
                placeholder="备注(可选)" placeholderTextColor={C.textMute}
                value={desc} onChangeText={setDesc} />
            </>
          )}

          {/* 日期时间 */}
          <View style={s.dateTimeRow}>
            <View style={{ flex: 2 }}>
              <Text style={s.modalLabel}>日期</Text>
              <TextInput style={s.modalInput} value={date} onChangeText={setDate}
                placeholder="YYYY-MM-DD" placeholderTextColor={C.textMute} />
            </View>
            <View style={{ flex: 1 }}>
              <Text style={s.modalLabel}>时间</Text>
              <TextInput style={s.modalInput} value={time} onChangeText={setTime}
                placeholder="HH:MM" placeholderTextColor={C.textMute} />
            </View>
          </View>

          <View style={s.btnRow}>
            <TouchableOpacity style={s.cancelBtn} onPress={onClose}>
              <Text style={s.cancelText}>取消</Text>
            </TouchableOpacity>
            <TouchableOpacity
              style={[s.confirmBtn, (!canSubmit || busy) && { opacity: 0.4 }]}
              disabled={!canSubmit || busy}
              onPress={onSubmit}>
              <Text style={s.confirmText}>{busy ? '保存中...' : '确定'}</Text>
            </TouchableOpacity>
          </View>
        </View>
      </KeyboardAvoidingView>
    </Modal>
  );
}

// ══════════════════════════════════════════════
//  账户管理 modal
// ══════════════════════════════════════════════
function AccountsManageModal({
  visible, onClose, accounts, userId, onChanged,
}: {
  visible: boolean; onClose: () => void;
  accounts: Account[]; userId: string; onChanged: () => void;
}) {
  const [name, setName] = useState('');
  const [balance, setBalance] = useState('');
  const [icon, setIcon] = useState('💰');
  const [busy, setBusy] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);

  const iconOptions = ['💰','💵','💳','🏦','📱','🪙','💼','💸'];

  useEffect(() => {
    if (visible) {
      setName(''); setBalance(''); setIcon('💰'); setEditingId(null);
    }
  }, [visible]);

  const startEdit = (a: Account) => {
    setEditingId(a.id);
    setName(a.name);
    setBalance(String(a.initial_balance));
    setIcon(a.icon);
  };

  const onSubmit = async () => {
    if (!name.trim() || busy) return;
    const bal = parseFloat(balance || '0');
    setBusy(true);
    try {
      if (editingId) {
        await axios.put(`${SERVER_URL}/accounts/${editingId}`, {
          name: name.trim(), initial_balance: bal, icon,
        });
      } else {
        await axios.post(`${SERVER_URL}/accounts`, {
          user_id: userId, name: name.trim(), initial_balance: bal, icon,
        });
      }
      setName(''); setBalance(''); setIcon('💰'); setEditingId(null);
      onChanged();
    } catch (e: any) {
      Alert.alert('保存失败', e?.message || '');
    } finally { setBusy(false); }
  };

  const onDelete = (a: Account) => {
    Alert.alert('删除账户', `删除「${a.name}」会同时删掉这个账户下的所有记录。确定?`, [
      { text: '取消', style: 'cancel' },
      { text: '删除', style: 'destructive', onPress: async () => {
        try {
          await axios.delete(`${SERVER_URL}/accounts/${a.id}`);
          onChanged();
        } catch { Alert.alert('删除失败'); }
      }},
    ]);
  };

  return (
    <Modal visible={visible} transparent animationType="slide" onRequestClose={onClose}>
      <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined} style={s.overlay}>
        <Pressable style={{ flex: 1 }} onPress={onClose} />
        <View style={[s.modalBox, { maxHeight: '85%' }]}>
          <Text style={s.modalTitle}>{editingId ? '编辑账户' : '添加账户'}</Text>

          <ScrollView>
            <TextInput style={s.modalInput}
              placeholder="账户名(如:现金、支付宝、银行卡)"
              placeholderTextColor={C.textMute}
              value={name} onChangeText={setName} />
            <TextInput style={s.modalInput}
              placeholder={editingId ? '初始余额' : '当前余额(填多少钱)'}
              placeholderTextColor={C.textMute}
              value={balance} onChangeText={setBalance}
              keyboardType="decimal-pad" />

            <Text style={s.modalLabel}>图标</Text>
            <View style={s.chipRow}>
              {iconOptions.map(ic => (
                <TouchableOpacity key={ic}
                  style={[s.iconChip, icon === ic && s.chipActive]}
                  onPress={() => setIcon(ic)}>
                  <Text style={{ fontSize: 18 }}>{ic}</Text>
                </TouchableOpacity>
              ))}
            </View>

            <View style={s.btnRow}>
              {editingId && (
                <TouchableOpacity style={s.cancelBtn}
                  onPress={() => { setEditingId(null); setName(''); setBalance(''); }}>
                  <Text style={s.cancelText}>取消编辑</Text>
                </TouchableOpacity>
              )}
              <TouchableOpacity
                style={[s.confirmBtn, (!name.trim() || busy) && { opacity: 0.4 }]}
                disabled={!name.trim() || busy}
                onPress={onSubmit}>
                <Text style={s.confirmText}>
                  {busy ? '保存中...' : (editingId ? '保存修改' : '添加账户')}
                </Text>
              </TouchableOpacity>
            </View>

            {/* 已有账户列表 */}
            {accounts.length > 0 && (
              <>
                <Text style={[s.modalLabel, { marginTop: 20 }]}>已有账户</Text>
                {accounts.map(a => (
                  <View key={a.id} style={s.acctRow}>
                    <TouchableOpacity style={{ flex: 1, flexDirection: 'row', alignItems: 'center' }}
                      onPress={() => startEdit(a)}>
                      <Text style={{ fontSize: 20, marginRight: 10 }}>{a.icon}</Text>
                      <View style={{ flex: 1 }}>
                        <Text style={s.acctName}>{a.name}</Text>
                        <Text style={s.acctBalance}>余额 ¥{a.balance.toFixed(2)}</Text>
                      </View>
                    </TouchableOpacity>
                    <TouchableOpacity onPress={() => onDelete(a)} style={s.acctDelBtn}>
                      <Text style={s.acctDelText}>×</Text>
                    </TouchableOpacity>
                  </View>
                ))}
              </>
            )}
          </ScrollView>

          <TouchableOpacity onPress={onClose} style={{ marginTop: 12, alignItems: 'center' }}>
            <Text style={{ color: C.textDim }}>关闭</Text>
          </TouchableOpacity>
        </View>
      </KeyboardAvoidingView>
    </Modal>
  );
}

// ══════════════════════════════════════════════
//  样式
// ══════════════════════════════════════════════
const s = StyleSheet.create({
  content:        { padding: 20, paddingTop: Platform.OS === 'ios' ? 60 : 50, paddingBottom: 40 },
  headerRow:      { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 },
  pageTitle:      { color: C.text, fontSize: 22, fontWeight: '700' },
  manageBtn:      { paddingHorizontal: 12, paddingVertical: 6, borderRadius: 10, borderWidth: 1, borderColor: C.border },
  manageBtnText:  { color: C.textDim, fontSize: 12 },

  insightBox: {
    flexDirection: 'row', alignItems: 'center',
    backgroundColor: C.card, borderRadius: 14, padding: 10,
    marginBottom: 16, borderWidth: 1, borderColor: C.border,
    borderLeftWidth: 2, borderLeftColor: C.accent,
  },
  insightText: { color: C.text, fontSize: 13, lineHeight: 18 },
  insightJp:   { color: C.textMute, fontSize: 11, marginTop: 2, fontStyle: 'italic' },

  balanceCard:    { backgroundColor: C.card2, borderRadius: 20, padding: 20, marginBottom: 14, borderWidth: 1, borderColor: C.border },
  balanceLabel:   { color: C.textDim, fontSize: 12, marginBottom: 8 },
  balanceAmount:  { fontSize: 32, fontWeight: '700', marginBottom: 16 },
  balanceRow:     { flexDirection: 'row', alignItems: 'center' },
  balanceItem:    { flex: 1, alignItems: 'center' },
  balanceItemLabel:{ color: C.textMute, fontSize: 11, marginBottom: 4 },
  balanceItemVal: { fontSize: 15, fontWeight: '600' },
  balanceDivider: { width: 1, height: 28, backgroundColor: C.border },

  pillsRow: { marginBottom: 16 },
  pillsInner: { gap: 8, paddingRight: 12 },
  pill: {
    paddingHorizontal: 14, paddingVertical: 8, borderRadius: 16,
    borderWidth: 1, borderColor: C.border, backgroundColor: C.card,
    alignItems: 'center', minWidth: 60,
  },
  pillActive: { borderColor: C.accent, backgroundColor: C.accent + '22' },
  pillText: { color: C.textDim, fontSize: 12, fontWeight: '600' },
  pillTextActive: { color: C.accent2 },
  pillBalance: { color: C.textMute, fontSize: 10, marginTop: 2 },
  pillAdd: { backgroundColor: 'transparent', borderStyle: 'dashed' },
  pillAddText: { color: C.textDim, fontSize: 16 },

  emptyStateCard: {
    backgroundColor: C.card, borderRadius: 14, padding: 18, marginBottom: 16,
    borderWidth: 1, borderColor: C.border, alignItems: 'center',
  },
  emptyStateTitle: { color: C.text, fontSize: 15, fontWeight: '600', marginBottom: 4 },
  emptyStateText:  { color: C.textDim, fontSize: 12, marginBottom: 12, textAlign: 'center' },
  emptyStateBtn:   { backgroundColor: C.accent, borderRadius: 10, paddingHorizontal: 18, paddingVertical: 10 },
  emptyStateBtnText: { color: '#fff', fontWeight: '600', fontSize: 13 },

  sectionLabel:   { color: C.textMute, fontSize: 11, letterSpacing: 2, marginBottom: 8, marginTop: 4 },
  catCard:        { backgroundColor: C.card, borderRadius: 16, padding: 16, marginBottom: 16, borderWidth: 1, borderColor: C.border },
  catRow:         { flexDirection: 'row', alignItems: 'center', marginBottom: 10 },
  catLabel:       { color: C.textDim, fontSize: 12, width: 40 },
  catBarWrap:     { flex: 1, height: 6, backgroundColor: C.border, borderRadius: 3, marginHorizontal: 10 },
  catBar:         { height: 6, backgroundColor: C.accent, borderRadius: 3 },
  catAmt:         { color: C.text, fontSize: 12, width: 52, textAlign: 'right' },

  recordHeader:   { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', marginTop: 4 },
  emptyText:      { color: C.textMute, fontSize: 13, textAlign: 'center', paddingVertical: 24 },
  recordRow:      { flexDirection: 'row', alignItems: 'center', backgroundColor: C.card, borderRadius: 14, padding: 12, marginBottom: 8, borderWidth: 1, borderColor: C.border },
  recordIcon:     { width: 36, height: 36, borderRadius: 10, alignItems: 'center', justifyContent: 'center', marginRight: 12 },
  recordDesc:     { color: C.text, fontSize: 14, fontWeight: '500' },
  recordCat:      { color: C.textMute, fontSize: 11, marginTop: 2 },
  recordAmt:      { fontSize: 14, fontWeight: '700' },
  addMiniBtn:     { backgroundColor: C.accent + '22', borderRadius: 12, paddingHorizontal: 12, paddingVertical: 6, borderWidth: 1, borderColor: C.accent + '55' },
  addMiniBtnText: { color: C.accent2, fontSize: 12, fontWeight: '600' },

  // Modal
  overlay:        { flex: 1, backgroundColor: '#00000088', justifyContent: 'flex-end' },
  modalBox:       { backgroundColor: C.card, borderTopLeftRadius: 24, borderTopRightRadius: 24, padding: 20, paddingBottom: 30 },
  modalTitle:     { color: C.text, fontSize: 17, fontWeight: '700', marginBottom: 16, textAlign: 'center' },
  modeSwitch:     { flexDirection: 'row', backgroundColor: C.bg, borderRadius: 12, padding: 4, marginBottom: 12 },
  modeBtn:        { flex: 1, paddingVertical: 8, borderRadius: 10, alignItems: 'center' },
  modeText:       { color: C.textDim, fontSize: 13, fontWeight: '600' },
  modalInput:     { backgroundColor: C.bg, borderRadius: 10, paddingHorizontal: 12, paddingVertical: 10, color: C.text, fontSize: 14, borderWidth: 1, borderColor: C.border, marginBottom: 10 },
  modalLabel:     { color: C.textMute, fontSize: 11, letterSpacing: 1, marginBottom: 8, marginTop: 4 },
  chipRow:        { flexDirection: 'row', flexWrap: 'wrap', gap: 6, marginBottom: 8 },
  chip:           { borderRadius: 14, paddingHorizontal: 10, paddingVertical: 5, borderWidth: 1, borderColor: C.border, backgroundColor: C.bg },
  chipActive:     { backgroundColor: C.accent + '33', borderColor: C.accent },
  chipText:       { color: C.textDim, fontSize: 12 },
  chipTextActive: { color: C.accent2, fontWeight: '600' },
  iconChip:       { width: 42, height: 42, borderRadius: 10, borderWidth: 1, borderColor: C.border, backgroundColor: C.bg, alignItems: 'center', justifyContent: 'center' },
  dateTimeRow:    { flexDirection: 'row', gap: 10 },
  btnRow:         { flexDirection: 'row', gap: 10, marginTop: 12 },
  cancelBtn:      { flex: 1, backgroundColor: C.bg, borderRadius: 12, paddingVertical: 12, alignItems: 'center', borderWidth: 1, borderColor: C.border },
  cancelText:     { color: C.textDim, fontWeight: '600' },
  confirmBtn:     { flex: 2, backgroundColor: C.accent, borderRadius: 12, paddingVertical: 12, alignItems: 'center' },
  confirmText:    { color: '#fff', fontWeight: '700' },

  acctRow:        { flexDirection: 'row', alignItems: 'center', backgroundColor: C.bg, borderRadius: 12, padding: 12, marginBottom: 8, borderWidth: 1, borderColor: C.border },
  acctName:       { color: C.text, fontSize: 14, fontWeight: '500' },
  acctBalance:    { color: C.textMute, fontSize: 11, marginTop: 2 },
  acctDelBtn:     { width: 28, height: 28, borderRadius: 14, backgroundColor: C.expense + '22', alignItems: 'center', justifyContent: 'center' },
  acctDelText:    { color: C.expense, fontSize: 16, fontWeight: '700' },
});