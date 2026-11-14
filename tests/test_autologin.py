"""Offline behavior/security tests: python3 -m pytest -q tests/test_autologin.py.

No real account, OTP inbox, Cookie file or network is accessed. Fake CDP exercises
service state transitions; the JS is additionally exercised in a real DOM by the
optional isolated-Chromium test at the bottom (no upstream login request).
"""
from __future__ import annotations

import copy
import io
import json
from contextlib import contextmanager
from email.message import Message
from pathlib import Path
import socket
import sys
import threading
import time
from types import SimpleNamespace
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import muse2api.autologin as login


class Clock:
    def __init__(self):
        self.value = 0.0
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.value

    def sleep(self, seconds):
        with self.lock:
            self.value += seconds


class FakePage:
    def __init__(self, engine, *, missing_cookie=False, reason=None, after_reason=None,
                 session=None, throw=None):
        self.engine = engine
        self.state = 'login'
        self.reason, self.after_reason = reason, after_reason
        self.missing_cookie, self.throw = missing_cookie, throw
        self.session = session if session is not None else {'http': 200, 'data': {'status': 'assigned', 'vm_id': 'not-an-account'}}
        self.calls = []
        self.fills = []
        self.closed = False

    def js(self, expression, **kwargs):
        self.calls.append(('js', expression))
        if self.throw:
            raise self.throw
        if 'muse-login-state' in expression:
            return {'ready': True, 'login': self.state == 'login', 'otp': self.state == 'otp',
                    'chat': self.state == 'chat', 'reason': self.after_reason if self.state == 'chat' else self.reason}
        if 'muse-login-fill' in expression:
            self.fills.append(expression)
            return True
        if 'muse-login-click' in expression:
            if self.state == 'login' and 'accept all' not in expression:
                self.state = 'otp'
                return True
            if self.state == 'otp':
                self.state = 'chat'
                return True
            return False
        if 'muse-login-session' in expression:
            self.engine.verified += 1
            return copy.deepcopy(self.session)
        return None

    def send(self, method, params=None, **kwargs):
        self.calls.append((method, params))
        return {'result': {}}


class FakeEngine:
    def __init__(self, options=None):
        self.local = threading.local()
        self.options = options or [{}]
        self.pages = []
        self.proxies = []
        self.closed = 0
        self.started = 0
        self.active = 0
        self.max_active = 0
        self.verified = 0

    @property
    def page(self):
        return getattr(self.local, 'page', None)

    @page.setter
    def page(self, value):
        self.local.page = value

    def start(self):
        self.started += 1

    def _open_page(self, proxy=None):
        assert self.page is None, 'previous account page leaked into another account'
        page = FakePage(self, **self.options[min(len(self.pages), len(self.options) - 1)])
        self.pages.append(page)
        self.proxies.append(copy.deepcopy(proxy))
        self.active += 1
        self.max_active = max(self.active, self.max_active)
        return page

    def read_cookies(self):
        if not self.page or self.page.state != 'chat' or self.page.missing_cookie:
            return {}
        return {'hatch_sess': {'value': 'session-secret-value', 'expires': 2000000000},
                'hatch_vml': {'value': 'vml-secret-value', 'expires': 1900000000},
                'hatch_native_auth_device': {'value': 'device-secret-value', 'expires': -1}}

    def close_current_page(self):
        if self.page is not None:
            self.page.closed = True
            self.closed += 1
            self.active -= 1
            self.page = None


