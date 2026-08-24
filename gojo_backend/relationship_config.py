"""relationship_config.py —— 感情判断系统 v4 · 参数与阈值集中管理

所有可调参数放在这里，方便后续跑测试用例后调参。
不放数据库连接、不放业务逻辑。

★ 修改任何参数前，最好先看 感情判断系统_v4_最终设计.md 里对应模块的说明，
  确保没绕过某条"铁律"。
"""

# ══════════════════════════════════════════════════════════════
# 状态数值范围
# ══════════════════════════════════════════════════════════════
# 每个维度的取值范围（除 Attachment 允许负 valence 与 W 独立，其余 0 起）
STATE_MIN = 0
STATE_MAX = 100
ATTACHMENT_MIN = 0     # Attachment 不允许负值；"讨厌但离不开"由 W<0 + Attach>0 表达
ATTACHMENT_MAX = 100
# W/F 允许存在但通常不越过；F 用 JSONB 分类型计数，这里给单类型上限
WARMTH_MIN = 0
WARMTH_MAX = 100

# ══════════════════════════════════════════════════════════════
# 基础增量单位（signal 的 base 值）—— 由 signal_type 决定
# ══════════════════════════════════════════════════════════════
# 单次事件对状态的基础影响（medium confidence 情况）
BASE_DELTA = {
    'small_care':         2,   # 小关心（问候、注意休息）
    'genuine_care':       4,   # 真诚关心（记住细节、主动关注）
    'self_disclosure':    3,   # 用户自我暴露（每层深度另加系数）
    'promise_kept':       5,   # 承诺兑现
    'promise_broken':     8,   # 承诺违反（对 Trust 打击更大）
    'boundary_respected': 4,   # 边界被尊重
    'boundary_violated':  6,   # 越界（无意）
    'reconciliation':     5,   # 修复成功
    'positive_reciprocal':3,   # 正向互惠（对暧昧信号的对等回应）
    'flirt_signal':       2,   # 单次调情信号（是否入 P 由阶段门控决定）
}

# ══════════════════════════════════════════════════════════════
# Confidence 三档系数（乘在 BASE_DELTA 上）
# ══════════════════════════════════════════════════════════════
CONFIDENCE_MULTIPLIER = {
    'high':   1.0,
    'medium': 0.6,
    'low':    0.0,   # ★ 铁律 4：low 不直接改状态，只入 pending_hypothesis
}

# ══════════════════════════════════════════════════════════════
# 亲密度洋葱模型 —— 触碰不同层，权重系数
# ══════════════════════════════════════════════════════════════
INTIMACY_LAYER_MULTIPLIER = {
    'outer':  1.0,   # 日常话题（天气、吃饭、工作琐事）
    'middle': 1.5,   # 价值观、经历
    'core':   3.0,   # 核心身份、创伤、最深秘密
}
# 用于陌生阶段冒犯的"双重冒犯"倍率：直接乘 3
STRANGER_BOUNDARY_MULTIPLIER = 3.0

# ══════════════════════════════════════════════════════════════
# Passion 阶段门控参数
# ══════════════════════════════════════════════════════════════
# pending_passion 转化门槛
PENDING_PASSION_THRESHOLD = 3          # 达到 N 次正向互惠才可能转化
PENDING_PASSION_MIN_TIMESPAN_HOURS = 6 # ★ 必须跨时间：至少 N 次分布在 6h+
PENDING_PASSION_MIN_SESSIONS = 2       # ★ 必须跨对话：至少 2 场不同会话
# Passion 转化后，进入"爱情候选"额外要求
PASSION_TO_LOVE_REQUIRED_TRUST = 55    # Trust 达标线
PASSION_TO_LOVE_REQUIRED_COMMITMENT = 45  # 承诺也要有基础
# Passion 衰减
PASSION_DECAY_PER_DAY_IF_INACTIVE = 0.5  # 长期无双向确认，缓慢衰减

# ══════════════════════════════════════════════════════════════
# 阶段判定阈值（从 I/C 数值反推所处阶段）
# ══════════════════════════════════════════════════════════════
STAGE_STRANGER_I_MAX = 5
STAGE_ACQUAINTANCE_I_MAX = 30
STAGE_FRIEND_C_MIN = 25          # C 达标才能算真正的"朋友"，否则只是熟人
# 关系已负面判定（F/W 比较）
NEGATIVE_RELATIONSHIP_F_TO_W_RATIO = 2.0  # F 是 W 的 2 倍以上
NEGATIVE_RELATIONSHIP_W_ZERO = 5           # 或 W 已经归零

