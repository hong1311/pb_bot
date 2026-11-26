"""Reproducible, simulation-only validation and gate diagnostics."""
from collections import Counter
from dataclasses import asdict, replace
from datetime import timedelta
from math import floor
from pathlib import Path
import csv
import json

from pb_bot.backtest.simulator import BacktestBroker, process_pair, settle_end_of_data, _sync_broker
from pb_bot.config import (BotConfig, NarrativeMode, PlaybookMode, StructureMode,
                           DiscretionMode, RegimeMode, EntryOrderMode)
from pb_bot.core.models import Direction
from pb_bot.core.sessions import cme_trading_date, SessionTracker
from pb_bot.data.validation import UTC, utc_minute
from pb_bot.engine.risk_manager import OpenPosition
from pb_bot.engine.strategy import PBStrategyEngine


def jsonable(value):
    # Enum subclasses of str serialize naturally; dates and times use ISO.
    return json.loads(json.dumps(value, default=lambda obj: obj.isoformat()))


def profiles(base):
    """Fixed progression; diagnostic only, not a parameter optimizer."""
    base = replace(base, narrative_mode=NarrativeMode.OBSERVE, model_mode=PlaybookMode.OBSERVE,
                   structure_mode=StructureMode.OBSERVE, discretion_mode=DiscretionMode.OBSERVE,
                   regime_mode=RegimeMode.OBSERVE)
    n = replace(base, narrative_mode=NarrativeMode.STRICT)
    m = replace(n, model_mode=PlaybookMode.STRICT)
    s = replace(m, structure_mode=StructureMode.STRICT)
    d = replace(s, discretion_mode=DiscretionMode.STRICT)
    return {'OBSERVE': base, 'NARRATIVE': n, 'MODELS': m, 'STRUCTURE': s,
            'DISCRETION': d, 'ALL_STRICT': replace(d, regime_mode=RegimeMode.STRICT),
            'REGIME_ONLY': replace(base, regime_mode=RegimeMode.STRICT)}


class NextOpenBroker(BacktestBroker):
    """Queue market-close signals, fill at the next minute open with costs.

    Only market-entry validation is implemented; legacy limit simulation remains
    available outside this runner. No partial fills, queue model or intrabar
    order book. A same-bar stop beats a target. Planned dollar risk is a
    conservative quantity cap including modeled entry/stop slip and round fees.
    Actual gap-through-stop losses can exceed this planned budget.
    """
    def __init__(self, config, slippage_ticks, commission_per_side):
        super().__init__(config, slippage_ticks, commission_per_side)
        self.execution_events = []

    def _event(self, ts, reason, order_id, **details):
        self.execution_events.append({'timestamp': ts.isoformat(), 'reason': reason,
                                      'order_id': order_id, **details})

    def submit_order(self, order):
        if order.order_type != 'MARKET':
            raise ValueError('Next-open validation supports MARKET signals only')
        if self.position is not None or self.pending_order is not None:
            raise RuntimeError('Order/position already active')
        self._order_seq += 1
        order_id = f'SIM_{self._order_seq:04d}'
        self.pending_order = (order_id, order)
        self._event(order.timestamp, 'QUEUED', order_id, signal_price=order.entry_price, quantity=order.quantity)
        return order_id

    def _cancel(self, ts, reason):
        if self.pending_order is not None:
            self._event(ts, reason, self.pending_order[0])
            self.pending_order = None

    def expire_pending(self, nq_bar, es_bar=None):
        if self.pending_order is None:
            return
        _, req = self.pending_order
        now = nq_bar.timestamp
        lag = now.astimezone(UTC) - req.timestamp.astimezone(UTC)
        if lag != timedelta(minutes=1):
            self._cancel(now, 'NOT_IMMEDIATE_NEXT_MINUTE')
            return
        cfg = self.config
        if cme_trading_date(now) != cme_trading_date(req.timestamp) or not SessionTracker.in_trading_window(now, cfg.am_start, cfg.am_end, cfg.allow_pm_session, cfg.pm_start, cfg.pm_end):
            self._cancel(now, 'OUTSIDE_ENTRY_WINDOW')
            return
        es_target = req.metadata.get('es_target_price')
        if es_bar is not None and es_target is not None and (es_bar.open >= es_target if req.direction == Direction.BULLISH else es_bar.open <= es_target):
            self._cancel(now, 'ES_TARGET_ALREADY_REACHED')

    def process_bar(self, bar):
        self.expire_pending(bar)
        if self.pending_order is not None:
            order_id, req = self.pending_order
            sign = 1 if req.direction == Direction.BULLISH else -1
            fill = bar.open + sign * self.slippage_pts
            risk = sign * (fill - req.stop_loss)
            reward = sign * (req.take_profit - fill)
            if (sign * (bar.open - req.stop_loss) <= 0 or sign * (req.take_profit - bar.open) <= 0
                    or risk <= 0 or reward <= 0 or reward / risk < self.config.min_rr
                    or (self.config.strict_max_rr and reward / risk > self.config.max_target_rr)):
                self._cancel(bar.timestamp, 'FILL_GEOMETRY_OR_RR')
            elif self.config.max_stop_pts is not None and risk > self.config.max_stop_pts:
                self._cancel(bar.timestamp, 'FILL_STOP_TOO_WIDE')
            else:
                point_value = self.config.instrument.point_value
                # The requested quantity embeds any reduced-risk decision. Never
                # increase it or its signal-price nominal risk allocation.
                budget = min(self.balance * self.config.risk_per_trade_pct,
                             req.quantity * abs(req.entry_price - req.stop_loss) * point_value)
                unit_risk = (risk + self.slippage_pts) * point_value + 2 * self.commission_per_side
                qty = min(req.quantity, floor(budget / unit_risk)) if unit_risk > 0 else 0
                if qty <= 0:
                    self._cancel(bar.timestamp, 'FILL_RISK_EXCEEDS_BUDGET')
                else:
                    self.pending_order = None
                    self.position = OpenPosition(order_id, req.symbol, req.direction, qty, fill,
                        req.stop_loss, req.stop_loss, req.take_profit, bar.timestamp,
                        be_trigger_price=req.metadata.get('be_trigger_price'),
                        es_target_price=req.metadata.get('es_target_price'), reason=req.reason,
                        metadata=dict(req.metadata))
                    self._just_opened_on_bar_ts = None
                    self._event(bar.timestamp, 'FILLED', order_id, fill_price=fill, quantity=qty,
                                signal_quantity=req.quantity, planned_risk_usd=unit_risk * qty)
        return super().process_bar(bar)


