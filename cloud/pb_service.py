"""Runtime coordination, private status and durable reference updates."""
import html
import json
import queue
from datetime import timedelta, time
from pb_telegram import now, NY
from pb_market import delivery_valid, aware, MINUTE


def init(st):
    st.db.executescript('''
      CREATE TABLE IF NOT EXISTS reference_tracks(id TEXT PRIMARY KEY, last_bar TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'OPEN');
      CREATE TABLE IF NOT EXISTS notices(id TEXT PRIMARY KEY, chat TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL, created TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING');
    ''')
    # Older assistant releases allowed arbitrary paper-log alerts. Retain all journal history, but never send legacy pending items.
    for row in st.pending():
        s=json.loads(row['payload'])
        if not s.get('demo') and not s.get('qualified'): st.expire(s['id'])
    with st.db:
        for row in st.pending_groups():
            if not json.loads(row['payload']).get('qualified'):
                st.db.execute("UPDATE group_deliveries SET state='EXPIRED' WHERE signal_id=? AND chat_id=?",(row['signal_id'],row['chat_id']))


def enqueue(st,key,chat,kind,message,t=None):
    if not chat: return
    t=t or now()
    with st.db:
        st.db.execute('INSERT OR IGNORE INTO notices(id,chat,kind,message,created) VALUES(?,?,?,?,?)',(key+':'+str(chat),str(chat),kind,message,t.isoformat()))


def notify(st,key,kind,message,group=False,t=None):
    enqueue(st,key,st.get('chat'),kind,message,t)
    if group: enqueue(st,key,st.get('group_chat'),kind,message,t)


def deliver_notices(st,tg,t=None):
    t=t or now()
    for row in st.db.execute("SELECT * FROM notices WHERE state='PENDING' ORDER BY rowid").fetchall():
        if row['chat'] not in (st.get('chat'),st.get('group_chat')) or not timedelta(0)<=t-aware(row['created'])<=timedelta(hours=2):
            state='EXPIRED'
        else:
            try: tg.send(int(row['chat']),row['message']); state='SENT'
            except RuntimeError: continue
        with st.db: st.db.execute('UPDATE notices SET state=? WHERE id=?',(state,row['id']))


def accept_signal(st,s,t=None):
    t=t or now()
    if not delivery_valid(s,t): return False
    # Enforce daily cap across service restarts; past examples and legacy research do not count.
    rows=st.db.execute('SELECT payload FROM signals WHERE day=? AND demo=0',(s['day'],)).fetchall()
    count=sum(json.loads(r[0]).get('qualified') is True for r in rows)
    if count>=2: return False
    if not st.add_signal(s): return False
    with st.db: st.db.execute('INSERT OR IGNORE INTO reference_tracks(id,last_bar) VALUES(?,?)',(s['id'],s['bar_open']))
    return True


def reference_result(s,b):
    if s['direction']=='LONG': sl=b['low']<=s['sl']; tp=b['high']>=s['tp']
    else: sl=b['high']>=s['sl']; tp=b['low']<=s['tp']
    return 'BOTH_TOUCHED' if sl and tp else 'STOP_LEVEL_TOUCHED' if sl else 'TARGET_LEVEL_TOUCHED' if tp else None


def close_reference(st,row,s,state,t):
    labels={'BOTH_TOUCHED':'Both reference levels touched within one minute; sequence unknown.', 'STOP_LEVEL_TOUCHED':'Reference stop level touched.', 'TARGET_LEVEL_TOUCHED':'Reference target level touched.', 'UNVERIFIED_GAP':'Reference tracking ended: missing or unobserved minute(s). Outcome unknown.', 'WINDOW_END':'Reference monitoring window ended; no verified terminal level touch.', 'SETUP_INVALIDATED':'Original analysis no longer supported by current narrative or regime; reference tracking ended.'}
    msg=f'<b>MNQ reference update | #{html.escape(s["id"])}</b>\n{labels[state]}\nYahoo NQ proxy · not a broker fill or an instruction to manage a position.'
    with st.db:
        st.db.execute('UPDATE reference_tracks SET state=? WHERE id=?',(state,s['id']))
        # Only send follow-ups to destinations that received the original alert.
        original=st.db.execute('SELECT sent,status FROM signals WHERE id=?',(s['id'],)).fetchone()
        if original and original['sent'] and original['status']!='EXPIRED':
            st.db.execute('INSERT OR IGNORE INTO notices(id,chat,kind,message,created) SELECT ?,v,?,?,? FROM settings WHERE k=\'chat\'',('ref:'+s['id']+':private','reference',msg,t.isoformat()))
        gc=st.get('group_chat')
        if gc and st.db.execute("SELECT 1 FROM group_deliveries WHERE signal_id=? AND chat_id=? AND state='SENT'",(s['id'],gc)).fetchone():
            st.db.execute('INSERT OR IGNORE INTO notices(id,chat,kind,message,created) VALUES(?,?,?,?,?)',('ref:'+s['id']+':'+gc,gc,'reference',msg,t.isoformat()))


