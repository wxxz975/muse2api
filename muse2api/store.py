"""账号池与任务存储（JSON 落盘，原子写）。"""
from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import threading
import time
import uuid

# 包内导入；同时兼容以文件方式单独加载（测试/工具会用 spec_from_file_location）。
try:
    from .account_identity import (AccountIdentityConflict, identities_conflict,
                                   identity_keys, merge_identities, normalize_identity)
    from .proxy import public_proxy
except ImportError:  # pragma: no cover - standalone file loading
    import os as _os, sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
    from account_identity import (AccountIdentityConflict, identities_conflict,
                                  identity_keys, merge_identities, normalize_identity)
    from proxy import public_proxy

_LOCK = threading.RLock()
_UNSET = object()

# 登录会话存在性检查；可用性仍须经上游认证（与 engine 一致）
# hatch_vml 是可选工作区令牌，缺失不能据此拒绝会话导入
ESSENTIAL_COOKIES = ("hatch_sess",)


def _is_pos(v) -> bool:
    try:
        return int(v) > 0
    except (TypeError, ValueError):
        return False


# hatch_vml 固定约 2 天，且不因使用而延长 —— 它就是决定账号寿命的那条 cookie。
# 实测对照：生成前后 synced_at 变了、也捞到新 cookie，但 4 条核心 cookie 的
# expires 一动不动。所以拿不到 hatch_vml 的 expires 时，按「导入时间 + 2 天」估算，
# 而不是退回到 hatch_native_auth_device 的 30 天（那个不决定生死，会误导用户）。
VML_TTL = 2 * 86400


def min_expiry(cookies_exp: dict | None) -> int | None:
    """核心 cookie 里最早过期的那个（通用兜底，不区分哪条决定寿命）。"""
    if not cookies_exp:
        return None
    vals = []
    for name in ESSENTIAL_COOKIES:
        v = cookies_exp.get(name)
        try:
            v = int(v)
        except (TypeError, ValueError):
            continue
        if v > 0:
            vals.append(v)
    if not vals:
        for v in cookies_exp.values():
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            if v > 0:
                vals.append(v)
    return min(vals) if vals else None


def account_expiry(cookies_exp: dict | None,
                   base_ts: int | None = None) -> int | None:
    """账号实际失效时间。

    优先级：
      1) hatch_vml 的真实 expires（最准）；
      2) 拿不到就用 base_ts(导入/同步时刻) + VML_TTL 估算 —— 因为寿命就是 2 天；
      3) 都没有再退回核心 cookie 最早过期。
    """
    ce = cookies_exp or {}
    vml = ce.get("hatch_vml")
    if _is_pos(vml):
        return int(vml)
    if base_ts:
        return int(base_ts) + VML_TTL
    return min_expiry(ce)


def _read(path: str, default):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return default


