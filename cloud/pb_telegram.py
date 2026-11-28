#!/usr/bin/env python3
"""Private local Telegram research alerts and manual MNQ journal. No broker API."""
import argparse
import getpass
import fcntl
import hashlib
import html
import json
import math
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')
POINT_VALUE = 2.0
VERSION = 'complete-research-assistant'
HOME = Path.home() / '.pb_telegram'


def now():
    return datetime.now(timezone.utc)


def ny_day():
    return now().astimezone(NY).date().isoformat()


def safe_text(x, limit=300):
    return str(x or '').strip()[:limit]


def number(x, positive=True):
    v = float(x)
    if not math.isfinite(v) or (positive and v <= 0):
        raise ValueError('Invalid numeric value.')
    return v


def qty_value(x):
    q = int(x)
    if not 1 <= q <= 100:
        raise ValueError('Quantity must be a whole number from 1 to 100.')
    return q


def price(x):
    v = number(x)
    if abs(v * 4 - round(v * 4)) > 1e-7:
        raise ValueError('MNQ prices must be in 0.25-point increments.')
    return v


def levels(direction, entry, sl, tp):
    if direction == 'LONG' and sl < entry < tp:
        return entry - sl, tp - entry
    if direction == 'SHORT' and tp < entry < sl:
        return sl - entry, entry - tp
    raise ValueError('Stop and target must be on the correct sides of entry.')


def alignment(e, rr):
    """Confirmed evidence points, not a win probability. Unknown != failed."""
    n, s, d, r = (e.get(k) if isinstance(e.get(k), dict) else {} for k in ('narrative','structural_leg','discretion','regime'))
    reason = str(e.get('reason',''))
    labels = s.get('evidence') or []
    # Only assert SMT if the actual structural evidence explicitly names it.
    smt = any(re.search(r'\bSMT\b|(?:^|_)SMT(?:_|$)', str(x), re.I) for x in labels)
    direction = e.get('direction')
    ratio = d.get('displacement_ratio')
    checks = [
        ('HTF narrative + DOL',25, bool(n.get('phase') == 'ACTIVE' and n.get('direction') == direction and n.get('condition_respected') is True and n.get('primary_dol') and not n.get('opposing_inversion')) if n else None),
        ('Structural leg evidence',20, bool(s.get('direction') == direction and labels and s.get('gap_ids')) if s else None),
        ('Explicit SMT evidence',15, True if smt else None),
        ('IFVG recorded by engine',15, True if re.search(r'\b\d+[mMhH]_IFVG\b',reason) else None),
        ('Regime + news calendar',10, (False if r.get('blocked') is True else True if r.get('blocked') is False and r.get('calendar_available') is True else None)),
        ('Displacement + discretion',10, (bool(d.get('allowed') is True and float(ratio)>=0.6) if isinstance(ratio,(int,float)) and math.isfinite(ratio) else False if d.get('allowed') is False else None)),
        ('Gross R:R at least 1:1.4',5, rr >= 1.4),
    ]
    points = sum(w for _,w,x in checks if x is True)
    unknown = sum(w for _,w,x in checks if x is None)
    return points, unknown, checks


def signal(event, demo=False):
    direction={'BULLISH':'LONG','BEARISH':'SHORT'}.get(event.get('direction'))
    if direction is None or event.get('event') != 'ORDER_SUBMITTED':
        raise ValueError('Unsupported order event.')
    en, sl, tp = [price(event[k]) for k in ('price','sl','tp')]
    qty = qty_value(event['qty'])
    distance, reward = levels(direction,en,sl,tp)
    stamp = datetime.fromisoformat(event['logged_at_ny'])
    if stamp.tzinfo is None:
        raise ValueError('Missing timestamp offset.')
    sid = hashlib.sha256(json.dumps([event.get('order_id'),stamp.isoformat(),direction,en,sl,tp],separators=(',',':')).encode()).hexdigest()[:10]
    score, unknown, checks = alignment(event,reward/distance)
    model = safe_text(event.get('playbook_model') or str(event.get('reason','')).split('|')[0] or 'Unclassified',80)
    strict = all(event.get(k+'_mode')=='STRICT' for k in ('narrative','model','structure','discretion','regime'))
    return dict(id=sid, direction=direction, entry=en, sl=sl, tp=tp, qty=qty, rr=reward/distance, risk=distance*qty*POINT_VALUE, reward=reward*qty*POINT_VALUE, score=score, unknown=unknown, checks=checks, model=model, stamp=stamp.isoformat(), day=stamp.astimezone(NY).date().isoformat(), strict=strict, order_type=safe_text(event.get('order_type') or 'UNKNOWN',30), demo=demo, reason=safe_text(event.get('reason'),500))


