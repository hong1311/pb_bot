"""Conservative automated research feed. No broker or order-routing API.
All timestamps are closed minute bar open times, normalized to UTC.
"""
import gzip
import os
import json
import math
import queue
import threading
from datetime import datetime, date, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pb_bot.config import BotConfig, NarrativeMode, PlaybookMode, StructureMode, DiscretionMode, RegimeMode
from pb_bot.core.models import Bar
from pb_bot.core.calendar import EconomicCalendar, NewsEvent
from pb_bot.backtest.simulator import BacktestBroker, process_pair
from pb_bot.engine.strategy import PBStrategyEngine
from pb_telegram import signal, now

NY = ZoneInfo('America/New_York')
UTC = timezone.utc
MINUTE = timedelta(minutes=1)
MAX_CLOSE_AGE = 90
SCORE_MIN = 80
WARMUP_SESSIONS = 3  # Research-only compromise for free minute-data retention.


def aware(s):
    x = datetime.fromisoformat(s) if isinstance(s, str) else s
    if x.tzinfo is None or x.utcoffset() is None:
        raise ValueError('timestamp is timezone-naive')
    return x.astimezone(UTC)


def clock(day, t):
    return datetime.combine(day, t, NY).astimezone(UTC)


def session_start(day):
    return clock(day - timedelta(days=1), time(18))


def minutes(a, b):
    while a < b:
        yield a
        a += MINUTE


def regular_day(day):
    # Conservative cash-session proxy. Operator-reviewed CME date is also mandatory.
    import exchange_calendars as xc
    c = xc.get_calendar('XNYS')
    ds = day.isoformat()
    return (c.is_session(ds) and c.session_open(ds).to_pydatetime() == clock(day, time(9,30))
            and c.session_close(ds).to_pydatetime() == clock(day, time(16)))


def previous_days(day, regular=regular_day):
    days = []
    for i in range(1, 15):
        d = day - timedelta(days=i)
        if d.weekday() < 5 and regular(d):
            days.append(d)
        if len(days) == WARMUP_SESSIONS:
            return list(reversed(days))
    raise ValueError('insufficient regular sessions')


def entry_window(t):
    local = t.astimezone(NY)
    return local.weekday() < 5 and time(9,30) <= local.time() < time(11)


def reviewed_calendar(path, day, t):
    """Explicit operator review, never fabricate news-free days. No external certification."""
    try:
        data = json.loads(path.read_text())
        reviewed = aware(data['reviewed_at'])
        if not timedelta(0) <= t - reviewed <= timedelta(days=7):
            raise ValueError('review expired')
        if not str(data.get('source','')).strip():
            raise ValueError('source missing')
        if day.isoformat() not in data['regular_cme_days']:
            raise ValueError('CME session not reviewed')
        covered = set(data['covered_news_dates'])
        if not {day.isoformat(), (day+timedelta(days=1)).isoformat()} <= covered:
            raise ValueError('today and next day news coverage required')
        events = []
        if not isinstance(data['events'], list): raise ValueError('invalid events')
        for e in data['events']:
            d = date.fromisoformat(e['date'])
            tm = time.fromisoformat(e['time_ny'])
            title = e['title'].strip(); impact = e['impact'].upper()
            if d.isoformat() not in covered or not title or impact not in ('RED','ORANGE','YELLOW') or tm.tzinfo or tm.second or tm.microsecond:
                raise ValueError('invalid event')
            events.append(NewsEvent(d,tm,title,impact))
        return EconomicCalendar(events), 'reviewed schedule/news (operator declaration)'
    except (OSError, KeyError, TypeError, AttributeError, ValueError, OverflowError):
        return None, 'calendar not reviewed or invalid; qualified alerts locked'


def bars_from_frame(df, symbol, t):
    """Reject invalid, duplicate, off-grid, or naive observations; no fabricated data."""
    if df is None or df.empty or df.index.tz is None or df.index.has_duplicates:
        raise ValueError('empty, naive or duplicate feed')
    out = {}
    for ts, row in df.iterrows():
        stamp = aware(ts.to_pydatetime())
        if stamp.second or stamp.microsecond or stamp > t:
            raise ValueError('off-minute or future feed')
        if stamp + MINUTE > t: continue
        o,h,l,c = (float(row[k]) for k in ('Open','High','Low','Close'))
        v = float(row.get('Volume',0))
        if not all(math.isfinite(x) and x>0 and abs(x*4-round(x*4))<1e-6 for x in (o,h,l,c)) or not math.isfinite(v) or v<0 or not l<=min(o,c)<=max(o,c)<=h:
            raise ValueError('invalid or off-tick OHLCV')
        out[stamp] = Bar(symbol,'1m',stamp.astimezone(NY),o,h,l,c,v)
    if not out: raise ValueError('no closed bars')
    return out