def _write(path: str, obj):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Store:
    def __init__(self, cfg):
        self.cfg = cfg
        with _LOCK:
            self.accounts: list[dict] = _read(cfg.accounts_file, [])
            self.tasks: dict[str, dict] = _read(cfg.tasks_file, {})
            self.migration_report = self.deduplicate()
            for a in self.accounts:
                ce = a.get("cookies_exp") or {}
                if _is_pos(ce.get("hatch_vml")):
                    a["expires_at"] = int(ce["hatch_vml"])

    @staticmethod
    def _identity(account: dict) -> dict | None:
        try:
            return normalize_identity(account.get("identity"))
        except ValueError:
            return None

    @staticmethod
    def _session(account: dict) -> str | None:
        value = (account.get("cookies") or {}).get("hatch_sess")
        return value if isinstance(value, str) and value else None

    def _component(self, seeds: list[dict], identity: dict | None = None) -> list[dict]:
        group = list(seeds)
        keys = identity_keys(identity)
        sessions = {self._session(a) for a in group} - {None}
        for a in group:
            keys.update(identity_keys(self._identity(a)))
        changed = True
        while changed:
            changed = False
            for a in self.accounts:
                if any(a.get("id") == member.get("id") for member in group):
                    continue
                akeys = identity_keys(self._identity(a))
                if ((self._session(a) and self._session(a) in sessions) or keys & akeys):
                    group.append(a)
                    sessions.add(self._session(a))
                    keys.update(akeys)
                    changed = True
        return group

    @staticmethod
    def _proxy_key(proxy) -> str:
        if not proxy:
            return "direct"
        return json.dumps({k: proxy.get(k, "") for k in ("server", "username", "password")},
                          sort_keys=True)

    def _validate_group(self, group: list[dict], identity=None, proxy=_UNSET) -> dict | None:
        merged = identity
        routes = {self._proxy_key(a.get("proxy")) for a in group}
        if proxy is not _UNSET:
            routes.add(self._proxy_key(proxy))
        if len(routes) > 1:
            raise AccountIdentityConflict("Conflicting account proxy routes; no accounts were changed")
        for a in group:
            merged = merge_identities(merged, self._identity(a))
        return merged

    def _canonical(self, group: list[dict]) -> dict:
        positions = {a["id"]: i for i, a in enumerate(self.accounts)}
        return min(group, key=lambda a: (a.get("created_at") or float("inf"),
                                        positions.get(a["id"], len(positions))))

    @staticmethod
    def _latest(group: list[dict]) -> dict:
        return max(enumerate(group), key=lambda p: (
            p[1].get("synced_at") or p[1].get("updated_at") or p[1].get("created_at") or 0, p[0]))[1]

    def _fold(self, group: list[dict], identity=None, source: dict | None = None) -> tuple[dict, dict]:
        canonical = self._canonical(group)
        source = source if source is not None else self._latest(group)
        merged = copy.deepcopy(canonical)
        # These fields belong to one concrete session; never mix cookies/expiries
        # from unrelated browser sessions of an otherwise identical account.
        for key in ("cookies", "cookies_exp", "expires_at", "expiry_anchor", "ok", "note", "synced_at"):
            if key in source:
                merged[key] = copy.deepcopy(source[key])
        # Incomplete duplicates of the exact session may omit expiry metadata.
        # Preserve known real expires only when that cookie value is unchanged.
        ce = dict(merged.get("cookies_exp") or {})
        cookies = dict(merged.get("cookies") or {})
        for member in group:
            if self._session(member) and self._session(member) == self._session(source):
                for name, value in (member.get("cookies") or {}).items():
                    cookies.setdefault(name, value)
            for name, value in (member.get("cookies_exp") or {}).items():
                if (_is_pos(value) and cookies.get(name)
                        and cookies.get(name) == (member.get("cookies") or {}).get(name)):
                    ce.setdefault(name, int(value))
        merged["cookies"], merged["cookies_exp"] = cookies, ce
        if not merged.get("label"):
            merged["label"] = next((a["label"] for a in group if a.get("label")), canonical["id"])
        created = [a["created_at"] for a in group if _is_pos(a.get("created_at"))]
        if created:
            merged["created_at"] = min(created)
        merged["enabled"] = all(a.get("enabled", True) for a in group)
        merged["use_count"] = sum(max(0, int(a.get("use_count") or 0)) for a in group)
        for key in ("last_used", "last_keepalive", "checked_at", "updated_at"):
            vals = [a[key] for a in group if a.get(key) is not None]
            if vals:
                merged[key] = max(vals)
        aliases = {alias for a in group for alias in a.get("aliases", []) if isinstance(alias, str)}
        aliases.update(a["id"] for a in group if a["id"] != canonical["id"])
        aliases.discard(canonical["id"])
        if aliases:
            merged["aliases"] = sorted(aliases)
        if identity:
            merged["identity"] = copy.deepcopy(identity)
        else:
            merged.pop("identity", None)
        ce = merged.get("cookies_exp") or {}
        if _is_pos(ce.get("hatch_vml")):
            merged["expires_at"] = int(ce["hatch_vml"])
            merged["expiry_anchor"] = None
        # Keep the selected session's import anchor even when canonical.created_at
        # is older. A keepalive must not turn that into a new 48h lifetime.
        elif not merged.get("expiry_anchor"):
            merged["expiry_anchor"] = source.get("expiry_anchor") or source.get("created_at")
        return canonical, merged

    def _backup_accounts(self) -> str | None:
        if not os.path.isfile(self.cfg.accounts_file):
            return None
        path = self.cfg.accounts_file + ".dedup-" + uuid.uuid4().hex + ".bak"
        # O_EXCL + 0600 avoids a readable window for an account-secret backup.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as dst, open(self.cfg.accounts_file, "rb") as src:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        return path

    def _commit_group(self, group: list[dict], canonical: dict, merged: dict) -> dict:
        removed = {id(a) for a in group if a is not canonical}
        accounts = [merged if a is canonical else a for a in self.accounts if id(a) not in removed]
        if removed:
            self._backup_accounts()
        _write(self.cfg.accounts_file, accounts)
        # Existing engine/app references to the canonical account remain live.
        canonical.clear()
        canonical.update(merged)
        self.accounts = [canonical if a is merged else a for a in accounts]
        return canonical

    def deduplicate(self) -> dict:
        """Migrate safe duplicate components, preserving bytes and all old IDs.

        Conflicting connected components are left intact (not partially merged).
        The report contains local IDs and reason codes only, never identities,
        cookies or proxy credentials. Startup calls this before workers start.
        """
        with _LOCK:
            plans, conflicts, visited = [], [], set()
            for account in self.accounts:
                if id(account) in visited:
                    continue
                group = self._component([account])
                visited.update(id(a) for a in group)
                if len(group) < 2:
                    continue
                try:
                    identity = self._validate_group(group)
                except AccountIdentityConflict:
                    conflicts.append({"ids": [a["id"] for a in group], "reason": "identity_or_proxy_conflict"})
                    continue
                canonical, merged = self._fold(group, identity)
                plans.append((group, canonical, merged))
            aliases = {a["id"]: canonical["id"] for group, canonical, _ in plans
                       for a in group if a is not canonical}
            backup = None
            if plans:
                removed = {id(a) for group, canonical, _ in plans for a in group if a is not canonical}
                replacements = {id(c): m for _, c, m in plans}
                updated = [replacements.get(id(a), a) for a in self.accounts if id(a) not in removed]
                backup = self._backup_accounts()
                _write(self.cfg.accounts_file, updated)
                for _, canonical, merged in plans:
                    canonical.clear()
                    canonical.update(merged)
                old_canonical = {id(m): c for _, c, m in plans}
                self.accounts = [old_canonical.get(id(a), a) for a in updated]
            report = {"merged": len(aliases), "aliases": aliases, "conflicts": conflicts,
                      "backup": backup}
            self.migration_report = report
            return report

    def resolve_account_id(self, aid: str | None) -> str | None:
        with _LOCK:
            for a in self.accounts:
                if a["id"] == aid:
                    return aid
            matches = [a["id"] for a in self.accounts if aid in a.get("aliases", [])]
            if len(matches) > 1:
                raise AccountIdentityConflict("Ambiguous legacy account ID")
            return matches[0] if matches else aid

    @staticmethod
    def _session_update(account: dict, cookies: dict, cookies_exp: dict | None,
                        *, now: int, new_session: bool) -> None:
        old_cookies = account.get("cookies") or {}
        old_exp = account.get("cookies_exp") or {}
        if new_session:
            current = dict(cookies)
            exp = {}
            account["expiry_anchor"] = now
        else:
            current = {**old_cookies, **cookies}
            exp = {k: int(v) for k, v in old_exp.items()
                   if _is_pos(v) and old_cookies.get(k) == current.get(k)}
        # Importers may supply measured expiry metadata without the optional
        # cookie value (notably session-only hatch_vml imports). Preserve that
        # explicit observation; absence of a value is not a new 48h lifetime.
        for key, value in (cookies_exp or {}).items():
            if _is_pos(value):
                exp[key] = int(value)
        account["cookies"] = current
        account["cookies_exp"] = exp
        if _is_pos(exp.get("hatch_vml")):
            account["expiry_anchor"] = None
            account["expires_at"] = int(exp["hatch_vml"])
        else:
            anchor = account.get("expiry_anchor") or account.get("created_at") or now
            account["expiry_anchor"] = anchor
            account["expires_at"] = account_expiry(exp, anchor)

    # ---------- 账号 ----------
    def add_account(self, cookies: dict, label: str = "",
                    cookies_exp: dict | None = None, *, identity: dict | None = None,
                    proxy=_UNSET) -> dict:
        identity = normalize_identity(identity)
        if not isinstance(cookies, dict):
            raise ValueError("Cookies must be a mapping")
        with _LOCK:
            now = int(time.time())
            probe = {"cookies": cookies}
            seeds = [a for a in self.accounts if self._session(probe)
                     and self._session(a) == self._session(probe)]
            group = self._component(seeds, identity)
            verified = self._validate_group(group, identity, proxy)
            if group:
                canonical, merged = self._fold(group, verified)
                self._session_update(merged, cookies, cookies_exp, now=now,
                                     new_session=self._session(merged) != self._session(probe))
                if label:
                    merged["label"] = label
                if proxy is not _UNSET:
                    merged["proxy"] = copy.deepcopy(proxy)
                merged["updated_at"] = now
                return self._commit_group(group, canonical, merged)
            aid = uuid.uuid4().hex[:12]
            acc = {"id": aid, "label": label or aid, "enabled": True, "created_at": now,
                   "last_used": None, "last_keepalive": None, "use_count": 0,
                   "ok": None, "note": "", "synced_at": None}
            self._session_update(acc, cookies, cookies_exp, now=now, new_session=True)
            if verified:
                acc["identity"] = verified
            if proxy is not _UNSET:
                acc["proxy"] = copy.deepcopy(proxy)
            _write(self.cfg.accounts_file, self.accounts + [acc])
            self.accounts.append(acc)
            return acc

    def bind_identity(self, aid: str, identity: dict | None) -> dict | None:
        """Bind trusted upstream identity and merge; never count as business use."""
        with _LOCK:
            account = self.get_account(aid)
            if account is None or identity is None:
                return account
            verified = normalize_identity(identity)
            merge_identities(self._identity(account), verified)
            group = self._component([account], verified)
            merged_identity = self._validate_group(group, verified)
            canonical, merged = self._fold(group, merged_identity)
            return self._commit_group(group, canonical, merged)

    @staticmethod
    def public_account(account: dict) -> dict:
        item = copy.deepcopy({k: v for k, v in account.items()
                              if k not in ("cookies", "proxy", "logs")})
        item["proxy"] = public_proxy(account.get("proxy"))
        item["cookie_count"] = len(account.get("cookies", {}))
        return item

    def list_accounts(self) -> list[dict]:
        out = []
        for a in self.accounts:
            item = self.public_account(a)
            ce = a.get("cookies_exp") or {}
            if _is_pos(ce.get("hatch_vml")):
                item["expires_at"] = int(ce["hatch_vml"])
            item["cookie_count"] = len(a.get("cookies", {}))
            item["essential_ok"] = all(
                a.get("cookies", {}).get(n) for n in ESSENTIAL_COOKIES)
            # 有效期是 hatch_vml 的实测 expires，还是按「2 天寿命」推算的
            item["expires_estimated"] = not _is_pos(ce.get("hatch_vml"))
            out.append(item)
        return out

    def delete_account(self, aid: str) -> bool:
        with _LOCK:
            aid = self.resolve_account_id(aid)
            n = len(self.accounts)
            self.accounts = [a for a in self.accounts if a["id"] != aid]
            _write(self.cfg.accounts_file, self.accounts)
            return len(self.accounts) != n

    def pick_account(self, preferred_id: str | None = None, rotate: bool = True,
                     force_rotate: bool = False, exclude_id: str | None = None,
                     exclude_ids: set[str] | list[str] | None = None,
                     record_use: bool = True) -> dict | None:
        """选择可用账号：优先复用当前已预热的健康账号（减少切号重连耗时），每满 15 次或遇错自动轮转。
        exclude_ids: 需要排除的账号 ID 集合（例如正在被其他并发线程占用的账号）。"""
        with _LOCK:
            preferred_id = self.resolve_account_id(preferred_id)
            exclude_id = self.resolve_account_id(exclude_id)
            if exclude_ids is not None:
                exclude_ids = {self.resolve_account_id(aid) for aid in exclude_ids}
            live = [a for a in self.accounts if a.get("enabled", True) and a.get("cookies")]
            if not live:
                return None
            blocked = set(exclude_ids or [])
            if exclude_id:
                blocked.add(exclude_id)
            if blocked:
                available = [a for a in live if a["id"] not in blocked]
                if available:
                    live = available
                elif exclude_ids is not None:
                    # 并发模式下要求避开所有繁忙账号，但当前无剩余空闲账号
                    return None
                elif len(live) > 1:
                    live = [a for a in live if a["id"] != exclude_id] or live
            if preferred_id and not rotate and not force_rotate:
                for a in live:
                    if a["id"] == preferred_id and a.get("ok") is not False:
                        self._sticky_id = preferred_id
                        self._sticky_count = 1
                        if record_use:
                            a["last_used"] = time.time()
                            a["use_count"] = a.get("use_count", 0) + 1
                            _write(self.cfg.accounts_file, self.accounts)
                        return a
            healthy = [a for a in live if a.get("ok") is not False]
            candidates = healthy if healthy else live
            candidates.sort(key=lambda a: (a.get("last_used") or 0.0, a.get("use_count") or 0))
            acc = candidates[0]
            self._sticky_id = acc["id"]
            self._sticky_count = 1
            if record_use:
                acc["last_used"] = time.time()
                acc["use_count"] = acc.get("use_count", 0) + 1
                _write(self.cfg.accounts_file, self.accounts)
            return acc

    def touch_keepalive(self, aid: str, ok: bool | None = True, note: str = ""):
        """ok=None 仅记录未确认的检测；保留上次账号状态、成功保活时间和有效期。"""
        with _LOCK:
            aid = self.resolve_account_id(aid)
            now_ts = int(time.time())
            for a in self.accounts:
                if a["id"] == aid:
                    if ok is not None:
                        a["ok"] = ok
                    if ok is True:
                        a["last_keepalive"] = now_ts
                        # ponytail: 只信实际 Cookie 到期时间或原导入锚点，不凭检测成功虚推 48h。
                        a["expires_at"] = account_expiry(
                            a.get("cookies_exp"), a.get("expiry_anchor") or a.get("created_at"))
                    if note:
                        a["note"] = note[:300]
                    a["checked_at"] = now_ts
            _write(self.cfg.accounts_file, self.accounts)

    def mark(self, aid: str, ok: bool, note: str = ""):
        with _LOCK:
            aid = self.resolve_account_id(aid)
            for a in self.accounts:
                if a["id"] == aid:
                    a["ok"] = ok
                    a["note"] = note[:300]
                    a["checked_at"] = int(time.time())
            _write(self.cfg.accounts_file, self.accounts)

    def get_account(self, aid: str) -> dict | None:
        with _LOCK:
            aid = self.resolve_account_id(aid)
            return next((a for a in self.accounts if a["id"] == aid), None)

    def update_account(self, aid: str, *, verified_session: bool = False,
                       identity=_UNSET, **kw) -> dict | None:
        """Update with atomic deduplication when cookies/identity change.

        A new manual session cannot inherit an old verified identity. Only trusted
        code handling authenticated upstream rotation may pass verified_session.
        The flag is consumed, never stored. Maintenance never advances an expiry
        anchor. Partial expiry updates preserve unchanged cookies' real expires.
        """
        with _LOCK:
            account = self.get_account(aid)
            if account is None:
                return None
            if "id" in kw or "aliases" in kw:
                raise ValueError("Account IDs and aliases cannot be edited")
            candidate = copy.deepcopy(account)
            incoming = normalize_identity(identity) if identity is not _UNSET else None
            if incoming:
                merge_identities(self._identity(account), incoming)
            cookies = kw.pop("cookies", None)
            ce = kw.pop("cookies_exp", None)
            explicit_anchor = kw.pop("expiry_anchor", _UNSET)
            now = int(time.time())
            if cookies is not None:
                if not isinstance(cookies, dict):
                    raise ValueError("Cookies must be a mapping")
                changed = self._session(account) != self._session({"cookies": cookies})
                self._session_update(candidate, cookies, ce, now=now, new_session=changed)
                if changed and not verified_session:
                    candidate.pop("identity", None)
                if changed and verified_session:
                    old_exp = account.get("cookies_exp") or {}
                    old_cookies = account.get("cookies") or {}
                    for name, value in old_exp.items():
                        if (_is_pos(value)
                                and candidate["cookies"].get(name) == old_cookies.get(name)):
                            candidate["cookies_exp"].setdefault(name, int(value))
                    if _is_pos(candidate["cookies_exp"].get("hatch_vml")):
                        candidate["expires_at"] = int(candidate["cookies_exp"]["hatch_vml"])
                        candidate["expiry_anchor"] = None
                    else:
                        candidate["expiry_anchor"] = account.get("expiry_anchor") or account.get("created_at")
                        candidate["expires_at"] = account.get("expires_at")
            elif ce is not None:
                self._session_update(candidate, candidate.get("cookies") or {}, ce,
                                     now=now, new_session=False)
            if explicit_anchor is not _UNSET and explicit_anchor is not None:
                if cookies is not None and self._session(account) != self._session(candidate):
                    candidate["expiry_anchor"] = explicit_anchor
                    candidate["expires_at"] = account_expiry(candidate.get("cookies_exp"), explicit_anchor)
                    if _is_pos(candidate.get("cookies_exp", {}).get("hatch_vml")):
                        candidate["expiry_anchor"] = None
            for key, value in kw.items():
                if value is not None or key in ("ok", "proxy"):
                    candidate[key] = copy.deepcopy(value)
            if incoming:
                candidate["identity"] = merge_identities(self._identity(candidate), incoming)
            group = self._component([candidate])
            merged_identity = self._validate_group(group)
            canonical, merged = self._fold(group, merged_identity, source=candidate)
            actual_group = [account if a is candidate else a for a in group]
            actual_canonical = account if canonical is candidate else canonical
            return self._commit_group(actual_group, actual_canonical, merged)

    def stats(self) -> dict:
        now = int(time.time())
        total = len(self.accounts)
        enabled = sum(1 for a in self.accounts if a.get("enabled", True))
        bad = sum(1 for a in self.accounts
                  if a.get("enabled", True) and a.get("ok") is False)
        healthy = sum(1 for a in self.accounts
                      if a.get("enabled", True) and a.get("ok") is True)
        expiring = sum(1 for a in self.accounts
                       if a.get("enabled", True)
                       and a.get("expires_at")
                       and a["expires_at"] - now < 3 * 86400)
        expired = sum(1 for a in self.accounts
                      if a.get("enabled", True)
                      and a.get("expires_at") and a["expires_at"] <= now)
        return {"total": total, "enabled": enabled, "disabled": total - enabled,
                "healthy": healthy, "error": bad,
                "expiring": expiring, "expired": expired}

    # ---------- 任务 ----------
    def create_task(self, kind: str, prompt: str) -> dict:
        with _LOCK:
            tid = "task_" + uuid.uuid4().hex[:20]
            t = {"id": tid, "object": "task", "kind": kind, "prompt": prompt,
                 "status": "queued", "created_at": int(time.time()),
                 "updated_at": int(time.time()), "result": None, "error": None}
            self.tasks[tid] = t
            _write(self.cfg.tasks_file, self.tasks)
            return t

    def update_task(self, tid: str, **kw):
        with _LOCK:
            t = self.tasks.get(tid)
            if not t:
                return None
            t.update(kw)
            t["updated_at"] = int(time.time())
            _write(self.cfg.tasks_file, self.tasks)
            return t

    def get_task(self, tid: str) -> dict | None:
        return self.tasks.get(tid)

    def list_tasks(self, limit: int = 50) -> list[dict]:
        items = sorted(self.tasks.values(),
                       key=lambda t: t.get("created_at") or 0, reverse=True)
        return items[:limit]

    def delete_task(self, tid: str) -> bool:
        with _LOCK:
            if tid in self.tasks:
                del self.tasks[tid]
                _write(self.cfg.tasks_file, self.tasks)
                return True
            return False

    def clear_tasks(self, keep: int = 0) -> int:
        with _LOCK:
            items = sorted(self.tasks.values(),
                           key=lambda t: t.get("created_at") or 0, reverse=True)
            keep_ids = {t["id"] for t in items[:keep]}
            removed = len(self.tasks) - len(keep_ids)
            self.tasks = {k: v for k, v in self.tasks.items() if k in keep_ids}
            _write(self.cfg.tasks_file, self.tasks)
            return max(0, removed)
