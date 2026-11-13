"""Opt-in, once-per-local-day storage cleanup; no cron and no background thread.

Integration: construct CleanupManager(media_dir, data/cleanup.json,
data/cleanup-state.json, journal), and call run_daily in a worker every ~30s.
get_settings/status are read-only. update_settings accepts a partial strict object.

Scheduling uses a durable local-date high-water mark, written and fsynced BEFORE
any deletion. Disabled ticks and ticks before the scheduled time do not claim a
day. Enabling after the time runs on the next tick; disabling/re-enabling, changing
the time or timezone, restarting, and a backwards clock never clear a claim. A
timezone change to an earlier local date waits until a date above the high-water
mark. DST folds run at most once; a missing wall time runs at the first valid
minute after the gap. A crashed claimed run is not retried that day.

Media accounting includes logical sizes of all top-level regular files, including
protected files, but never follows symlinks or descends into directories. Only
allowlisted media can be removed. New/changing files are protected; therefore a
configured target is not guaranteed. Reports contain counts, not names or bodies.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, time as wall_time, timedelta, timezone
try:
    import fcntl
except ImportError:  # Keep app/settings importable on Windows; deletion fails closed.
    fcntl = None
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_MIB = 1024 * 1024
_MIN_AGE_SECONDS = 300
_MEDIA_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".heic", ".heif",
    ".bmp", ".tif", ".tiff", ".mp4", ".webm", ".mov", ".m4v", ".3gp",
    ".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus",
})
_TEMP_NAME = re.compile(r"(?:^|[._-])(?:tmp|temp|part|partial|download|crdownload)(?:$|[._-])", re.I)
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_LOCK = threading.Lock()


def _supports_cleanup():
    return (fcntl is not None
            and all(hasattr(os, key) for key in
                    ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK", "pread", "pwrite"))
            and all(fn in os.supports_dir_fd for fn in (os.open, os.stat, os.unlink))
            and os.listdir in os.supports_fd)


class CleanupSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool = False
    check_time: str = "03:00"
    timezone: str = "Asia/Shanghai"
    media_enabled: bool = True
    media_max_mb: int = Field(default=10240, ge=1, le=1048576)
    media_target_mb: int = Field(default=8192, ge=0)
    logs_enabled: bool = True
    logs_max_mb: int = Field(default=100, ge=1, le=1048576)
    logs_target_mb: int = Field(default=80, ge=0)

    @field_validator("check_time")
    @classmethod
    def valid_time(cls, value: str) -> str:
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
            raise ValueError("check_time must be HH:MM (00:00..23:59)")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("timezone must be a valid IANA ZoneInfo name") from None
        return value

    @model_validator(mode="after")
    def targets_below_limits(self):
        if self.media_target_mb >= self.media_max_mb:
            raise ValueError("media_target_mb must be less than media_max_mb")
        if self.logs_target_mb >= self.logs_max_mb:
            raise ValueError("logs_target_mb must be less than logs_max_mb")
        return self


class _UnsafeDirectory(OSError):
    pass


def _identity(value):
    return value.st_dev, value.st_ino


def _file_version(value):
    return (_identity(value), value.st_mode, value.st_nlink, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


class _DirectoryAnchor:
    """Pin a directory identity, not a symlink destination that can change later."""
    def __init__(self, path):
        self.path = Path(os.path.abspath(os.fspath(path)))
        self.canonical = os.path.realpath(self.path)
        self.identity = None
        self.invalid = False
        try:
            value = os.lstat(self.path)
            if not stat.S_ISDIR(value.st_mode):
                self.invalid = True
            else:
                self.identity = _identity(value)
        except FileNotFoundError:
            pass
        except OSError:
            self.invalid = True

    def open(self):
        if self.invalid or os.path.realpath(self.path) != self.canonical:
            raise _UnsafeDirectory("directory identity changed")
        fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            value = os.fstat(fd)
            if self.identity is None:
                self.identity = _identity(value)
            self.verify(fd)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def verify(self, fd):
        value = os.lstat(self.path)
        if (not stat.S_ISDIR(value.st_mode) or _identity(value) != self.identity
                or _identity(os.fstat(fd)) != self.identity
                or os.path.realpath(self.path) != self.canonical):
            raise _UnsafeDirectory("directory identity changed")

    def verify_name(self, fd, name):
        self.verify(fd)
        if (not name or name in {".", ".."} or os.path.basename(name) != name
                or os.path.realpath(self.path / name) != os.path.join(self.canonical, name)):
            raise _UnsafeDirectory("file escaped the pinned directory")


def _error(operation, exc, reason):
    # Never interpolate exception text or a filename (either could contain secrets).
    return {"operation": operation, "error_type": type(exc).__name__, "reason": reason}


def _report(max_bytes, target_bytes, status="disabled"):
    return {"status": status, "max_bytes": max_bytes, "target_bytes": target_bytes,
            "bytes_before": 0, "bytes_after": 0, "bytes_removed": 0, "removed": 0,
            "files_removed": 0, "files_truncated": 0, "skipped": {}, "errors": [],
            "threshold_exceeded": False, "target_not_reached": False,
            "measurement_complete": True}


def _skip(result, reason):
    result["skipped"][reason] = result["skipped"].get(reason, 0) + 1


def _scan(anchor, fd, names, result, *, count_skips):
    anchor.verify(fd)
    entries = []
    for name in (os.listdir(fd) if names is None else names):
        try:
            value = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            result["errors"].append(_error("stat", exc, "file_stat_failed"))
            result["measurement_complete"] = False
            continue
        if stat.S_ISREG(value.st_mode):
            entries.append((name, value))
        elif count_skips:
            _skip(result, "symlink" if stat.S_ISLNK(value.st_mode) else
                  "directory" if stat.S_ISDIR(value.st_mode) else "non_regular")
    anchor.verify(fd)
    return entries


def _protection(name, value, now, min_age_seconds, *, media):
    if media:
        if name.startswith(".") or name.endswith("~"):
            return "protected_name"
        if _TEMP_NAME.search(name):
            return "temporary"
        if Path(name).suffix.lower() not in _MEDIA_EXTENSIONS:
            return "extension"
    if value.st_nlink != 1:
        return "hardlink"
    if now - value.st_mtime < min_age_seconds:
        return "recent"
    return None


def _open_candidate(anchor, fd, name, snapshot, *, writable):
    anchor.verify_name(fd, name)
    flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW | os.O_NONBLOCK
    item_fd = os.open(name, flags, dir_fd=fd)
    try:
        current = os.fstat(item_fd)
        if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                or _file_version(current) != _file_version(snapshot)):
            raise _UnsafeDirectory("file changed after scan")
        anchor.verify_name(fd, name)
        at_name = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if _file_version(at_name) != _file_version(current):
            raise _UnsafeDirectory("file changed after open")
        return item_fd
    except BaseException:
        os.close(item_fd)
        raise


def _remove_entry(anchor, fd, name, snapshot, item_fd):
    anchor.verify_name(fd, name)
    current = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if (_file_version(current) != _file_version(snapshot)
            or _file_version(os.fstat(item_fd)) != _file_version(snapshot)):
        raise _UnsafeDirectory("file changed before unlink")
    # Relative unlink never follows a last-moment symlink into another directory.
    os.unlink(name, dir_fd=fd)


def _trim_active(anchor, fd, name, snapshot, item_fd, keep_bytes):
    """Keep newest complete JSONL lines in-place; never unlink an active writer path."""
    anchor.verify_name(fd, name)
    if (_file_version(os.stat(name, dir_fd=fd, follow_symlinks=False)) != _file_version(snapshot)
            or _file_version(os.fstat(item_fd)) != _file_version(snapshot)):
        raise _UnsafeDirectory("active log changed before truncate")
    start = max(0, snapshot.st_size - keep_bytes)
    if start and keep_bytes:
        # A retained tail must start on a record boundary, not in UTF-8/JSON text.
        if os.pread(item_fd, 1, start - 1) != b"\n":
            while start < snapshot.st_size:
                chunk = os.pread(item_fd, min(65536, snapshot.st_size - start), start)
                if not chunk:
                    raise OSError("unexpected short log read")
                newline = chunk.find(b"\n")
                start += newline + 1 if newline >= 0 else len(chunk)
                if newline >= 0:
                    break
    copied, remaining = 0, snapshot.st_size - start
    while copied < remaining:
        chunk = os.pread(item_fd, min(65536, remaining - copied), start + copied)
        if not chunk:
            raise OSError("unexpected short log read")
        written = 0
        while written < len(chunk):
            count = os.pwrite(item_fd, chunk[written:], copied + written)
            if not count:
                raise OSError("unexpected short log write")
            written += count
        copied += len(chunk)
    os.ftruncate(item_fd, remaining)
    os.fsync(item_fd)
    return snapshot.st_size - remaining


def _purge_files(anchor, max_bytes, target_bytes, *, now, min_age_seconds=_MIN_AGE_SECONDS,
                 names=None, active_name=None):
    """Shared by media cleanup and Journal.purge (which must hold the journal lock)."""
    for value in (max_bytes, target_bytes):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("cleanup byte limits must be integers")
    if not 0 <= target_bytes < max_bytes:
        raise ValueError("target_bytes must be nonnegative and less than max_bytes")
    if (isinstance(min_age_seconds, bool) or not isinstance(min_age_seconds, (int, float))
            or not math.isfinite(min_age_seconds) or min_age_seconds < 0):
        raise ValueError("min_age_seconds must be finite and nonnegative")
    result = _report(max_bytes, target_bytes, "within_limit")
    if not _supports_cleanup():
        result.update(status="error", bytes_before=None, bytes_after=None,
                      measurement_complete=False, target_not_reached=None)
        result["errors"].append({"operation": "platform", "error_type": "UnsupportedPlatform",
                                 "reason": "safe_cleanup_primitives_unavailable"})
        return result
    fd = None
    try:
        fd = anchor.open()
        entries = _scan(anchor, fd, names, result, count_skips=True)
        total = sum(value.st_size for _, value in entries)
        result["bytes_before"] = result["bytes_after"] = total
        result["threshold_exceeded"] = total > max_bytes
        candidates = []
        for name, value in entries:
            protection = _protection(name, value, now, min_age_seconds, media=names is None)
            if protection:
                _skip(result, protection)
            else:
                candidates.append((name, value))
        if result["threshold_exceeded"]:
            for name, value in sorted(candidates, key=lambda item: (item[1].st_mtime_ns, item[0])):
                if total <= target_bytes:
                    break
                item_fd = None
                try:
                    item_fd = _open_candidate(anchor, fd, name, value, writable=name == active_name)
                    if name == active_name:
                        keep = max(0, target_bytes - (total - value.st_size))
                        removed = _trim_active(anchor, fd, name, value, item_fd, keep)
                        result["files_truncated"] += 1
                    else:
                        _remove_entry(anchor, fd, name, value, item_fd)
                        removed = value.st_size
                        result["files_removed"] += 1
                    total -= removed
                    result["bytes_removed"] += removed
                except FileNotFoundError:
                    _skip(result, "changed")
                except _UnsafeDirectory as exc:
                    _skip(result, "changed")
                    result["errors"].append(_error("delete", exc, "identity_changed"))
                    # Directory substitution aborts the run; a changed file can be skipped.
                    anchor.verify(fd)
                except OSError as exc:
                    result["errors"].append(_error("delete", exc, "deletion_failed"))
                finally:
                    if item_fd is not None:
                        os.close(item_fd)
        current = _scan(anchor, fd, names, result, count_skips=False)
        result["bytes_after"] = sum(value.st_size for _, value in current)
        result["target_not_reached"] = (result["threshold_exceeded"]
                                        and result["bytes_after"] > target_bytes)
        result["status"] = ("partial" if result["errors"] or result["target_not_reached"] else
                            "cleaned" if result["threshold_exceeded"] else "within_limit")
    except FileNotFoundError as exc:
        if fd is None and anchor.identity is None:
            result["status"] = "no_files"
        else:
            result["errors"].append(_error("directory", exc, "directory_missing"))
            result["status"] = "error"
            result["bytes_after"] = None
            result["measurement_complete"] = False
            result["target_not_reached"] = None
    except OSError as exc:
        result["errors"].append(_error("directory", exc, "unsafe_or_unavailable_directory"))
        result["status"] = "error"
        if fd is None:
            result["bytes_before"] = None
        result["bytes_after"] = None
        result["measurement_complete"] = False
        result["target_not_reached"] = None
    finally:
        if fd is not None:
            os.close(fd)
    result["removed"] = result["bytes_removed"]
    return result


def _utc_now():
    return datetime.now(timezone.utc)


def _as_utc(now=None):
    if now is None:
        return _utc_now()
    if isinstance(now, datetime):
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be a timezone-aware datetime or UNIX timestamp")
        return now.astimezone(timezone.utc)
    if isinstance(now, (int, float)) and not isinstance(now, bool) and math.isfinite(now):
        return datetime.fromtimestamp(now, timezone.utc)
    raise ValueError("now must be a timezone-aware datetime or UNIX timestamp")


def _scheduled_for(day, zone, check_time):
    hour, minute = map(int, check_time.split(":"))
    naive = datetime.combine(day, wall_time(hour, minute))
    # Includes unusual transitions that skip a whole local date.
    for offset in range(1441):
        candidate = naive + timedelta(minutes=offset)
        valid = []
        for fold in (0, 1):
            value = candidate.replace(tzinfo=zone, fold=fold)
            utc = value.astimezone(timezone.utc)
            if utc.astimezone(zone).replace(tzinfo=None) == candidate:
                valid.append(utc)
        if valid:
            return min(valid)
    raise ValueError("could not resolve daily scheduled time")


def _next_run(settings, state, now):
    if not settings.enabled:
        return None
    zone = ZoneInfo(settings.timezone)
    day = now.astimezone(zone).date()
    if state["last_checked_date"]:
        day = max(day, date.fromisoformat(state["last_checked_date"]) + timedelta(days=1))
    return _scheduled_for(day, zone, settings.check_time).isoformat()


def _read_json(path):
    if path.is_symlink():
        raise OSError("cleanup settings/state must not be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        value = os.fstat(stream.fileno())
        if not stat.S_ISREG(value.st_mode) or value.st_size > 1024 * 1024:
            raise ValueError("invalid cleanup settings/state file")
        return json.load(stream)


def _atomic_json(path, value):
    """A directory fsync is part of success: an unpersisted claim cannot delete."""
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError("cleanup settings/state must not be a symlink")
    fd, temporary = tempfile.mkstemp(prefix=".cleanup-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class CleanupManager:
    def __init__(self, media_dir, settings_path, state_path, journal):
        self.media_dir = Path(os.path.abspath(os.fspath(media_dir)))
        self.settings_path = Path(os.path.abspath(os.fspath(settings_path)))
        self.state_path = Path(os.path.abspath(os.fspath(state_path)))
        if self.settings_path == self.state_path:
            raise ValueError("cleanup settings and state paths must differ")
        self.journal = journal
        self._media_anchor = _DirectoryAnchor(self.media_dir)
        with _LOCKS_LOCK:
            self._lock = _LOCKS.setdefault(str(self.state_path), threading.RLock())
        self._settings = CleanupSettings()
        self._state = {"last_checked_date": None, "last_run": None, "last_result": None}
        self._settings_error = self._state_error = None
        with self._lock:
            self._reload_settings()
            self._reload_state()

    def _reload_settings(self):
        try:
            self._settings = CleanupSettings.model_validate(_read_json(self.settings_path))
            self._settings_error = None
        except FileNotFoundError:
            self._settings = CleanupSettings()
            self._settings_error = None
        except (OSError, ValueError) as exc:
            self._settings = CleanupSettings()  # Malformed persisted opt-in fails closed.
            self._settings_error = type(exc).__name__

    def _reload_state(self):
        try:
            state = _read_json(self.state_path)
            if not isinstance(state, dict):
                raise ValueError("invalid cleanup state")
            day = state.get("last_checked_date")
            if day is not None and (not isinstance(day, str) or date.fromisoformat(day).isoformat() != day):
                raise ValueError("invalid cleanup date")
            run = state.get("last_run")
            if run is not None:
                _as_utc(datetime.fromisoformat(run))
            if state.get("last_result") is not None and not isinstance(state["last_result"], dict):
                raise ValueError("invalid cleanup result")
            self._state = {"last_checked_date": day, "last_run": run,
                           "last_result": state.get("last_result")}
            self._state_error = None
        except FileNotFoundError:
            self._state = {"last_checked_date": None, "last_run": None, "last_result": None}
            self._state_error = None
        except (OSError, ValueError, TypeError) as exc:
            self._state_error = type(exc).__name__

    @contextmanager
    def _state_file_lock(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        path = Path(str(self.state_path) + ".lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_NONBLOCK", 0), 0o600)
        try:
            value = os.fstat(fd)
            if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
                raise OSError("invalid cleanup lock file")
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            # Only settings can reach this on unsupported platforms. run_daily
            # rejects deletion without both cross-process locking and dirfd safety.
            yield
        finally:
            os.close(fd)

    def get_settings(self) -> dict:
        with self._lock:
            self._reload_settings()
            return self._settings.model_dump()

    def update_settings(self, changes: dict) -> dict:
        if not isinstance(changes, dict):
            raise ValueError("cleanup settings must be an object")
        with self._lock, self._state_file_lock():
            self._reload_settings()
            value = CleanupSettings.model_validate({**self._settings.model_dump(), **changes})
            _atomic_json(self.settings_path, value.model_dump())
            self._settings, self._settings_error = value, None
            return value.model_dump()

    def status(self) -> dict:
        with self._lock:
            self._reload_settings()
            self._reload_state()
            result = deepcopy(self._state)
            result["next_run"] = (_next_run(self._settings, self._state, _utc_now())
                                  if not self._settings_error and not self._state_error
                                  and _supports_cleanup() else None)
            if not _supports_cleanup():
                result["cleanup_supported"] = False
                result["unsupported_reason"] = "safe_cleanup_primitives_unavailable"
            if self._settings_error:
                result["settings_error"] = self._settings_error
            if self._state_error:
                result["state_error"] = self._state_error
            return result

    def _write_state(self, state):
        _atomic_json(self.state_path, state)
        self._state = deepcopy(state)
        self._state_error = None

    def run_daily(self, now=None) -> dict:
        utc = _as_utc(now)
        with self._lock:
            self._reload_settings()
            if self._settings_error:
                return {"status": "error", "errors": [{"operation": "settings",
                        "error_type": self._settings_error, "reason": "invalid_settings"}]}
            if not self._settings.enabled:
                return {"status": "disabled", "errors": []}
            if not _supports_cleanup():
                return {"status": "error", "errors": [{"operation": "platform",
                        "error_type": "UnsupportedPlatform",
                        "reason": "safe_cleanup_primitives_unavailable"}]}
            try:
                with self._state_file_lock():
                    self._reload_settings()
                    self._reload_state()
                    if self._settings_error or self._state_error:
                        return {"status": "error", "errors": [{"operation": "load",
                                "error_type": self._settings_error or self._state_error,
                                "reason": "invalid_settings_or_state"}]}
                    settings = self._settings
                    if not settings.enabled:
                        return {"status": "disabled", "errors": []}
                    zone = ZoneInfo(settings.timezone)
                    day = utc.astimezone(zone).date()
                    if self._state["last_checked_date"] and day.isoformat() <= self._state["last_checked_date"]:
                        return {"status": "already_checked", "errors": [],
                                "next_run": _next_run(settings, self._state, utc)}
                    if utc < _scheduled_for(day, zone, settings.check_time):
                        return {"status": "not_due", "errors": [],
                                "next_run": _next_run(settings, self._state, utc)}
                    result = {"status": "claimed", "checked_date": day.isoformat(),
                              "started_at": utc.isoformat(), "finished_at": None,
                              "media": None, "logs": None, "errors": []}
                    state = {"last_checked_date": day.isoformat(), "last_run": utc.isoformat(),
                             "last_result": result}
                    try:
                        self._write_state(state)
                    except OSError as exc:
                        return {"status": "claim_failed",
                                "errors": [_error("claim", exc, "claim_not_durable")]}
                    result["media"] = (_purge_files(self._media_anchor, settings.media_max_mb * _MIB,
                                        settings.media_target_mb * _MIB, now=utc.timestamp())
                                       if settings.media_enabled else
                                       _report(settings.media_max_mb * _MIB, settings.media_target_mb * _MIB))
                    if not settings.logs_enabled:
                        result["logs"] = _report(settings.logs_max_mb * _MIB, settings.logs_target_mb * _MIB)
                    else:
                        try:
                            result["logs"] = self.journal.purge(settings.logs_max_mb * _MIB,
                                                               settings.logs_target_mb * _MIB, now=utc)
                        except Exception as exc:
                            result["logs"] = _report(settings.logs_max_mb * _MIB, settings.logs_target_mb * _MIB, "error")
                            result["logs"].update(bytes_before=None, bytes_after=None, measurement_complete=False,
                                                   target_not_reached=None)
                            result["logs"]["errors"].append(_error("logs", exc, "journal_purge_failed"))
                    result["status"] = ("partial" if any(value["status"] in {"error", "partial"}
                                         for value in (result["media"], result["logs"])) else "completed")
                    result["finished_at"] = _utc_now().isoformat()
                    state["last_result"] = result
                    try:
                        self._write_state(state)
                    except OSError as exc:
                        # The pre-delete claim remains durable, even if reporting failed.
                        result["status"] = "partial"
                        result["errors"].append(_error("state", exc, "result_not_persisted_claim_retained"))
                        self._state = deepcopy(state)
                    return deepcopy(result)
            except OSError as exc:
                return {"status": "claim_failed", "errors": [_error("lock", exc, "claim_lock_unavailable")]}