class FakeSource:
    def __init__(self, url, proxy=None, *, values=None, error=None):
        self.url, self.proxy = url, copy.deepcopy(proxy)
        self.values = values if values is not None else [['111111'], ['111111', '654321']]
        self.error = error
        self.calls = 0
        self.closed = False

    def codes(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.values[min(self.calls - 1, len(self.values) - 1)]

    def close(self):
        self.closed = True
        self.url, self.proxy = '', None


def service(*, options=None, automatic=True, source_values=None, source_error=None,
            on_success=None, on_event=None, sleep=None, **kwargs):
    engine, clock, saved, sources = FakeEngine(options), Clock(), [], []

    def save(cookies, expires, identity, **meta):
        saved.append(copy.deepcopy({'cookies': cookies, 'expires': expires, 'identity': identity, **meta}))
        return 'account-1'

    def source(url, proxy=None):
        item = FakeSource(url, proxy=proxy, values=source_values, error=source_error)
        sources.append(item)
        return item

    svc = login.AutoLoginService(engine, on_success or save, clock=clock,
        wall_clock=lambda: 1800000000, sleep=(clock.sleep if automatic else sleep),
        page_timeout=2, otp_timeout=2, login_timeout=2, poll_interval=0.01,
        otp_interval=0.02, otp_source_factory=source, on_event=on_event, **kwargs)
    return SimpleNamespace(svc=svc, engine=engine, clock=clock, saved=saved, sources=sources)


def until(svc, batch_id, wanted=None, index=0):
    """Wait on actual worker transitions, not sleeps as proof of behavior."""
    deadline = time.monotonic() + 3
    with svc._condition:
        while time.monotonic() < deadline:
            snapshot = svc.status(batch_id)
            item = snapshot['items'][index]
            if item['status'] in (wanted or login.TERMINAL):
                return snapshot
            svc._condition.wait(timeout=0.01)
    raise AssertionError('worker did not reach expected state: ' + repr(svc.status(batch_id)))


def submit_auto(h, **overrides):
    return h.svc.submit([{'account': 'person@example.org', 'otp_url': 'https://mail.example.org/inbox?token=source-secret', **overrides}])


def test_automatic_otp_success_callback_real_expiries_and_scrubbed_snapshots():
    events = []
    h = service(on_event=events.append)
    proxy = {'server': 'http://127.0.0.1:8080', 'username': 'proxy-user', 'password': 'proxy-secret'}
    try:
        batch = submit_auto(h, label='label-secret', proxy=proxy, proxy_id='proxy-1')
        done = until(h.svc, batch['id'])
        assert done['status'] == 'completed'
        assert done['items'][0]['status'] == 'succeeded'
        assert done['items'][0]['account_id'] == 'account-1'
        assert h.engine.verified == 1
        assert len(h.saved) == 1
        saved = h.saved[0]
        assert saved['cookies']['hatch_sess'] == 'session-secret-value'
        assert saved['expires'] == {'hatch_sess': 2000000000, 'hatch_vml': 1900000000}
        assert saved['identity'] == {'provider': 'muse', 'verified': True, 'source': 'otp', 'email': 'person@example.org'}
        assert saved['label'] == 'label-secret' and saved['proxy'] == proxy and saved['proxy_id'] == 'proxy-1'
        assert h.engine.proxies == [proxy] and h.sources[0].proxy is None
        assert h.engine.closed == 1 and h.sources[0].closed
        assert all(page.closed for page in h.engine.pages)
        assert any('654321' in x for x in h.engine.pages[0].fills)
        assert not any('111111' in x for x in h.engine.pages[0].fills)
        internal = h.svc._batches[batch['id']].items[0]
        assert internal.credentials is None and internal.pending_otp is None
        encoded = json.dumps([batch, done, h.svc.list_batches(), events, repr(internal)])
        for secret in ['person@example.org', '654321', 'source-secret', 'mail.example.org',
                       'session-secret-value', 'vml-secret-value', 'device-secret-value',
                       'proxy-secret', 'proxy-user', 'label-secret']:
            assert secret not in encoded
        assert not any('logout' in str(call).lower() for call in h.engine.pages[0].calls)
    finally:
        h.svc.close()


def test_manual_otp_submit_has_no_network_source_and_accepts_index_or_id():
    h = service(automatic=False)
    try:
        batch = h.svc.submit([{'account': 'person@example.org'}])
        until(h.svc, batch['id'], {'waiting_otp'})
        with pytest.raises(login.LoginValidationError, match='six digits'):
            h.svc.submit_otp(batch['id'], 0, 'code-with-secret')
        h.svc.submit_otp(batch['id'], batch['items'][0]['id'], '654321')
        done = until(h.svc, batch['id'])
        assert done['items'][0]['status'] == 'succeeded'
        assert not h.sources and len(h.saved) == 1
        with pytest.raises(login.LoginConflictError):
            h.svc.submit_otp(batch['id'], 0, '654321')
    finally:
        h.svc.close()


def test_stale_automatic_code_times_out_without_submitting_or_store():
    h = service(source_values=[['111111']])
    try:
        batch = submit_auto(h)
        done = until(h.svc, batch['id'])
        assert done['items'][0]['reason'] == 'otp_timeout'
        assert not h.saved
        assert len(h.engine.pages[0].fills) == 1
        assert h.clock() >= 2 and h.engine.closed == 1
    finally:
        h.svc.close()


def test_cancel_running_and_queued_accounts_does_not_authenticate_them():
    h = service(automatic=False, max_batches=1)
    try:
        batch = h.svc.submit([{'account': 'person@example.org'}, {'account': 'second@example.org'}])
        until(h.svc, batch['id'], {'waiting_otp'})
        with pytest.raises(login.LoginCapacityError):
            h.svc.submit([{'account': 'third@example.org'}])
        h.svc.cancel(batch['id'])
        done = until(h.svc, batch['id'])
        assert all(item['status'] == 'cancelled' for item in done['items'])
        assert not h.saved and len(h.engine.pages) == 1 and h.engine.closed == 1
        assert all(item.credentials is None for item in h.svc._batches[batch['id']].items)
    finally:
        h.svc.close()


@pytest.mark.parametrize('reason', ['captcha', 'onboarding', 'mfa_required', 'password_required', 'account_selection'])
@pytest.mark.parametrize('after', [False, True])
def test_human_challenges_never_succeed_even_when_cookie_exists(reason, after):
    h = service(options=[{'after_reason' if after else 'reason': reason}])
    try:
        batch = submit_auto(h)
        item = until(h.svc, batch['id'])['items'][0]
        assert item['status'] == 'needs_manual' and item['reason'] == reason
        assert not h.saved and h.engine.verified == 0 and h.engine.closed == 1
    finally:
        h.svc.close()


@pytest.mark.parametrize('options,reason', [
    ({'missing_cookie': True}, 'missing_session_cookie'),
    ({'session': {'http': 401}}, 'session_unverified'),
    ({'session': {'http': 200, 'data': {'status': 'unassigned'}}}, 'session_unassigned'),
    ({'after_reason': 'invalid_otp'}, 'invalid_otp'),
    ({'throw': RuntimeError('https://mail.example/?token=secret-cookie-otp-password')}, 'browser_error'),
])
def test_false_success_is_blocked_and_error_is_sanitized(options, reason):
    events = []
    h = service(options=[options], on_event=events.append)
    try:
        batch = submit_auto(h)
        done = until(h.svc, batch['id'])
        assert done['items'][0]['status'] == 'failed'
        assert done['items'][0]['reason'] == reason
        assert not h.saved and h.engine.closed == 1
        assert 'secret-cookie-otp-password' not in json.dumps([done, events])
    finally:
        h.svc.close()


def test_store_failure_and_event_sink_failure_are_safe_and_truthful():
    def store(*args, **kwargs):
        raise RuntimeError('secret-storage-cookie')
    def event(record):
        raise RuntimeError('secret-event-cookie')
    h = service(on_success=store, on_event=event)
    try:
        batch = submit_auto(h)
        done = until(h.svc, batch['id'])
        assert done['items'][0]['reason'] == 'store_failed'
        assert 'secret-' not in json.dumps(done)
    finally:
        h.svc.close()


def test_missing_persistence_receipt_is_not_reported_as_success():
    h = service(on_success=lambda *args, **kwargs: None)
    try:
        batch = submit_auto(h)
        item = until(h.svc, batch['id'])['items'][0]
        assert item['status'] == 'failed' and item['reason'] == 'store_unconfirmed'
        assert item['account_id'] is None and h.engine.closed == 1
    finally:
        h.svc.close()


def test_phone_without_stable_account_id_is_not_misidentified():
    h = service()
    try:
        batch = submit_auto(h, account='+8613800138000')
        assert until(h.svc, batch['id'])['items'][0]['status'] == 'succeeded'
        assert h.saved[0]['identity'] is None
    finally:
        h.svc.close()


def test_observed_native_identity_is_whitelisted_without_access_token():
    h = service(options=[{'session': {'http': 200, 'data': {'status': 'assigned',
        'auth_meta_session': {'frl_account_id': 'stable-identity', 'frl_access_token': 'never-store-token'}}}}])
    try:
        batch = submit_auto(h)
        assert until(h.svc, batch['id'])['items'][0]['status'] == 'succeeded'
        assert h.saved[0]['identity']['user_id'] == 'stable-identity'
        assert 'never-store-token' not in json.dumps(h.saved)
    finally:
        h.svc.close()


def test_serial_worker_fresh_page_per_account_and_slot_cleanup():
    slot_events = []
    @contextmanager
    def slot():
        slot_events.append('enter')
        try:
            yield
        finally:
            slot_events.append('exit')
    h = service(slot=slot)
    try:
        batch = h.svc.submit([{'account': 'first@example.org', 'otp_url': 'https://mail.example.org/one'},
                              {'account': 'second@example.org', 'otp_url': 'https://mail.example.org/two'}])
        until(h.svc, batch['id'], index=1)
        assert len(h.saved) == 2 and len(h.engine.pages) == 2
        assert h.engine.max_active == 1 and h.engine.closed == 2
        assert slot_events == ['enter', 'exit', 'enter', 'exit']
    finally:
        h.svc.close()


def test_slot_timeout_never_opens_page_or_falls_back():
    @contextmanager
    def slot():
        raise TimeoutError('capacity-secret-not-for-output')
        yield
    h = service(slot=slot)
    try:
        batch = submit_auto(h)
        item = until(h.svc, batch['id'])['items'][0]
        assert item['reason'] == 'browser_capacity'
        assert not h.engine.pages and not h.saved
    finally:
        h.svc.close()


@pytest.mark.parametrize('raw', [
    [], [{'account': 'not-an-account'}], [{'account': 's p@example.org'}],
    [{'account': 'person@example.org', 'password': 'secret'}],
    [{'account': 'person@example.org', 'otp_cmd': 'secret'}],
    [{'account': 'person@example.org', 'otp_file': '/secret'}],
    [{'account': 'person@example.org', 'proxy_id': 'proxy-without-resolution'}],
    [{'account': 'person@example.org', 'proxy': {'server': 'http://proxy:80', 'password': '\nsecret'}}],
    [{'account': 'person@example.org'}, {'account': 'PERSON@example.org'}],
])
def test_invalid_input_is_bounded_and_does_not_echo_values(raw):
    h = service()
    try:
        with pytest.raises(login.LoginValidationError) as error:
            h.svc.submit(raw)
        assert 'secret' not in str(error.value)
        assert not h.engine.pages
    finally:
        h.svc.close()


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'ftp://example.org/mail',
    'http://127.0.0.1/mail?token=secret', 'http://[::1]/', 'http://169.254.169.254/',
    'http://[::ffff:127.0.0.1]/', 'http://localhost/', 'https://a.local/',
    'https://user:secret@example.org/', 'https://example.org:0/',
    'https://example.org/#secret', 'https://example.org/\nsecret'])