def read_annotations(path):
    """Disjoint, manually labeled evaluation windows; bar-open timestamps."""
    rows = []
    with Path(path).open(newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            start, end = utc_minute(row['start']), utc_minute(row['end'])
            expected = row['expected'].upper()
            direction = row['direction'].upper()
            if start >= end or expected not in ('ENTER', 'NO_ENTRY') or direction not in ('BULLISH', 'BEARISH', 'ANY'):
                raise ValueError('Invalid annotation window, expected result or direction')
            if (expected == 'ENTER' and direction == 'ANY') or (expected == 'NO_ENTRY' and direction != 'ANY'):
                raise ValueError('ENTER must specify a direction; NO_ENTRY must specify ANY')
            if rows and start < rows[-1]['end']:
                raise ValueError('Annotation windows must be ordered and non-overlapping')
            rows.append({'start': start, 'end': end, 'expected': expected, 'direction': direction,
                         'label': row.get('label', '')})
    return rows


def compare_annotations(annotations, orders, rejections, evaluated_days, measured_minutes):
    results = []
    for a in annotations:
        if cme_trading_date(a['start']) not in evaluated_days or cme_trading_date(a['end'] - timedelta(minutes=1)) not in evaluated_days:
            continue
        start, end = a['start'], a['end']
        if any(start + i * timedelta(minutes=1) not in measured_minutes for i in range((end - start) // timedelta(minutes=1))):
            raise ValueError('Annotation window contains unobserved minutes; do not score it')
        found = [o for o in orders if start <= o.timestamp.astimezone(UTC) < end]
        matching = [o for o in found if a['direction'] in ('ANY', o.direction.value)]
        # Evaluate signals, not fills. Additional/wrong-direction orders are
        # mismatches, not silently counted as a correct positive setup.
        if a['expected'] == 'ENTER':
            passed = len(found) == len(matching) == 1
        else:
            passed = not found
        results.append({**a, 'passed': passed, 'signal_count': len(found), 'matching_signals': len(matching),
                        'rejections': dict(Counter(r.code for r in rejections if start <= r.timestamp.astimezone(UTC) < end))})
    return jsonable(results)


def run_replay(nq, es, config, calendar=None, *, evaluated_days, slippage_ticks=1,
               commission_per_side=0.62, next_open=True, annotations=(), detail_path=None):
    if config.entry_mode != EntryOrderMode.MARKET_ON_CLOSE and next_open:
        raise ValueError('Validation currently supports market-close signals only')
    if not nq or len(nq) != len(es):
        raise ValueError('Replay requires paired input')
    from math import isfinite
    if slippage_ticks < 0 or int(slippage_ticks) != slippage_ticks or not isfinite(commission_per_side) or commission_per_side < 0:
        raise ValueError('Costs must be finite and nonnegative')
    days = list(dict.fromkeys(cme_trading_date(b.timestamp) for b in nq))
    if not evaluated_days or evaluated_days != set(days[-len(evaluated_days):]):
        raise ValueError('Evaluated sessions must be the nonempty chronological suffix after warmup')
    broker = (NextOpenBroker(config, slippage_ticks, commission_per_side) if next_open else
              BacktestBroker(config, slippage_ticks, commission_per_side))
    engine = PBStrategyEngine(config, broker, calendar=calendar)
    peak = config.account_balance
    max_mark_dd = 0.0
    count = 0
    last = None
    measured_minutes = set() if annotations else None
    for n, e in zip(nq, es):
        if n.timestamp.astimezone(UTC) != e.timestamp.astimezone(UTC):
            raise ValueError('Replay encountered unsynchronized input')
        if last is not None and n.timestamp.astimezone(UTC) <= last.timestamp.astimezone(UTC):
            raise ValueError('Replay timestamps must increase strictly')
        day = cme_trading_date(n.timestamp)
        measured = day in evaluated_days
        if measured:
            count += 1
            if measured_minutes is not None:
                measured_minutes.add(n.timestamp.astimezone(UTC))
        process_pair(engine, broker, n, e, warmup=not measured)
        # For validation, stay within the selected contract and session. No
        # exposure to an unobserved maintenance/holiday/roll interval.
        if next_open and n.timestamp.hour == 16 and n.timestamp.minute == 59:
            broker._cancel(n.timestamp, "SESSION_CLOSE_CANCELLED")
            if broker.position is not None:
                broker.close_position_market(broker.position.symbol, "SESSION_CLOSE")
                _sync_broker(engine, broker, broker.process_market_close(n))
        mark = broker.balance
        pos = broker.position
        if pos:
            sign = 1 if pos.direction == Direction.BULLISH else -1
            mark += (sign * (n.close - pos.entry_price) - broker.slippage_pts) * config.instrument.point_value * pos.quantity
            mark -= 2 * commission_per_side * pos.quantity
        if measured:
            peak = max(peak, mark)
            max_mark_dd = max(max_mark_dd, peak - mark)
        last = n
    if last:
        settle_end_of_data(engine, broker, last)
        max_mark_dd = max(max_mark_dd, peak - broker.balance)
    trades = broker.trades
    gross_win = sum(t.pnl_dollars for t in trades if t.pnl_dollars > 0)
    gross_loss = -sum(t.pnl_dollars for t in trades if t.pnl_dollars < 0)
    net_rs = []
    for t in trades:
        planned = ((abs(t.entry_price - t.initial_stop_loss) + broker.slippage_pts) * config.instrument.point_value + 2 * commission_per_side) * t.quantity
        if planned > 0:
            net_rs.append(t.pnl_dollars / planned)
    events = {'narrative': engine.narrative.events, 'model': engine.playbook.events,
              'structure': engine.structure.events, 'discretion': engine.discretion.events,
              'regime': engine.regime.events, 'execution': getattr(broker, 'execution_events', [])}
    # Audit counts and detailed logs exclude warmup; counts of rejection events
    # are not counts of unique candidate trades (multiple reasons may share a bar).
    def in_evaluation(ts):
        from datetime import datetime
        return cme_trading_date(datetime.fromisoformat(ts)) in evaluated_days
    def measured_events(items):
        out = []
        for event in items:
            ts = event.get('bar_timestamp', event.get('timestamp'))
            if ts and in_evaluation(ts):
                out.append(event)
        return out
    events = {k: measured_events(v) for k, v in events.items()}
    rejected = [r for r in engine.rejections if cme_trading_date(r.timestamp) in evaluated_days]
    orders = [o for o in engine.submitted_orders if cme_trading_date(o.timestamp) in evaluated_days]
    summary = {'configuration': jsonable(asdict(config)), 'fill_model': 'NEXT_MINUTE_OPEN' if next_open else 'LEGACY_CLOSE',
               'slippage_ticks': slippage_ticks, 'commission_per_side': commission_per_side,
               'evaluated_days': sorted(str(d) for d in evaluated_days), 'evaluated_minutes': count,
               'signals': len(orders), 'trades': len(trades), 'wins': sum(t.pnl_dollars > 0 for t in trades),
               'net_pnl': round(broker.balance - config.account_balance, 2), 'final_balance': round(broker.balance, 2),
               'profit_factor': gross_win / gross_loss if gross_loss else None,
               'net_expectancy_r': sum(net_rs) / len(net_rs) if net_rs else None,
               'max_minute_close_marked_drawdown': round(max_mark_dd, 2),
               'rejections': dict(Counter(r.code for r in rejected)),
               'structure_reasons': dict(Counter(x['reason'] for x in events['structure'])),
               'model_reasons': dict(Counter(d['reason'] for x in events['model'] for d in x['decisions'])),
               'narrative_reasons': dict(Counter(x['event'] for x in events['narrative'])),
               'discretion_reasons': dict(Counter(x['reason'] for x in events['discretion'])),
               'regime_reasons': dict(Counter(x['event'] for x in events['regime'])),
               'execution_reasons': dict(Counter(x['reason'] for x in events['execution'])),
               'event_counts': {k: len(v) for k, v in events.items()},
               'annotations': compare_annotations(annotations, orders, rejected, evaluated_days, measured_minutes or set())}
    if detail_path is not None:
        detail_path = Path(detail_path)
        detail_path.mkdir(parents=True, exist_ok=True)
        payloads = {**events, 'orders': [asdict(o) for o in orders], 'trades': [asdict(t) for t in trades],
                    'rejections': [asdict(r) for r in rejected]}
        for name, rows in payloads.items():
            (detail_path / f'{name}.jsonl').write_text(''.join(json.dumps(jsonable(r)) + '\n' for r in rows), encoding='utf-8')
    return summary