def format_signal(s):
    mark='🟢' if s['direction']=='LONG' else '🔴'
    confirmed=[name for name,_,state in s['checks'] if state is True]
    conf='Confidence (alignment): %d/100 confirmed' % s['score']
    if s['unknown']:
        conf+=' · %d pts unknown' % s['unknown']
    lines=[f"{mark} <b>MNQ {s['direction']} | #{s['id']}</b>",html.escape(s['model'])+' · '+datetime.fromisoformat(s['stamp']).astimezone(NY).strftime('%H:%M NY'),'',f'<b>{conf}</b>', '',f"Entry: <b>{s['entry']:,.2f}</b>",f"Stop loss: <b>{s['sl']:,.2f}</b>",f"Take profit: <b>{s['tp']:,.2f}</b>",f"R:R: 1:{s['rr']:.2f} · Simulated quantity: {s['qty']} MNQ",f"Gross risk/reward: ${s['risk']:.2f} / ${s['reward']:.2f}",'','<b>Recorded confluences</b>']
    lines.extend('✓ '+html.escape(x) for x in confirmed)
    if not confirmed: lines.append('No confirmed evidence recorded.')
    if s.get('qualified') and not s['demo']:
        lines.extend(['','<b>QUALIFIED RESEARCH ALERT · MANUAL REVIEW</b>', 'Closed paired bar: '+datetime.fromisoformat(s['bar_close']).astimezone(NY).strftime('%H:%M:%S NY'), 'Expires: '+datetime.fromisoformat(s['expires_at']).astimezone(NY).strftime('%H:%M:%S NY'), 'Yahoo NQ/ES proxy · broker price and Alpha eligibility unverified.', 'Alignment is not win probability. No broker order.'])
    else:
        lines.extend(['','<b>DEMO EXAMPLE</b>' if s['demo'] else '<b>RESEARCH ONLY · NOT A LIVE ENTRY ALERT</b>', 'Price freshness and Alpha eligibility unverified.','Alignment is not win probability. Manual journal only.'])
    return '\n'.join(lines)


def keyboard(sid):
    return {'inline_keyboard':[[{'text':'✓ Took Trade','callback_data':'take:'+sid},{'text':'Skip Trade','callback_data':'skip:'+sid}],[{'text':'View Analysis','callback_data':'analysis:'+sid},{'text':'Dashboard','callback_data':'dashboard'}]]}