def test_unsafe_url_is_rejected_before_start_without_echo(url):
    h = service()
    try:
        with pytest.raises(login.LoginValidationError) as error:
            submit_auto(h, otp_url=url)
        assert 'secret' not in str(error.value)
        assert not h.engine.pages
    finally:
        h.svc.close()


def answers(*ips):
    return [(socket.AF_INET6 if ':' in ip else socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443)) for ip in ips]


@pytest.mark.parametrize('ips', [('127.0.0.1',), ('169.254.169.254',), ('10.0.0.1',),
    ('::1',), ('::ffff:192.168.1.2',), ('224.0.0.1',), ('93.184.216.34', '10.0.0.1')])
def test_dns_private_or_mixed_answers_rejected_before_http(ips):
    source = login.PublicOtpSource('https://mail.example.org/?token=secret')
    with patch.object(login.socket, 'getaddrinfo', return_value=answers(*ips)), \
         patch.object(login.urllib.request, 'build_opener') as open_mock:
        with pytest.raises(login.OtpSafetyError):
            source.codes()
        open_mock.assert_not_called()
    source.close()


def test_socket_is_pinned_to_checked_ip_with_original_tls_hostname():
    handler = login._PinnedHTTPHandler('93.184.216.34')
    connection = handler._connection('mail.example.org', login.http.client.HTTPSConnection)
    assert connection.host == 'mail.example.org'
    with patch.object(login.socket, 'create_connection', return_value='fake-socket') as connect:
        assert connection._create_connection(('mail.example.org', 443), 10, None) == 'fake-socket'
        connect.assert_called_once_with(('93.184.216.34', 443), 10, None)


