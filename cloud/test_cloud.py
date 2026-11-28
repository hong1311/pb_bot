import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from cloud_start import validate
from export_journal import export
from pb_telegram import Store

class CloudStartup(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state=Path(self.tmp.name)
        self.mount=f'1 0 0:1 / {self.state} rw - ext4 /dev/test rw'
        self.env={'PB_TELEGRAM_TOKEN':'12345:'+'a'*30}
    def test_reject_ephemeral(self):
        with self.assertRaisesRegex(ValueError,'persistent volume'): validate(self.state,self.env,'')
    def test_reject_missing_secret(self):
        with self.assertRaisesRegex(ValueError,'private runtime secret'): validate(self.state,{},self.mount)
    def test_require_existing_journal(self):
        with self.assertRaisesRegex(ValueError,'Import the existing journal'): validate(self.state,self.env,self.mount)
    def test_explicit_new_journal(self):
        self.env['PB_ALLOW_EMPTY_JOURNAL']='1'
        self.assertEqual(validate(self.state,self.env,self.mount),self.env['PB_TELEGRAM_TOKEN'])
    def test_corrupt_journal(self):
        (self.state/'journal.sqlite3').write_bytes(b'not sqlite')
        with self.assertRaisesRegex(ValueError,'journal cannot be read'): validate(self.state,self.env,self.mount)
    def test_preserve_journal_and_pairing(self):
        db=Store(self.state/'journal.sqlite3')
        db.set('chat','123'); db.set('group_chat','-456'); db.set('botid','12345'); db.db.close()
        (self.state/'token').write_text('not exported')
        dest=self.state/'backup.sqlite3'; export(self.state,dest)
        backup=Store(dest)
        self.assertEqual(backup.get('chat'),'123'); self.assertEqual(backup.get('group_chat'),'-456'); backup.db.close()
        self.assertNotIn(b'not exported',dest.read_bytes())
        self.assertEqual(validate(self.state,self.env,self.mount),self.env['PB_TELEGRAM_TOKEN'])
    def test_read_only_volume(self):
        with patch('cloud_start.os.access',return_value=False):
            with self.assertRaisesRegex(ValueError,'writable'): validate(self.state,self.env,self.mount)
