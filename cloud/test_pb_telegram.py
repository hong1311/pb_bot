import json
import tempfile
import unittest
from pathlib import Path
from pb_telegram import Store, Tail, demo_event, signal, format_signal, alignment, levels, price, qty_value, command, callback


class FakeTelegram:
    def __init__(self): self.messages=[]
    def send(self,*args): self.messages.append(args)


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)
        self.st=Store(self.path/'journal.db')
    def tearDown(self): self.st.db.close(); self.tmp.cleanup()
    def example(self,demo=False):
        s=signal(demo_event(),demo); self.st.add_signal(s); return s
    def test_signal_calculation(self):
        s=self.example(); self.assertEqual(s['direction'],'SHORT'); self.assertEqual(s['risk'],40); self.assertEqual(s['reward'],100); self.assertEqual(s['rr'],2.5)
    def test_confidence_unknown(self):
        s=self.example(); self.assertEqual(s['score'],90); self.assertEqual(s['unknown'],10); self.assertFalse(s['strict'])
    def test_unknown_evidence(self):
        e=demo_event(); e.pop('narrative'); e.pop('structural_leg'); s=signal(e); self.assertEqual(s['score'],30); self.assertEqual(s['unknown'],70)
    def test_invalid_prices(self):
        for p in ['nan','inf','0','-5','1.1']:
            with self.assertRaises(ValueError): price(p)
        with self.assertRaises(ValueError): levels('LONG',100,101,102)
    def test_duplicate_signal(self):
        s=self.example(); self.assertFalse(self.st.add_signal(s))
    def test_manual_close(self):
        s=self.example(); sid=s['id']; self.st.take(sid,26180,1,26200,26130); self.assertEqual(self.st.close(sid,26130,2),(98,2.45)); self.assertIn('$+98.00',self.st.dashboard())
    def test_long_loss(self):
        sid=self.st.manual('long',100,2,95,110); self.assertEqual(self.st.close(sid,95,2),(-22,-1.1))
    def test_double_entry_and_close(self):
        s=self.example(); sid=s['id']; self.st.take(sid,26180,1,26200,26130)
        with self.assertRaises(ValueError): self.st.take(sid,26180,1,26200,26130)
        self.st.close(sid,26130,2)
        with self.assertRaises(ValueError): self.st.close(sid,26130,2)
    def test_test_trade_excluded(self):
        s=self.example(True); self.st.take(s['id'],26180,1,26200,26130); self.st.close(s['id'],26130,0); self.assertIn('Signals: 0',self.st.dashboard()); self.assertIn('Closed today: 0',self.st.dashboard())
    def test_persistence(self):
        s=self.example(); self.st.take(s['id'],26180,1,26200,26130); self.st.note(s['id'],'reviewed'); self.st.db.close(); self.st=Store(self.path/'journal.db'); self.assertIn('OPEN',self.st.journal()); self.assertEqual(self.st.db.execute('select note from notes').fetchone()[0],'reviewed')
    def test_tail_existing_and_partial(self):
        f=self.path/'events'; f.write_text('{"old":1}\n'); t=Tail(f); self.assertEqual(t.read(),[])
        with f.open('a') as h: h.write('{"new":2}')
        self.assertEqual(t.read(),[])
        with f.open('a') as h: h.write('\n')
        self.assertEqual(t.read(),[{'new':2}]); self.assertEqual(t.read(),[])
    def test_tail_new_file(self):
        f=self.path/'new'; t=Tail(f); self.assertEqual(t.read(),[]); f.write_text('{"first":1}\n'); self.assertEqual(t.read(),[{'first':1}])
    def test_skip(self):
        s=self.example(); self.st.skip(s['id']); self.assertIn('Skipped: 1',self.st.dashboard())
    def test_buttons_no_execution(self):
        s=self.example(); tg=FakeTelegram(); callback(self.st,tg,1,'take:'+s['id']); self.assertIn('/take',tg.messages[0][1]); self.assertEqual(self.st.db.execute('select count(*) from trades').fetchone()[0],0)
    def test_demo_command(self):
        tg=FakeTelegram(); command(self.st,1,tg,'/demo'); self.assertIn('DEMO EXAMPLE',tg.messages[0][1]); self.assertEqual(self.st.db.execute('select count(*) from signals where demo=1').fetchone()[0],1)
    def test_escape_html(self):
        e=demo_event(); e['playbook_model']='<tag>'; self.assertIn('&lt;tag&gt;',format_signal(signal(e)))
    def test_expired(self):
        s=self.example(); self.st.expire(s['id']); self.assertFalse(self.st.pending()); self.assertIn('Expired: 1',self.st.dashboard())

if __name__=='__main__': unittest.main()
