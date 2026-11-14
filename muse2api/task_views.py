"""Read-only projections for authenticated task APIs; no app/store dependencies.

Integration: project_task(task, accounts=store.list_accounts(),
                          media_dir=CFG.media_dir, media_url_fn=media_url)

The callback accepts a filename, not a path. Its canonical URL establishes the
only trusted public origin for old URL-only records. No network requests, media
reads, directory scans, or task/account mutations occur here. A missing known
file is deleted; an unsafe/unverifiable location is unknown. Neither case
changes a completed task's status or starts generation again.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import math
import os
from pathlib import Path
import re
import stat
from typing import Any
from urllib.parse import urlsplit

__all__ = ["project_task"]

_REDACTED = "[REDACTED]"
_UUID = r"(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})"
_NAME = re.compile(rf"{_UUID}(?:\.[a-z0-9]{{1,8}})?", re.I)
_EXTENSIONS = {
    "image": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif", ".bmp",
              ".tif", ".tiff", ".heic", ".heif"},
    "video": {".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi", ".ogv", ".mpg", ".mpeg"},
    "audio": {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".aif", ".aiff", ".wma"},
}
_EXTENSION_KIND = {ext: kind for kind, extensions in _EXTENSIONS.items() for ext in extensions}
_GENERIC_EXTENSIONS = {"", ".bin", ".dat"}
_MIME_KIND = {
    "image/png": "image", "image/jpeg": "image", "image/jpg": "image",
    "image/webp": "image", "image/gif": "image", "image/avif": "image",
    "image/bmp": "image", "image/tiff": "image", "image/heic": "image", "image/heif": "image",
    "video/mp4": "video", "video/webm": "video", "video/quicktime": "video",
    "video/x-m4v": "video", "video/x-matroska": "video", "video/x-msvideo": "video",
    "video/ogg": "video", "video/mpeg": "video",
    "audio/mpeg": "audio", "audio/mp3": "audio", "audio/wav": "audio",
    "audio/x-wav": "audio", "audio/wave": "audio", "audio/mp4": "audio",
    "audio/m4a": "audio", "audio/x-m4a": "audio", "audio/aac": "audio",
    "audio/flac": "audio", "audio/x-flac": "audio", "audio/ogg": "audio",
    "audio/opus": "audio", "audio/aiff": "audio", "audio/x-aiff": "audio",
}
_SECRET_KEY = re.compile(
    r"authorization|cookie|token|password|passwd|passphrase|secret|credential|"
    r"otp|apikey|accesskey|privatekey|verificationcode|hatchsess|hatchvml|hatchgw|"
    r"hash|requestkey|idempotency|^(?:pwd|pin|codeverifier|session|sessionid)$")
_RAW_KEYS = {
    "base64", "b64", "b64json", "imagedata", "audiodata", "videodata", "mediadata",
    "raw", "rawdata", "rawreference", "rawreferences", "rawreferencedataurls",
    "referencedata", "referencedataurls", "referenceimages", "referenceimage",
    "referenceaudios", "referenceaudio", "referencevideos", "referencevideo",
    "referencefiles", "references", "images", "audios", "videos", "files",
    "html", "dom", "pagebody", "pagecontent", "uploadcontent", "filecontent",
}
_GENERATION_KEYS = (
    "model", "size", "aspect_ratio", "resolution", "width", "height", "duration",
    "reference_counts", "response_format", "n", "quality", "style", "seed", "extra", "timeout",
)
_DATA_URL = re.compile(r"\bdata:[^\s\x22\x27<>]+", re.I)
_FILE_URL = re.compile(r"\bfile:[^\s\x22\x27<>]+", re.I)
_HEADER = re.compile(r"(?im)\b(?:proxy-authorization|authorization|set-cookie|cookies?)\s*[:=]\s*[^\r\n]+")
_ASSIGNMENT = re.compile(
    r"(?P<key>[\x22\x27]?(?:(?:[a-z]+[_-])?token|password|passwd|pwd|passphrase|secret|"
    r"api[_-]?key|otp(?:[ _-]?code)?|verification[ _-]?code|code_verifier|"
    r"session(?:id)?|hatch_[\w-]+|验证码|取码链接)[\x22\x27]?\s*[:=]\s*)"
    r"(?:\x22[^\x22]*\x22|\x27[^\x27]*\x27|[^\s,;\}\]\x22\x27]+)", re.I)
_BEARER = re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.I)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?\b")
_OPAQUE = re.compile(r"(?<![\w])[A-Za-z0-9_+/=-]{48,}(?![\w])")
_URL = re.compile(r"\b(?:https?|socks5h?|socks4a?)://[^\s<>\x22\x27\x60]+", re.I)
_LOCAL_PATH = re.compile(
    r"(?<![\w:/])/(?!/|v1(?:/|$)|admin(?:/|$))[^\s\x22\x27<>\x60,;\)\]}]+|"
    r"\b[A-Za-z]:[\\/][^\s\x22\x27<>]+|\\\\[^\s\x22\x27<>]+")
_OMIT = object()


def _origin(parsed) -> tuple[str, str, int | None]:
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def _display_url(match: re.Match) -> str:
    try:
        parsed = urlsplit(match.group(0))
        if not parsed.hostname:
            return _REDACTED
        host = parsed.hostname
        if ":" in host:
            host = "[" + host + "]"
        port = ":" + str(parsed.port) if parsed.port else ""
        suffix = "/[REDACTED]" if parsed.path not in ("", "/") else parsed.path
        return parsed.scheme.lower() + "://" + host + port + suffix
    except ValueError:
        return _REDACTED


def _text(value: str, *, identifier: bool = False) -> str:
    value = _DATA_URL.sub("[REDACTED data URL]", value)
    value = _FILE_URL.sub("[REDACTED path]", value)
    value = _HEADER.sub(_REDACTED, value)
    value = _ASSIGNMENT.sub(lambda match: match["key"] + _REDACTED, value)
    value = _BEARER.sub(_REDACTED, value)
    value = _JWT.sub(_REDACTED, value)
    value = _URL.sub(_display_url, value)
    value = _LOCAL_PATH.sub("[REDACTED path]", value)
    if not identifier:
        value = _OPAQUE.sub(_REDACTED, value)
    return value


def _copy(value: Any, *, key: str = "", ancestors: frozenset[int] = frozenset(), depth: int = 0) -> Any:
    """Copy JSON-shaped values without stringifying arbitrary objects or secrets."""
    if depth > 24 or id(value) in ancestors:
        return _OMIT
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _text(value, identifier=key in {"id", "task_id", "account_id", "account"})
    ancestors = ancestors | {id(value)}
    if isinstance(value, Mapping):
        out = {}
        for name, item in value.items():
            if not isinstance(name, str):
                continue
            normalized = re.sub(r"[^a-z0-9]", "", name.lower())
            if (name.startswith("_") or _SECRET_KEY.search(normalized)
                    or normalized in {"key", "path", "filepath", "localpath", "mediapath", "downloadpath"}
                    or normalized.endswith("localpath")
                    or (normalized in _RAW_KEYS and not isinstance(item, (int, float, bool)))):
                continue
            copied = _copy(item, key=name, ancestors=ancestors, depth=depth + 1)
            if copied is not _OMIT:
                out[_text(name)] = copied
        return out
    if isinstance(value, (list, tuple)):
        return [copied for item in value
                if (copied := _copy(item, ancestors=ancestors, depth=depth + 1)) is not _OMIT]
    return _OMIT


def _safe_name(value: Any) -> str | None:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        return None
    ext = Path(value).suffix.lower()
    return value if ext in _EXTENSION_KIND or ext in _GENERIC_EXTENSIONS else None


def _canonical_url(name: str, media_url_fn: Callable[[str], str]) -> str:
    fallback = "/v1/media/" + name
    try:
        value = media_url_fn(name)
        if not isinstance(value, str) or any(ch.isspace() or ord(ch) < 32 for ch in value):
            return fallback
        parsed = urlsplit(value)
        if (parsed.query or parsed.fragment or parsed.username is not None or "\\" in value
                or "%" in parsed.path or any(part in {".", ".."} for part in parsed.path.split("/"))
                or not parsed.path.endswith(fallback)):
            return fallback
        if parsed.scheme:
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                return fallback
            _origin(parsed)
        elif parsed.netloc or value != fallback:
            return fallback
        return value
    except Exception:
        return fallback


def _name_from_url(value: Any, media_url_fn: Callable[[str], str]) -> str | None:
    if not isinstance(value, str) or not value or any(ch.isspace() or ord(ch) < 32 for ch in value):
        return None
    try:
        parsed = urlsplit(value)
        if ("\\" in value or "%" in parsed.path or parsed.username is not None
                or any(part in {".", ".."} for part in parsed.path.split("/"))):
            return None
        name = _safe_name(parsed.path.rsplit("/", 1)[-1])
        if not name:
            return None
        canonical = urlsplit(_canonical_url(name, media_url_fn))
        if parsed.scheme:
            if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
                    or not canonical.scheme or _origin(parsed) != _origin(canonical)
                    or parsed.path != canonical.path):
                return None
        elif parsed.netloc or parsed.path not in {"/v1/media/" + name, canonical.path}:
            return None
        return name
    except ValueError:
        return None


def _record(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _legacy_item(value: Any) -> Mapping[str, Any]:
    response = _record(value)
    data = response.get("data")
    if isinstance(data, (list, tuple)) and data:
        return _record(data[0])
    return response


def _positive_int(value: Any, *, zero: bool = False) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= (0 if zero else 1):
        return value
    return None


def _record_number(records: Sequence[Mapping[str, Any]], keys: Sequence[str], *, zero: bool = False) -> int | None:
    for record in records:
        for key in keys:
            if (number := _positive_int(record.get(key), zero=zero)) is not None:
                return number
    return None


def _probe_file(media_dir: str | os.PathLike[str], name: str) -> tuple[str, int | None]:
    """Stat just one direct child using a pinned directory; never follow symlinks."""
    fd = None
    try:
        root = os.path.abspath(os.fspath(media_dir))
        before = os.stat(root, follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode):
            return "unknown", None
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        pinned = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (pinned.st_dev, pinned.st_ino):
            return "unknown", None
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        after = os.stat(root, follow_symlinks=False)
        if ((after.st_dev, after.st_ino) != (pinned.st_dev, pinned.st_ino)
                or not stat.S_ISDIR(after.st_mode)
                or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            return "unknown", None
        return "available", info.st_size
    except FileNotFoundError:
        return "deleted", None
    except (OSError, TypeError, ValueError, NotImplementedError):
        return "unknown", None
    finally:
        if fd is not None:
            os.close(fd)


def _media(task: Mapping[str, Any], media_dir: str | os.PathLike[str],
           media_url_fn: Callable[[str], str]) -> dict[str, Any] | None:
    result = _record(task.get("result"))
    old_result = _legacy_item(result.get("image_response"))
    old_task = _legacy_item(task.get("image_response"))
    records = [result, old_result, old_task]
    name = None
    if result.get("filename") is not None:
        name = _safe_name(result["filename"])
    else:
        for value in (task.get("url"), result.get("url"), old_result.get("url"),
                      old_task.get("url"), _record(task.get("video")).get("url")):
            if (name := _name_from_url(value, media_url_fn)):
                break
    evidence = any(task.get(key) for key in ("url", "video", "image_response")) or bool(result)
    terminal_media = task.get("kind") in _EXTENSIONS and task.get("status") in {
        "completed", "succeeded", "success", "done"}
    if not name and not evidence and not terminal_media:
        return None
    out = {"url": None, "filename": name, "kind": None, "exists": False,
           "bytes": None, "availability": "unknown"}
    if not name:
        return out
    ext = Path(name).suffix.lower()
    kind = _EXTENSION_KIND.get(ext)
    if not kind:
        for record in records:
            mime = record.get("mime") or record.get("content_type")
            if isinstance(mime, str):
                kind = _MIME_KIND.get(mime.split(";", 1)[0].strip().lower())
            recorded_ext = record.get("ext")
            if not kind and isinstance(recorded_ext, str):
                kind = _EXTENSION_KIND.get("." + recorded_ext.lower().lstrip("."))
            if kind:
                break
    out["kind"] = kind
    out["bytes"] = _record_number(records, ("bytes", "size"), zero=True)
    dimensions = records + [_record(record.get("metadata")) for record in records]
    for dimension, alias in (("width", "w"), ("height", "h")):
        if (number := _record_number(dimensions, (dimension, alias))) is not None:
            out[dimension] = number
    if not kind:
        return out
    availability, actual_bytes = _probe_file(media_dir, name)
    out["availability"] = availability
    out["exists"] = availability == "available"
    if actual_bytes is not None:
        out["bytes"] = actual_bytes
    if availability != "unknown":
        out["url"] = _canonical_url(name, media_url_fn)
    return out


def _account(value: Mapping[str, Any], accounts: Sequence[Mapping[str, Any]]) -> tuple[str | None, Mapping[str, Any]]:
    aid = value.get("account_id") or value.get("account")
    if isinstance(aid, Mapping):
        aid = aid.get("id") or aid.get("account_id")
    if not isinstance(aid, str) or not aid:
        return None, {}
    for account in accounts:
        aliases = account.get("aliases")
        if account.get("id") == aid or (isinstance(aliases, (list, tuple)) and aid in aliases):
            return str(account.get("id") or aid), account
    return aid, {}


def _accounts(value: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [{**record, "id": record.get("id") or key} for key, record in value.items()
                if isinstance(key, str) and isinstance(record, Mapping)]
    return [record for record in value if isinstance(record, Mapping)]


def _generation(task: Mapping[str, Any]) -> dict[str, Any]:
    out = _copy(_record(task.get("generation")))
    for record in (_record(task.get("request")), _record(task.get("parameters")), task):
        for key in _GENERATION_KEYS:
            if key not in out and key in record:
                out.update(_copy({key: record[key]}))
    return out


def _image_response(value: Any, media: Mapping[str, Any] | None) -> dict[str, Any]:
    """Old OpenAI response envelopes carry only the first bounded, safe record."""
    response = _record(value)
    item = _legacy_item(response)
    out = {}
    if "created" in response:
        out["created"] = _copy(response["created"])
    safe_item = {}
    for key in ("revised_prompt", "size", "bytes", "width", "height"):
        if key in item:
            copied = _copy(item[key])
            if copied is not _OMIT:
                safe_item[key] = copied
    if media:
        if media.get("url"):
            safe_item["url"] = media["url"]
        if media.get("kind"):
            safe_item["kind"] = media["kind"]
    out["data"] = [safe_item] if safe_item else []
    return out


def project_task(
    task: Mapping[str, Any],
    accounts: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]],
    media_dir: str | os.PathLike[str],
    media_url_fn: Callable[[str], str],
) -> dict[str, Any]:
    """Return a detached JSON-safe task view for an authenticated caller.

    Accounts accepts the store's list or an id-to-record mapping, including
    aliases. The current account label is resolved at read time; historical
    labels explicitly recorded on attempts are retained. Generation parameters
    come only from persisted request/generation fields, never result byte sizes.
    Missing dimensions remain absent; missing byte sizes are None.
    """
    if not isinstance(task, Mapping):
        raise TypeError("task must be a mapping")
    out = _copy(task)
    account_records = _accounts(accounts)
    aid, account = _account(task, account_records)
    out["account_id"] = _text(aid, identifier=True) if aid else None
    label = account.get("label") if account else None
    out["account_label"] = _text(label) if isinstance(label, str) else None
    if "account" in out:
        out["account"] = out["account_id"]
    attempts = task.get("attempts")
    if isinstance(attempts, (list, tuple)):
        projected_attempts = []
        for attempt in attempts:
            if not isinstance(attempt, Mapping):
                continue
            safe_attempt = _copy(attempt)
            attempt_id, attempt_account = _account(attempt, account_records)
            safe_attempt["account_id"] = _text(attempt_id, identifier=True) if attempt_id else None
            historical = attempt.get("account_label")
            attempt_label = historical if isinstance(historical, str) else attempt_account.get("label")
            safe_attempt["account_label"] = _text(attempt_label) if isinstance(attempt_label, str) else None
            if "account" in safe_attempt:
                safe_attempt["account"] = safe_attempt["account_id"]
            projected_attempts.append(safe_attempt)
        out["attempts"] = projected_attempts
    out["generation"] = _generation(task)
    media = _media(task, media_dir, media_url_fn)
    out["media"] = media
    # Rebuild delivery fields from the one verified record, never an upstream URL.
    out.pop("url", None)
    if media and media.get("url"):
        out["url"] = media["url"]
    if isinstance(out.get("result"), dict):
        result = out["result"]
        result.pop("url", None)
        result.pop("filename", None)
        if media and media.get("filename"):
            result["filename"] = media["filename"]
        if media and media.get("url"):
            result["url"] = media["url"]
        if media and media.get("kind"):
            result["kind"] = media["kind"]
        elif result.get("kind") not in _EXTENSIONS:
            result.pop("kind", None)
        if "image_response" in result:
            result["image_response"] = _image_response(_record(task.get("result")).get("image_response"), media)
    if "image_response" in out:
        out["image_response"] = _image_response(task["image_response"], media)
    if "video" in out:
        out["video"] = {"url": media["url"]} if media and media.get("url") else None
    return out
