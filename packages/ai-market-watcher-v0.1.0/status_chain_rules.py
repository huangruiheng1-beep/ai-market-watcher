#!/usr/bin/env python3
"""
底部状态链追踪器 · 规则层  -  Bottom Status-Chain Rules
================================================================
对应《数字资产市场观察助手》PPT 第 7 页 / 知识点 8：底部不是一次超卖，而是
`底部观察 → 筑底进行 → 待多头触发 → 确认移交` 的状态机。本模块只定义状态、
转移规则和判定阈值，不做 IO、不连数据库、不联网。

核心纪律（照抄 PPT，不得违反）：
  - 失效位优先于预测：结构破坏立即撤销候选，失效线与容差比形态本身更重要。
  - 左侧负责"提前观察"，右侧负责"确认触发"，两者不混在一个分数里。
  - 不预测涨跌，只输出"当前在哪个状态、失效线在哪、查看顺序"。
  - 降级 / 过期 / 失效都是一等公民，必须写入状态链，不能直接抹掉旧状态。

阈值状态：PPT 只给出状态机骨架，未给出"失效线算法 / 已走远幅度 / 突破量能"
的精确值。这些由本工具自定候选值，集中写在 RULES_CONFIG 并标注
rules_status=candidate_unverified，必须反映到报告/manifest，不得宣称已复刻原系统。

本模块纯标准库实现，不依赖 pandas/numpy/sqlite3，便于单测在任意 Python 上跑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

# ============================================================
# 状态枚举（用字符串常量，与 SQLite TEXT 列一一对应，简单可读）
# ============================================================
NOISE = "NOISE"            # 无信号：不在观察池
OBSERVE = "OBSERVE"        # 底部观察：左侧信号出现，仅入观察池
BASE = "BASE"              # 筑底进行：多信号收敛、结构开始形成，此时写定失效线
TRIGGER = "TRIGGER"        # 待多头触发：价格逼近触发位，等右侧确认
CONFIRM = "CONFIRM"        # 确认移交：右侧确认达成，移交"已确认优先复核"
INVALID = "INVALID"        # 失效撤销（终态）：收盘破失效线，结构破坏
EXPIRED = "EXPIRED"        # 已走远（终态·风险）：远离未确认，追单风险

ALL_STATES = (NOISE, OBSERVE, BASE, TRIGGER, CONFIRM, INVALID, EXPIRED)
ACTIVE_STATES = frozenset({OBSERVE, BASE, TRIGGER, CONFIRM})
TERMINAL_STATES = frozenset({INVALID, EXPIRED})
# 需要携带失效线的活跃态（OBSERVE 尚未写定失效线）
STATES_WITH_INVALIDATION = frozenset({BASE, TRIGGER, CONFIRM})

# ============================================================
# 触发信号常量（写入 transitions.trigger_signal）
# ============================================================
TRIG_LEFT_SIGNAL = "left_signal"             # 任意左侧信号（SKDJ超卖/低位上穿/背离/TD9/榜单观察池）
TRIG_STRUCTURE_HL = "structure_hl"           # 出现更高低点，结构改善
TRIG_APPROACHING = "approaching_trigger"     # 价格逼近触发位
TRIG_BREAKOUT = "breakout"                   # 收盘突破触发位 + 量能配合
TRIG_INVALIDATION = "invalidation_breach"    # 收盘破失效线
TRIG_MOVED_AWAY = "moved_away"               # 远离未确认（追单风险）
TRIG_SIGNAL_GONE = "signal_disappeared"      # 左侧信号消失
TRIG_STRUCTURE_DEGRADE = "structure_degrade" # 结构退化
TRIG_REACTIVATE = "reactivate"               # 终态后新左侧信号，重新激活
TRIG_DATA_INSUFFICIENT = "data_insufficient" # 数据不足 fail closed
TRIG_MANUAL = "manual"

# ============================================================
# 转移类型（写入 transitions.transition_type）
# ============================================================
T_OPEN = "open"                # 开新 episode（NOISE/终态 → OBSERVE）
T_UPGRADE = "upgrade"          # 状态升级（OBSERVE→BASE、BASE→TRIGGER）
T_CONFIRM = "confirm"          # 确认移交（TRIGGER→CONFIRM）
T_DOWNGRADE = "downgrade"      # 降级（OBSERVE→NOISE、BASE→OBSERVE、TRIGGER→BASE、CONFIRM→TRIGGER/BASE）
T_INVALIDATE = "invalidate"    # 失效撤销（→INVALID）
T_EXPIRE = "expire"            # 已走远（→EXPIRED）
T_REACTIVATE = "reactivate"    # 终态重新激活（INVALID/EXPIRED→OBSERVE，开新 episode）

# ============================================================
# 阈值配置（本工具自定候选值 · rules_status=candidate_unverified）
# ============================================================
# PPT 未给出失效线算法、已走远幅度、突破量能的精确值，以下为本工具自定候选，
# 集中写在此处并会写入报告/manifest。未经历史回测验证，不得宣称复刻原系统。
RULES_CONFIG = {
    "invalidation_tolerance_pct": 1.0,    # 失效线 = 结构低点 * (1 - 1.0%)，容差比形态本身更重要
    "moved_away_pct": 8.0,                 # 较观察起点收盘上涨 >= 8% 仍未确认 → EXPIRED（追单风险）
    "breakout_volume_ratio": 1.5,          # 突破日成交量 >= 1.5x 近 N 日均量（阶段B ingest 用）
    "breakout_volume_window": 20,          # 均量回看窗口（阶段B 用）
    "rules_status": "candidate_unverified",
    "note": ("失效线容差/已走远幅度/突破量能阈值为本工具自定候选值，PPT 未给出精确值；"
             "集中写在 RULES_CONFIG 并反映到报告，未经历史回测验证。"),
}


def compute_invalidation_level(structure_low: float) -> float:
    """失效线 = 结构低点 * (1 - 容差)。进入 BASE 时调用，写定失效线。"""
    tol = RULES_CONFIG["invalidation_tolerance_pct"] / 100.0
    return round(float(structure_low) * (1.0 - tol), 4)


def invalidation_breached(close: Optional[float],
                          invalidation_level: Optional[float]):
    """收盘破失效线判定。返回 (breached: bool, reason: str|None)。

    用严格小于（close < invalidation_level）表示"跌破"。失效线本身已含容差
    （结构低点 - 1%），故此条件意味着收盘已跌破结构低点超过容差。
    """
    if close is None or invalidation_level is None:
        return False, None
    if close < invalidation_level:
        return True, f"收盘 {close} 跌破失效线 {invalidation_level}"
    return False, None


# ============================================================
# 合法转移图（from_state → 允许的 to_state 集合；停留不算转移）
# ============================================================
ALLOWED_TRANSITIONS = {
    NOISE:    {OBSERVE},
    OBSERVE:  {BASE, NOISE, EXPIRED},
    BASE:     {TRIGGER, OBSERVE, INVALID, EXPIRED},
    TRIGGER:  {CONFIRM, BASE, INVALID, EXPIRED},
    CONFIRM:  {TRIGGER, BASE, INVALID, EXPIRED},
    INVALID:  {OBSERVE},   # 重新激活，开新 episode
    EXPIRED:  {OBSERVE},   # 重新激活，开新 episode
}


def is_allowed(from_state: str, to_state: str) -> bool:
    """是否为合法的状态变化（from==to 视为停留，不算转移，返回 False）。"""
    if from_state == to_state:
        return False
    return to_state in ALLOWED_TRANSITIONS.get(from_state, set())


def transition_type_for(from_state: str, to_state: str) -> str:
    """根据起止状态推断转移类型。"""
    if to_state == INVALID:
        return T_INVALIDATE
    if to_state == EXPIRED:
        return T_EXPIRE
    if from_state in TERMINAL_STATES and to_state == OBSERVE:
        return T_REACTIVATE
    if from_state == NOISE and to_state == OBSERVE:
        return T_OPEN
    if to_state == CONFIRM:
        return T_CONFIRM
    if to_state in (BASE, TRIGGER):
        # 升级方向：OBSERVE→BASE、BASE→TRIGGER 为 upgrade；其余降级为 downgrade
        order = {NOISE: 0, OBSERVE: 1, BASE: 2, TRIGGER: 3, CONFIRM: 4}
        if order.get(to_state, -1) > order.get(from_state, -1):
            return T_UPGRADE
        return T_DOWNGRADE
    if to_state == NOISE:
        return T_DOWNGRADE
    return T_DOWNGRADE


# ============================================================
# 当日信号包（dataclass）
# ============================================================
# 阶段 A 纯逻辑：所有判定输入显式化成布尔字段，引擎不依赖 pandas/parquet。
# 阶段 B 的 ingest 负责从工具1-4 CSV + cache parquet 计算这些字段并填入。
@dataclass
class SignalPack:
    ticker: str
    evidence_date: str                       # 行情截至日期 YYYY-MM-DD（≠生成时间）
    has_left_signal: bool = False            # 任意左侧信号出现
    left_signal_tags: List[str] = field(default_factory=list)  # 如 ["SKDJ_oversold","divergence_rsi"]
    signal_disappeared: bool = False         # 原有左侧信号消失（OBSERVE→NOISE 用）
    source_report: Optional[str] = None      # 来源报告路径
    # 结构 / 价格确认（阶段B由价格帧计算后填入；阶段A单测直接给）
    structure_improved: bool = False         # 出现更高低点，结构改善
    structure_low: Optional[float] = None    # 结构低点（进入 BASE 时用于写失效线）
    structure_degraded: bool = False         # 结构退化（更高低点被破坏）
    approaching_trigger: bool = False        # 价格逼近触发位
    breakout_confirmed: bool = False         # 收盘突破触发位 + 量能配合
    close: Optional[float] = None            # 当日已完成日K收盘
    moved_away: bool = False                 # 较观察起点上涨 >= moved_away_pct 仍未确认
    data_sufficient: bool = True             # 数据不足 fail closed（False 时不写活跃状态）
    # 阶段B：四类报告详细字段（落 daily_snapshots / transitions.source_detail，供阶段C证据展示）
    resonance_layer: Optional[str] = None    # 共振分层（多头共振/偏多/…）
    ranking: Optional[int] = None            # 五榜单编号 1-5
    ranking_name: Optional[str] = None       # 五榜单名称
    skdj_scenario: Optional[str] = None      # 下跌超跌/上升回调/顶部超买/…
    skdj_k: Optional[float] = None
    skdj_d: Optional[float] = None
    divergence_flag: bool = False            # 是否存在底/顶背离
    td9_count: Optional[int] = None          # TD9 计数
    source_detail: Optional[str] = None      # 详细证据字符串（如 "SKDJ K=12 D=15; 底背离(30)"）
    notes: Optional[str] = None


# ============================================================
# 转移决策（引擎输出）
# ============================================================
@dataclass
class TransitionDecision:
    next_state: str
    transition_type: Optional[str]      # None = 无转移（停留）
    trigger_signal: Optional[str]
    invalidation_level: Optional[float] # 转移后应写入的失效线（活跃态继承/写定；其余 None）
    invalidation_reason: Optional[str]
    source_report: Optional[str]
    price: Optional[float]
    open_new_episode: bool = False      # 进入 OBSERVE 时开新 episode
    close_episode: bool = False         # 进入 CONFIRM/INVALID/EXPIRED 时关闭 episode
    episode_end_reason: Optional[str] = None   # confirmed/invalidated/expired
    notes: Optional[str] = None


def _stay(current_state: str, pack: SignalPack,
          current_inv_level: Optional[float]) -> TransitionDecision:
    """无转移：保持当前状态。活跃态保留失效线，终态/NOISE/OBSERVE 失效线无意义时清空。"""
    inv = current_inv_level if current_state in STATES_WITH_INVALIDATION else None
    return TransitionDecision(
        next_state=current_state,
        transition_type=None,
        trigger_signal=None,
        invalidation_level=inv,
        invalidation_reason=None,
        source_report=pack.source_report,
        price=pack.close,
        open_new_episode=False,
        close_episode=False,
    )


# ============================================================
# 状态机引擎（核心）
# ============================================================
def evaluate_transition(current_state: str,
                        pack: SignalPack,
                        current_invalidation_level: Optional[float] = None
                        ) -> TransitionDecision:
    """根据当前状态 + 当日信号包，决定下一步状态与转移。

    优先级（失效位优先于预测）：
      0. 数据不足 → fail closed，不写活跃状态（落 NOISE / 不推进）
      1. 终态 + 新左侧信号 → 重新激活，开新 episode → OBSERVE
      2. 活跃态(带失效线) + 收盘破失效线 → INVALID（记原因）
      3. 活跃态(未确认) + 已走远 → EXPIRED（风险，非错误）
      4. 正向转移：NOISE→OBSERVE / OBSERVE→BASE(写失效线) / BASE→TRIGGER /
         TRIGGER→CONFIRM / 各降级
      5. 无匹配 → 停留

    参数:
      current_state: 当前状态（NOISE/OBSERVE/BASE/TRIGGER/CONFIRM/INVALID/EXPIRED）
      pack:          当日信号包（见 SignalPack）
      current_invalidation_level: 当前失效线（仅 BASE/TRIGGER/CONFIRM 有意义）

    返回: TransitionDecision。transition_type=None 表示无转移（停留）。
    """
    # 0) 数据不足 fail closed：保持原状，不推进、不破位、不降级（留痕 data_insufficient）
    #    终态保持终态；活跃态保持活跃（不因一天缺数据就丢掉筑底过程，违背可回放/可审计）；
    #    NOISE 保持 NOISE（即"缺数据 ticker 当 NOISE 留痕"）。
    if not pack.data_sufficient:
        inv = current_invalidation_level if current_state in STATES_WITH_INVALIDATION else None
        return TransitionDecision(
            next_state=current_state,
            transition_type=None,
            trigger_signal=TRIG_DATA_INSUFFICIENT,
            invalidation_level=inv,
            invalidation_reason=None,
            source_report=pack.source_report,
            price=pack.close,
            open_new_episode=False,
            close_episode=False,
            notes="fail closed: data insufficient, hold state",
        )

    src = pack.source_report
    tag = pack.left_signal_tags[0] if pack.left_signal_tags else TRIG_LEFT_SIGNAL

    # 1) 终态重新激活：新左侧信号 → OBSERVE（开新 episode）
    if current_state in TERMINAL_STATES:
        if pack.has_left_signal:
            return TransitionDecision(
                next_state=OBSERVE,
                transition_type=T_REACTIVATE,
                trigger_signal=TRIG_REACTIVATE,
                invalidation_level=None,
                invalidation_reason=None,
                source_report=src,
                price=pack.close,
                open_new_episode=True,
                close_episode=False,
                notes=f"reactivate by left signal: {tag}",
            )
        return _stay(current_state, pack, current_invalidation_level)

    # 2) 失效优先：活跃态(带失效线) 收盘破失效线 → INVALID
    if current_state in STATES_WITH_INVALIDATION and current_invalidation_level is not None:
        breached, reason = invalidation_breached(pack.close, current_invalidation_level)
        if breached:
            return TransitionDecision(
                next_state=INVALID,
                transition_type=T_INVALIDATE,
                trigger_signal=TRIG_INVALIDATION,
                invalidation_level=None,   # 终态失效线无意义，清空
                invalidation_reason=reason,
                source_report=src,
                price=pack.close,
                open_new_episode=False,
                close_episode=True,
                episode_end_reason="invalidated",
            )

    # 3) 已走远（风险）：未确认活跃态远离 → EXPIRED
    if current_state in (OBSERVE, BASE, TRIGGER) and pack.moved_away:
        return TransitionDecision(
            next_state=EXPIRED,
            transition_type=T_EXPIRE,
            trigger_signal=TRIG_MOVED_AWAY,
            invalidation_level=None,
            invalidation_reason=None,
            source_report=src,
            price=pack.close,
            open_new_episode=False,
            close_episode=True,
            episode_end_reason="expired",
            notes=(f"moved away >= {RULES_CONFIG['moved_away_pct']}% unconfirmed "
                   f"(risk, not error)"),
        )

    # 4) 正向 / 降级转移
    if current_state == NOISE:
        if pack.has_left_signal:
            return TransitionDecision(
                next_state=OBSERVE,
                transition_type=T_OPEN,
                trigger_signal=tag,
                invalidation_level=None,
                invalidation_reason=None,
                source_report=src,
                price=pack.close,
                open_new_episode=True,
                close_episode=False,
                notes=f"enter observe by left signal: {tag}",
            )
        return _stay(current_state, pack, current_invalidation_level)

    if current_state == OBSERVE:
        if pack.structure_improved and pack.structure_low is not None:
            inv = compute_invalidation_level(pack.structure_low)
            return TransitionDecision(
                next_state=BASE,
                transition_type=T_UPGRADE,
                trigger_signal=TRIG_STRUCTURE_HL,
                invalidation_level=inv,
                invalidation_reason=None,
                source_report=src,
                price=pack.close,
                open_new_episode=False,
                close_episode=False,
                notes=(f"structure improved (higher low={pack.structure_low}); "
                       f"invalidation set to {inv}"),
            )
        if pack.signal_disappeared:
            return TransitionDecision(
                next_state=NOISE,
                transition_type=T_DOWNGRADE,
                trigger_signal=TRIG_SIGNAL_GONE,
                invalidation_level=None,
                invalidation_reason=None,
                source_report=src,
                price=pack.close,
                open_new_episode=False,
                close_episode=True,                 # 信号消失：关闭这条观察链
                episode_end_reason="signal_disappeared",
                notes="left signal disappeared",
            )
        return _stay(current_state, pack, current_invalidation_level)

    if current_state == BASE:
        if pack.approaching_trigger:
            return TransitionDecision(
                next_state=TRIGGER,
                transition_type=T_UPGRADE,
                trigger_signal=TRIG_APPROACHING,
                invalidation_level=current_invalidation_level,  # 继承失效线
                invalidation_reason=None,
                source_report=src,
                price=pack.close,
                open_new_episode=False,
                close_episode=False,
                notes="approaching trigger level",
            )
        if pack.structure_degraded:
            return TransitionDecision(
                next_state=OBSERVE,
                transition_type=T_DOWNGRADE,
                trigger_signal=TRIG_STRUCTURE_DEGRADE,
                invalidation_level=None,   # 降回 OBSERVE，失效线清空
                invalidation_reason=None,
                source_report=src,
                price=pack.close,
                open_new_episode=False,
                close_episode=False,
                notes="structure degraded back to observe",
            )
        return _stay(current_state, pack, current_invalidation_level)

    if current_state == TRIGGER:
        if pack.breakout_confirmed:
            return TransitionDecision(
                next_state=CONFIRM,
                transition_type=T_CONFIRM,
                trigger_signal=TRIG_BREAKOUT,
                invalidation_level=current_invalidation_level,  # 继承（回破可降级）
                invalidation_reason=None,
                source_report=src,
                price=pack.close,
                open_new_episode=False,
                close_episode=True,             # CONFIRM 关闭 episode（成功移交）
                episode_end_reason="confirmed",
                notes="breakout confirmed; transfer to priority review",
            )
        if pack.structure_degraded:
            return TransitionDecision(
                next_state=BASE,
                transition_type=T_DOWNGRADE,
                trigger_signal=TRIG_STRUCTURE_DEGRADE,
                invalidation_level=current_invalidation_level,  # 退回 BASE 仍带失效线
                invalidation_reason=None,
                source_report=src,
                price=pack.close,
                open_new_episode=False,
                close_episode=False,
                notes="trigger lost, back to base",
            )
        return _stay(current_state, pack, current_invalidation_level)

    if current_state == CONFIRM:
        if pack.structure_degraded:
            # 回破确认位 → 降级；CONFIRM 已关闭旧 episode，降级开新 episode
            return TransitionDecision(
                next_state=TRIGGER,
                transition_type=T_DOWNGRADE,
                trigger_signal=TRIG_STRUCTURE_DEGRADE,
                invalidation_level=current_invalidation_level,
                invalidation_reason=None,
                source_report=src,
                price=pack.close,
                open_new_episode=True,    # 旧 episode 在 CONFIRM 时已关闭
                close_episode=False,
                notes="pullback after confirm; reopen episode",
            )
        return _stay(current_state, pack, current_invalidation_level)

    # 兜底：保持
    return _stay(current_state, pack, current_invalidation_level)


def rules_manifest() -> dict:
    """导出规则清单片段，供 tracker 写入 manifest / 报告头部。"""
    return {
        "states": list(ALL_STATES),
        "active_states": sorted(ACTIVE_STATES),
        "terminal_states": sorted(TERMINAL_STATES),
        "allowed_transitions": {k: sorted(v) for k, v in ALLOWED_TRANSITIONS.items()},
        "rules_config": RULES_CONFIG,
        "trigger_signals": [
            TRIG_LEFT_SIGNAL, TRIG_STRUCTURE_HL, TRIG_APPROACHING, TRIG_BREAKOUT,
            TRIG_INVALIDATION, TRIG_MOVED_AWAY, TRIG_SIGNAL_GONE,
            TRIG_STRUCTURE_DEGRADE, TRIG_REACTIVATE, TRIG_DATA_INSUFFICIENT, TRIG_MANUAL,
        ],
        "transition_types": [
            T_OPEN, T_UPGRADE, T_CONFIRM, T_DOWNGRADE, T_INVALIDATE, T_EXPIRE, T_REACTIVATE,
        ],
    }
