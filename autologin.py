"""Bounded, in-process Muse email/phone + OTP login service (not password login).

Integration (admin authentication and persistence belong to app.py)::

    def save(cookies, expires, identity, *, label, proxy, proxy_id):
        return store.add_account(cookies, label=label, cookies_exp=expires,
                                 identity=identity, proxy=proxy)["id"]
    service = AutoLoginService(engine, save, slot=browser_slot,
                               on_event=lambda event: journal.emit(**event))
    batch = service.submit([{"account": "user@example.org", "otp_url": ""}])
    service.status(batch["id"])  # snapshots contain no input credentials
    service.submit_otp(batch["id"], 0, "123456")  # zero-based index OR item UUID
    service.cancel(batch["id"])  # optional item index/UUID
    service.close()  # at shutdown; does NOT stop the shared engine

slot is a zero-argument context-manager factory; acquire/release the application's
shared browser capacity there, with a bounded acquisition timeout. Its TimeoutError
is reported as browser_capacity. on_success is synchronous, runs on the single
worker, and must return an account id (or {"id": ...}); raise on persistence failure.
on_event receives ONLY safe fixed-schema lifecycle events (no exception text).
Keep callbacks fast. Neither this module nor its snapshots persist credentials.
Terminal snapshots expire; app may persist those safe snapshots and mark unfinished
ones interrupted on restart. Restart does not replay login inputs or OTPs.

Each account gets engine._open_page(proxy=...) in a fresh BrowserContext, and is
closed via engine.close_current_page() in the same thread. Never logs out, selects
an arbitrary account, completes onboarding, or bypasses CAPTCHA/MFA. OTP URLs use
public-only DNS-pinned sockets, per-hop validation and a per-account CookieJar
(including generator.email). File/command/IMAP/password sources are not accepted.
"""
from __future__ import annotations

import copy
import html
import http.client
import http.cookiejar
import ipaddress
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Callable

from proxy import HTTPProxyRelay, normalize_server, split_proxy_url

LOGIN_URL = "https://muse.ai/"
LOGIN_INPUT = ('input[aria-label="Mobile number or email"], '
               'input[autocomplete*="username"], input[inputmode="email"]')
OTP_INPUT = ('input[autocomplete="one-time-code"], '
             'input[inputmode="numeric"][maxlength="6"]')
CONTINUE = ["continue", "next", "confirm", "log in", "login", "verify", "submit",
            "继续", "下一步", "确认", "登录", "验证", "登入"]
COOKIE_BUTTONS = ["accept all", "allow all", "accept", "agree", "reject all",
                  "only necessary", "全部接受", "同意", "接受", "拒绝"]
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "needs_manual"})
_CODE = re.compile(r"^[0-9]{6}$")
_EMAIL = re.compile(r"^[^\s@<>|]+@[^\s@<>|]+\.[^\s@<>|]+$")


class LoginValidationError(ValueError):
    """Messages deliberately exclude the rejected input."""


class LoginCapacityError(RuntimeError):
    pass


class LoginConflictError(ValueError):
    pass


class LoginNotFoundError(KeyError):
    pass


class OtpSafetyError(ValueError):
    pass


class _FlowError(Exception):
    def __init__(self, reason: str, status: str = "failed"):
        self.reason, self.status = reason, status
        super().__init__(reason)


def _url_parts(url: str):
    try:
        if not isinstance(url, str) or not 1 <= len(url) <= 4096:
            raise ValueError
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
            raise ValueError
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or not 1 <= port <= 65535
                or any(c in parsed.hostname for c in "\\@%")):
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii")
        if host.rstrip(".").lower() in ("localhost", "localhost.localdomain"):
            raise ValueError
        return parsed, host, port
    except (ValueError, UnicodeError):
        raise OtpSafetyError("otp_url_invalid") from None


