from typing import Optional
from dataclasses import dataclass, field
from datetime import time
from enum import Enum
from typing import Dict, Tuple


class EntryOrderMode(str, Enum):
    MARKET_ON_CLOSE = "MARKET_ON_CLOSE"
    LIMIT_IFVG_EDGE = "LIMIT_IFVG_EDGE"
    LIMIT_CISD_OPEN = "LIMIT_CISD_OPEN"


class NarrativeMode(str, Enum):
    OBSERVE = "OBSERVE"  # Record the new narrative without changing V2.0 orders
    STRICT = "STRICT"    # Require a pre-established narrative and its locked DOL


class PlaybookMode(str, Enum):
    OBSERVE = "OBSERVE"  # Audit new model gates without changing orders
    STRICT = "STRICT"    # Require an eligible model AND strict narrative


class PlaybookModel(str, Enum):
    MAC = "MAC"
    CONTINUATION = "CONTINUATION"
    PO3 = "PO3"


class BreakEvenMode(str, Enum):
    OFF = "OFF"
    STRICT_INTERNAL = "STRICT_INTERNAL"  # Any internal high/low wick (Breaker Blocks rule)
    SWING_5M = "SWING_5M"                # 5m internal high/low or opposing FVG (Charting Session rule)


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    tick_size: float
    point_value: float  # USD per 1.00 full index point


NQ_SPEC = InstrumentSpec(symbol="NQ", tick_size=0.25, point_value=20.0)
MNQ_SPEC = InstrumentSpec(symbol="MNQ", tick_size=0.25, point_value=2.0)
ES_SPEC = InstrumentSpec(symbol="ES", tick_size=0.25, point_value=50.0)
MES_SPEC = InstrumentSpec(symbol="MES", tick_size=0.25, point_value=5.0)


@dataclass
class BotConfig:
    # Markets
    primary_symbol: str = "NQ"
    correlated_symbol: str = "ES"
    instrument: InstrumentSpec = field(default_factory=lambda: MNQ_SPEC)

    # Account & Risk (Lesson #10 / Building Discretion)
    account_balance: float = 50_000.0
    risk_per_trade_pct: float = 0.005       # Fixed 0.5% per trade
    continuation_size_mult: float = 0.5     # Reduced size on extreme premium/discount continuations
    continuation_extreme_threshold: float = 0.705  # Deep premium/discount Fib threshold
    max_trades_per_day: int = 2             # 1-2 trades/day hard cap
    max_losses_per_day: int = 1             # Stop trading for the day after 1 stop-loss
    max_wins_per_day: Optional[int] = None  # Optional cap (e.g., 1) to stop after hitting daily target
    max_stop_pts: Optional[float] = None      # Reject overextended setups with SL wider than N points
    enable_10am_macro_pause: bool = False     # Pause entries 09:55-10:10 NY around 10:00 USD releases
    pending_order_max_minutes: int = 15  # Explicit configurable limit-order lifetime
    min_rr: float = 1.0                     # Hard floor: reject < 1:1 RR
    max_target_rr: float = 2.5              # Avoid chasing distant >2.5R intraday targets
    strict_max_rr: bool = False             # When True, reject targets exceeding max_target_rr
    min_ifvg_tf_minutes: int = 1            # Minimum TF in minutes for IFVG entry (1, 2, 3, 4, 5)
    preferred_max_rr: float = 2.0           # Standard 1:1 - 1:2 intraday DOL range
    sl_tick_buffer: int = 1                 # Ticks beyond swing extreme wick

    # Timezone & Execution Windows (America/New_York)
    timezone: str = "America/New_York"
    am_start: time = time(9, 30)
    am_end: time = time(11, 0)
    allow_pm_session: bool = False
    pm_start: time = time(13, 30)
    pm_end: time = time(15, 30)

    # Timeframe Matrices (CISD / DOL 2.0 / Charting Session)
    ifvg_scan_ladder: Tuple[str, ...] = ("5m", "4m", "3m", "2m", "1m")
    htf_to_ltf_alignment: Dict[str, str] = field(
        default_factory=lambda: {
            "1M": "1D",
            "1W": "4H",
            "1D": "1H",
            "4H": "15m",
            "1H": "5m",
            "15m": "1m",
        }
    )
    sponsorship_min_timeframes: Tuple[str, ...] = ("30m", "1H", "4H", "1D")
    reversal_key_level_min_tf: str = "15m"
    continuation_key_level_min_tf: str = "5m"

    # Execution & Management Toggles
    narrative_mode: NarrativeMode = NarrativeMode.OBSERVE
    model_mode: PlaybookMode = PlaybookMode.OBSERVE
    enabled_models: Tuple[PlaybookModel, ...] = tuple(PlaybookModel)
    model_evidence_minutes: int = 45
    po3_accumulation_minutes: int = 15
    po3_max_range_efficiency: float = 0.35
    entry_mode: EntryOrderMode = EntryOrderMode.MARKET_ON_CLOSE
    be_mode: BreakEvenMode = BreakEvenMode.SWING_5M
    enable_target_smt_exit: bool = True
    enable_0930_judas_1m_override: bool = False
    max_candles_for_clean_inversion: int = 4  # >= 5 candles flagged as slow/choppy LRLR

    # Phase 4 Regime & Time Alignment Filters (Recognizing Market Conditions / Time & Structure)
    overnight_exhaustion_pts: float = 300.0
    overnight_exhaustion_pct: float = 0.015  # Scales 300pt rule proportionally to index level
    enable_dual_smt_chop_filter: bool = True
    enable_overnight_exhaustion_filter: bool = True
    enable_am_5m_fvg_volatility_filter: bool = True
    enable_time_alignment_matrix: bool = True
    enable_stacked_fvg_eq_filter: bool = True
    enable_lunch_block: bool = True

    def __post_init__(self):
        if self.model_mode == PlaybookMode.STRICT and self.narrative_mode != NarrativeMode.STRICT:
            raise ValueError("STRICT model mode requires STRICT narrative mode")
        if not self.enabled_models or any(m not in tuple(PlaybookModel) for m in self.enabled_models):
            raise ValueError("Enable at least one valid playbook model")
        if self.model_evidence_minutes < 1 or self.po3_accumulation_minutes < 3:
            raise ValueError("Invalid model evidence or accumulation window")
        if not 0 < self.po3_max_range_efficiency <= 1:
            raise ValueError("PO3 range efficiency must be in (0, 1]")