def test_proxy_tunnels_checked_ip_never_unchecked_hostname_or_direct():
    handler = login._PinnedHTTPHandler('93.184.216.34', 'http://127.0.0.1:17777')
    connection = handler._connection('mail.example.org', login.http.client.HTTPSConnection)
    tunnel = Mock(sock='proxy-socket')
    with patch.object(login.http.client, 'HTTPConnection', return_value=tunnel) as construct, \
         patch.object(login.socket, 'create_connection') as direct:
        assert connection._create_connection(('mail.example.org', 443), 10, None) == 'proxy-socket'
        construct.assert_called_once_with('127.0.0.1', 17777, timeout=10)
        tunnel.set_tunnel.assert_called_once_with('93.184.216.34', 443)
        tunnel.connect.assert_called_once()
        direct.assert_not_called()


class Response(io.BytesIO):
    def __init__(self, body=b'code: 654321', cookies=None):
        super().__init__(body)
        self.headers = Message()
        self.headers['Content-Type'] = 'text/html; charset=utf-8'
        if cookies:
            self.headers['Set-Cookie'] = cookies
        self.status = self.code = 200
        self.msg = self.reason = 'OK'
        self.url = 'https://mail.example.org/inbox'

    def info(self):
        return self.headers

    def geturl(self):
        return self.url