def _public_address(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global or address.is_multicast or address.is_reserved:
            raise ValueError
        return str(address)
    except ValueError:
        raise OtpSafetyError("otp_url_not_public") from None


def _resolve_public(url: str) -> tuple[str, int, str]:
    """Reject mixed public/private DNS answers; the checked IP is the socket peer."""
    _, host, port = _url_parts(url)
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    checked = [_public_address(entry[4][0]) for entry in addresses]
    if not checked:
        raise OtpSafetyError("otp_url_unresolved")
    return host, port, checked[0]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _PinnedHTTPHandler(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
    """Preserve Host/TLS SNI/certificate checks while pinning direct/proxy target."""
    def __init__(self, address: str, relay_url: str | None = None):
        super().__init__()
        self.address, self.relay_url = address, relay_url

    def _connection(self, host, connection_type, **kwargs):
        connection = connection_type(host, **kwargs)

        def connect(address, timeout, source_address):
            if not self.relay_url:
                return socket.create_connection((self.address, address[1]), timeout,
                                                source_address)
            relay = urllib.parse.urlsplit(self.relay_url)
            tunnel = http.client.HTTPConnection(relay.hostname, relay.port, timeout=timeout)
            tunnel.set_tunnel(self.address, address[1])
            try:
                tunnel.connect()
                sock, tunnel.sock = tunnel.sock, None
                return sock
            finally:
                tunnel.close()

        connection._create_connection = connect
        return connection

    def http_open(self, req):
        return self.do_open(lambda host, **kw: self._connection(
            host, http.client.HTTPConnection, **kw), req)

    def https_open(self, req):
        return self.do_open(lambda host, **kw: self._connection(
            host, http.client.HTTPSConnection, **kw), req)


def extract_codes(text: str) -> list[str]:
    """Six-digit codes only; ignore script/style, URL and HTML-id false positives."""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)\b[0-9a-f]{16,}\b", " ", text)
    text = re.sub(r"https?://[^\s<>]+", " ", text)
    text = html.unescape(re.sub(r"<[^>]*>", " ", text))
    found = re.findall(r"(?:code|passcode|otp|verification|验证码|登录码|确认码)"
                       r"[^0-9]{0,40}([0-9]{6})(?![0-9])", text, re.I)
    if not found:
        found = re.findall(r"(?<![0-9])([0-9]{6})(?![0-9])", text)
    return list(dict.fromkeys(found))


class PublicOtpSource:
    """Memory-only cookie session; no redirects/proxies can bypass DNS pinning."""
    MAX_BYTES = 1024 * 1024

    def __init__(self, url: str, proxy: dict | None = None, timeout: float = 10,
                 guard: Callable | None = None):
        _url_parts(url)
        self._url, self._proxy, self.timeout = url, proxy, timeout
        self.guard = guard or (lambda: None)
        self.jar = http.cookiejar.CookieJar()
        self._relay = None

    def codes(self) -> list[str]:
        try:
            url = self._url
            deadline = time.monotonic() + self.timeout
            for _ in range(6):
                self.guard()
                _, _, address = _resolve_public(url)
                self.guard()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _FlowError("otp_source_unavailable", "needs_manual")
                if self._proxy and self._relay is None:
                    self._relay = HTTPProxyRelay(self._proxy)
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({}), _NoRedirect(),
                    _PinnedHTTPHandler(address, self._relay.url if self._relay else None),
                    urllib.request.HTTPCookieProcessor(self.jar))
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0", "Accept": "text/html,application/json,text/plain"})
                try:
                    response = opener.open(req, timeout=remaining)
                except urllib.error.HTTPError as exc:
                    location = exc.headers.get("Location")
                    if exc.code not in (301, 302, 303, 307, 308) or not location:
                        exc.close()
                        raise _FlowError("otp_source_unavailable", "needs_manual") from None
                    exc.close()
                    target = urllib.parse.urljoin(url, location)
                    _url_parts(target)
                    if urllib.parse.urlsplit(url).scheme == "https" and urllib.parse.urlsplit(target).scheme != "https":
                        raise OtpSafetyError("otp_redirect_downgrade")
                    url = target
                    continue
                with response:
                    raw = bytearray()
                    read = getattr(response, "read1", response.read)
                    while True:
                        self.guard()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise _FlowError("otp_source_unavailable", "needs_manual")
                        # Limit the *whole* response, not just each socket read;
                        # trickled bytes cannot keep an OTP poll alive forever.
                        try:
                            response.fp.raw._sock.settimeout(remaining)
                        except AttributeError:
                            pass
                        chunk = read(min(65536, self.MAX_BYTES + 1 - len(raw)))
                        if not chunk:
                            break
                        raw.extend(chunk)
                        if len(raw) > self.MAX_BYTES:
                            raise OtpSafetyError("otp_response_too_large")
                    charset = response.headers.get_content_charset() or "utf-8"
                    try:
                        text = raw.decode(charset, "replace")
                    except LookupError:
                        text = raw.decode("utf-8", "replace")
                return extract_codes(text)
            raise OtpSafetyError("otp_redirect_limit")
        except (OtpSafetyError, _FlowError):
            raise
        except Exception:
            # URLs, proxy credentials, mail bodies and response errors NEVER escape.
            raise _FlowError("otp_source_unavailable", "needs_manual") from None

    def close(self):
        self.jar.clear()
        self._url, self._proxy = "", None
        self.guard = lambda: None
        if self._relay:
            self._relay.close()
            self._relay = None