def latest_pair(nq, es, t):
    a,b = max(nq),max(es)
    if a!=b: raise ValueError('NQ/ES latest bars unmatched')
    age=(t-(a+MINUTE)).total_seconds()
    if not 0<=age<=MAX_CLOSE_AGE: raise ValueError('latest closed bars stale')
    return a


def checked_pairs(nq,es,start,end):
    keys=list(minutes(start,end))
    if any(k not in nq or k not in es for k in keys):
        raise ValueError('missing minute(s) in required session')
    return [(nq[k],es[k]) for k in keys]


def event_for(order, t):
    md = dict(order.metadata)
    stamp=aware(order.timestamp)
    return dict(md, event='ORDER_SUBMITTED', order_id='research-'+stamp.isoformat(), logged_at_ny=t.astimezone(NY).isoformat(), bar_open=stamp.isoformat(), direction=order.direction.value, qty=order.quantity, order_type=order.order_type, price=order.entry_price, sl=order.stop_loss, tp=order.take_profit, reason=order.reason)


def qualify(e, t, cal, threshold=SCORE_MIN):
    """Score cannot override mandatory checks; returns a research alert, not advice."""
    try:
        b=aware(e['bar_open']); close=b+MINUTE
        if not entry_window(b) or not entry_window(t) or b.astimezone(NY).date()!=t.astimezone(NY).date(): return None
        if not 0<=(t-close).total_seconds()<=MAX_CLOSE_AGE or cal is None: return None
        day=t.astimezone(NY).date(); nt=t.astimezone(NY)
        if cal.has_pm_red_news(day) or cal.is_day_before_nfp_or_cpi(day) or cal.active_red_news_freeze(nt): return None
        s=signal(e)
        r=e.get('regime') or {}; d=e.get('discretion') or {}
        if not s['strict'] or not e.get('model_evidence') or e.get('playbook_model') not in ('MAC','CONTINUATION','PO3') or r.get('blocked') is not False or r.get('calendar_available') is not True or d.get('allowed') is not True: return None
        checks={name:state for name,_,state in s['checks']}
        for k in ('HTF narrative + DOL','Structural leg evidence','IFVG recorded by engine','Regime + news calendar','Gross R:R at least 1:1.4'):
            if checks.get(k) is not True: return None
        if any(v is False for v in checks.values()) or s['score']<threshold: return None
        # Stable across restarts, independent of wall-clock log time / simulator order ID.
        import hashlib
        s['id']=hashlib.sha256(json.dumps([b.isoformat(),s['direction'],s['entry'],s['sl'],s['tp']],separators=(',',':')).encode()).hexdigest()[:10]
        s.update(qualified=True, bar_open=b.isoformat(), bar_close=close.isoformat(), expires_at=(close+timedelta(seconds=MAX_CLOSE_AGE)).isoformat(), feed='Yahoo NQ=F / ES=F (continuous futures proxy)', threshold=threshold, narrative_id=(e.get('narrative') or {}).get('narrative_id'))
        return s
    except (KeyError,ValueError,TypeError,OverflowError,AttributeError): return None


def delivery_valid(s,t,review_path=None):
    try:
        if review_path is not None:
            day=t.astimezone(NY).date(); cal,_=reviewed_calendar(Path(review_path),day,t)
            if cal is None or cal.has_pm_red_news(day) or cal.is_day_before_nfp_or_cpi(day) or cal.active_red_news_freeze(t.astimezone(NY)):
                return False
        return (s.get('qualified') is True and not s.get('demo') and entry_window(t)
                and aware(s['bar_close']) <= t <= aware(s['expires_at'])
                and aware(s['bar_open']).astimezone(NY).date()==t.astimezone(NY).date())
    except (KeyError,TypeError,ValueError): return False


def strict_config(calendar):
    return BotConfig(narrative_mode=NarrativeMode.STRICT, model_mode=PlaybookMode.STRICT, structure_mode=StructureMode.STRICT, discretion_mode=DiscretionMode.STRICT, regime_mode=RegimeMode.STRICT, regime_require_calendar=True, min_rr=1.4)