class Store:
    def __init__(self,path):
        self.db=sqlite3.connect(path)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY,v TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS signals(id TEXT PRIMARY KEY, day TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'NEW', sent INTEGER NOT NULL DEFAULT 0, demo INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS trades(id TEXT PRIMARY KEY, direction TEXT NOT NULL, entry REAL NOT NULL, sl REAL NOT NULL,tp REAL NOT NULL, qty INTEGER NOT NULL, opened TEXT NOT NULL, closed TEXT, exit REAL, fees REAL, net REAL, r REAL, demo INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, trade_id TEXT NOT NULL, stamp TEXT NOT NULL, note TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS group_deliveries(signal_id TEXT NOT NULL, chat_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING', PRIMARY KEY(signal_id,chat_id));
        ''')
    def get(self,k,default=None):
        row=self.db.execute('SELECT v FROM settings WHERE k=?',(k,)).fetchone()
        return row[0] if row else default
    def set(self,k,v):
        with self.db: self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(k,str(v)))
    def add_signal(self,s):
        with self.db:
            c=self.db.execute('INSERT OR IGNORE INTO signals(id,day,payload,demo) VALUES(?,?,?,?)',(s['id'],s['day'],json.dumps(s),int(s['demo'])))
            added=c.rowcount==1
            group=self.get('group_chat')
            if added and group and not s['demo']:
                self.db.execute('INSERT OR IGNORE INTO group_deliveries(signal_id,chat_id) VALUES(?,?)',(s['id'],group))
        return added
    def link_group(self,cid,title):
        with self.db:
            self.db.execute("UPDATE group_deliveries SET state='CANCELLED' WHERE state='PENDING'")
            self.db.executemany('INSERT OR REPLACE INTO settings VALUES(?,?)',[('group_chat',str(cid)),('group_title',title),('group_code',''),('group_code_until','0')])
    def unlink_group(self):
        with self.db:
            self.db.execute("UPDATE group_deliveries SET state='CANCELLED' WHERE state='PENDING'")
            self.db.execute("DELETE FROM settings WHERE k IN ('group_chat','group_title','group_code','group_code_until')")
    def pending_groups(self):
        return self.db.execute("SELECT g.signal_id,g.chat_id,s.payload FROM group_deliveries g JOIN signals s ON s.id=g.signal_id WHERE g.state='PENDING' AND g.chat_id=? ORDER BY s.rowid",(self.get('group_chat'),)).fetchall()
    def finish_group(self,sid,cid,state):
        with self.db: self.db.execute('UPDATE group_deliveries SET state=? WHERE signal_id=? AND chat_id=?',(state,sid,cid))
    def pending(self):
        return self.db.execute('SELECT * FROM signals WHERE sent=0 ORDER BY rowid').fetchall()
    def sent(self,sid):
        with self.db: self.db.execute('UPDATE signals SET sent=1 WHERE id=?',(sid,))
    def expire(self,sid):
        with self.db: self.db.execute("UPDATE signals SET sent=1,status='EXPIRED' WHERE id=? AND status='NEW'",(sid,))
    def find(self,sid):
        row=self.db.execute('SELECT * FROM signals WHERE id=?',(sid,)).fetchone()
        if not row: raise ValueError('Unknown signal ID.')
        return row,json.loads(row['payload'])
    def skip(self,sid):
        row,_=self.find(sid)
        if row['status']=='TAKEN': raise ValueError('Trade already recorded.')
        with self.db: self.db.execute("UPDATE signals SET status='SKIPPED' WHERE id=?",(sid,))
    def take(self,sid,entry,qty,sl,tp):
        row,s=self.find(sid)
        if row['status']=='TAKEN': raise ValueError('Trade already recorded.')
        entry,sl,tp=map(price,(entry,sl,tp)); qty=qty_value(qty)
        levels(s['direction'],entry,sl,tp)
        with self.db:
            self.db.execute('INSERT INTO trades(id,direction,entry,sl,tp,qty,opened,demo) VALUES(?,?,?,?,?,?,?,?)',(sid,s['direction'],entry,sl,tp,qty,now().astimezone(NY).isoformat(),row['demo']))
            self.db.execute("UPDATE signals SET status='TAKEN' WHERE id=?",(sid,))
    def manual(self,direction,entry,qty,sl,tp):
        direction=direction.upper()
        entry,sl,tp=map(price,(entry,sl,tp)); qty=qty_value(qty)
        levels(direction,entry,sl,tp)
        sid='m'+secrets.token_hex(4)
        with self.db: self.db.execute('INSERT INTO trades(id,direction,entry,sl,tp,qty,opened) VALUES(?,?,?,?,?,?,?)',(sid,direction,entry,sl,tp,qty,now().astimezone(NY).isoformat()))
        return sid
    def close(self,sid,exit_price,fees):
        exit_price=price(exit_price); fees=number(fees,positive=False)
        if fees<0: raise ValueError('Fees must be zero or positive.')
        t=self.db.execute('SELECT * FROM trades WHERE id=?',(sid,)).fetchone()
        if not t or t['closed']: raise ValueError('No open trade with that ID.')
        gross=(exit_price-t['entry'])*(1 if t['direction']=='LONG' else -1)*t['qty']*POINT_VALUE
        net=gross-fees; initial_risk=abs(t['entry']-t['sl'])*t['qty']*POINT_VALUE
        r=net/initial_risk
        with self.db: self.db.execute('UPDATE trades SET closed=?,exit=?,fees=?,net=?,r=? WHERE id=?',(now().astimezone(NY).isoformat(),exit_price,fees,net,r,sid))
        return net,r
    def note(self,sid,text):
        if not self.db.execute('SELECT 1 FROM trades WHERE id=?',(sid,)).fetchone(): raise ValueError('Unknown trade ID.')
        if not text.strip(): raise ValueError('Note cannot be empty.')
        with self.db: self.db.execute('INSERT INTO notes(trade_id,stamp,note) VALUES(?,?,?)',(sid,now().isoformat(),text[:1500]))
    def dashboard(self,day=None):
        day=day or ny_day()
        signals=self.db.execute('SELECT status,COUNT(*) AS n FROM signals WHERE demo=0 AND day=? GROUP BY status',(day,)).fetchall()
        n={x['status']:x['n'] for x in signals}
        closed=self.db.execute('SELECT net,r FROM trades WHERE demo=0 AND substr(closed,1,10)=?',(day,)).fetchall()
        opened=self.db.execute('SELECT COUNT(*) FROM trades WHERE demo=0 AND closed IS NULL').fetchone()[0]
        wins=sum(t['net']>0 for t in closed); net=sum(t['net'] for t in closed); r=sum(t['r'] for t in closed)
        return f'📊 <b>PB DASHBOARD</b>\n{day} · NY session date\n\nSignals: {sum(n.values())}\nTaken: {n.get("TAKEN",0)} · Skipped: {n.get("SKIPPED",0)} · Unreviewed: {n.get("NEW",0)} · Expired: {n.get("EXPIRED",0)}\nOpen journal trades: {opened}\nClosed today: {len(closed)}\n\nRecorded net P&amp;L: <b>${net:+,.2f}</b>\nTotal R: {r:+.2f}R\nWin rate: {f"{wins/len(closed):.0%}" if closed else "N/A"}\n\nManual records only · excludes test examples.\nNot an Alpha account balance or risk-limit monitor.'
    def trade_detail(self,sid):
        t=self.db.execute('SELECT * FROM trades WHERE id=?',(sid,)).fetchone()
        if not t: raise ValueError('Unknown trade ID.')
        state='OPEN' if not t['closed'] else f'Closed: {t["exit"]:,.2f} · net ${t["net"]:+.2f} · {t["r"]:+.2f}R'
        notes=self.db.execute('SELECT note FROM notes WHERE trade_id=? ORDER BY id DESC LIMIT 5',(sid,)).fetchall()
        detail=f'<b>Trade #{html.escape(sid)}</b>\n{t["direction"]} · {t["qty"]} MNQ · {state}\nEntry: {t["entry"]:,.2f}\nInitial SL: {t["sl"]:,.2f} · TP: {t["tp"]:,.2f}\nRecorded entry time: {t["opened"]}\n'+('TEST EXAMPLE\n' if t['demo'] else '')
        detail+='\n<b>Recent notes</b>\n'+('\n'.join('• '+html.escape(x['note'][:300]) for x in notes) if notes else 'None yet.')
        return detail
    def journal(self):
        rows=self.db.execute('SELECT * FROM trades ORDER BY opened DESC LIMIT 12').fetchall()
        if not rows: return 'Journal is empty. Use /take after a signal, or /manual for an independent MNQ trade.'
        return '<b>Recent journal records</b>\n'+ '\n'.join(f"#{t['id']} · {t['direction']} {t['qty']} MNQ · {('TEST · ' if t['demo'] else '')}{'OPEN' if not t['closed'] else f'{t["net"]:+.2f} USD / {t["r"]:+.2f}R'}" for t in rows)


class Telegram:
    def __init__(self,token): self.base='https://api.telegram.org/bot'+token+'/'
    def call(self,method,**params):
        # Do not surface urllib exception URLs (they contain the token).
        try:
            req=Request(self.base+method,data=json.dumps(params).encode(),headers={'Content-Type':'application/json'})
            with urlopen(req,timeout=40) as f: data=json.load(f)
        except (URLError,OSError,ValueError): raise RuntimeError('Telegram API connection failed; check token/network.') from None
        if not data.get('ok'): raise RuntimeError('Telegram API request rejected.')
        return data['result']
    def send(self,chat,text,buttons=None):
        params=dict(chat_id=chat,text=text,parse_mode='HTML')
        if buttons: params['reply_markup']=buttons
        return self.call('sendMessage',**params)


HELP='''<b>PB Trading Assistant</b>\nResearch only. No broker connection.\n\n/dashboard — today’s dashboard\n/journal — recent trades\n/trade ID — trade details and notes\n/demo — sample alert (excluded from stats)\n/take ID ENTRY QTY SL TP — record actual manual entry\n/skip ID — mark skipped\n/close ID EXIT TOTAL_FEES — record full closure\n/manual LONG|SHORT ENTRY QTY SL TP — independent MNQ entry\n/note ID TEXT — journal note\n/status — local service status\n/group — link or replace a signal group\n/groupdemo — send a labeled test to linked group\n/groupoff — disconnect group\n/help — commands\n\nPrices must be on 0.25 ticks. Record actual fees in USD. Partial exits not supported.'''


def demo_event():
    return dict(event='ORDER_SUBMITTED',order_id='demo-'+secrets.token_hex(4),logged_at_ny=now().astimezone(NY).isoformat(),direction='BEARISH',qty=1,price=26180,sl=26200,tp=26130,reason='MAC|1m_IFVG',playbook_model='MAC Reversal',narrative=dict(phase='ACTIVE',direction='BEARISH',condition_respected=True,primary_dol={'price':26130},opposing_inversion=False),structural_leg=dict(direction='BEARISH',evidence=['EXTERNAL_SWEEP','SMT'],gap_ids=['example']),regime=dict(blocked=False,calendar_available=False),discretion=dict(allowed=True,displacement_ratio=0.65),order_type='MARKET')


def command(st,chat,tg,text):
    parts=text.strip().split(); cmd=parts[0].split('@')[0].lower() if parts else ''
    if cmd in ('/start','/help'): return tg.send(chat,HELP)
    if cmd=='/dashboard':
        from pb_service import health_text
        return tg.send(chat,st.dashboard()+'\n\n'+html.escape(health_text(st)))
    if cmd=='/journal': return tg.send(chat,st.journal())
    if cmd=='/trade' and len(parts)==2: return tg.send(chat,st.trade_detail(parts[1]))
    if cmd=='/status':
        from pb_service import health_text
        return tg.send(chat,'Local Telegram service online · research only.\n'+html.escape(health_text(st))+'\nGroup: '+(html.escape(st.get('group_title','Linked')) if st.get('group_chat') else 'Not linked'))
    if cmd=='/group':
        code=secrets.token_hex(8)
        st.set('group_code',code); st.set('group_code_until',str(int(time.time())+600))
        name=st.get('bot_username')
        suffix='@'+name if name else ''
        active=html.escape(st.get('group_title','Linked')) if st.get('group_chat') else 'None'
        return tg.send(chat,f'<b>Group broadcasting</b>\nCurrent group: {active}\nAdd me to your group, then as a group administrator send this command there within 10 minutes:\n<code>/linkgroup{suffix} {code}</code>\nOnly your paired account can link a group. Linking replaces the previous destination. Journal and dashboard stay private.\n/groupdemo tests delivery; /groupoff disconnects.')
    if cmd=='/groupoff':
        st.unlink_group(); return tg.send(chat,'Group broadcasting disconnected. Your journal is unchanged.')
    if cmd=='/groupdemo':
        group=st.get('group_chat')
        if not group: raise ValueError('No group linked. Use /group first.')
        s=signal(demo_event(),True)
        tg.send(int(group),format_signal(s))
        return tg.send(chat,'Labeled demo example sent to '+html.escape(st.get('group_title','group'))+'. No journal entry created.')
    if cmd=='/demo':
        s=signal(demo_event(),True); st.add_signal(s); tg.send(chat,format_signal(s),keyboard(s['id'])); st.sent(s['id']); return
    if cmd=='/take' and len(parts)==6:
        st.take(*parts[1:]); return tg.send(chat,'Recorded manual entry #'+html.escape(parts[1])+'. No broker order sent.\nUse /close ID EXIT TOTAL_FEES after full closure.')
    if cmd=='/skip' and len(parts)==2: st.skip(parts[1]); return tg.send(chat,'Marked skipped.')
    if cmd=='/close' and len(parts)==4:
        net,r=st.close(*parts[1:]); return tg.send(chat,f'Closed journal record. Net: ${net:+.2f} · {r:+.2f}R. No broker order sent.')
    if cmd=='/manual' and len(parts)==6:
        sid=st.manual(*parts[1:]); return tg.send(chat,'Recorded independent trade #'+sid+'. No broker order sent.')
    if cmd=='/note' and len(parts)>=3:
        st.note(parts[1],text.split(maxsplit=2)[2]); return tg.send(chat,'Note saved locally.')
    return tg.send(chat,HELP)


def callback(st,tg,chat,data):
    if data=='dashboard': return tg.send(chat,st.dashboard())
    action,sep,sid=data.partition(':')
    if not sep: raise ValueError('Unknown action.')
    _,s=st.find(sid)
    if action=='take': return tg.send(chat,f'Confirm actual entry (not an order):\n<code>/take {sid} ENTRY QTY SL TP</code>\nExample using suggested values:\n<code>/take {sid} {s["entry"]:.2f} {s["qty"]} {s["sl"]:.2f} {s["tp"]:.2f}</code>\nEdit to reflect your actual fill and initial stop/target.')
    if action=='skip': st.skip(sid); return tg.send(chat,'Marked skipped. No broker action.')
    if action=='analysis':
        detail='\n'.join(('✓' if x is True else '✗' if x is False else '?')+' '+html.escape(n)+f' ({w} pts)' for n,w,x in s['checks'])
        return tg.send(chat,f'<b>#{sid} Evidence breakdown</b>\n{detail}\n\n{html.escape(s["reason"])}\n\nEngine strict: {s["strict"]}. Research weights are uncalibrated; missing evidence is unknown. Not trade approval.')
    raise ValueError('Unknown action.')


class Tail:
    def __init__(self,path): self.path=path; self.inode=None; self.offset=0; self.started=False
    def read(self):
        try: stat=self.path.stat()
        except FileNotFoundError:
            self.started=True
            return []
        ident=(stat.st_dev,stat.st_ino)
        if not self.started:
            self.started=True; self.inode=ident
            # Startup deliberately ignores older events. Start assistant before paper runner.
            self.offset=stat.st_size
            return []
        if self.inode!=ident or stat.st_size<self.offset: self.inode=ident; self.offset=0
        records=[]
        with self.path.open('rb') as f:
            f.seek(self.offset)
            while True:
                line=f.readline()
                if not line.endswith(b'\n'): break
                self.offset=f.tell()
                try: records.append(json.loads(line))
                except (ValueError,UnicodeError): print('Skipped invalid log record.',flush=True)
        return records


def init_home():
    HOME.mkdir(mode=0o700,parents=True,exist_ok=True)
    HOME.chmod(0o700)


def setup():
    init_home()
    token=getpass.getpass('Paste BotFather token (hidden): ').strip()
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}',token): raise SystemExit('Token format invalid.')
    user=Telegram(token).call('getMe')
    # This is a single-user assistant, with local long polling.
    file=HOME/'token'
    fd=os.open(file,os.O_CREAT|os.O_WRONLY|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f: f.write(token+'\n')
    file.chmod(0o600)
    print('Connected to @'+user['username']+'. Token saved in private local folder, not GitHub.')


def process_update(st,tg,u,pair):
    """Only the paired private chat can journal or configure broadcasting."""
    cb=u.get('callback_query'); msg=cb.get('message',{}) if cb else u.get('message',{})
    chat=msg.get('chat',{}); actor=(cb if cb else msg).get('from',{})
    cid=chat.get('id'); uid=actor.get('id'); text=msg.get('text','')
    if not isinstance(text,str): text=''
    if chat.get('type')=='private' and isinstance(cid,int) and cid==uid:
        if pair and not st.get('chat') and not cb and text.strip()=='/pair '+pair:
            st.set('chat',cid); pair=None; tg.send(cid,'Paired privately. Use /demo and /dashboard.\n'+HELP)
        elif st.get('chat')==str(cid):
            try:
                if cb:
                    tg.call('answerCallbackQuery',callback_query_id=cb['id']); callback(st,tg,cid,cb.get('data',''))
                else: command(st,cid,tg,text)
            except (ValueError,KeyError,TypeError,OverflowError,sqlite3.IntegrityError) as exc:
                tg.send(cid,'Could not record: '+html.escape(safe_text(exc,250)))
    elif chat.get('type') in ('group','supergroup') and not cb and not msg.get('sender_chat') and isinstance(uid,int) and st.get('chat')==str(uid):
        parts=text.split()
        name=st.get('bot_username','').lower()
        if len(parts)==2 and parts[0].lower() in ('/linkgroup','/linkgroup@'+name):
            code=st.get('group_code','')
            if not code or not secrets.compare_digest(code.encode(),parts[1].encode()) or time.time()>int(st.get('group_code_until','0')):
                tg.send(uid,'Group link code invalid or expired. Send /group privately for a new code.')
            else:
                # An identified, paired owner must also be an administrator of this group.
                try:
                    admins=tg.call('getChatAdministrators',chat_id=cid)
                    allowed=any(a.get('user',{}).get('id')==uid and a.get('status') in ('creator','administrator') for a in admins)
                except RuntimeError:
                    allowed=False
                if not allowed:
                    tg.send(uid,'Could not verify that you administer this group. Use a group you administer and send /group again.')
                else:
                    title=safe_text(chat.get('title') or 'Telegram group',120)
                    # Test posting before persisting the destination. No private data is sent.
                    try:
                        tg.send(cid,'<b>PB Trading Assistant connected.</b> Research signals only. Personal journal and dashboard are not shared here.')
                    except RuntimeError:
                        tg.send(uid,'Cannot post to this group. Check the bot can send messages, then retry the link.')
                    else:
                        st.link_group(cid,title)
                        tg.send(uid,'Group linked: '+html.escape(title)+'. Send /groupdemo here to test.')
    # All other group messages and callbacks are ignored, including journal commands.
    return pair


def deliver_groups(st,tg,qualified_only=False,calendar_path=None):
    """Group queue independent of private delivery; never replay old signals."""
    for row in st.pending_groups():
        s=json.loads(row['payload']); cid=row['chat_id']; sid=row['signal_id']
        age=(now()-datetime.fromisoformat(s['stamp'])).total_seconds()
        if qualified_only:
            from pb_market import delivery_valid
            valid=delivery_valid(s,now(),calendar_path)
            ref=st.db.execute("SELECT state FROM reference_tracks WHERE id=?",(sid,)).fetchone()
            if ref and ref[0]!="OPEN": valid=False
        else: valid=not s['demo'] and 0<=age<=120
        if not valid:
            st.finish_group(sid,cid,'EXPIRED'); continue
        try:
            # No buttons or private journal/dashboard information in group posts.
            tg.send(int(cid),format_signal(s))
        except RuntimeError:
            print('Group delivery failed; will retry while signal is fresh.',flush=True)
            break
        st.finish_group(sid,cid,'SENT')


def run(log=None, token=None):
    init_home()
    # The OS releases this lock on shutdown. Prevent competing getUpdates consumers.
    lock=os.open(HOME/'service.lock',os.O_CREAT|os.O_RDWR,0o600)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock); raise SystemExit('PB assistant already running. Stop the old Terminal instance first.') from None
    try:
        run_locked(log, token=token)
    finally:
        os.close(lock)


def run_locked(log, token=None):
    token=token if token is not None else (HOME/'token').read_text().strip()
    tg=Telegram(token); st=Store(HOME/'journal.sqlite3')
    # Restrict journal to local account even when system umask is permissive.
    (HOME/'journal.sqlite3').chmod(0o600)
    identity=tg.call('getMe'); botid=str(identity['id'])
    if st.get('botid') not in (None,botid): raise SystemExit('Token belongs to a different bot; use a separate journal profile.')
    st.set('botid',botid); st.set('bot_username',identity['username'])
    pair=secrets.token_hex(4) if not st.get('chat') else None
    if pair: print('Open private chat @'+identity['username']+' and send /pair '+pair,flush=True)
    else: print('PB assistant '+VERSION+' online. Send /group or /dashboard in Telegram.',flush=True)
    if log: raise SystemExit('Legacy unqualified log forwarding is disabled; use the integrated monitor.')
    from pb_market import start_monitor, delivery_valid
    from pb_service import init as init_service, drain, deliver_notices
    init_service(st)
    q,stop,worker=start_monitor(HOME/'calendar_review.json')
    while True:
        try:
            drain(st,q)
            if st.get('chat'):
                for row in st.pending():
                    s=json.loads(row['payload']); age=(now()-datetime.fromisoformat(s['stamp'])).total_seconds()
                    if not s['demo']:
                        ref=st.db.execute('SELECT state FROM reference_tracks WHERE id=?',(s['id'],)).fetchone()
                        if not delivery_valid(s,now(),HOME/'calendar_review.json') or (ref and ref[0]!='OPEN'):
                            st.expire(s['id']); continue
                    try:
                        tg.send(int(st.get('chat')),format_signal(s),keyboard(s['id'])); st.sent(s['id'])
                    except RuntimeError:
                        print('Private delivery failed; will retry while signal is fresh.',flush=True); break
                deliver_groups(st,tg,qualified_only=True,calendar_path=HOME/'calendar_review.json')
                deliver_notices(st,tg)
            updates=tg.call('getUpdates',offset=int(st.get('offset','0')),timeout=5,allowed_updates=['message','callback_query'])
            for u in updates:
                try:
                    pair=process_update(st,tg,u,pair)
                except RuntimeError:
                    # Consume failed commands to avoid replaying a journal mutation.
                    print('Telegram command response failed; check /status or /journal before retrying.',flush=True)
                st.set('offset',u['update_id']+1)
        except RuntimeError as exc:
            print(str(exc),flush=True); time.sleep(5)


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--setup',action='store_true'); p.add_argument('--log',type=Path,help=argparse.SUPPRESS); args=p.parse_args()
    if args.setup:
        try: setup()
        except RuntimeError as exc: raise SystemExit(str(exc)) from None
    else:
        try: run(args.log)
        except FileNotFoundError: raise SystemExit('Run python3 pb_telegram.py --setup first.') from None
        except KeyboardInterrupt: print('\nStopped. Journal retained locally.')
        except RuntimeError as exc: raise SystemExit(str(exc)) from None

if __name__=='__main__': main()
