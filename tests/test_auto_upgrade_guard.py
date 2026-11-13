"""Read-only release checks never fetch commits, rewrite source, or restart."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from releases import LATEST_RELEASE_URL, REPO_URL, ReleaseChecker
import requests


class Response:
    def __init__(self, status, payload=None):
        self.status_code, self.payload = status, payload
    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class ReleaseChecks(unittest.TestCase):
    def test_new_release_is_reported_without_mutation(self):
        checker = ReleaseChecker()
        with patch('releases.requests.get', return_value=Response(200, {
                'tag_name': 'v1.7.0', 'html_url': REPO_URL + '/releases/tag/v1.7.0'})) as get:
            result = checker.check('1.6.12', token='test-only-secret')
        self.assertTrue(result['has_update'])
        self.assertEqual(result['local_version'], '1.6.12')
        self.assertEqual(result['remote_version'], 'v1.7.0')
        self.assertFalse(result['automatic_upgrade'])
        self.assertEqual(get.call_args.args, (LATEST_RELEASE_URL,))
        self.assertFalse(get.call_args.kwargs['allow_redirects'])
        self.assertNotIn('test-only-secret', json.dumps(result))

    def test_missing_release_is_unknown_not_falsely_current(self):
        with patch('releases.requests.get', return_value=Response(404)):
            result = ReleaseChecker().check('1.6.12')
        self.assertEqual(result['status'], 'no_releases')
        self.assertIsNone(result['up_to_date'])
        self.assertIsNone(result['remote_version'])
        self.assertFalse(result['has_update'])

    def test_current_or_newer_local_is_not_downgraded(self):
        for tag in ('v1.6.12', 'v1.6.9'):
            with self.subTest(tag=tag), patch('releases.requests.get', return_value=Response(200, {'tag_name': tag})):
                result = ReleaseChecker().check('1.6.12')
            self.assertTrue(result['up_to_date'])
            self.assertFalse(result['has_update'])
            self.assertEqual(result['local_version'], '1.6.12')

    def test_invalid_draft_or_prerelease_is_not_installable(self):
        for data in (None, [], {'tag_name':'nonsense'}, {'tag_name':'v2.0.0','draft':True},
                     {'tag_name':'v2.0.0-rc1','prerelease':True}):
            with self.subTest(data=data), patch('releases.requests.get', return_value=Response(200, data)):
                result=ReleaseChecker().check('1.6.12')
            self.assertEqual(result['status'], 'error')
            self.assertFalse(result['has_update'])

    def test_failures_do_not_leak_transport_credentials(self):
        failures = (Response(429), Response(500), Response(302), Response(200, ValueError('password=hidden')))
        for reply in failures:
            with patch('releases.requests.get', return_value=reply):
                result=ReleaseChecker().check('1.6.12')
            self.assertEqual(result['status'], 'error')
            self.assertIsNone(result['up_to_date'])
        with patch('releases.requests.get', side_effect=requests.ConnectionError('token=hidden')):
            result=ReleaseChecker().check('1.6.12')
        self.assertNotIn('hidden', json.dumps(result))

    def test_untrusted_release_link_cannot_change_destination(self):
        for url in ('javascript:alert(1)', 'https://evil.invalid/releases/a',
                    'https://github.com/wxxz975/muse2api/releases/tag/v2?token=hidden'):
            with patch('releases.requests.get', return_value=Response(200, {'tag_name':'v2.0.0','html_url':url})):
                result=ReleaseChecker().check('1.6.12')
            self.assertEqual(result['release_url'], REPO_URL+'/releases')

    def test_cache_is_copy_and_keyed_by_local_version(self):
        checker=ReleaseChecker()
        with patch('releases.requests.get', return_value=Response(404)) as get:
            first=checker.check('1.6.12');first['status']='tampered'
            self.assertEqual(checker.check('1.6.12')['status'], 'no_releases')
            self.assertEqual(get.call_count,1)
            checker.check('1.6.12',force=True)
            checker.check('1.6.13')
            self.assertEqual(get.call_count,3)

    def test_application_has_no_silent_upgrade_or_repository_mutation(self):
        source=(ROOT/'app.py').read_text()
        for forbidden in ('def _silent_upgrade_worker', 'def _upgrade_from_github_sync',
                          'def _ensure_git_repo', 'def _git(', 'os._exit(', 'codeload.github.com',
                          'commits?sha=main', 'x-access-token:'):
            self.assertNotIn(forbidden,source)
        loop=source.split('async def _keepalive_loop():',1)[1].split('@app.',1)[0]
        self.assertNotIn('_check_update_sync',loop)
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {
                'MUSE2API_HOME':home,'MUSE2API_PROFILE_ROOT':home,'MUSE2API_KEY':'test-only'}):
            spec=importlib.util.spec_from_file_location('readonly_release_app',ROOT/'app.py')
            module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module
            spec.loader.exec_module(module)
            try:
                with self.assertRaises(module.HTTPException) as caught:
                    module.removed_online_repository_mutation()
                self.assertEqual(caught.exception.status_code,410)
                self.assertTrue(any(r.path=='/admin/update/check' for r in module.app.routes))
            finally:
                module.login_service.close()


if __name__=='__main__':
    unittest.main()