_STATE_JS = r"""/* muse-login-state */ (() => {
  const visible = e => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
  const has = s => [...document.querySelectorAll(s)].some(visible);
  const body = (document.body ? document.body.innerText : '').slice(0,12000).toLowerCase();
  let reason = null;
  if (has('[data-testid="native-auth-name-fields"]')) reason = 'onboarding';
  else if (has('[data-testid="native-auth-account-list"]') || /choose an account|select an account|选择账号/.test(body)) reason = 'account_selection';
  else if (has('iframe[src*="captcha"],iframe[src*="challenges.cloudflare.com"],[data-sitekey]') || /verify you are human|complete the captcha|checking your browser|人机验证/.test(body)) reason = 'captcha';
  else if (/enter your password|输入密码/.test(body)) reason = 'password_required';
  else if (/two.factor|authenticator app|security key|passkey|通行密钥|双重验证/.test(body)) reason = 'mfa_required';
  else if (/too many attempts|too many tries|尝试次数过多/.test(body)) reason = 'rate_limited';
  else if (/account has been restricted|account is disabled|账号受限/.test(body)) reason = 'account_restricted';
  else if (/invalid code|code is incorrect|wrong code|验证码不正确/.test(body)) reason = 'invalid_otp';
  return {ready: document.readyState === 'complete', login: has(__LOGIN__), otp: has(__OTP__),
          chat: has('textarea'), reason};
})()""".replace("__LOGIN__", json.dumps(LOGIN_INPUT)).replace("__OTP__", json.dumps(OTP_INPUT))

_SET_JS = r"""/* muse-login-fill */ ((selector, value) => {
  const el = [...document.querySelectorAll(selector)].find(e => e.getClientRects().length);
  if (!el) return false;
  el.focus();
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  setter.call(el, value);
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return el.value === value;
})(__SELECTOR__, __VALUE__)"""

_CLICK_JS = r"""/* muse-login-click */ ((texts, header) => {
  const pane = document.querySelector('[data-testid="native-auth-access-pane"]');
  const nodes = (pane || document).querySelectorAll('button,[role="button"],input[type="submit"]');
  for (const b of nodes) {
    if (b.disabled || b.getAttribute('aria-disabled') === 'true' || !b.getClientRects().length) continue;
    if (!pane && !header && b.closest('header')) continue;
    const text = (b.innerText || b.value || '').trim().toLowerCase();
    if (texts.includes(text)) { b.click(); return true; }
  }
  return false;
})(__TEXTS__, __HEADER__)"""

_SESSION_JS = r"""/* muse-login-session */ (async () => {
  if (location.origin !== 'https://muse.ai') return {http: 0};
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10000);
  try {
    const r = await fetch('/api/session', {credentials: 'same-origin', redirect: 'error', signal: controller.signal});
    if (r.status !== 200) return {http: r.status};
    const data = await r.json();
    // Only the observed account field. NO access token, generic id, VM or device.
    const out = {status: typeof data.status === 'string' ? data.status : null};
    const uid = data.auth_meta_session && data.auth_meta_session.frl_account_id;
    if (typeof uid === 'string' && uid.length <= 256 || typeof uid === 'number')
      out.auth_meta_session = {frl_account_id: uid};
    return {http: 200, data: out};
  } catch (_) { return {http: 0}; }
  finally { clearTimeout(timer); }
})()"""


