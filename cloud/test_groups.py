import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from pb_telegram import Store, command, demo_event, signal, process_update, deliver_groups, now

class FakeTelegram:
    def __init__(self):
        self.messages=[]; self.calls=[]; self.admins=[{'status':'creator','user':{'id':42}}]; self.fail_chat=None
    def send(self,chat,text,buttons=None):
        if chat==self.fail_chat: raise RuntimeError('Simulated failure')
        self.messages.append((chat,text,buttons))
    def call(self,method,**kw):
        self.calls.append((method,kw)); return self.admins if method=='getChatAdministrators' else True

class GroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'journal.db'; self.st=Store(self.path); self.tg=FakeTelegram()
        self.st.set('chat',42); self.st.set('bot_username','TestBot')
    def tearDown(self): self.st.db.close(); self.tmp.cleanup()
    def update(self,text,uid=42,cid=-123,kind='supergroup',sender_chat=None):
        msg={'chat':{'id':cid,'type':kind,'title':'Friends <group>'},'from':{'id':uid},'text':text}
        if sender_chat: msg['sender_chat']=sender_chat
        process_update(self.st,self.tg,{'message':msg},None)
    def prepare(self):
        self.update('/group',cid=42,kind='private'); return self.st.get('group_code')
    def link(self): self.update('/linkgroup@TestBot '+self.prepare()); self.assertEqual(self.st.get('group_chat'),'-123')
    def sample(self,demo=False,age=0):
        e=demo_event(); e['logged_at_ny']=(now()-timedelta(seconds=age)).isoformat(); s=signal(e,demo); self.st.add_signal(s); return s
    def test_link_and_escape_title(self):
        self.link(); self.assertEqual(self.tg.calls[-1],('getChatAdministrators',{'chat_id':-123})); self.assertIn('&lt;group&gt;',self.tg.messages[-1][1]); self.assertEqual(self.st.get('group_code'),'')
    def test_non_owner_cannot_link_even_with_code(self):
        code=self.prepare(); self.update('/linkgroup@TestBot '+code,uid=43); self.assertIsNone(self.st.get('group_chat')); self.assertEqual(self.tg.calls,[])
    def test_owner_must_be_group_admin(self):
        self.tg.admins=[{'status':'creator','user':{'id':43}}]; self.update('/linkgroup '+self.prepare()); self.assertIsNone(self.st.get('group_chat'))
    def test_expired_code(self):
        code=self.prepare(); self.st.set('group_code_until',int(time.time())-1); self.update('/linkgroup '+code); self.assertIsNone(self.st.get('group_chat'))
    def test_anonymous_sender_denied(self):
        code=self.prepare(); self.update('/linkgroup '+code,sender_chat={'id':-123}); self.assertIsNone(self.st.get('group_chat'))
    def test_wrong_bot_suffix_ignored(self):
        self.update('/linkgroup@OtherBot '+self.prepare()); self.assertIsNone(self.st.get('group_chat'))
    def test_cannot_post_does_not_link(self):
        code=self.prepare(); self.tg.fail_chat=-123; self.update('/linkgroup '+code); self.assertIsNone(self.st.get('group_chat'))
    def test_private_and_group_journal_isolation(self):
        self.link(); self.tg.messages.clear(); sid=self.st.manual('LONG',100,1,95,110)
        for cmd in ['/dashboard','/journal','/groupoff','/manual LONG 100 1 95 110',f'/close {sid} 110 0',f'/note {sid} spam','/groupdemo']:
            self.update(cmd); self.update(cmd,uid=43,cid=43,kind='private')
        self.assertEqual(self.tg.messages,[]); self.assertEqual(self.st.get('group_chat'),'-123'); self.assertEqual(self.st.db.execute('SELECT count(*) FROM trades').fetchone()[0],1); self.assertIsNone(self.st.db.execute('SELECT closed FROM trades').fetchone()[0]); self.assertEqual(self.st.db.execute('SELECT count(*) FROM notes').fetchone()[0],0)
    def test_group_callbacks_ignored(self):
        self.link(); s=self.sample(); self.tg.messages.clear(); process_update(self.st,self.tg,{'callback_query':{'from':{'id':42},'id':'abc','data':'skip:'+s['id'],'message':{'chat':{'id':-123,'type':'group'}}}},None); self.assertEqual(self.st.find(s['id'])[0]['status'],'NEW'); self.assertEqual(self.tg.messages,[])
    def test_groupdemo_no_journal_or_buttons(self):
        self.link(); self.tg.messages.clear(); self.update('/groupdemo',cid=42,kind='private'); self.assertEqual(self.tg.messages[0][0],-123); self.assertIn('DEMO EXAMPLE',self.tg.messages[0][1]); self.assertIsNone(self.tg.messages[0][2]); self.assertEqual(self.st.db.execute('SELECT count(*) FROM signals').fetchone()[0],0)
    def test_private_demo_never_broadcast(self):
        self.link(); self.tg.messages.clear(); self.update('/demo',cid=42,kind='private'); deliver_groups(self.st,self.tg); self.assertEqual([x[0] for x in self.tg.messages],[42]); self.assertEqual(len(self.st.pending_groups()),0)
    def test_new_research_delivery_only_once(self):
        self.sample(); self.link(); s=self.sample(); self.st.add_signal(s); self.tg.messages.clear(); deliver_groups(self.st,self.tg); deliver_groups(self.st,self.tg); self.assertEqual(len(self.tg.messages),1); self.assertEqual(self.tg.messages[0][0],-123); self.assertIsNone(self.tg.messages[0][2]); self.assertIn('RESEARCH ONLY',self.tg.messages[0][1]); self.assertEqual(self.st.find(s['id'])[0]['sent'],0)
    def test_stale_and_future_events_not_delivered(self):
        self.link(); self.sample(age=121); self.sample(age=-5); self.tg.messages.clear(); deliver_groups(self.st,self.tg); self.assertEqual(self.tg.messages,[]); self.assertEqual(len(self.st.pending_groups()),0)
    def test_group_delivery_retry_independent(self):
        self.link(); s=self.sample(); self.st.sent(s['id']); self.tg.messages.clear(); self.tg.fail_chat=-123; deliver_groups(self.st,self.tg); self.assertEqual(len(self.st.pending_groups()),1); self.assertFalse(self.st.pending()); self.tg.fail_chat=None; deliver_groups(self.st,self.tg); self.assertEqual(len(self.tg.messages),1); self.assertEqual(len(self.st.pending_groups()),0)
    def test_unlink_cancels_pending(self):
        self.link(); self.sample(); self.update('/groupoff',cid=42,kind='private'); self.tg.messages.clear(); deliver_groups(self.st,self.tg); self.assertEqual(self.tg.messages,[]); self.assertIsNone(self.st.get('group_chat'))
    def test_replace_does_not_replay_old_pending(self):
        self.link(); self.sample(); self.update('/linkgroup '+self.prepare(),cid=-456); self.tg.messages.clear(); deliver_groups(self.st,self.tg); self.assertEqual(self.tg.messages,[]); self.sample(); deliver_groups(self.st,self.tg); self.assertEqual(self.tg.messages[0][0],-456)
    def test_persistent_link_and_journal(self):
        self.link(); sid=self.st.manual('LONG',100,1,95,110); self.st.db.close(); self.st=Store(self.path); self.assertEqual(self.st.get('group_chat'),'-123'); self.assertIn(sid,self.st.journal()); self.assertEqual(self.st.get('chat'),'42')
    def test_old_database_migration(self):
        import sqlite3
        path=Path(self.tmp.name)/'old.db'; db=sqlite3.connect(path); db.executescript("CREATE TABLE settings(k TEXT PRIMARY KEY,v TEXT NOT NULL); INSERT INTO settings VALUES('chat','42');"); db.commit(); db.close(); old=Store(path); self.assertEqual(old.get('chat'),'42'); self.assertEqual(old.pending_groups(),[]); old.db.close()

if __name__=='__main__': unittest.main()
