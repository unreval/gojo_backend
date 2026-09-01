"""记账路由:账户 CRUD / 收支 CRUD / 转账 / 五条悟短评
insights 用五条悟的人设,但显式跳过用户长短期记忆表,只喂财务摘要。
"""
import json
import anthropic
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import ANTHROPIC_KEY, DEFAULT_CHARACTER_ID, MODEL_JP_AUX
from accounting import (
    list_accounts, create_account, update_account, delete_account,
    list_records, create_record, delete_record, create_transfer,
    summary_last_n_days,
)
from characters import get_character, retrieve_character_memory
from characters_data._loader import load_canon_lock
from utils import extract_json
from ai_client import extract_text

router = APIRouter()
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


# ══════════════════════════════════════════════
#  账户
# ══════════════════════════════════════════════

@router.get('/accounts')
async def get_accounts(user_id: str = 'default'):
    return JSONResponse({'accounts': list_accounts(user_id)})


@router.post('/accounts')
async def post_account(data: dict):
    name = (data.get('name') or '').strip()
    if not name:
        return JSONResponse({'error': 'name required'}, status_code=400)
    new_id = create_account(
        user_id=data.get('user_id', 'default'),
        name=name,
        initial_balance=float(data.get('initial_balance', 0) or 0),
        icon=data.get('icon', '💰'),
        sort_order=int(data.get('sort_order', 0) or 0),
    )
    return JSONResponse({'ok': True, 'id': new_id})


@router.put('/accounts/{account_id}')
async def put_account(account_id: int, data: dict):
    if not update_account(account_id, data):
        return JSONResponse({'error': 'nothing to update'}, status_code=400)
    return JSONResponse({'ok': True})


@router.delete('/accounts/{account_id}')
async def del_account(account_id: int):
    delete_account(account_id)
    return JSONResponse({'ok': True})


# ══════════════════════════════════════════════
#  收支记录
# ══════════════════════════════════════════════

@router.get('/accounting/records')
async def get_records(user_id: str = 'default', limit: int = 200):
    return JSONResponse({'records': list_records(user_id, limit=limit)})


@router.post('/accounting/records')
async def post_record(data: dict):
    """
    创建一条记录。来源:
    (a) 前端手动加账 modal
    (b) 聊天里 pending_transaction 用户点了"记账"
    """
    user_id = data.get('user_id', 'default')
    account_id = data.get('account_id')
    type_ = data.get('type')
    desc = (data.get('desc') or '').strip()
    amount = data.get('amount')
    if not account_id or type_ not in ('in', 'out') or not desc or amount is None:
        return JSONResponse({'error': 'account_id/type/desc/amount required'}, status_code=400)
    try:
        amount = float(amount)
    except Exception:
        return JSONResponse({'error': 'amount must be a number'}, status_code=400)
    if amount <= 0:
        return JSONResponse({'error': 'amount must be > 0'}, status_code=400)

    new_id = create_record(
        user_id=user_id,
        account_id=int(account_id),
        type_=type_,
        category=data.get('category', '其他'),
        desc=desc,
        amount=amount,
        record_date=data.get('date'),      # 前端保证 YYYY-MM-DD
        record_time=data.get('time'),      # HH:MM 或 None
    )
    return JSONResponse({'ok': True, 'id': new_id})


@router.delete('/accounting/records/{record_id}')
async def del_record(record_id: int):
    delete_record(record_id)
    return JSONResponse({'ok': True})


# ══════════════════════════════════════════════
#  转账(UI 手动触发,LLM 不检测)
# ══════════════════════════════════════════════

@router.post('/accounting/transfer')
async def post_transfer(data: dict):
    user_id = data.get('user_id', 'default')
    from_id = data.get('from_account_id')
    to_id = data.get('to_account_id')
    amount = data.get('amount')
    if not from_id or not to_id or not amount:
        return JSONResponse({'error': 'from/to/amount required'}, status_code=400)
    if from_id == to_id:
        return JSONResponse({'error': 'from and to must be different'}, status_code=400)
    try:
        amount = float(amount)
    except Exception:
        return JSONResponse({'error': 'amount must be a number'}, status_code=400)
    if amount <= 0:
        return JSONResponse({'error': 'amount must be > 0'}, status_code=400)

    result = create_transfer(
        user_id=user_id,
        from_account_id=int(from_id),
        to_account_id=int(to_id),
        amount=amount,
        desc=(data.get('desc') or '转账').strip(),
        record_date=data.get('date'),
        record_time=data.get('time'),
    )
    return JSONResponse({'ok': True, **result})