def fetch(start,t):
    import yfinance as yf
    data=[]
    for ticker,sym in [('NQ=F','NQ'),('ES=F','ES')]:
        frame=yf.Ticker(ticker).history(start=start,end=t,interval='1m',prepost=True,auto_adjust=False,raise_errors=True,timeout=15)
        data.append(bars_from_frame(frame,sym,t))
    return data


class WarmupCache:
    """Only complete prior sessions from the same Yahoo pair; never used for live freshness."""
    def __init__(self, root):
        self.root=Path(root)
    def file(self,day): return self.root/(day.isoformat()+'.json.gz')
    def save(self,day,nq,es):
        pairs=checked_pairs(nq,es,session_start(day),clock(day,time(17)))
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        path=self.file(day); tmp=path.with_suffix('.tmp')
        rows=[[aware(a.timestamp).isoformat(),[a.open,a.high,a.low,a.close,a.volume],[b.open,b.high,b.low,b.close,b.volume]] for a,b in pairs]
        with gzip.open(tmp,'wt',encoding='utf-8') as f: json.dump({'source':'Yahoo NQ=F/ES=F','rows':rows},f,separators=(',',':'))
        tmp.chmod(0o600); os.replace(tmp,path)
    def load(self,day,nq,es):
        try:
            return checked_pairs(nq,es,session_start(day),clock(day,time(17)))
        except ValueError: pass
        try:
            with gzip.open(self.file(day),'rt',encoding='utf-8') as f: data=json.load(f)
            if data['source']!='Yahoo NQ=F/ES=F': raise ValueError('wrong cache source')
            x={};y={}
            for raw,av,bv in data['rows']:
                k=aware(raw)
                if k in x: raise ValueError('duplicate cached bars')
                for sym,values,d,fresh in [('NQ',av,x,nq),('ES',bv,y,es)]:
                    o,h,l,c,v=values
                    if not all(math.isfinite(z) and z>0 and abs(z*4-round(z*4))<1e-6 for z in (o,h,l,c)) or not math.isfinite(v) or v<0 or not l<=min(o,c)<=max(o,c)<=h: raise ValueError('invalid cache')
                    b=Bar(sym,'1m',k.astimezone(NY),o,h,l,c,v)
                    if k in fresh and (o,h,l,c)!=(fresh[k].open,fresh[k].high,fresh[k].low,fresh[k].close): raise ValueError('cached price revised')
                    d[k]=b
            return checked_pairs(x,y,session_start(day),clock(day,time(17)))
        except (OSError,ValueError,KeyError,TypeError,EOFError):
            raise ValueError('missing minute(s) in required session') from None

    def collect(self,nq,es,t,regular):
        local=t.astimezone(NY); day=local.date()
        for i in range(8):
            d=day-timedelta(days=i)
            if d.weekday()>=5 or clock(d,time(17))>t or not regular(d): continue
            try: self.save(d,nq,es)
            except ValueError: pass