@dataclass(repr=False)
class _Credentials:
    account: str
    otp_url: str
    label: str
    proxy: dict | None
    proxy_id: str | None

    def clear(self):
        self.account = self.otp_url = self.label = ""
        self.proxy = self.proxy_id = None


@dataclass
class _Item:
    id: str
    index: int
    credentials: _Credentials | None = field(repr=False)
    deadline: float = field(repr=False)
    status: str = "queued"
    stage: str = "queued"
    reason: str | None = None
    account_id: str | None = None
    pending_otp: str | None = field(default=None, repr=False)
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    signal: threading.Event = field(default_factory=threading.Event, repr=False)
    committing: bool = False

    def clear(self):
        if self.credentials:
            self.credentials.clear()
        self.credentials, self.pending_otp = None, None

    def snapshot(self):
        return {"id": self.id, "index": self.index, "status": self.status,
                "state": self.status, "stage": self.stage, "reason": self.reason,
                "account_id": self.account_id}


@dataclass
class _Batch:
    id: str
    items: list[_Item]
    created_at: float
    updated_at: float
    finished_at: float | None = None

    def snapshot(self):
        statuses = [item.status for item in self.items]
        if all(s in TERMINAL for s in statuses):
            status = "cancelled" if all(s == "cancelled" for s in statuses) else "completed"
        elif "waiting_otp" in statuses:
            status = "waiting_otp"
        elif "running" in statuses:
            status = "running"
        else:
            status = "queued"
        return {"id": self.id, "status": status, "state": status,
                "created_at": self.created_at, "updated_at": self.updated_at,
                "items": [item.snapshot() for item in self.items],
                "counts": {s: statuses.count(s) for s in ("queued", "running", "waiting_otp", *sorted(TERMINAL))}}