@pytest.mark.parametrize('target', ['http://127.0.0.1/', 'https://private.example.org/mail', 'file:///etc/passwd', 'http://mail.example.org/mail'])
def test_each_redirect_is_rechecked_without_exposing_query_tokens(target):
    headers = Message()
    headers['Location'] = target
    error = urllib.error.HTTPError('https://mail.example.org/?token=secret', 302, 'found', headers, io.BytesIO())
    opener = Mock()
    opener.open.side_effect = error
    source = login.PublicOtpSource('https://mail.example.org/?token=secret')
    def resolve(host, port, **kwargs):
        return answers('10.0.0.1' if host == 'private.example.org' else '93.184.216.34')
    with patch.object(login.socket, 'getaddrinfo', side_effect=resolve), \
         patch.object(login.urllib.request, 'build_opener', return_value=opener):
        with pytest.raises(login.OtpSafetyError) as caught:
            source.codes()
        assert 'secret' not in str(caught.value)
        assert opener.open.call_count == 1
    source.close()


def test_generator_cookie_jar_survives_poll_and_response_is_bounded():
    source = login.PublicOtpSource('https://mail.example.org/inbox')
    cookies_seen = []
    class FakeHTTP(urllib.request.HTTPSHandler):
        def https_open(self, request):
            cookies_seen.append(request.get_header('Cookie'))
            return Response(cookies='inbox=fixture; Path=/; Secure')
    real_build = urllib.request.build_opener
    def build(*handlers):
        return real_build(*(handler for handler in handlers if not isinstance(handler, login._PinnedHTTPHandler)), FakeHTTP())
    with patch.object(login.socket, 'getaddrinfo', return_value=answers('93.184.216.34')), \
         patch.object(login.urllib.request, 'build_opener', side_effect=build):
        assert source.codes() == ['654321']
        assert source.codes() == ['654321']
    assert cookies_seen == [None, 'inbox=fixture']
    assert len(source.jar) == 1
    source.close()
    assert len(source.jar) == 0


