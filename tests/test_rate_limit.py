import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from insnet.db import Database
from insnet.engine import GalleryDL
from insnet.sync import Coordinator, SyncService

class RateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.db=Database(self.root/'data')
        self.a=self.db.add_account('owner',self.root/'cookie.txt')

    def test_cooldown_persists_and_blocks_all_entrypoints(self):
        self.db.mark_account_limited(self.a)
        db=Database(self.root/'data')
        self.assertIsNotNone(db.account_cooldown(self.a))
        coordinator=Coordinator(db,SyncService(db,self.root/'archive'))
        for kind in ('creator','check'):
            with self.assertRaisesRegex(ValueError,'冷却'):
                coordinator.start(self.a,kind,'test')
        self.assertIsNone(db.due_creators(self.a))
        self.assertIsNone(db.account_cooldown(db.add_account('other',self.root/'other.txt')))

    def test_gallery_disables_internal_retries_and_reports_elapsed(self):
        result=SimpleNamespace(returncode=0,stdout='[]',stderr='')
        with patch('insnet.engine.subprocess.run',return_value=result) as run:
            GalleryDL()._run(None,['-j','https://www.instagram.com/id:1/posts/'])
        cmd=run.call_args.args[0]
        self.assertIn('extractor.instagram.retries=0',cmd)
        self.assertIn('extractor.instagram.sleep-request=3.0',cmd)

    def test_cooldown_expiry_and_repeat_never_shorten(self):
        until=self.db.mark_account_limited(self.a)
        self.assertGreaterEqual(self.db.mark_account_limited(self.a),until)
        with self.db.connect() as c:
            c.execute("UPDATE accounts SET cooldown_until=datetime('now','-1 second') WHERE id=?",(self.a,))
        self.assertIsNone(self.db.account_cooldown(self.a))

    def test_gallery_error_has_timing_and_keeps_429(self):
        from insnet.engine import GalleryError
        result=SimpleNamespace(returncode=1,stdout='',stderr='HTTP 429')
        with patch('insnet.engine.subprocess.run',return_value=result):
            with self.assertRaisesRegex(GalleryError,'耗时.*重试=0.*\\nHTTP 429'):
                GalleryDL()._run(None,['url'])

    def test_authorization_429_sets_account_cooldown(self):
        from insnet.engine import GalleryError
        p=self.root/'cookie.txt'
        p.write_text('.instagram.com\tTRUE\t/\tTRUE\t0\tsessionid\ttest-secret\n')
        service=SyncService(self.db,self.root/'archive')
        coordinator=Coordinator(self.db,service)
        rid=self.db.create_run(self.a,'authorization-check')
        with patch.object(service.gallery,'posts',side_effect=GalleryError('HTTP 429')):
            coordinator._execute(rid,self.db.account(self.a),'check')
        self.assertIsNotNone(self.db.account_cooldown(self.a))

if __name__=='__main__':unittest.main()
