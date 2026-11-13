"""Persisted, hot-reloadable account maintenance settings (no credentials)."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pydantic import BaseModel, ConfigDict, Field


class MaintenanceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    keepalive_enabled: bool = True
    keepalive_interval_minutes: int = Field(default=360, ge=1, le=10080)
    quota_enabled: bool = True
    quota_interval_minutes: int = Field(default=60, ge=1, le=10080)
    include_disabled: bool = False


class MaintenanceSettingsStore:
    def __init__(self, path: str, defaults: MaintenanceSettings | None = None):
        self.path = path
        self._lock = threading.RLock()
        self._value = defaults or MaintenanceSettings()
        self.load_error = None
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as stream:
                    self._value = MaintenanceSettings.model_validate(json.load(stream))
            except (OSError, ValueError) as exc:
                # Leave the invalid file intact; show the fault without taking the API down.
                self.load_error = type(exc).__name__

    def get(self) -> MaintenanceSettings:
        with self._lock:
            return self._value.model_copy(deep=True)

    def update(self, changes: dict) -> MaintenanceSettings:
        with self._lock:
            value = MaintenanceSettings.model_validate({**self._value.model_dump(), **changes})
            directory = os.path.dirname(self.path) or "."
            os.makedirs(directory, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".maintenance-", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(value.model_dump(), stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self._value = value
            self.load_error = None
            return value.model_copy(deep=True)


def operation_due(account: dict, operation: str, interval_minutes: int,
                  now: float, force: bool = False) -> bool:
    """Successful runs obey the configured cadence; failures get a short backoff.

    Busy skips never move an attempt/success timestamp, so they retry next tick.
    Maintenance does not write last_used/use_count (business activity only).
    """
    if force:
        return True
    last_attempt = account.get(operation + "_checked_at") or 0
    last_success = (account.get("last_keepalive") if operation == "keepalive" else
                    (account.get("quota") or {}).get("checked_at")) or 0
    if now - last_attempt < min(interval_minutes * 60, 300):
        return False
    return not last_success or now - last_success >= interval_minutes * 60