def test_otp_response_size_limit_and_code_filtering():
    source = login.PublicOtpSource('https://mail.example.org/inbox')
    opener = Mock()
    opener.open.return_value = Response(b'a' * (source.MAX_BYTES + 1))
    with patch.object(login.socket, 'getaddrinfo', return_value=answers('93.184.216.34')), \
         patch.object(login.urllib.request, 'build_opener', return_value=opener):
        with pytest.raises(login.OtpSafetyError, match='too_large'):
            source.codes()
    assert login.extract_codes('<script>123456</script><div id="987654">OTP: <b>654321</b></div>') == ['654321']
    assert login.extract_codes('<a href="https://x.test/123456">mail</a> deadbeef123456aa 123456789') == []
    source.close()


def test_history_limit_retention_input_ttl_and_shutdown_scrub():
    h = service(max_history=2, max_batches=1, retention=5)
    try:
        ids = []
        for i in range(4):
            batch = submit_auto(h, account=f'person{i}@example.org')
            ids.append(batch['id'])
            until(h.svc, batch['id'])
        assert len(h.svc.list_batches()) == 2
        with pytest.raises(login.LoginNotFoundError):
            h.svc.status(ids[0])
        h.clock.sleep(6)
        assert h.svc.list_batches() == []
    finally:
        h.svc.close()
    waiting = service(automatic=False)
    batch = waiting.svc.submit([{'account': 'person@example.org'}, {'account': 'second@example.org'}])
    until(waiting.svc, batch['id'], {'waiting_otp'})
    waiting.svc.close()
    assert all(item.credentials is None for item in waiting.svc._batches[batch['id']].items)


