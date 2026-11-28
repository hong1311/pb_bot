import copy
import json
import queue
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from pb_bot.core.calendar import EconomicCalendar, NewsEvent
from pb_telegram import Store, demo_event, format_signal, deliver_groups
from pb_market import (qualify, delivery_valid, latest_pair, checked_pairs, bars_from_frame, reviewed_calendar, regular_day, entry_window, MarketMonitor, session_start, clock, MINUTE, SCORE_MIN)
from pb_service import init, accept_signal, track_reference, reference_result, drain, health_text, finish_references, deliver_notices
from datetime import time

T=datetime(2026,11,30,14,43,10,tzinfo=timezone.utc)
B=datetime(2026,11,30,14,42,tzinfo=timezone.utc)
DAY=date(2026,11,30)


def event():
    e=demo_event(); e.update(logged_at_ny=T.isoformat(),bar_open=B.isoformat(),playbook_model='MAC',model_evidence={'kind':'HTF_FIRST_TAP'})
    for k in ('narrative','model','structure','discretion','regime'): e[k+'_mode']='STRICT'
    e['regime']['calendar_available']=True
    return e


def sample(**kw):
    e=event(); e.update(kw)
    return qualify(e,T,EconomicCalendar([]))


class FakeTG:
    def __init__(self): self.sent=[]
    def send(self,c,text,buttons=None): self.sent.append((c,text,buttons))


class Gates(unittest.TestCase):
    def test_qualified(self):
        s=sample(); self.assertIsNotNone(s); self.assertEqual(s['score'],100); self.assertTrue(delivery_valid(s,T)); self.assertIn('QUALIFIED RESEARCH ALERT',format_signal(s)); self.assertEqual(s['threshold'],80)
    def test_stable_id(self):
        a=event(); b=event(); b['logged_at_ny']=(T+timedelta(seconds=2)).isoformat()
        self.assertEqual(qualify(a,T,EconomicCalendar([]))['id'],qualify(b,T,EconomicCalendar([]))['id'])
    def test_no_news_no_alert(self): self.assertIsNone(qualify(event(),T,None))
    def test_observe_rejected(self): self.assertIsNone(sample(model_mode='OBSERVE'))
    def test_no_model_evidence_rejected(self): self.assertIsNone(sample(model_evidence=None))
    def test_invalid_structure(self): self.assertIsNone(sample(structural_leg=None))
    def test_blocked_regime(self): self.assertIsNone(sample(regime={'blocked':True,'calendar_available':True}))
    def test_not_win_probability(self): self.assertIn('Alignment is not win probability',format_signal(sample()))
    def test_weak_rr(self): self.assertIsNone(sample(tp=26175))
    def test_old_future_weekend(self):
        for delta in (timedelta(minutes=5),timedelta(minutes=-5),timedelta(days=5)):
            self.assertIsNone(qualify(event(),T+delta,EconomicCalendar([])))
        self.assertFalse(entry_window(datetime(2026,11,28,14,42,tzinfo=timezone.utc)))
    def test_expiry_based_on_bar_not_log(self):
        s=sample(); self.assertFalse(delivery_valid(s,B+MINUTE+timedelta(seconds=91)))
    def test_news_freeze(self):
        for e in (NewsEvent(DAY,time(9,43),'Release','RED'), NewsEvent(DAY,time(14),'FOMC','RED'),NewsEvent(DAY+timedelta(days=1),time(8,30),'CPI','RED')):
            self.assertIsNone(qualify(event(),T,EconomicCalendar([e])))
    def test_unknown_smt_not_fabricated(self):
        e=event(); e['structural_leg']['evidence']=['EXTERNAL_SWEEP']; s=qualify(e,T,EconomicCalendar([])); self.assertEqual(s['score'],85); self.assertEqual(s['unknown'],15)
        self.assertIsNone(qualify(e,T,EconomicCalendar([]),threshold=90))
    def test_failed_discretion(self): self.assertIsNone(sample(discretion={'allowed':False,'displacement_ratio':0.9}))
    def test_conservative_market_calendar(self):
        self.assertTrue(regular_day(DAY)); self.assertFalse(regular_day(date(2026,11,27))); self.assertFalse(regular_day(date(2026,11,28)))


