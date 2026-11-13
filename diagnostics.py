"""Bounded, redacted local diagnostics. Never pass prompt/media/page bodies here.

Integration::

    journal = Journal(Path(cfg.data_dir) / "diagnostics.jsonl")
    engine = MuseEngine(cfg, journal=journal)
    with journal.context(task_id=task_id, account_id=account_id, attempt=1):
        engine.generate(...)
    # Bind only behind the application's authenticated admin API:
    journal.read(offset=0, limit=100, task_id=task_id)

ContextVars isolate async tasks and threads. For a manually-created worker thread,
enter context() inside that thread (or use contextvars.copy_context().run). Error
records contain frame locations, never exception messages, source lines or locals.
safe_message() is defense in depth for display strings, not permission to log bodies.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import threading
from typing import Any
from urllib.parse import urlsplit
import uuid

_REDACTED = "[REDACTED]"
_MAX_TEXT = 2000
_MAX_ITEMS = 50
_LEVELS = {"debug", "info", "warning", "error", "critical"}
_SENSITIVE_KEY = re.compile(
    r"authorization|cookie|token|password|passwd|secret|credential|otp|"
    r"api[_-]?key|access[_-]?key|private[_-]?key|verification[_-]?code|pwd|passphrase|"
    r"hatch[_-](?:sess|vml|gw|native)|^(?:code|pin|totp|sms_code|mfa_code)$|取码|验证码", re.I)
_CONTENT_KEY = re.compile(
    r"^(?:prompt|prompts|content|contents|body|body_text|html|dom|page|page_text|"
    r"page_body|page_content|snippet|tail|text|raw|payload|request|response|"
    r"image_data|reference_images?|reference_data|media_data|data|b64|base64|"
    r"file_content|upload_content|input_text|output_text)$", re.I)
_URL = re.compile(r"\b(?:https?|socks5h?|socks4a?)://[^\s<>\x22\x27\x60]+", re.I)
_ASSIGNMENT = re.compile(
    r"(?P<key>[\x22\x27]?(?:authorization|proxy[_-]?authorization|cookie|set-cookie|"
    r"(?:[a-z]+[_-])?token|password|passwd|pwd|passphrase|secret|"
    r"api[_-]?key|otp(?:[ _-]?code)?|verification[ _-]?code|code_verifier|"
    r"hatch_[\w-]+|验证码|取码链接)[\x22\x27]?\s*[:=]\s*)"
    r"(?P<value>\[REDACTED[^\]]*\]|\x22[^\x22]*\x22|\x27[^\x27]*\x27|[^\s,;\}\]\x22\x27]+)", re.I)
_HEADER = re.compile(
    r"(?im)\b(?:proxy-authorization|authorization|set-cookie|cookie)\s*:\s*[^\r\n]+")
_HTML = re.compile(r"<\s*/?\s*[a-z][^>]*>", re.I)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?\b")
_OPAQUE = re.compile(r"(?<![\w])[A-Za-z0-9_+/=-]{48,}(?![\w])")
_OTP = re.compile(r"(?<![\w.:/+\-])\d{6,8}(?![\w.:/+\-])")
_BEARER = re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.I)
_DATA_URL = re.compile(r"\bdata:[^\s\x22\x27<>]+", re.I)
_MODEL_BODY = re.compile(
    r"(?s)(模型未生成媒体，仅返回文本\s*[:：]|当前账号所在地区不支持该能力\s*[:：]).*")
_PATH_LOCKS: dict[str, threading.RLock] = {}
_REGISTRY_LOCK = threading.Lock()
_JOURNALS: dict[str, Journal] = {}


def _safe_url(match: re.Match) -> str:
    """Only retain an origin. Mailbox/signed URL secrets can be in paths too."""
    try:
        parsed = urlsplit(match.group(0))
        host = parsed.hostname or ""
        if not host:
            return _REDACTED
        if ":" in host:
            host = "[" + host + "]"
        port = ":" + str(parsed.port) if parsed.port else ""
        out = parsed.scheme.lower() + "://" + host + port
        if parsed.path and parsed.path != "/":
            out += "/[REDACTED]"
        elif parsed.path:
            out += "/"
        if parsed.query:
            out += "?[REDACTED]"
        if parsed.fragment:
            out += "#[REDACTED]"
        return out
    except (TypeError, ValueError):
        return _REDACTED


def safe_message(value: Any) -> str:
    """Redact credential-shaped strings for UI/errors; never stringify objects.

    URL paths, userinfo, query and fragment are removed, including mailbox URLs.
    HTML and known model-refusal bodies are omitted rather than regex-scrubbed.
    Unknown free-text secrets cannot be inferred; do not supply raw page/prompt
    text. Journal.error deliberately does not call str(exception).
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        if isinstance(value, (bool, int, float)):
            return str(value)
        return "[omitted " + type(value).__name__[:64] + "]"
    # Bound work before applying regex to untrusted error strings.
    text = value[:16000]
    if _HTML.search(text):
        return "[REDACTED page content]"
    text = _MODEL_BODY.sub(lambda m: m.group(1) + " [REDACTED model text]", text)
    text = _DATA_URL.sub("[REDACTED data URL]", text)
    text = _URL.sub(_safe_url, text)
    text = _BEARER.sub("[REDACTED authorization]", text)
    text = _ASSIGNMENT.sub(lambda m: m.group("key") + _REDACTED, text)
    text = _HEADER.sub(lambda m: m.group(0).split(":", 1)[0] + ": " + _REDACTED, text)
    text = re.sub(r"(?i)\b(password|passwd|pwd|passphrase|secret|otp(?: code)?|verification code)"
                  r"\s+(?:is\s+)?(?!(?:\[REDACTED))[^\s,;]+",
                  lambda m: m.group(1) + " " + _REDACTED, text)
    text = _JWT.sub(_REDACTED, text)
    text = _OPAQUE.sub(_REDACTED, text)
    text = _OTP.sub("[REDACTED code]", text)
    # Covers common scheme-less proxy user:password@host forms as well.
    text = re.sub(r"(?<![\w])[^\s/@:]+:[^\s/@]+@(?=[^\s/]+)",
                  "[REDACTED]@", text)
    text = "".join(c if c in "\n\t" or ord(c) >= 32 else " " for c in text)
    return text[:_MAX_TEXT] + ("…[truncated]" if len(text) > _MAX_TEXT else "")


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Return a bounded JSON-safe copy, recursively suppressing sensitive keys."""
    if _depth > 8:
        return "[omitted depth]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return safe_message(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "[REDACTED binary]"
    if isinstance(value, dict):
        out = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_ITEMS:
                out["_truncated"] = True
                break
            if not isinstance(key, str):
                continue
            name = safe_message(key)[:96]
            if _SENSITIVE_KEY.search(key) or _CONTENT_KEY.search(key):
                out[name] = _REDACTED
            else:
                out[name] = redact(item, _depth=_depth + 1)
        return out
    if isinstance(value, (tuple, list, set, frozenset)):
        return [redact(item, _depth=_depth + 1) for item in list(value)[:_MAX_ITEMS]]
    return "[omitted " + type(value).__name__[:64] + "]"


def _frames(exc: BaseException) -> list[dict]:
    """Walk traceback objects directly: no source lookup, repr(), locals or args."""
    frames = []
    tb = exc.__traceback__
    while tb is not None:
        code = tb.tb_frame.f_code
        frames.append({"file": safe_message(os.path.basename(code.co_filename))[:160],
                       "line": tb.tb_lineno,
                       "function": safe_message(code.co_name)[:96]})
        tb = tb.tb_next
    return frames[-40:]


def _reverse_lines(stream):
    """Read newest lines without loading a whole retained log into memory."""
    stream.seek(0, os.SEEK_END)
    position = stream.tell()
    pending = b""
    while position:
        size = min(position, 65536)
        position -= size
        stream.seek(position)
        parts = (stream.read(size) + pending).split(b"\n")
        pending = parts[0]
        for line in reversed(parts[1:]):
            if line:
                yield line
    if pending:
        yield pending


class Journal:
    """Thread-safe rotating JSONL. One path lock is shared by all local instances.

    max_bytes bounds each file, backup_count bounds rotated files. Rotation and
    reads hold the same lock, so pagination sees complete JSON records. Pagination
    is offset-based over retained records; concurrent new records/rotation can
    shift later pages. This is a local single-process journal, not an audit DB.
    """

    def __init__(self, path, max_bytes: int = 2 * 1024 * 1024, backup_count: int = 5):
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1024:
            raise ValueError("max_bytes must be an integer >= 1024")
        if isinstance(backup_count, bool) or not isinstance(backup_count, int) or not 0 <= backup_count <= 20:
            raise ValueError("backup_count must be between 0 and 20")
        self.path = Path(os.path.abspath(os.fspath(path)))
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        with _REGISTRY_LOCK:
            self._lock = _PATH_LOCKS.setdefault(str(self.path), threading.RLock())
        self._context = ContextVar("diagnostics_" + uuid.uuid4().hex, default={})
        self._last_write_error: str | None = None
        self._last_write_at: str | None = None
        # Pin retention to the original directory, not a later symlink target.
        from cleanup import _DirectoryAnchor
        self._purge_anchor = _DirectoryAnchor(self.path.parent)

    @contextmanager
    def context(self, **fields):
        """Bind safe correlation fields; restore outer context even on failure."""
        merged = dict(self._context.get())
        merged.update({k: v for k, v in fields.items() if v is not None})
        token = self._context.set(redact(merged))
        try:
            yield self
        finally:
            self._context.reset(token)

    def emit(self, event: str, level: str = "info", **fields) -> dict:
        """Append one redacted event; report disk failure without breaking the job.

        Returned persisted=False/write_error and health() distinguish an unwritten
        event. No in-memory fallback pretends to be persistent diagnostics.
        """
        level = str(level).lower()
        if level == "warn":
            level = "warning"
        if level not in _LEVELS:
            raise ValueError("invalid diagnostic level")
        values = {**self._context.get(), **{k: v for k, v in fields.items() if v is not None}}
        record = {"id": uuid.uuid4().hex,
                  "time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                  "level": level, "event": safe_message(event)[:96], "persisted": True,
                  "task_id": None, "account_id": None, "stage": None,
                  "attempt": None, "error_type": None, "reason": None, "duration_ms": None}
        for key, value in redact(values).items():
            if key not in {"id", "time", "level", "event", "persisted", "write_error"}:
                record[key] = value
        if not isinstance(record.get("attempt"), int) or isinstance(record.get("attempt"), bool):
            record["attempt"] = None
        for key in ("task_id", "account_id", "stage", "error_type"):
            if record[key] is not None:
                record[key] = safe_message(record[key])[:160]
        duration = record.get("duration_ms")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration):
            record["duration_ms"] = round(max(0.0, duration), 3)
        else:
            record["duration_ms"] = None
        raw = self._encode(record)
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.path.is_symlink():
                    raise OSError("diagnostic log must not be a symlink")
                if self.path.exists() and self.path.stat().st_size + len(raw) > self.max_bytes:
                    self._rotate()
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(self.path, flags, 0o600)
                with os.fdopen(fd, "ab") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(raw)
                    stream.flush()
                    if level in {"error", "critical"}:
                        os.fsync(stream.fileno())
                self._last_write_error = None
                self._last_write_at = record["time"]
        except OSError as exc:
            self._last_write_error = type(exc).__name__
            record["persisted"] = False
            record["write_error"] = self._last_write_error
            logging.getLogger("muse2api").error("Diagnostic persistence failed (%s)", self._last_write_error)
        return record

    def _encode(self, record: dict) -> bytes:
        def encode():
            return (json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                    + "\n").encode("utf-8")
        raw = encode()
        if len(raw) <= self.max_bytes:
            return raw
        # A single huge event must never defeat the file-size bound.
        keep = {"id", "time", "level", "event", "persisted", "task_id", "account_id", "stage",
                "attempt", "error_type", "reason", "duration_ms", "traceback"}
        for key in list(record):
            if key not in keep:
                record.pop(key)
        record["truncated"] = True
        record["traceback"] = record.get("traceback", [])[-3:]
        for key in ("event", "task_id", "account_id", "stage", "error_type", "reason"):
            if record.get(key) is not None:
                record[key] = safe_message(record[key])[:96]
        raw = encode()
        if len(raw) > self.max_bytes:
            record.pop("traceback", None)
            for key in ("reason", "attempt"):
                record[key] = "[truncated]"
            raw = encode()
        if len(raw) > self.max_bytes:
            for key in ("event", "task_id", "account_id", "stage", "error_type"):
                if isinstance(record.get(key), str):
                    record[key] = record[key][:32]
            raw = encode()
        return raw

    def _rotate(self):
        # Explicit known paths only; never recursively remove a directory.
        if not self.backup_count:
            self.path.unlink(missing_ok=True)
            return
        for number in range(self.backup_count, 1, -1):
            source = Path(str(self.path) + "." + str(number - 1))
            target = Path(str(self.path) + "." + str(number))
            if source.exists() or source.is_symlink():
                os.replace(source, target)
        os.replace(self.path, Path(str(self.path) + ".1"))

    def purge(self, max_bytes: int, target_bytes: int, *, now=None,
              min_age_seconds: float = 300) -> dict:
        """FIFO cleanup of only this Journal and its configured rotation slots.

        Limits are combined logical bytes, independent of the per-file rotation
        bound. Nothing is deleted unless combined size exceeds max_bytes. Holds
        the same lock as emit/read/rotation for the whole operation. Recent logs
        are protected, so target_not_reached is an honest possible result. An old
        active file keeps its inode and newest complete JSONL records by in-place
        tail compaction/truncation; it is never unlinked. Unmanaged logs are not
        counted or touched, including similarly named slots above backup_count.
        """
        from cleanup import _as_utc, _purge_files
        timestamp = _as_utc(now).timestamp()
        names = [self.path.name] + [self.path.name + "." + str(number)
                                    for number in range(1, self.backup_count + 1)]
        with self._lock:
            return _purge_files(self._purge_anchor, max_bytes, target_bytes, now=timestamp,
                                min_age_seconds=min_age_seconds, names=names,
                                active_name=self.path.name)

    def error(self, event: str, exc: BaseException, **fields) -> dict:
        """Persist a failure without ever interpolating untrusted exception text."""
        fields.setdefault("reason", "exception raised; see error_type and traceback")
        fields["error_type"] = type(exc).__name__
        fields["traceback"] = _frames(exc)
        fields.pop("level", None)
        return self.emit(event, level="error", **fields)

    def read(self, *, offset: int = 0, limit: int = 100, task_id: str | None = None,
             account_id: str | None = None, level: str | None = None) -> dict:
        """Return newest-first filtered records, never cookies or raw export data."""
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a nonnegative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if level is not None:
            level = str(level).lower()
            if level == "warn":
                level = "warning"
            if level not in _LEVELS:
                raise ValueError("invalid diagnostic level")
        filters = {key: safe_message(value)[:160] for key, value in
                   (("task_id", task_id), ("account_id", account_id), ("level", level))
                   if value is not None}
        items, total = [], 0
        with self._lock:
            for number in range(self.backup_count + 1):
                path = self.path if not number else Path(str(self.path) + "." + str(number))
                try:
                    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                except FileNotFoundError:
                    continue
                with os.fdopen(fd, "rb") as stream:
                    for line in _reverse_lines(stream):
                        try:
                            entry = json.loads(line)
                        except (ValueError, UnicodeDecodeError):
                            # A crash/foreign writer may leave one incomplete line.
                            continue
                        if not isinstance(entry, dict) or not isinstance(entry.get("event"), str):
                            continue
                        if any(entry.get(k) != v for k, v in filters.items()):
                            continue
                        if total >= offset and len(items) < limit:
                            items.append(redact(entry))
                        total += 1
        return {"items": items, "total": total, "offset": offset, "limit": limit,
                "next_offset": offset + len(items) if offset + len(items) < total else None}

    def health(self) -> dict:
        return {"ok": self._last_write_error is None, "error_type": self._last_write_error,
                "last_write_at": self._last_write_at}


def get_journal(path=None) -> Journal:
    """Get a lazy per-path instance. Supplying cfg.data_dir explicitly is preferred."""
    if path is None:
        base = os.environ.get("MUSE2API_HOME") or os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(base, "data", "diagnostics.jsonl")
    key = os.path.abspath(os.fspath(path))
    with _REGISTRY_LOCK:
        found = _JOURNALS.get(key)
    if found is not None:
        return found
    candidate = Journal(key)
    with _REGISTRY_LOCK:
        return _JOURNALS.setdefault(key, candidate)
