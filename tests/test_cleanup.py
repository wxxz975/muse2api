"""Offline behavioral retention tests. All deletion happens in pytest tmp_path.

Run: python3 -m pytest -q tests/test_cleanup.py tests/test_diagnostics.py
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import sys
import threading
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cleanup
from cleanup import CleanupManager, CleanupSettings
from diagnostics import Journal

MIB = 1024 * 1024
NOW = datetime(2026, 11, 14, 4, tzinfo=ZoneInfo("Asia/Shanghai"))
STAMP = NOW.timestamp()


def write_file(path, size, *, mtime=STAMP - 1000, content=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if content is None:
        with path.open("wb") as stream:
            stream.truncate(size)
    else:
        path.write_bytes(content)
    os.utime(path, (mtime, mtime))
    return path


def manager(tmp_path, *, journal=None, **settings):
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    journal = journal or Journal(tmp_path / "diagnostics.jsonl")
    value = CleanupManager(media, tmp_path / "cleanup.json", tmp_path / "cleanup-state.json", journal)
    if settings:
        value.update_settings(settings)
    return value


def enabled_manager(tmp_path, **settings):
    return manager(tmp_path, enabled=True, logs_enabled=False, media_max_mb=1,
                   media_target_mb=0, **settings)


@pytest.mark.parametrize("changes", [
    {"media_max_mb": 0}, {"media_max_mb": 1048577}, {"media_max_mb": True},
    {"media_max_mb": "5"}, {"media_max_mb": 2.0}, {"media_target_mb": -1},
    {"media_target_mb": 10240}, {"logs_max_mb": 0}, {"logs_max_mb": 1048577},
    {"logs_target_mb": 100}, {"logs_target_mb": -1}, {"logs_target_mb": False},
    {"enabled": 1}, {"media_enabled": "true"}, {"logs_enabled": None},
    {"check_time": "3:00"}, {"check_time": "24:00"}, {"check_time": "03:60"},
    {"check_time": "03:00:00"}, {"timezone": "Invalid/Moon"},
    {"timezone": "../UTC"}, {"timezone": "/UTC"}, {"unrecognized": 1},
])
def test_strict_settings_reject_invalid_without_mutation(tmp_path, changes):
    value = manager(tmp_path)
    expected = value.update_settings({"check_time": "04:00"})
    persisted = value.settings_path.read_bytes()
    with pytest.raises(ValueError):
        value.update_settings(changes)
    assert value.get_settings() == expected
    assert value.settings_path.read_bytes() == persisted
    assert not value.state_path.exists()


def test_defaults_settings_persist_and_reads_never_delete(tmp_path):
    value = manager(tmp_path)
    media = write_file(value.media_dir / "old.png", 2 * MIB)
    assert value.get_settings() == {"enabled": False, "check_time": "03:00",
        "timezone": "Asia/Shanghai", "media_enabled": True, "media_max_mb": 10240,
        "media_target_mb": 8192, "logs_enabled": True, "logs_max_mb": 100, "logs_target_mb": 80}
    assert value.status() == {"last_checked_date": None, "last_run": None,
                              "last_result": None, "next_run": None}
    assert not value.settings_path.exists() and not value.state_path.exists()
    assert value.run_daily(NOW)["status"] == "disabled"
    value.update_settings({"enabled": True, "timezone": "UTC", "media_max_mb": 1, "media_target_mb": 0})
    reopened = manager(tmp_path)
    assert reopened.get_settings()["timezone"] == "UTC"
    assert reopened.status()["next_run"]
    assert media.exists() and not value.state_path.exists()
    assert stat.S_IMODE(value.settings_path.stat().st_mode) == 0o600
    assert json.loads(value.settings_path.read_text())["enabled"] is True


def test_daily_schedule_restart_and_setting_changes_never_reclaim_day(tmp_path, monkeypatch):
    value = enabled_manager(tmp_path)
    file = write_file(value.media_dir / "old.png", 2 * MIB, mtime=STAMP - 7200)
    before = NOW.replace(hour=2, minute=59, second=59)
    assert value.run_daily(before)["status"] == "not_due"
    assert not value.state_path.exists() and file.exists()
    assert value.run_daily(NOW.replace(hour=3))["status"] == "completed"
    assert not file.exists()
    write_file(file, 2 * MIB)
    reopened = manager(tmp_path)
    assert reopened.run_daily(NOW)["status"] == "already_checked"
    reopened.update_settings({"enabled": False, "check_time": "00:00"})
    assert reopened.run_daily(NOW)["status"] == "disabled"
    reopened.update_settings({"enabled": True, "check_time": "23:00"})
    assert reopened.run_daily(NOW.replace(hour=23))["status"] == "already_checked"
    assert reopened.run_daily(NOW - timedelta(days=1))["status"] == "already_checked"
    monkeypatch.setattr(cleanup, "_utc_now", lambda: NOW.astimezone(timezone.utc))
    assert reopened.status()["next_run"] == "2026-11-15T15:00:00+00:00"
    next_day = NOW + timedelta(days=1)
    assert reopened.run_daily(next_day)["status"] == "not_due"
    assert reopened.run_daily(next_day.replace(hour=23))["status"] == "completed"
    assert not file.exists()


def test_enable_and_move_schedule_after_time_runs_on_next_tick(tmp_path):
    value = manager(tmp_path)
    assert value.run_daily(NOW)["status"] == "disabled"
    value.update_settings({"enabled": True, "check_time": "05:00", "logs_enabled": False})
    assert value.run_daily(NOW)["status"] == "not_due"
    value.update_settings({"check_time": "03:00"})
    assert value.run_daily(NOW)["status"] == "completed"
    assert value.run_daily(NOW)["status"] == "already_checked"


def test_timezone_switch_and_utc_midnight_are_local_daily(tmp_path):
    value = enabled_manager(tmp_path)
    assert value.run_daily(NOW)["status"] == "completed"  # 14th Shanghai, 13th UTC.
    value.update_settings({"timezone": "UTC", "check_time": "00:00"})
    assert value.run_daily(NOW)["status"] == "already_checked"
    assert value.run_daily(datetime(2026, 11, 14, 0, tzinfo=timezone.utc))["status"] == "already_checked"
    assert value.run_daily(datetime(2026, 11, 15, 0, tzinfo=timezone.utc))["status"] == "completed"


def test_dst_gap_runs_first_valid_minute_fold_runs_only_once(tmp_path, monkeypatch):
    zone = ZoneInfo("America/New_York")
    value = enabled_manager(tmp_path, timezone=zone.key, check_time="02:30")
    before = datetime(2026, 3, 8, 1, 59, 59, tzinfo=zone)
    monkeypatch.setattr(cleanup, "_utc_now", lambda: before.astimezone(timezone.utc))
    assert value.status()["next_run"] == "2026-03-08T07:00:00+00:00"
    assert value.run_daily(before)["status"] == "not_due"
    assert value.run_daily(datetime(2026, 3, 8, 3, 0, tzinfo=zone))["status"] == "completed"
    assert value.run_daily(datetime(2026, 3, 8, 3, 30, tzinfo=zone))["status"] == "already_checked"
    value.update_settings({"check_time": "01:30"})
    first = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    second = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
    assert value.run_daily(first)["status"] == "completed"
    assert manager(tmp_path).run_daily(second)["status"] == "already_checked"


@pytest.mark.parametrize("bad_now", [datetime(2026, 11, 14), "2026-11-14", True, float("nan")])
def test_now_requires_explicit_timezone_or_timestamp(tmp_path, bad_now):
    with pytest.raises(ValueError):
        enabled_manager(tmp_path).run_daily(bad_now)


def test_concurrent_managers_claim_once_before_delete(tmp_path, monkeypatch):
    first = enabled_manager(tmp_path)
    second = manager(tmp_path)
    write_file(first.media_dir / "old.png", 2 * MIB)
    real_purge = cleanup._purge_files
    calls = []

    def checked(*args, **kwargs):
        state = json.loads(first.state_path.read_text())
        assert state["last_checked_date"] == "2026-11-14"
        assert state["last_result"]["status"] == "claimed"
        assert state["last_result"]["media"] is None
        calls.append(1)
        return real_purge(*args, **kwargs)

    monkeypatch.setattr(cleanup, "_purge_files", checked)
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda n: (first if n % 2 else second).run_daily(STAMP), range(16)))
    assert len(calls) == 1
    assert sum(result["status"] == "completed" for result in outcomes) == 1
    assert sum(result["status"] == "already_checked" for result in outcomes) == 15


@pytest.mark.parametrize("failure", ["replace", "fsync"])
def test_claim_persistence_failure_never_deletes(tmp_path, monkeypatch, failure):
    value = enabled_manager(tmp_path)
    file = write_file(value.media_dir / "old.png", 2 * MIB)

    def denied(*args, **kwargs):
        raise PermissionError("private-path-cookie-do-not-copy")

    monkeypatch.setattr(cleanup.os, failure, denied)
    result = value.run_daily(NOW)
    assert result["status"] == "claim_failed" and file.exists()
    assert "private-path-cookie" not in json.dumps(result)
    assert not list(tmp_path.glob(".cleanup-*.tmp"))


def test_result_persistence_failure_keeps_predelete_claim(tmp_path, monkeypatch):
    value = enabled_manager(tmp_path)
    write_file(value.media_dir / "old.png", 2 * MIB)
    real_write = value._write_state
    calls = []

    def fail_result(state):
        calls.append(1)
        if len(calls) > 1:
            raise OSError("secret")
        return real_write(state)

    monkeypatch.setattr(value, "_write_state", fail_result)
    result = value.run_daily(NOW)
    assert result["status"] == "partial"
    assert result["errors"][0]["reason"] == "result_not_persisted_claim_retained"
    assert json.loads(value.state_path.read_text())["last_result"]["status"] == "claimed"
    assert manager(tmp_path).run_daily(NOW)["status"] == "already_checked"


def test_crash_after_claim_is_not_retried_on_restart(tmp_path, monkeypatch):
    value = enabled_manager(tmp_path)
    file = write_file(value.media_dir / "old.png", 2 * MIB)

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cleanup, "_purge_files", interrupted)
    with pytest.raises(KeyboardInterrupt):
        value.run_daily(NOW)
    assert file.exists()
    assert manager(tmp_path).run_daily(NOW)["status"] == "already_checked"


def test_invalid_settings_or_state_fail_closed(tmp_path):
    value = enabled_manager(tmp_path)
    file = write_file(value.media_dir / "old.png", 2 * MIB)
    value.state_path.write_text("not-json")
    assert value.run_daily(NOW)["status"] == "error"
    assert value.status()["state_error"] == "JSONDecodeError"
    assert value.state_path.read_text() == "not-json" and file.exists()
    value.settings_path.write_text("not-json")
    assert manager(tmp_path).run_daily(NOW)["status"] == "error"
    assert value.get_settings()["enabled"] is False and file.exists()


@pytest.mark.parametrize("size", [MIB - 1, MIB])
def test_below_or_exact_max_never_deletes_even_above_target(tmp_path, size):
    value = enabled_manager(tmp_path)
    file = write_file(value.media_dir / "old.png", size)
    result = value.run_daily(NOW)["media"]
    assert result["status"] == "within_limit" and file.exists()
    assert result["bytes_before"] == result["bytes_after"] == size
    assert result["bytes_removed"] == 0 and result["threshold_exceeded"] is False


def test_fifo_mtime_then_name_stops_at_target_not_empty(tmp_path):
    value = manager(tmp_path, enabled=True, logs_enabled=False, media_max_mb=3, media_target_mb=2)
    # Creation and lexicographic order differ from mtime ordering.
    newest = write_file(value.media_dir / "0-newest.png", MIB, mtime=STAMP - 500)
    later_tie = write_file(value.media_dir / "b.mp4", MIB, mtime=STAMP - 1000)
    earlier_tie = write_file(value.media_dir / "a.webp", MIB, mtime=STAMP - 1000)
    oldest = write_file(value.media_dir / "z-oldest.jpg", MIB, mtime=STAMP - 2000)
    result = value.run_daily(NOW)["media"]
    assert result["status"] == "cleaned"
    assert not oldest.exists() and not earlier_tie.exists()
    assert later_tie.exists() and newest.exists()
    assert result["bytes_before"] == 4 * MIB and result["bytes_after"] == 2 * MIB
    assert result["bytes_removed"] == 2 * MIB and result["files_removed"] == 2
    assert result["target_not_reached"] is False


def test_media_skips_recent_temp_unknown_hardlinks_directories_and_symlinks(tmp_path):
    value = enabled_manager(tmp_path)
    media = value.media_dir
    old = write_file(media / "valid.PNG", MIB)
    recent = write_file(media / "new.mp4", MIB, mtime=STAMP - 299)
    temp = write_file(media / "movie.part.mp4", 100)
    hidden = write_file(media / ".gitkeep", 10)
    unknown = write_file(media / "accounts.json", 20, content=b"sensitive-body")
    external = write_file(tmp_path / "outside.png", MIB)
    (media / "link.png").symlink_to(external)
    (media / "dir-link").symlink_to(tmp_path, target_is_directory=True)
    (media / "broken.mp4").symlink_to(tmp_path / "absent")
    os.link(external, media / "hard.png")
    nested = write_file(media / "nested" / "never.png", MIB)
    result = value.run_daily(NOW)["media"]
    assert not old.exists()
    assert all(p.exists() for p in [recent, temp, hidden, unknown, external, nested, media / "hard.png"])
    assert (media / "link.png").is_symlink() and (media / "broken.mp4").is_symlink()
    assert result["skipped"] == {"symlink": 3, "directory": 1, "recent": 1,
                                   "temporary": 1, "protected_name": 1, "extension": 1, "hardlink": 1}
    assert result["status"] == "partial" and result["target_not_reached"] is True
    assert result["bytes_before"] == 3 * MIB + 100 + 10 + len(b"sensitive-body")
    assert "sensitive-body" not in json.dumps(result)


def test_min_age_boundary_exactly_300_is_eligible(tmp_path):
    value = enabled_manager(tmp_path)
    file = write_file(value.media_dir / "eligible.png", 2 * MIB, mtime=STAMP - 300)
    assert value.run_daily(NOW)["media"]["files_removed"] == 1
    assert not file.exists()


@pytest.mark.parametrize("when", ["before", "during"])
def test_media_directory_replaced_with_symlink_cannot_delete_outside(tmp_path, monkeypatch, when):
    value = enabled_manager(tmp_path)
    source = write_file(value.media_dir / "old.png", 2 * MIB)
    outside = write_file(tmp_path / "outside" / "old.png", 2 * MIB)
    pinned = tmp_path / "original-media"

    def swap():
        value.media_dir.rename(pinned)
        value.media_dir.symlink_to(outside.parent, target_is_directory=True)

    if when == "before":
        swap()
    else:
        real_open = cleanup._open_candidate

        def swapped(*args, **kwargs):
            swap()
            return real_open(*args, **kwargs)

        monkeypatch.setattr(cleanup, "_open_candidate", swapped)
    result = value.run_daily(NOW)["media"]
    assert result["status"] == "error" and result["measurement_complete"] is False
    assert outside.stat().st_size == 2 * MIB and (pinned / source.name).exists()


def test_changing_file_write_race_skipped_and_measured(tmp_path, monkeypatch):
    value = enabled_manager(tmp_path)
    file = write_file(value.media_dir / "old.png", 2 * MIB)
    real_open = cleanup._open_candidate

    def writing(*args, **kwargs):
        with file.open("ab") as stream:
            stream.write(b"growing")
        os.utime(file, (STAMP, STAMP))
        return real_open(*args, **kwargs)

    monkeypatch.setattr(cleanup, "_open_candidate", writing)
    result = value.run_daily(NOW)["media"]
    assert file.exists() and result["files_removed"] == 0
    assert result["bytes_after"] == 2 * MIB + len(b"growing")
    assert result["skipped"]["changed"] == 1 and result["target_not_reached"] is True


def test_file_symlink_substitution_between_scan_and_unlink_is_never_followed(tmp_path, monkeypatch):
    value = enabled_manager(tmp_path)
    file = write_file(value.media_dir / "old.png", 2 * MIB)
    outside = write_file(tmp_path / "external.png", 2 * MIB)
    real_remove = cleanup._remove_entry

    def swapped(*args, **kwargs):
        file.unlink()
        file.symlink_to(outside)
        return real_remove(*args, **kwargs)

    monkeypatch.setattr(cleanup, "_remove_entry", swapped)
    result = value.run_daily(NOW)["media"]
    assert file.is_symlink() and outside.stat().st_size == 2 * MIB
    assert result["files_removed"] == 0 and result["errors"]


def test_partial_delete_error_continues_fifo_and_reports_actual_bytes(tmp_path, monkeypatch):
    value = enabled_manager(tmp_path)
    first = write_file(value.media_dir / "a.png", MIB, mtime=STAMP - 2000)
    second = write_file(value.media_dir / "b.png", MIB, mtime=STAMP - 1000)
    real_remove = cleanup._remove_entry

    def denied(anchor, fd, name, *args):
        if name == first.name:
            raise PermissionError("private-filename-body")
        return real_remove(anchor, fd, name, *args)

    monkeypatch.setattr(cleanup, "_remove_entry", denied)
    result = value.run_daily(NOW)["media"]
    assert first.exists() and not second.exists()
    assert result["bytes_before"] == 2 * MIB and result["bytes_after"] == MIB
    assert result["files_removed"] == 1 and result["status"] == "partial"
    assert result["errors"][0]["error_type"] == "PermissionError"
    assert "private-filename-body" not in json.dumps(value.status())


def test_media_and_logs_have_independent_enable_flags_and_thresholds(tmp_path):
    value = manager(tmp_path, enabled=True, media_enabled=False, logs_enabled=True,
                    logs_max_mb=1, logs_target_mb=0)
    file = write_file(value.media_dir / "old.png", 2 * MIB)
    write_file(value.journal.path, 2 * MIB)
    inode = value.journal.path.stat().st_ino
    result = value.run_daily(NOW)
    assert result["media"]["status"] == "disabled" and file.exists()
    assert result["logs"]["bytes_removed"] == 2 * MIB
    assert value.journal.path.stat().st_size == 0 and value.journal.path.stat().st_ino == inode


def test_journal_fifo_retains_newer_slots_and_protects_unmanaged_logs(tmp_path):
    journal = Journal(tmp_path / "diagnostics.jsonl", backup_count=3)
    active = write_file(journal.path, 60, mtime=STAMP - 400)
    oldest = write_file(tmp_path / "diagnostics.jsonl.1", 60, mtime=STAMP - 900)
    newer = write_file(tmp_path / "diagnostics.jsonl.2", 60, mtime=STAMP - 600)
    unmanaged = [write_file(tmp_path / name, 200) for name in
                 ["chromium.log", ".env", "accounts.json", "tasks.json", "diagnostics.jsonl.4", "diagnostics.jsonl.old"]]
    result = journal.purge(150, 120, now=NOW)
    assert result["status"] == "cleaned" and result["bytes_removed"] == 60
    assert not oldest.exists() and newer.exists() and active.exists()
    assert all(path.stat().st_size == 200 for path in unmanaged)
    assert result["bytes_before"] == 180 and result["bytes_after"] == 120


def test_journal_active_compaction_keeps_inode_and_newest_complete_records(tmp_path):
    journal = Journal(tmp_path / "diagnostics.jsonl", max_bytes=200000, backup_count=2)
    records = [journal.emit("event", number=n) for n in range(30)]
    os.utime(journal.path, (STAMP - 1000, STAMP - 1000))
    inode = journal.path.stat().st_ino
    before = journal.path.stat().st_size
    result = journal.purge(before - 1, 1000, now=NOW)
    assert result["status"] == "cleaned" and result["files_truncated"] == 1
    assert result["files_removed"] == 0 and journal.path.stat().st_ino == inode
    retained = [json.loads(line) for line in journal.path.read_text().splitlines()]
    assert retained and retained[-1] == records[-1]
    assert [row["number"] for row in retained] == list(range(30 - len(retained), 30))
    assert journal.path.stat().st_size <= 1000
    assert journal.emit("after-purge")["persisted"] is True
    assert journal.read()["items"][0]["event"] == "after-purge"
    assert journal.health()["ok"] is True


def test_journal_recent_active_is_protected_target_failure_honest(tmp_path):
    journal = Journal(tmp_path / "diagnostics.jsonl")
    active = write_file(journal.path, 200, mtime=STAMP - 299)
    result = journal.purge(100, 0, now=NOW)
    assert result["status"] == "partial" and result["target_not_reached"] is True
    assert result["bytes_after"] == 200 and result["skipped"] == {"recent": 1}
    assert active.stat().st_size == 200


def test_journal_symlink_slots_and_directory_are_protected(tmp_path):
    journal = Journal(tmp_path / "diagnostics.jsonl", backup_count=2)
    outside = write_file(tmp_path.parent / (tmp_path.name + "-external.log"), 200)
    try:
        journal.path.symlink_to(outside)
        (tmp_path / "diagnostics.jsonl.1").symlink_to(outside)
        nested = write_file(tmp_path / "diagnostics.jsonl.2" / "nested.log", 200)
        result = journal.purge(100, 0, now=NOW)
        assert result["bytes_before"] == 0 and result["files_removed"] == 0
        assert result["skipped"] == {"symlink": 2, "directory": 1}
        assert outside.stat().st_size == 200 and nested.exists()
    finally:
        outside.unlink(missing_ok=True)


def test_journal_rotation_still_works_after_slots_purged(tmp_path):
    journal = Journal(tmp_path / "diagnostics.jsonl", max_bytes=1024, backup_count=3)
    for n in range(20):
        journal.emit("before", number=n)
    for path in tmp_path.iterdir():
        os.utime(path, (STAMP - 1000, STAMP - 1000))
    result = journal.purge(1024, 600, now=NOW)
    assert result["status"] == "cleaned" and result["bytes_after"] <= 600
    for n in range(20):
        assert journal.emit("after", number=n)["persisted"] is True
    assert journal.read()["items"][0]["number"] == 19
    assert len(list(tmp_path.iterdir())) <= 4
    for path in tmp_path.iterdir():
        assert path.stat().st_size <= 1024
        assert all(isinstance(json.loads(line), dict) for line in path.read_text().splitlines())


def test_journal_purge_holds_emit_read_shared_path_lock(tmp_path, monkeypatch):
    journal = Journal(tmp_path / "diagnostics.jsonl", max_bytes=100000)
    other = Journal(journal.path, max_bytes=100000)
    for n in range(20):
        journal.emit("before", number=n)
    os.utime(journal.path, (STAMP - 1000, STAMP - 1000))
    entered, resume = threading.Event(), threading.Event()
    emitting, reading = threading.Event(), threading.Event()
    real_trim = cleanup._trim_active

    def paused(*args, **kwargs):
        entered.set()
        assert resume.wait(5)
        return real_trim(*args, **kwargs)

    def emit():
        emitting.set()
        return other.emit("concurrent")

    def read():
        reading.set()
        return other.read()

    monkeypatch.setattr(cleanup, "_trim_active", paused)
    with ThreadPoolExecutor(max_workers=3) as pool:
        purge = pool.submit(journal.purge, 1000, 500, now=NOW)
        assert entered.wait(5)
        writer = pool.submit(emit)
        reader = pool.submit(read)
        assert emitting.wait(5) and reading.wait(5)
        assert not writer.done() and not reader.done()
        resume.set()
        assert purge.result()["status"] == "cleaned"
        assert writer.result()["persisted"] is True and reader.result()["items"]
    assert journal.read()["items"][0]["event"] == "concurrent"


def test_unsupported_platform_keeps_settings_usable_and_deletion_fail_closed(tmp_path, monkeypatch):
    value = enabled_manager(tmp_path)
    file = write_file(value.media_dir / "old.png", 2 * MIB)
    monkeypatch.setattr(cleanup, "fcntl", None)
    assert value.get_settings()["enabled"] is True
    assert value.update_settings({"check_time": "04:00"})["check_time"] == "04:00"
    state = value.status()
    assert state["next_run"] is None and state["cleanup_supported"] is False
    result = value.run_daily(NOW)
    assert result["status"] == "error"
    assert result["errors"][0]["error_type"] == "UnsupportedPlatform"
    assert file.exists() and not value.state_path.exists()
    assert value.journal.purge(1, 0, now=NOW)["status"] == "error"


def test_cleanup_imports_when_fcntl_module_is_unavailable():
    # A fresh interpreter, not a monkeypatch of an already imported module.
    import subprocess
    code = 'import sys; sys.modules["fcntl"]=None; import cleanup; from diagnostics import Journal; assert cleanup.fcntl is None; assert cleanup.CleanupSettings().enabled is False'
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_cross_process_daily_claim_is_once(tmp_path):
    import subprocess
    value = enabled_manager(tmp_path)
    write_file(value.media_dir / "old.png", 2 * MIB)
    code = ("import json,sys; from cleanup import CleanupManager; from diagnostics import Journal; "
            "from pathlib import Path; p=Path(sys.argv[1]); "
            'm=CleanupManager(p/"media",p/"cleanup.json",p/"cleanup-state.json",Journal(p/"diagnostics.jsonl")); '
            "print(json.dumps(m.run_daily(float(sys.argv[2]))))")
    commands = [[sys.executable, "-c", code, str(tmp_path), str(STAMP)] for _ in range(4)]
    processes = [subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for command in commands]
    outcomes = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, stderr
            outcomes.append(json.loads(stdout))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
    assert sum(result["status"] == "completed" for result in outcomes) == 1
    assert sum(result["status"] == "already_checked" for result in outcomes) == 3


def test_logs_directory_replaced_after_journal_construction_is_not_followed(tmp_path):
    directory = tmp_path / "logs"
    directory.mkdir()
    journal = Journal(directory / "diagnostics.jsonl")
    old = write_file(journal.path, 200)
    outside = write_file(tmp_path / "outside" / "diagnostics.jsonl", 200)
    directory.rename(tmp_path / "original-logs")
    directory.symlink_to(outside.parent, target_is_directory=True)
    result = journal.purge(100, 0, now=NOW)
    assert result["status"] == "error" and result["bytes_after"] is None
    assert outside.stat().st_size == 200
    assert (tmp_path / "original-logs" / old.name).stat().st_size == 200


def test_scan_error_keeps_partial_summary_even_if_target_reached(tmp_path, monkeypatch):
    value = enabled_manager(tmp_path)
    write_file(value.media_dir / "old.png", 2 * MIB)
    real_scan = cleanup._scan

    def partial_scan(anchor, fd, names, result, **kwargs):
        entries = real_scan(anchor, fd, names, result, **kwargs)
        if kwargs.get("count_skips"):
            result["errors"].append({"operation": "stat", "error_type": "PermissionError",
                                    "reason": "file_stat_failed"})
            result["measurement_complete"] = False
        return entries

    monkeypatch.setattr(cleanup, "_scan", partial_scan)
    result = value.run_daily(NOW)
    assert result["status"] == "partial" and result["media"]["target_not_reached"] is False
    assert result["media"]["bytes_after"] == 0 and result["media"]["errors"]


def test_log_failure_does_not_prevent_media_cleanup_and_is_persisted(tmp_path, monkeypatch):
    value = manager(tmp_path, enabled=True, media_max_mb=1, media_target_mb=0)
    file = write_file(value.media_dir / "old.png", 2 * MIB)

    def failed(*args, **kwargs):
        raise OSError("never-copy-cookie-private")

    monkeypatch.setattr(value.journal, "purge", failed)
    result = value.run_daily(NOW)
    assert result["status"] == "partial" and not file.exists()
    assert result["logs"]["status"] == "error" and result["logs"]["bytes_after"] is None
    assert result["media"]["bytes_removed"] == 2 * MIB
    assert "never-copy-cookie-private" not in value.state_path.read_text()
    assert manager(tmp_path).status()["last_result"] == result