def track_reference(st,b,t=None):
    t=t or now(); k=aware(b['bar_open'])
    for row in st.db.execute("SELECT r.*,s.payload FROM reference_tracks r JOIN signals s ON r.id=s.id WHERE r.state='OPEN'").fetchall():
        s=json.loads(row['payload']); last=aware(row['last_bar'])
        if k<=last: continue
        if k.astimezone(NY).date().isoformat()!=s['day'] or k-last!=MINUTE:
            state='UNVERIFIED_GAP'
        elif (t-(k+MINUTE)).total_seconds()>90:
            state='UNVERIFIED_GAP'
        else:
            state=reference_result(s,b)
            if not state and (b.get('regime_blocked') is True or (s.get('narrative_id') and 'narrative_id' in b and (b.get('narrative_id')!=s['narrative_id'] or b.get('narrative_phase') not in ('ACTIVE',)))):
                state='SETUP_INVALIDATED'
        if state: close_reference(st,row,s,state,t)
        else:
            with st.db: st.db.execute('UPDATE reference_tracks SET last_bar=? WHERE id=?',(k.isoformat(),s['id']))


def finish_references(st,t):
    local=t.astimezone(NY)
    for row in st.db.execute("SELECT r.*,s.payload FROM reference_tracks r JOIN signals s ON r.id=s.id WHERE r.state='OPEN'").fetchall():
        s=json.loads(row['payload']); last=aware(row['last_bar'])
        if s['day']<local.date().isoformat() or (s['day']==local.date().isoformat() and local.time()>=time(11,35)):
            recent=local.date().isoformat()==s['day'] and t-last<=timedelta(minutes=3)
            close_reference(st,row,s,'WINDOW_END' if recent else 'UNVERIFIED_GAP',t)


def health_text(st):
    raw=st.get('market_health')
    if not raw: return 'Market monitor: starting / not yet checked.'
    try:
        h=json.loads(raw); age=(now()-aware(h['stamp'])).total_seconds()
        text='Market monitor: '+h['state']
        if age>120: text+=' · heartbeat stale'
        if h.get('last_closed'): text+='\nLast paired closed bar: '+aware(h['last_closed']).astimezone(NY).strftime('%H:%M:%S NY')
        if h.get('review'): text+='\n'+h['review']
        if 'rejections' in h: text+='\nEngine rejections (incl. warmup): '+str(h['rejections'])
        return text
    except (ValueError,KeyError,TypeError): return 'Market monitor: status unavailable.'


def drain(st,q,t=None):
    t=t or now()
    while True:
        try: e=q.get_nowait()
        except queue.Empty: break
        kind=e['kind']
        if kind=='signal': accept_signal(st,e['signal'],t)
        elif kind=='reference': track_reference(st,e,t)
        elif kind=='health':
            st.set('market_health',json.dumps(e)); day=e.get('day',t.astimezone(NY).date().isoformat()); state=e['state']
            local=t.astimezone(NY)
            if time(8,30)<=local.time()<time(11,35) and local.weekday()<5 and not state.startswith('CLOSED'):
                st.set('monitor_day',day)
                # Send one concise startup notification, not repeated data warnings to friends.
                notify(st,'start:'+day,'session','<b>PB AM monitor started</b>\nResearch-only NQ/ES analysis. Qualified alerts require fresh paired data and reviewed calendar.',True,t)
                if state.startswith('BLOCKED') or 'calendar locked' in state:
                    notify(st,'health:'+day+':'+state,'health','⚠️ '+html.escape(state)+'\nNo qualified alerts while checks fail.',False,t)
            if st.get('monitor_day')==day and local.time()>=time(11,35):
                rows=st.db.execute('SELECT payload FROM signals WHERE day=? AND demo=0',(day,)).fetchall()
                n=sum(json.loads(r[0]).get('qualified') is True for r in rows)
                notify(st,'summary:'+day,'session',f'<b>PB AM session ended</b>\nQualified research alerts: {n}\nZero alerts may reflect missing data, calendar lock or no eligible setup. No broker trades inferred.',True,t)
    finish_references(st,t)
