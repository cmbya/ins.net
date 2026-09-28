import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from insnet.db import Database
from insnet.engine import GalleryError
from insnet.sync import Coordinator, SyncService


class AuthorizationCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.cookie_path = root / 'cookies.txt'
        self.db = Database(root / 'data')
        self.account_id = self.db.add_account('owner', self.cookie_path)
        self.service = SyncService(self.db, root / 'data')
        self.coordinator = Coordinator(self.db, self.service)

    def write_cookie(self, expiry):
        self.cookie_path.write_text(
            f'.instagram.com\tTRUE\t/\tTRUE\t{expiry}\tsessionid\tsecret-value\n'
            '.instagram.com\tTRUE\t/\tTRUE\t0\tds_user_id\t123456\n')

    def check(self):
        run_id = self.db.create_run(self.account_id, 'authorization-check')
        self.coordinator._execute(run_id, self.db.account(self.account_id), 'check')
        return self.db.runs()[0], self.db.run_logs(run_id)

    def test_uses_cookie_user_id_and_reports_interface_reachability_only(self):
        self.write_cookie(int(time.time()) + 3600)
        with patch.object(self.service.gallery, 'posts', return_value=[]) as posts:
            run, logs = self.check()
        posts.assert_called_once_with(str(self.cookie_path),
                                      'https://www.instagram.com/id:123456/posts/', 1)
        self.assertEqual(run['status'], 'complete')
        self.assertEqual(self.db.account(self.account_id)['cookie_status'], 'reachable')
        self.assertTrue(any('不能单独证明 Cookie 有效' in item['message'] for item in logs))

    def test_login_redirect_is_unknown_not_invalid_cookie(self):
        self.write_cookie(int(time.time()) + 3600)
        with patch.object(self.service.gallery, 'posts', side_effect=GalleryError(
                'HTTP redirect to home page (https://www.instagram.com/)')):
            run, logs = self.check()
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(self.db.account(self.account_id)['cookie_status'], 'unknown')
        self.assertTrue(any('Cookie 是否有效尚无法判定' in item['message'] for item in logs))

    def test_expired_session_is_rejected_without_network_request(self):
        self.write_cookie(int(time.time()) - 3600)
        with patch.object(self.service.gallery, 'posts') as posts:
            run, _ = self.check()
        posts.assert_not_called()
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(self.db.account(self.account_id)['cookie_status'], 'expired')


if __name__ == '__main__':
    unittest.main()