def test_cancellation_cannot_race_storage_commit():
    entered, release = threading.Event(), threading.Event()
    def store(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return 'account-1'
    h = service(on_success=store)
    try:
        batch = submit_auto(h)
        assert entered.wait(2)
        with pytest.raises(login.LoginConflictError):
            h.svc.cancel(batch['id'])
        release.set()
        assert until(h.svc, batch['id'])['items'][0]['status'] == 'succeeded'
    finally:
        release.set()
        h.svc.close()


def test_unavailable_mailbox_switches_to_manual_without_logging_transport_error():
    h = service(automatic=False, source_error=login._FlowError('otp_source_unavailable', 'needs_manual'))
    try:
        batch = submit_auto(h)
        waiting = until(h.svc, batch['id'], {'waiting_otp'})
        assert waiting['items'][0]['reason'] == 'manual_code_required'
        h.svc.submit_otp(batch['id'], 0, '654321')
        assert until(h.svc, batch['id'])['items'][0]['status'] == 'succeeded'
        assert len(h.saved) == 1 and h.sources[0].closed
    finally:
        h.svc.close()


def test_unsafe_mailbox_never_switches_to_manual_or_reads_source_again():
    h = service(source_error=login.OtpSafetyError('otp_url_not_public'))
    try:
        batch = submit_auto(h)
        done = until(h.svc, batch['id'])
        assert done['items'][0]['reason'] == 'otp_source_forbidden'
        assert not h.saved and h.sources[0].calls == 1
    finally:
        h.svc.close()


def test_queued_and_running_input_ttl_expire_without_replaying_credentials():
    h = service(automatic=False, input_ttl=1)
    try:
        batch = h.svc.submit([{'account': 'first@example.org'}, {'account': 'second@example.org'}])
        until(h.svc, batch['id'], {'waiting_otp'})
        h.clock.sleep(2)
        # Snapshot reaps queued secrets even while another item is running.
        snapshot = h.svc.status(batch['id'])
        assert snapshot['items'][1]['reason'] == 'input_expired'
        assert h.svc._batches[batch['id']].items[1].credentials is None
        h.svc._batches[batch['id']].items[0].signal.set()
        done = until(h.svc, batch['id'])
        assert all(x['reason'] == 'input_expired' for x in done['items'])
        assert not h.saved and len(h.engine.pages) == 1
        assert all(x.credentials is None for x in h.svc._batches[batch['id']].items)
    finally:
        h.svc.close()


def test_source_guard_and_total_read_deadline_block_trickle_response():
    source = login.PublicOtpSource('https://mail.example.org/inbox', timeout=2)
    opener = Mock()
    class SlowResponse(Response):
        def read1(self, size=-1):
            return b'6'
    opener.open.return_value = SlowResponse()
    with patch.object(login.socket, 'getaddrinfo', return_value=answers('93.184.216.34')), \
         patch.object(login.urllib.request, 'build_opener', return_value=opener), \
         patch.object(login.time, 'monotonic', side_effect=[0, 0, 0, 1, 3]):
        with pytest.raises(login._FlowError, match='otp_source_unavailable'):
            source.codes()
    source.guard = Mock(side_effect=login._FlowError('cancelled', 'cancelled'))
    with patch.object(login.socket, 'getaddrinfo') as resolve:
        with pytest.raises(login._FlowError, match='cancelled'):
            source.codes()
        resolve.assert_not_called()
    source.close()


def test_real_chromium_local_login_dom_http_only_cookies_and_isolation(tmp_path):
    """Real CDP + JS + local HTTP fixture, explicitly NOT a live Muse OTP test."""
    import shutil
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from muse2api.engine import MuseEngine

    chromium = (shutil.which('chromium') or shutil.which('google-chrome') or
                next((str(p) for p in [Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')]
                      if p.is_file()), None))
    if not chromium:
        pytest.skip('isolated Chromium executable unavailable')
    cookie_requests, http_requests, counters = [], [], []
    markup = b'''<!doctype html><html><body>
      <header><button onclick="window.wrong++">Continue</button></header>
      <section data-testid="native-auth-access-pane"><input aria-label="Mobile number or email">
      <button id="next">Continue</button></section>
      <script>
        window.wrong=0;window.inputEvents=0;window.steps=[];
        document.addEventListener('input',()=>window.inputEvents++);
        document.querySelector('#next').onclick=()=>{
          const v=document.querySelector('input').value;
          if(!v.includes('@'))throw new Error('account missing');
          window.steps.push('account');
          document.querySelector('section').innerHTML='<input autocomplete="one-time-code" inputmode="numeric" maxlength="6"><button id="verify">Verify</button>';
          document.querySelector('#verify').onclick=async()=>{
            const otp=document.querySelector('input').value;
            if(otp!=='654321')throw new Error('wrong fixture OTP');
            window.steps.push('otp');
            const r=await fetch('/fixture-cookie');if(!r.ok)throw new Error('cookie fixture failed');
            document.querySelector('section').innerHTML='<textarea aria-label="message"></textarea>';
          };
        };
      </script></body></html>'''
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            http_requests.append(self.path)
            body, cookie = markup, None
            if self.path == '/fixture-cookie':
                cookie = f'hatch_sess=fixture-session-{len(cookie_requests)}; Path=/; HttpOnly; Max-Age=3600'
                body = b'OK'
            elif self.path == '/api/session':
                assert self.headers.get('Cookie', '').startswith('hatch_sess=fixture-session-')
                body = b'{"status":"assigned","vm_id":"vm-not-identity"}'
            elif self.path == '/':
                cookie_requests.append(self.headers.get('Cookie'))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json' if self.path == '/api/session' else 'text/html')
            self.send_header('Content-Length', str(len(body)))
            if cookie:
                self.send_header('Set-Cookie', cookie)
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        cdp_port = sock.getsockname()[1]
    cfg = SimpleNamespace(chromium=chromium, cdp_port=cdp_port, home_dir=str(tmp_path), extra_path='',
        profile_dir=str(tmp_path / 'profile'), data_dir=str(tmp_path),
        download_dir=str(tmp_path / 'downloads'), site_url=origin + '/')

    class FixturePage:
        def __init__(self, raw):
            self.raw = raw
        def send(self, method, params=None, **kwargs):
            if method == 'Page.navigate':
                assert params['url'] == login.LOGIN_URL
                params = {'url': origin + '/'}
            return self.raw.send(method, params, **kwargs)
        def js(self, expression, **kwargs):
            if expression == login._SESSION_JS:
                # Same production JS; only permitted origin is the local fixture here.
                expression = expression.replace("location.origin !== 'https://muse.ai'",
                                                'location.origin !== ' + json.dumps(origin))
            return self.raw.js(expression, **kwargs)
        def close(self):
            self.raw.close()

    class FixtureEngine(MuseEngine):
        def _open_page(self, proxy=None):
            return FixturePage(super()._open_page(proxy=proxy))
    engine = FixtureEngine(cfg)
    saved, sources = [], []
    def source(url, proxy=None):
        s = FakeSource(url, proxy=proxy)
        sources.append(s)
        return s
    def save(cookies, expires, identity, **meta):
        counters.append(engine.page.js('({wrong:window.wrong,events:window.inputEvents,steps:window.steps,visibleCookies:document.cookie})'))
        saved.append((cookies, expires, identity))
        return 'fixture-account'
    svc = login.AutoLoginService(engine, save, otp_source_factory=source,
        page_timeout=5, otp_timeout=5, login_timeout=5, poll_interval=0.02)
    try:
        batch = svc.submit([{'account': 'first@example.org', 'otp_url': 'https://mail.example.org/one'},
                            {'account': 'second@example.org', 'otp_url': 'https://mail.example.org/two'}])
        # Browser startup is allowed extra time; every state/result is asserted below.
        deadline = time.monotonic() + 20
        with svc._condition:
            while time.monotonic() < deadline:
                result = svc.status(batch['id'])
                if all(x['status'] in login.TERMINAL for x in result['items']):
                    break
                svc._condition.wait(timeout=0.05)
        assert [x['status'] for x in result['items']] == ['succeeded', 'succeeded'], result
        assert len(saved) == 2 and cookie_requests == [None, None]
        assert saved[0][0]['hatch_sess'] != saved[1][0]['hatch_sess']
        assert all(x[1]['hatch_sess'] > time.time() for x in saved)
        assert [x[2]['email'] for x in saved] == ['first@example.org', 'second@example.org']
        assert all(x['wrong'] == 0 and x['events'] == 2 and x['steps'] == ['account', 'otp']
                   and 'hatch_sess' not in x['visibleCookies'] for x in counters)
        assert not engine._active_contexts and all(s.closed for s in sources)
        assert http_requests.count('/api/session') == 2
    finally:
        svc.close()
        engine.stop()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-q']))