def _validate_accounts(accounts, maximum: int) -> list[_Credentials]:
    if not isinstance(accounts, list) or not 1 <= len(accounts) <= maximum:
        raise LoginValidationError(f"accounts must contain 1..{maximum} entries")
    result, seen = [], set()
    allowed = {"account", "otp_url", "label", "proxy", "proxy_id"}
    for index, raw in enumerate(accounts):
        prefix = f"accounts[{index}]: "
        if not isinstance(raw, dict) or set(raw) - allowed:
            raise LoginValidationError(prefix + "unsupported fields; only email/phone + OTP are supported")
        account = raw.get("account")
        if not isinstance(account, str) or len(account) > 254:
            raise LoginValidationError(prefix + "invalid email or phone")
        account = account.strip()
        if not (_EMAIL.fullmatch(account) or re.fullmatch(r"\+?[0-9]{6,15}", account)):
            raise LoginValidationError(prefix + "invalid email or phone")
        key = account.casefold()
        if key in seen:
            raise LoginValidationError(prefix + "duplicate login input")
        seen.add(key)
        label = raw.get("label", "")
        if not isinstance(label, str) or len(label) > 80 or any(ord(c) < 32 for c in label):
            raise LoginValidationError(prefix + "invalid label")
        url = raw.get("otp_url", "")
        if not isinstance(url, str):
            raise LoginValidationError(prefix + "invalid otp_url")
        if url:
            url = url.replace("{account}", urllib.parse.quote(account, safe="@+"))
            try:
                parsed, host, _ = _url_parts(url)
                try:
                    ipaddress.ip_address(host)
                except ValueError:
                    if host.lower().rstrip(".").endswith((".localhost", ".local", ".internal")):
                        raise OtpSafetyError("otp_url_not_public")
                else:
                    _public_address(host)
            except OtpSafetyError:
                raise LoginValidationError(prefix + "otp_url must be a public HTTP(S) address") from None
        proxy = raw.get("proxy")
        if proxy is not None:
            if not isinstance(proxy, dict) or set(proxy) - {"server", "username", "password"}:
                raise LoginValidationError(prefix + "invalid proxy")
            try:
                proxy = split_proxy_url(proxy)
                proxy = {"server": normalize_server(proxy["server"]),
                         "username": proxy.get("username", ""), "password": proxy.get("password", "")}
                if any(not isinstance(proxy[k], str) or len(proxy[k]) > 255 or
                       any(ord(c) < 32 for c in proxy[k]) for k in ("username", "password")):
                    raise ValueError
                proxy["username"].encode("latin-1")
                proxy["password"].encode("latin-1")
            except (ValueError, KeyError, TypeError, UnicodeError):
                raise LoginValidationError(prefix + "invalid proxy") from None
        proxy_id = raw.get("proxy_id")
        if proxy_id is not None and (not isinstance(proxy_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", proxy_id)):
            raise LoginValidationError(prefix + "invalid proxy_id")
        if proxy_id and not proxy:
            raise LoginValidationError(prefix + "proxy_id must be resolved by the application")
        result.append(_Credentials(account, url, label, proxy, proxy_id))
    return result


class AutoLoginService:
    """Single-worker batch queue; no browser work happens in admin HTTP handlers."""
    def __init__(self, engine, on_success: Callable, *, slot: Callable | None = None,
                 on_event: Callable | None = None, max_batches: int = 8,
                 max_accounts: int = 20, max_history: int = 20,
                 input_ttl: float = 900, retention: float = 900,
                 page_timeout: float = 45, otp_timeout: float = 180,
                 login_timeout: float = 45, poll_interval: float = 0.4,
                 otp_interval: float = 2, clock: Callable = time.monotonic,
                 wall_clock: Callable = time.time, sleep: Callable | None = None,
                 otp_source_factory: Callable = PublicOtpSource):
        if (not 1 <= max_batches <= 32 or not 1 <= max_accounts <= 50 or not 1 <= max_history <= 100
                or not 1 <= input_ttl <= 1800 or not 1 <= retention <= 3600
                or not all(0 < n <= 600 for n in (page_timeout, otp_timeout, login_timeout))
                or not 0 < poll_interval <= 10 or not 0 < otp_interval <= 30):
            raise LoginValidationError("invalid login service limits")
        self.engine, self.on_success = engine, on_success
        self.slot, self.on_event = slot or nullcontext, on_event
        self.max_batches, self.max_accounts, self.max_history = max_batches, max_accounts, max_history
        self.input_ttl, self.retention = input_ttl, retention
        self.page_timeout, self.otp_timeout, self.login_timeout = page_timeout, otp_timeout, login_timeout
        self.poll_interval, self.otp_interval = poll_interval, otp_interval
        self.clock, self.wall_clock, self.sleep = clock, wall_clock, sleep
        self.otp_source_factory = otp_source_factory
        self._condition = threading.Condition(threading.RLock())
        self._batches: dict[str, _Batch] = {}
        self._closed = False
        self._thread = threading.Thread(target=self._worker, name="muse-autologin", daemon=True)
        self._thread.start()

    def _prune_locked(self):
        now = self.clock()
        for batch in list(self._batches.values()):
            for item in batch.items:
                if item.status == "queued" and now >= item.deadline:
                    item.status = "failed"
                    item.reason, item.stage = "input_expired", "finished"
                    item.clear()
            if all(item.status in TERMINAL for item in batch.items) and batch.finished_at is None:
                batch.finished_at = now
            if batch.finished_at is not None and now - batch.finished_at >= self.retention:
                del self._batches[batch.id]
        completed = [b for b in self._batches.values() if b.finished_at is not None]
        for batch in completed[:-self.max_history]:
            del self._batches[batch.id]

    def submit(self, accounts: list[dict]) -> dict:
        credentials = _validate_accounts(accounts, self.max_accounts)
        with self._condition:
            self._prune_locked()
            if self._closed:
                raise LoginCapacityError("login service is closed")
            if sum(b.finished_at is None for b in self._batches.values()) >= self.max_batches:
                raise LoginCapacityError("login batch capacity reached")
            now, wall = self.clock(), self.wall_clock()
            batch = _Batch(uuid.uuid4().hex, [
                _Item(uuid.uuid4().hex, i, c, now + self.input_ttl)
                for i, c in enumerate(credentials)], wall, wall)
            self._batches[batch.id] = batch
            snapshot = batch.snapshot()
            self._condition.notify_all()
        self._emit(batch, None, "login.batch")
        return snapshot

    def _get_locked(self, batch_id):
        self._prune_locked()
        batch = self._batches.get(batch_id)
        if batch is None:
            raise LoginNotFoundError("login batch not found")
        return batch

    @staticmethod
    def _item_locked(batch, item_id):
        if isinstance(item_id, int) and not isinstance(item_id, bool):
            item = next((x for x in batch.items if x.index == item_id), None)
        else:
            item = next((x for x in batch.items if x.id == item_id), None)
        if item is None:
            raise LoginNotFoundError("login item not found")
        return item

    def status(self, batch_id: str) -> dict:
        with self._condition:
            return self._get_locked(batch_id).snapshot()

    def list_batches(self) -> list[dict]:
        with self._condition:
            self._prune_locked()
            return [b.snapshot() for b in reversed(list(self._batches.values()))]

    def submit_otp(self, batch_id: str, item_id: str | int, code: str) -> dict:
        if not isinstance(code, str) or not _CODE.fullmatch(code.strip()):
            raise LoginValidationError("OTP must contain exactly six digits")
        with self._condition:
            batch = self._get_locked(batch_id)
            item = self._item_locked(batch, item_id)
            if item.status != "waiting_otp" or item.cancel.is_set() or item.pending_otp is not None:
                raise LoginConflictError("login item is not accepting an OTP")
            item.pending_otp = code.strip()
            item.signal.set()
            return batch.snapshot()

    def cancel(self, batch_id: str, item_id: str | int | None = None) -> dict:
        with self._condition:
            batch = self._get_locked(batch_id)
            items = [self._item_locked(batch, item_id)] if item_id is not None else batch.items
            if any(item.committing and item.status not in TERMINAL for item in items):
                raise LoginConflictError("login is already committing; cancellation is too late")
            for item in items:
                if item.status in TERMINAL:
                    continue
                item.cancel.set()
                item.signal.set()
                if item.status == "queued":
                    item.status, item.stage, item.reason = "cancelled", "finished", "cancelled"
                    item.clear()
            batch.updated_at = self.wall_clock()
            self._condition.notify_all()
            return batch.snapshot()

    def close(self, timeout: float = 5):
        with self._condition:
            self._closed = True
            for batch in self._batches.values():
                for item in batch.items:
                    if item.status not in TERMINAL and not item.committing:
                        item.cancel.set()
                        item.signal.set()
                        if item.status == "queued":
                            item.status, item.stage, item.reason = "cancelled", "finished", "shutdown"
                            item.clear()
            self._condition.notify_all()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=timeout)

    def _emit(self, batch, item, event="login.item"):
        if not self.on_event:
            return
        with self._condition:
            record = {"event": event, "batch_id": batch.id, "time": self.wall_clock(),
                      "status": batch.snapshot()["status"]}
            if item:
                record.update(item.snapshot())
                record["item_id"] = record.pop("id")
        try:
            self.on_event(record)
        except Exception:
            pass  # Never leak sink exceptions or let logging change a login result.

    def _change(self, batch, item, status, stage, reason=None):
        with self._condition:
            item.status, item.stage, item.reason = status, stage, reason
            batch.updated_at = self.wall_clock()
            self._condition.notify_all()
        self._emit(batch, item)

    def _check(self, item):
        if item.cancel.is_set() or self._closed:
            raise _FlowError("cancelled", "cancelled")
        if self.clock() >= item.deadline:
            raise _FlowError("input_expired")

    def _pause(self, item, seconds=None):
        self._check(item)
        if self.sleep:
            self.sleep(self.poll_interval if seconds is None else seconds)
        else:
            item.signal.wait(self.poll_interval if seconds is None else seconds)
            item.signal.clear()
        self._check(item)

    def _worker(self):
        while True:
            with self._condition:
                self._prune_locked()
                if self._closed:
                    return
                pair = next(((b, i) for b in self._batches.values() for i in b.items if i.status == "queued"), None)
                if pair is None:
                    self._condition.wait(timeout=1)
                    continue
                batch, item = pair
                item.status, item.stage = "running", "browser"
            self._run_item(batch, item)

    @staticmethod
    def _state(page):
        value = page.js(_STATE_JS, timeout=12)
        if not isinstance(value, dict) or "ready" not in value:
            raise _FlowError("page_state_unavailable")
        reason = value.get("reason")
        if reason in ("onboarding", "account_selection", "captcha", "password_required", "mfa_required"):
            raise _FlowError(reason, "needs_manual")
        if reason in ("rate_limited", "account_restricted", "invalid_otp"):
            raise _FlowError(reason)
        return value

    @staticmethod
    def _click(page, texts, header=False):
        expression = _CLICK_JS.replace("__TEXTS__", json.dumps(texts)).replace("__HEADER__", "true" if header else "false")
        return page.js(expression, timeout=12) is True

    @staticmethod
    def _fill(page, selector, value):
        expression = _SET_JS.replace("__SELECTOR__", json.dumps(selector)).replace("__VALUE__", json.dumps(value))
        if page.js(expression, timeout=12) is not True:
            raise _FlowError("input_fill_failed")

    def _submit_form(self, page, selector):
        if self._click(page, CONTINUE):
            return
        page.js("(() => { const e=document.querySelector(" + json.dumps(selector) + "); if(e)e.focus(); })()", timeout=12)
        common = {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}
        for kind, extra in (("keyDown", {}), ("char", {"text": "\r"}), ("keyUp", {})):
            page.send("Input.dispatchKeyEvent", {"type": kind, **common, **extra}, timeout=12)

    def _wait_state(self, page, item, key, timeout, reason):
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            self._check(item)
            state = self._state(page)
            if state.get(key):
                return state
            self._pause(item)
        raise _FlowError(reason)

    def _wait_code(self, batch, item, page, source, baseline):
        self._change(batch, item, "waiting_otp", "otp", "waiting_for_code" if source else "manual_code_required")
        deadline, next_poll = self.clock() + self.otp_timeout, self.clock()
        while self.clock() < deadline:
            self._check(item)
            self._state(page)
            with self._condition:
                code, item.pending_otp = item.pending_otp, None
            if code:
                return code
            if source and self.clock() >= next_poll:
                try:
                    codes = source.codes()
                except _FlowError:
                    source = None
                    self._change(batch, item, "waiting_otp", "otp", "manual_code_required")
                    continue
                self._check(item)
                if self.clock() >= deadline:
                    break
                fresh = [c for c in codes if c not in baseline and _CODE.fullmatch(c)]
                if fresh:
                    return fresh[0]
                next_poll = self.clock() + self.otp_interval
            self._pause(item)
        raise _FlowError("otp_timeout")  # Never retry with a stale baseline code.

    def _capture(self, batch, item, page, credentials, source):
        self._change(batch, item, "running", "login_page")
        navigation = page.send("Page.navigate", {"url": LOGIN_URL}, timeout=15)
        if isinstance(navigation, dict) and navigation.get("result", {}).get("errorText"):
            raise _FlowError("navigation_failed")
        self._wait_state(page, item, "ready", self.page_timeout, "page_timeout")
        self._click(page, COOKIE_BUTTONS)
        if not self._state(page).get("login"):
            self._click(page, ["log in", "login", "登录", "登入"], header=True)
        self._wait_state(page, item, "login", self.page_timeout, "login_form_missing")
        self._fill(page, LOGIN_INPUT, credentials.account)
        try:
            baseline = set(source.codes()) if source else set()
        except _FlowError:
            # An unavailable mailbox is not permission to use an old code. Allow
            # explicit manual OTP submission in this same isolated browser instead.
            source, baseline = None, set()
        self._check(item)
        self._submit_form(page, LOGIN_INPUT)
        self._wait_state(page, item, "otp", self.page_timeout, "otp_form_missing")
        code = self._wait_code(batch, item, page, source, baseline)
        self._change(batch, item, "running", "submit_otp")
        self._fill(page, OTP_INPUT, code)
        code = None
        self._check(item)
        self._submit_form(page, OTP_INPUT)
        self._change(batch, item, "running", "verify_session")
        deadline = self.clock() + self.login_timeout
        while self.clock() < deadline:
            self._check(item)
            state = self._state(page)  # CAPTCHA/onboarding checked BEFORE cookies.
            cookies = self.engine.read_cookies()
            session_cookie = cookies.get("hatch_sess", {})
            if state.get("chat") and not state.get("otp") and not state.get("login") and session_cookie.get("value"):
                break
            self._pause(item)
        else:
            raise _FlowError("missing_session_cookie" if not self.engine.read_cookies().get("hatch_sess", {}).get("value") else "login_timeout")
        reply = page.js(_SESSION_JS, await_promise=True, timeout=15)
        self._check(item)
        if not isinstance(reply, dict) or reply.get("http") != 200 or not isinstance(reply.get("data"), dict):
            raise _FlowError("session_unverified")
        data = reply["data"]
        if data.get("status") != "assigned":
            raise _FlowError("session_unassigned")
        self._state(page)
        from account_identity import identity_from_session, verified_email_identity
        identity = identity_from_session(data, verified=True)
        if _EMAIL.fullmatch(credentials.account):
            verified_email = verified_email_identity(credentials.account, source="otp")
            if identity and identity.get("email") and identity["email"].casefold() != verified_email["email"].casefold():
                raise _FlowError("identity_mismatch")
            identity = {**verified_email, **(identity or {})}
        cookies = self.engine.read_cookies()  # Include real Set-Cookie renewals from verification.
        values, expires = {}, {}
        for name, info in cookies.items():
            if not isinstance(name, str) or not isinstance(info, dict) or not isinstance(info.get("value"), str):
                continue
            if info["value"]:
                values[name] = info["value"]
                try:
                    expiry = int(float(info.get("expires", -1)))
                    if expiry > 0:
                        expires[name] = expiry
                except (ValueError, TypeError, OverflowError):
                    pass
        if not values.get("hatch_sess"):
            raise _FlowError("missing_session_cookie")
        if "hatch_sess" in expires and expires["hatch_sess"] <= self.wall_clock():
            raise _FlowError("expired_session_cookie")
        return values, expires, identity

    def _run_item(self, batch, item):
        source = None
        terminal_status, terminal_reason = "failed", "browser_error"
        self._emit(batch, item)
        try:
            self._check(item)
            credentials = item.credentials
            if credentials is None:
                raise _FlowError("input_expired")
            with self.slot():
                self._check(item)
                try:
                    self.engine.start()
                    self._check(item)
                    self.engine.page = self.engine._open_page(proxy=credentials.proxy)
                    self.engine.current_acc_id = None
                    if credentials.otp_url:
                        source = self.otp_source_factory(credentials.otp_url, proxy=credentials.proxy)
                        if isinstance(source, PublicOtpSource):
                            source.guard = lambda: self._check(item)
                    cookies, expires, identity = self._capture(batch, item, self.engine.page, credentials, source)
                    with self._condition:
                        self._check(item)
                        item.committing = True  # Cancellation cannot race the persistence side effect.
                    self._change(batch, item, "running", "storing")
                    try:
                        saved = self.on_success(cookies, expires, identity, label=credentials.label,
                                                proxy=copy.deepcopy(credentials.proxy), proxy_id=credentials.proxy_id)
                    except Exception:
                        raise _FlowError("store_failed") from None
                    account_id = saved.get("id") if isinstance(saved, dict) else saved
                    if not isinstance(account_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", account_id):
                        raise _FlowError("store_unconfirmed")
                    item.account_id = account_id
                    terminal_status, terminal_reason = "succeeded", None
                finally:
                    try:
                        self.engine.close_current_page()
                    except Exception:
                        self._emit(batch, item, "login.cleanup_failed")
        except _FlowError as exc:
            terminal_status, terminal_reason = exc.status, exc.reason
        except OtpSafetyError:
            terminal_status, terminal_reason = "failed", "otp_source_forbidden"
        except TimeoutError:
            terminal_status, terminal_reason = "failed", "browser_capacity" if item.stage == "browser" else "browser_timeout"
        except Exception:
            terminal_status, terminal_reason = "failed", "browser_error"
        finally:
            if source:
                try:
                    source.close()
                except Exception:
                    pass
            with self._condition:
                item.clear()
                item.status, item.stage, item.reason = terminal_status, "finished", terminal_reason
                item.committing = False
                batch.updated_at = self.wall_clock()
                if all(i.status in TERMINAL for i in batch.items):
                    batch.finished_at = self.clock()
                self._condition.notify_all()
            self._emit(batch, item)
            self._emit(batch, None, "login.batch")
