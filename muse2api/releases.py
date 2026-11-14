"""Read-only release checks for the maintained fork; never installs or restarts."""
from __future__ import annotations

import copy
import re
import threading
import time
from urllib.parse import urlsplit

import requests

REPOSITORY = "wxxz975/muse2api"
REPO_URL = "https://github.com/" + REPOSITORY
LATEST_RELEASE_URL = "https://api.github.com/repos/" + REPOSITORY + "/releases/latest"


def version_key(value: str):
    match = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.(\d+))?(?:[-+]([0-9A-Za-z.-]+))?", value.strip())
    if not match:
        return None
    major, minor, patch = (int(match.group(i) or 0) for i in (1, 2, 3))
    suffix = match.group(4)
    return (major, minor, patch, not bool(suffix), suffix or "")


def release_link(value, tag):
    if isinstance(value, str):
        try:
            parsed = urlsplit(value)
            if (parsed.scheme == "https" and parsed.netloc == "github.com"
                    and parsed.path.startswith("/" + REPOSITORY + "/releases/")
                    and not parsed.query and not parsed.fragment):
                return value
        except ValueError:
            pass
    # A fixed safe destination; upstream response URLs never become arbitrary links.
    return REPO_URL + "/releases"


class ReleaseChecker:
    def __init__(self, ttl: float = 90):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cached = None
        self._cached_at = 0.0
        self._local = None

    def check(self, local_version: str, *, force: bool = False, token: str = "") -> dict:
        with self._lock:
            now = time.monotonic()
            if (not force and self._cached is not None and self._local == local_version
                    and now - self._cached_at < self.ttl):
                return copy.deepcopy(self._cached)
            result = {"repo_url": REPO_URL, "local_version": local_version,
                      "remote_version": None, "has_update": False, "up_to_date": None,
                      "release_url": REPO_URL + "/releases", "checked_at": int(time.time()),
                      "status": "unknown", "error": None, "automatic_upgrade": False}
            headers = {"Accept": "application/vnd.github+json", "User-Agent": "muse2api-release-check"}
            if token:
                headers["Authorization"] = "Bearer " + token
            try:
                response = requests.get(LATEST_RELEASE_URL, headers=headers,
                                        timeout=6, allow_redirects=False)
                if response.status_code == 404:
                    result.update(status="no_releases")
                elif response.status_code != 200:
                    result.update(status="error", error=f"发布版本检查失败 (HTTP {response.status_code})")
                else:
                    data = response.json()
                    tag = data.get("tag_name") if isinstance(data, dict) else None
                    if (not isinstance(tag, str) or version_key(tag) is None
                            or data.get("draft") is True or data.get("prerelease") is True):
                        result.update(status="error", error="发布版本响应无有效正式版本")
                    else:
                        local = version_key(local_version)
                        if local is None:
                            result.update(status="error", error="本地版本格式无法比较")
                        else:
                            newer = version_key(tag) > local
                            result.update(status="available" if newer else "current", remote_version=tag,
                                          has_update=newer, up_to_date=not newer,
                                          release_url=release_link(data.get("html_url"), tag))
            except (requests.RequestException, ValueError, TypeError):
                # Never echo URLs, headers, token values, or uncontrolled exception messages.
                result.update(status="error", error="发布版本检查失败，请稍后重试")
            self._cached = copy.deepcopy(result)
            self._cached_at, self._local = time.monotonic(), local_version
            return result