# ══════════════════════════════════════════════
#  五条悟的记账短评
#  ★ 用他的核心人设 + canon_lock + 他自己的背景记忆
#  ★ 显式不喂用户 short_memory / long_memory
# ══════════════════════════════════════════════

@router.get('/accounting/insights')
async def get_insights(user_id: str = 'default',
                       character_id: str = DEFAULT_CHARACTER_ID):
    summary = summary_last_n_days(user_id, days=30)

    if not summary['has_data']:
        return JSONResponse({
            'text': 'まだ何も記録がないよ。まず口座を作ってから話そっか。',
            'zh': '还什么都没记呢。先建个账户再说吧。',
            'empty': True,
        })

    char = get_character(character_id)
    core = char['core_prompt'] if char else ''
    canon = load_canon_lock(character_id) or ''

    # ★ 只取角色自己的背景记忆(gojo 自己的事),不动 short/long memory
    self_recalls = retrieve_character_memory(character_id, '钱 消费 生活', limit=3)
    self_recall_text = ''
    if self_recalls:
        self_recall_text = '\n\n【你自己的一些事(可以自然带入)】\n' + '\n'.join(f'- {r}' for r in self_recalls)

    # 摘要转成给 LLM 看的紧凑格式
    lines = [f'最近 {summary["days"]} 天:']
    lines.append(f'- 总支出 ¥{summary["total_expense"]:.0f},总收入 ¥{summary["total_income"]:.0f}')
    if summary['by_category']:
        cat_str = ', '.join(f'{c["category"]} ¥{c["amount"]:.0f}' for c in summary['by_category'])
        lines.append(f'- 分类支出:{cat_str}')
    if summary['accounts']:
        acc_str = ', '.join(f'{a["name"]} ¥{a["balance"]:.0f}' for a in summary['accounts'])
        lines.append(f'- 各账户余额:{acc_str}')
    if summary['recent_expenses']:
        rec_str = ', '.join(f'{e["desc"]}¥{e["amount"]:.0f}' for e in summary['recent_expenses'])
        lines.append(f'- 最近几笔:{rec_str}')
    finance_ctx = '\n'.join(lines)

    system_prompt = f'''{core}
{canon}
{self_recall_text}

【当前场景:记账小结】
你在看对方的记账数据,像随口点评几句。
——不要 复读用户的具体数字,不要背清单。
——用你一贯的口吻:慵懒、偶尔毒舌、偶尔关心。
——1 条气泡就够,20-40 字。可以吐槽某个大项、可以夸夸、可以劝省着点。
——不要"分析"、"建议",别像财务顾问,你是五条悟。
——就当是路过看了一眼手账。

【输出格式】
返回合法单行 JSON:{{"jp":"日语","zh":"中文翻译"}}
'''

    user_prompt = f'''{finance_ctx}

看看对方最近的账,随口说一句吧。'''

    text_jp, text_zh = '', ''
    for attempt in range(3):
        try:
            response = claude_client.messages.create(
                model=MODEL_JP_AUX,
                max_tokens=300,
                system=system_prompt,
                messages=[{'role': 'user', 'content': user_prompt}],
            )
            raw = extract_text(response).strip()
            try:
                parsed = extract_json(raw)
            except Exception:
                parsed = None
            if not parsed:
                i, j = raw.find('{'), raw.rfind('}')
                if i != -1 and j > i:
                    parsed = json.loads(raw[i:j + 1])
            if parsed and parsed.get('jp') and parsed.get('zh'):
                text_jp = parsed['jp'].strip()
                text_zh = parsed['zh'].strip()
                break
        except Exception as e:
            print(f'[insights] attempt {attempt+1} error: {e}')

    if not text_zh:
        text_jp = 'ふーん、まあ普通に使ってるじゃん。'
        text_zh = '嗯……花得挺正常的嘛。'

    return JSONResponse({
        'jp': text_jp,
        'zh': text_zh,
        'empty': False,
    })