class MarketMonitor:
    def __init__(self, path, output, stop, fetcher=fetch, regular=regular_day, clock_now=now):
        self.path=Path(path); self.output=output; self.stop=stop; self.fetcher=fetcher; self.regular=regular; self.now=clock_now
        self.day=None; self.engine=None; self.broker=None; self.last=None; self.review_key=None
        self.seen={}; self.quarantine=False; self.fault=None; self.cache=WarmupCache(self.path.parent/'warmup_cache'); self.prefetched=None
    def put(self,kind,**kw): self.output.put(dict(kind=kind,**kw))
    def status(self,state,**kw):
        self.put('health',state=state,stamp=self.now().isoformat(),**kw)
    def reset(self,day):
        self.day=day; self.engine=None; self.broker=None; self.last=None; self.review_key=None; self.seen={}; self.quarantine=False; self.fault=None
    def poll(self):
        t=self.now(); local=t.astimezone(NY); day=local.date()
        if day!=self.day: self.reset(day)
        if local.weekday()>=5 or not self.regular(day): self.status('CLOSED / non-regular cash session',day=day.isoformat()); return
        if not time(8,30)<=local.time()<time(11,35): self.status('OUTSIDE AM MONITOR WINDOW',day=day.isoformat()); return
        if self.quarantine: self.status('BLOCKED: feed revised previously processed bars',day=day.isoformat()); return
        cal, review=reviewed_calendar(self.path,day,t)
        # Rebuild on a calendar change; never replay entries on restart or rebuild.
        key=self.path.read_bytes() if self.path.exists() else b''
        if key!=self.review_key:
            self.engine=None; self.broker=None; self.last=None; self.review_key=key; self.seen={}
        initial=self.engine is None
        start=t-timedelta(days=7)+timedelta(minutes=2) if initial else session_start(day)
        nq,es=self.fetcher(start,t)
        t=self.now()  # Freshness checked after both network requests.
        if t.astimezone(NY).date()!=day: raise ValueError('session changed during fetch')
        latest=latest_pair(nq,es,t)
        current=checked_pairs(nq,es,session_start(day),latest+MINUTE)
        def fingerprint(a,b): return (a.open,a.high,a.low,a.close,b.open,b.high,b.low,b.close)
        for a,b in current:
            k=aware(a.timestamp)
            if k in self.seen and self.seen[k]!=fingerprint(a,b):
                self.quarantine=True; raise ValueError('provider revised a processed bar')
        if initial:
            hist=[]
            for d in previous_days(day,self.regular):
                hist.extend(self.cache.load(d,nq,es))
            self.cache.collect(nq,es,t,self.regular)
            cfg=strict_config(cal); broker=BacktestBroker(cfg); engine=PBStrategyEngine(cfg,broker,calendar=cal)
            for a,b in hist+current:
                process_pair(engine,broker,a,b,warmup=True)
            self.engine=engine; self.broker=broker; self.last=latest
        else:
            for a,b in current:
                k=aware(a.timestamp)
                if k<=self.last: continue
                # Catch-up bars can update state, but cannot originate a new alert.
                active=(k==latest and entry_window(k) and entry_window(t) and cal is not None)
                before=len(self.engine.submitted_orders)
                process_pair(self.engine,self.broker,a,b,warmup=not active)
                self.last=k
                if k==latest:
                    n=self.engine.narrative.current
                    self.put('reference',bar_open=k.isoformat(),high=a.high,low=a.low,close=a.close,day=day.isoformat(),narrative_id=n.id if n else None,narrative_phase=n.phase.value if n else None,regime_blocked=self.engine.regime.blocked)
                for order in self.engine.submitted_orders[before:]:
                    s=qualify(event_for(order,self.now()),self.now(),cal)
                    if active and s: self.put('signal',signal=s)
        self.seen={aware(a.timestamp):fingerprint(a,b) for a,b in current}
        state='MONITORING — research alerts only' if cal is not None else 'MONITORING — calendar locked'
        self.status(state,day=day.isoformat(),last_closed=(latest+MINUTE).isoformat(),review=review,warmup_sessions=WARMUP_SESSIONS,paired=len(current),rejections=len(self.engine.rejections))
    def run(self):
        while not self.stop.is_set():
            try: self.poll()
            except Exception as e:
                # Fail closed; never leak exception URLs or credentials.
                detail=str(e) if type(e) is ValueError and str(e) in ('latest closed bars stale','NQ/ES latest bars unmatched','missing minute(s) in required session','empty, naive or duplicate feed','off-minute or future feed','invalid or off-tick OHLCV','no closed bars','provider revised a processed bar') else type(e).__name__
                self.status('BLOCKED: '+detail,day=self.now().astimezone(NY).date().isoformat())
                # Any uncertain engine mutation must be discarded, and recovery warms up without entries.
                self.engine=None; self.broker=None; self.last=None; self.seen={}
            t=self.now(); local=t.astimezone(NY)
            # Once a day outside the AM session, save complete historical sessions before
            # Yahoo's short retention rolls them away. No orders or alerts originate here.
            if not time(8,30)<=local.time()<time(11,35) and self.prefetched!=local.date():
                self.prefetched=local.date()
                try:
                    nq,es=self.fetcher(t-timedelta(days=7)+timedelta(minutes=2),t)
                    self.cache.collect(nq,es,t,self.regular)
                    print('Warmup cache refreshed (complete sessions only).',flush=True)
                except Exception:
                    print('Historical warmup prefetch unavailable; live checks remain fail-closed.',flush=True)
            self.stop.wait(30)


def start_monitor(path):
    q=queue.Queue(maxsize=0); stop=threading.Event()
    worker=MarketMonitor(path,q,stop)
    thread=threading.Thread(target=worker.run,name='PB market monitor',daemon=True)
    thread.start()
    return q,stop,thread