class Feed(unittest.TestCase):
    def frame(self,keys=None):
        return pd.DataFrame({'Open':100.,'High':101.,'Low':99.,'Close':100.25,'Volume':1},index=pd.DatetimeIndex(keys or [B]))
    def test_closed_only(self):
        d=bars_from_frame(self.frame([B,B+MINUTE]),'NQ',T); self.assertEqual(set(d),{B})
    def test_duplicate_and_naive(self):
        for keys in ([B,B],[B.replace(tzinfo=None)]):
            with self.assertRaises(ValueError): bars_from_frame(self.frame(keys),'NQ',T)
    def test_invalid_ohlc(self):
        for k,v in [('Low',102),('Open',100.1),('Volume',float('nan'))]:
            f=self.frame(); f[k]=v
            with self.assertRaises(ValueError): bars_from_frame(f,'NQ',T)
    def test_stale_and_unmatched(self):
        with self.assertRaisesRegex(ValueError,'stale'): latest_pair({B:1},{B:2},T+timedelta(minutes=4))
        with self.assertRaisesRegex(ValueError,'unmatched'): latest_pair({B:1},{B-MINUTE:2},T)
    def test_missing_pair(self):
        with self.assertRaisesRegex(ValueError,'missing'): checked_pairs({B:1},{B:2},B-MINUTE,B+MINUTE)
    def test_news_review_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'review.json'; self.assertIsNone(reviewed_calendar(p,DAY,T)[0]); d={'reviewed_at':(T-timedelta(days=1)).isoformat(),'source':'Operator reviewed official dates','regular_cme_days':[DAY.isoformat()],'covered_news_dates':[DAY.isoformat()],'events':[]}; p.write_text(json.dumps(d)); self.assertIsNone(reviewed_calendar(p,DAY,T)[0]); d['covered_news_dates'].append((DAY+timedelta(days=1)).isoformat()); p.write_text(json.dumps(d)); self.assertIsInstance(reviewed_calendar(p,DAY,T)[0],EconomicCalendar); d['reviewed_at']=(T-timedelta(days=8)).isoformat(); p.write_text(json.dumps(d)); self.assertIsNone(reviewed_calendar(p,DAY,T)[0])
    def test_weekend_no_fetch(self):
        with tempfile.TemporaryDirectory() as td:
            q=queue.Queue(); m=MarketMonitor(Path(td)/'review',q,threading.Event(),fetcher=lambda *a: self.fail('fetch on weekend'),clock_now=lambda:T-timedelta(days=2)); m.poll(); self.assertIn('CLOSED',q.get()['state'])


class Runtime(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'journal'; self.st=Store(self.path); self.st.set('chat',42); self.st.link_group(-123,'Group'); init(self.st)
    def tearDown(self): self.st.db.close(); self.tmp.cleanup()
    def test_cap_and_restart(self):
        for e in (sample(),sample(price=26180.25)): self.assertTrue(accept_signal(self.st,e,T))
        self.st.db.close(); self.st=Store(self.path); init(self.st); self.assertFalse(accept_signal(self.st,sample(price=26180.5),T)); self.assertEqual(len(self.st.pending_groups()),2)
    def test_repeat_and_expired(self):
        s=sample(); self.assertTrue(accept_signal(self.st,s,T)); self.assertFalse(accept_signal(self.st,s,T)); self.assertFalse(accept_signal(self.st,sample(price=26180.25),T+timedelta(minutes=3)))
    def test_cancel_legacy_pending(self):
        from pb_telegram import signal
        s=signal(demo_event()); self.st.add_signal(s); init(self.st); self.assertEqual(len(self.st.pending_groups()),0); self.assertEqual(len(self.st.pending()),0)
    def test_group_signal_qualified_only(self):
        s=sample(); self.assertTrue(accept_signal(self.st,s,T)); tg=FakeTG()
        with patch('pb_telegram.now',return_value=T): deliver_groups(self.st,tg,qualified_only=True); deliver_groups(self.st,tg,qualified_only=True)
        self.assertEqual(len(tg.sent),1); self.assertIsNone(tg.sent[0][2]); self.assertEqual(tg.sent[0][0],-123)
    def test_reference_ambiguous(self):
        s=sample(); self.assertEqual(reference_result(s,{'low':26100,'high':26300}), 'BOTH_TOUCHED')
        self.assertTrue(accept_signal(self.st,s,T)); self.st.sent(s['id']); self.st.finish_group(s['id'],'-123','SENT')
        track_reference(self.st,{'bar_open':(B+MINUTE).isoformat(),'high':26300,'low':26100},T+MINUTE)
        tg=FakeTG(); deliver_notices(self.st,tg,T+MINUTE); self.assertEqual(len(tg.sent),2); self.assertIn('sequence unknown',tg.sent[0][1])
    def test_gap_not_profit(self):
        s=sample(); accept_signal(self.st,s,T); self.st.sent(s['id']); track_reference(self.st,{'bar_open':(B+MINUTE*2).isoformat(),'high':26190,'low':26100},T+MINUTE*2)
        self.assertEqual(self.st.db.execute('SELECT state FROM reference_tracks').fetchone()[0],'UNVERIFIED_GAP'); self.assertEqual(self.st.db.execute('SELECT count(*) FROM trades').fetchone()[0],0)
    def test_session_summary_no_duplicate(self):
        q=queue.Queue(); q.put({'kind':'health','state':'BLOCKED: latest closed bars stale','stamp':T.isoformat(),'day':DAY.isoformat()}); drain(self.st,q,T); q.put({'kind':'health','state':'BLOCKED: latest closed bars stale','stamp':T.isoformat(),'day':DAY.isoformat()}); drain(self.st,q,T); self.assertEqual(self.st.db.execute('SELECT count(*) FROM notices').fetchone()[0],3)
        end=T.replace(hour=16,minute=35); q.put({'kind':'health','state':'OUTSIDE AM MONITOR WINDOW','stamp':end.isoformat(),'day':DAY.isoformat()}); drain(self.st,q,end); self.assertEqual(self.st.db.execute('SELECT count(*) FROM notices').fetchone()[0],5)
    def test_reference_window(self):
        s=sample(); accept_signal(self.st,s,T); finish_references(self.st,T.replace(hour=16,minute=35)); self.assertEqual(self.st.db.execute('SELECT state FROM reference_tracks').fetchone()[0],'UNVERIFIED_GAP')

if __name__=='__main__': unittest.main()