# ══════════════════════════════════════════════════════════════
# Trust 不对称动力学（建立慢、摧毁快）
# ══════════════════════════════════════════════════════════════
TRUST_POSITIVE_MULTIPLIER = 1.0    # 正向信号按基础值
TRUST_NEGATIVE_MULTIPLIER = 2.5    # ★ 一次严重背叛可能≈多次正向信号

# ══════════════════════════════════════════════════════════════
# Attachment 分离验证窗口
# ══════════════════════════════════════════════════════════════
ATTACHMENT_SEPARATION_HOURS = 24    # 超过 N 小时无互动，触发一次"分离检验"
ATTACHMENT_SEPARATION_DELTA_MAX = 8 # 单次分离检验最多调整 ±N

# ══════════════════════════════════════════════════════════════
# 时间衰减速率（每天）
# ══════════════════════════════════════════════════════════════
DECAY_PER_DAY = {
    'warmth':     0.1,
    'passion':    0.5,
    # F 走"修复"路径衰减，不走时间衰减；I/C/Trust/Attachment 基本不做时间衰减
}

# ══════════════════════════════════════════════════════════════
# Reciprocity 滑动窗口
# ══════════════════════════════════════════════════════════════
RECIPROCITY_WINDOW_SIZE = 15   # 最近 N 轮消息用于计算互惠度
RECIPROCITY_POSITIVE_THRESHOLD = 0.3    # 比例 > N 判定为"正向氛围"
RECIPROCITY_NEGATIVE_THRESHOLD = -0.3

# ══════════════════════════════════════════════════════════════
# Pursue-Withdraw 追逃诊断
# ══════════════════════════════════════════════════════════════
PURSUE_WITHDRAW_WINDOW_SIZE = 20
PURSUE_WITHDRAW_IMBALANCE_THRESHOLD = 0.65  # 一方主动发起比例超过 N，判定为追

# ══════════════════════════════════════════════════════════════
# Hypothesis pending 池
# ══════════════════════════════════════════════════════════════
HYPOTHESIS_ACTIVE_EVIDENCE_MIN = 3       # 至少 N 条同类 low 证据才升为 active
HYPOTHESIS_MAX_AGE_DAYS = 30             # 超期未确认自动过期清理

# ══════════════════════════════════════════════════════════════
# Boundary intent 判定权重
# ══════════════════════════════════════════════════════════════
BOUNDARY_INTENT_WEIGHTS = {
    ('yes', 'yes'):     3.0,    # known + intentional：最严重
    ('yes', 'unclear'): 2.0,
    ('yes', 'no'):      1.2,    # known 但无意重复
    ('unclear', 'yes'): 1.5,
    ('unclear', 'unclear'): 1.0,
    ('no', 'yes'):      1.3,
    ('no', 'no'):       1.0,    # 首次触碰
    ('no', 'unclear'):  1.0,
}
# 被明确表态后用户主动收手，Trust 补偿
BOUNDARY_RESPECTED_TRUST_BONUS = 3

# ══════════════════════════════════════════════════════════════
# Repair quality 三信号权重
# ══════════════════════════════════════════════════════════════
REPAIR_SIGNAL_WEIGHTS = {
    'acknowledgment':    0.35,   # 承认错误
    'responsibility':    0.35,   # 担责（不甩锅）
    'corrective_action': 0.30,   # 具体改正承诺或行动
}
# 三信号加权得分区间 → 修复档位
REPAIR_QUALITY_HIGH_MIN = 0.75
REPAIR_QUALITY_MEDIUM_MIN = 0.40
# 各档修复对 F/Trust 的影响系数
REPAIR_EFFECT = {
    'high':   {'f_reduce': 0.70, 'trust_bonus': 5},
    'medium': {'f_reduce': 0.35, 'trust_bonus': 2},
    'low':    {'f_reduce': 0.00, 'trust_bonus': -1},   # 敷衍反而扣一点
}

# ══════════════════════════════════════════════════════════════
# Signal Extractor（Observer LLM #1）
# ══════════════════════════════════════════════════════════════
# 用 MODEL_CN_AUX（DeepSeek）省钱，中文任务；如需换用 ai_client 里的常量
SIGNAL_EXTRACTOR_MAX_TOKENS = 400
SIGNAL_EXTRACTOR_TEMPERATURE = 0.2       # 尽量确定性
