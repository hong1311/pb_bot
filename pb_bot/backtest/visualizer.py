import json
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import List

from pb_bot.backtest.simulator import BacktestResult
from pb_bot.core.models import Bar


def generate_html_report(
    result: BacktestResult,
    nq_bars_1m: List[Bar],
    output_path: Path,
) -> Path:
    """Writes a self-contained offline HTML report with equity curve and per-trade candlestick charts."""
    trades = result.trades
    total = len(trades)
    wins = [t for t in trades if t.pnl_dollars > 0]
    losses = [t for t in trades if t.pnl_dollars < 0]
    win_rate = (len(wins) / total * 100.0) if total else 0.0
    net_pnl = result.final_balance - result.initial_balance
    avg_r = (sum(t.r_multiple for t in trades) / total) if total else 0.0
    gross_win = sum(t.pnl_dollars for t in wins)
    gross_loss = abs(sum(t.pnl_dollars for t in losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)

    peak = result.initial_balance
    max_dd = 0.0
    eq_points = [{"ts": "Start", "eq": round(result.initial_balance, 2)}]
    for ts, eq in result.equity_curve:
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        eq_points.append({"ts": ts.strftime("%Y-%m-%d %H:%M"), "eq": round(eq, 2)})

    trade_charts = []
    for idx, t in enumerate(trades, start=1):
        win_start = t.opened_at - timedelta(minutes=25)
        win_end = t.closed_at + timedelta(minutes=20)
        window_bars = [
            {
                "ts": b.timestamp.strftime("%H:%M"),
                "full_ts": b.timestamp.isoformat(),
                "o": b.open,
                "h": b.high,
                "l": b.low,
                "c": b.close,
            }
            for b in nq_bars_1m
            if win_start <= b.timestamp <= win_end
        ]
        trade_charts.append(
            {
                "id": idx,
                "order_id": t.order_id,
                "direction": t.direction.value,
                "qty": t.quantity,
                "opened": t.opened_at.strftime("%Y-%m-%d %H:%M"),
                "closed": t.closed_at.strftime("%H:%M"),
                "open_iso": t.opened_at.isoformat(),
                "close_iso": t.closed_at.isoformat(),
                "entry": t.entry_price,
                "exit": t.exit_price,
                "sl": t.initial_stop_loss,
                "tp": t.take_profit,
                "pnl": round(t.pnl_dollars, 2),
                "r": round(t.r_multiple, 2),
                "exit_reason": t.exit_reason,
                "setup_reason": t.setup_reason,
                "bars": window_bars,
            }
        )

    rej_counts = Counter(r.code for r in result.rejections).most_common(12)
    payload = json.dumps(
        {"equity": eq_points, "trades": trade_charts, "rejections": rej_counts}
    )

    pf_display = "inf" if gross_loss == 0 and gross_win > 0 else f"{pf:.2f}"
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>PB Trading Bot — Backtest Report</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0d1117; color: #e6edf3; margin: 0; padding: 24px; }}
  h1, h2 {{ margin: 0 0 12px; font-weight: 600; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 24px; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 14px 16px; }}
  .label {{ color: #8b949e; font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }}
  .val {{ font-size: 22px; font-weight: 700; margin-top: 4px; }}
  .pos {{ color: #3fb950; }} .neg {{ color: #f85149; }}
  .panel {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 16px; margin-bottom: 20px; }}
  canvas {{ width: 100%; display: block; background: #0b0f14; border-radius: 6px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #21262d; }}
  th {{ color: #8b949e; font-weight: 600; }}
  .meta {{ font-size: 13px; color: #8b949e; margin-bottom: 10px; }}
</style>
</head>
<body>
  <h1>PB Trading Bot — Backtest &amp; Audit Report</h1>
  <div class="grid">
    <div class="card"><div class="label">Final Balance</div><div class="val">${result.final_balance:,.2f}</div></div>
    <div class="card"><div class="label">Net PnL</div><div class="val {'pos' if net_pnl >= 0 else 'neg'}">${net_pnl:+,.2f}</div></div>
    <div class="card"><div class="label">Trades / Win Rate</div><div class="val">{total} ({win_rate:.1f}%)</div></div>
    <div class="card"><div class="label">Expectancy</div><div class="val {'pos' if avg_r >= 0 else 'neg'}">{avg_r:+.2f}R</div></div>
    <div class="card"><div class="label">Profit Factor</div><div class="val">{pf_display}</div></div>
    <div class="card"><div class="label">Max Drawdown</div><div class="val neg">${max_dd:,.2f}</div></div>
  </div>

  <div class="panel">
    <h2>Equity Curve</h2>
    <canvas id="eqCanvas" height="180"></canvas>
  </div>

  <div id="tradeCharts"></div>

  <div class="panel">
    <h2>Rejection Audit Log ({len(result.rejections)} filtered events)</h2>
    <table>
      <thead><tr><th>Rule / Filter Code</th><th>Count</th></tr></thead>
      <tbody id="rejBody"></tbody>
    </table>
  </div>

<script>
const DATA = {payload};

function drawEquity() {{
  const c = document.getElementById('eqCanvas');
  const ctx = c.getContext('2d');
  c.width = c.clientWidth * 2; c.height = 360;
  ctx.scale(2, 2);
  const w = c.clientWidth, h = 180, pad = 36;
  const pts = DATA.equity;
  if (pts.length < 2) {{
    ctx.fillStyle = '#8b949e'; ctx.fillText('No closed trades yet', pad, h/2); return;
  }}
  const vals = pts.map(p => p.eq);
  const minV = Math.min(...vals), maxV = Math.max(...vals);
  const span = Math.max(1, maxV - minV);
  ctx.strokeStyle = '#21262d'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(pad, h - pad); ctx.lineTo(w - 16, h - pad); ctx.stroke();

  ctx.strokeStyle = '#3fb950'; ctx.lineWidth = 2;
  ctx.beginPath();
  pts.forEach((p, i) => {{
    const x = pad + (i / (pts.length - 1)) * (w - pad - 20);
    const y = h - pad - ((p.eq - minV) / span) * (h - 2 * pad);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }});
  ctx.stroke();
  ctx.fillStyle = '#8b949e'; ctx.font = '11px monospace';
  ctx.fillText('$' + maxV.toFixed(0), 4, pad + 4);
  ctx.fillText('$' + minV.toFixed(0), 4, h - pad);
}}

function drawTrade(t) {{
  const wrap = document.createElement('div');
  wrap.className = 'panel';
  const cls = t.pnl >= 0 ? 'pos' : 'neg';
  wrap.innerHTML = `
    <h2>Trade #${{t.id}} — ${{t.direction}} x${{t.qty}} (<span class="${{cls}}">${{t.r >= 0 ? '+' : ''}}${{t.r}}R / $${{t.pnl}}</span>)</h2>
    <div class="meta">${{t.opened}} → ${{t.closed}} | Entry ${{t.entry.toFixed(2)}} | SL ${{t.sl.toFixed(2)}} | TP ${{t.tp.toFixed(2)}} | Exit ${{t.exit.toFixed(2)}} (${{t.exit_reason}}) | ${{t.setup_reason}}</div>
    <canvas height="260"></canvas>`;
  document.getElementById('tradeCharts').appendChild(wrap);

  const c = wrap.querySelector('canvas');
  const ctx = c.getContext('2d');
  c.width = c.clientWidth * 2; c.height = 520;
  ctx.scale(2, 2);
  const w = c.clientWidth, h = 260, padL = 62, padR = 16, padT = 16, padB = 26;
  const bars = t.bars;
  if (!bars.length) return;

  const prices = [t.entry, t.sl, t.tp, t.exit];
  bars.forEach(b => {{ prices.push(b.h, b.l); }});
  const minP = Math.min(...prices) - 2, maxP = Math.max(...prices) + 2;
  const span = Math.max(1, maxP - minP);
  const yOf = p => padT + ((maxP - p) / span) * (h - padT - padB);
  const step = (w - padL - padR) / bars.length;

  // Risk / Reward shaded boxes
  ctx.fillStyle = 'rgba(248,81,73,0.10)';
  ctx.fillRect(padL, Math.min(yOf(t.entry), yOf(t.sl)), w - padL - padR, Math.abs(yOf(t.entry) - yOf(t.sl)));
  ctx.fillStyle = 'rgba(63,185,80,0.10)';
  ctx.fillRect(padL, Math.min(yOf(t.entry), yOf(t.tp)), w - padL - padR, Math.abs(yOf(t.entry) - yOf(t.tp)));

  // Horizontal level lines
  [[t.entry, '#58a6ff', 'ENTRY'], [t.sl, '#f85149', 'SL'], [t.tp, '#3fb950', 'TP']].forEach(([val, col, lbl]) => {{
    const y = yOf(val);
    ctx.strokeStyle = col; ctx.setLineDash([4, 4]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(w - padR, y); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = col; ctx.font = '10px monospace';
    ctx.fillText(lbl + ' ' + val.toFixed(1), 4, y + 3);
  }});

  // Candles
  bars.forEach((b, i) => {{
    const x = padL + i * step + step / 2;
    const bull = b.c >= b.o;
    ctx.strokeStyle = bull ? '#3fb950' : '#f85149';
    ctx.fillStyle = bull ? '#3fb950' : '#f85149';
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, yOf(b.h)); ctx.lineTo(x, yOf(b.l)); ctx.stroke();
    const top = yOf(Math.max(b.o, b.c)), bot = yOf(Math.min(b.o, b.c));
    const bw = Math.max(2, step * 0.65);
    ctx.fillRect(x - bw / 2, top, bw, Math.max(1.5, bot - top));

    if (b.full_ts === t.open_iso) {{
      ctx.fillStyle = '#58a6ff'; ctx.beginPath(); ctx.arc(x, yOf(t.entry), 4, 0, Math.PI * 2); ctx.fill();
    }}
    if (b.full_ts === t.close_iso) {{
      ctx.fillStyle = '#e3b341'; ctx.beginPath(); ctx.arc(x, yOf(t.exit), 4, 0, Math.PI * 2); ctx.fill();
    }}
    if (i % Math.max(1, Math.floor(bars.length / 8)) === 0) {{
      ctx.fillStyle = '#8b949e'; ctx.font = '10px monospace';
      ctx.fillText(b.ts, x - 12, h - 8);
    }}
  }});
}}

drawEquity();
DATA.trades.forEach(drawTrade);
const rejBody = document.getElementById('rejBody');
DATA.rejections.forEach(([code, cnt]) => {{
  const tr = document.createElement('tr');
  tr.innerHTML = `<td><code>${{code}}</code></td><td>${{cnt}}</td>`;
  rejBody.appendChild(tr);
}});
</script>
</body>
</html>
"""
    out = Path(output_path)
    out.write_text(html, encoding="utf-8")
    return out
