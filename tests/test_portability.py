import json
import os
import subprocess
import sys

import pytest

from tg_knowledge import cli, reprocess, setup
from tg_knowledge.runtime import acquire_lock, process_running
from tg_knowledge.store import Archive
from tg_knowledge.telegram import parse_target


def test_father_link_targets_entire_forum():
    assert parse_target("https://t.me/c/FangChengBaoClub/84505") == ("FangChengBaoClub", 84505)


def test_exclusive_lock_blocks_second_process_and_recovers(tmp_path):
    path = tmp_path / "Папка с пробелами" / ".job.lock"
    lock = acquire_lock(path)
    code = (
        "from pathlib import Path; from tg_knowledge.runtime import acquire_lock; "
        f"acquire_lock(Path({str(path)!r}))"
    )
    try:
        blocked = subprocess.run([sys.executable, "-c", code], capture_output=True)
        assert blocked.returncode != 0
        assert b"BlockingIOError" in blocked.stderr
    finally:
        lock.release()
    again = acquire_lock(path)
    again.release()


def test_status_check_never_sends_signal_to_worker(tmp_path, monkeypatch, capsys):
    with Archive(tmp_path) as archive:
        archive.set("history_complete", True)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (tmp_path / "pipeline.pid").write_text(str(child.pid), encoding="utf-8")

        def no_signals(*args):
            raise AssertionError("Read-only status must never call os.kill")

        monkeypatch.setattr(os, "kill", no_signals)
        monkeypatch.setattr(sys, "argv", ["tg-knowledge", "--data", str(tmp_path), "status"])
        cli.main()
        status = json.loads(capsys.readouterr().out)
        assert status["pipeline_running"] is True
        assert child.poll() is None
    finally:
        # Restore os.kill before POSIX Popen.terminate uses it.
        monkeypatch.undo()
        child.terminate()
        child.wait(timeout=10)


def test_reused_pid_is_not_reported_as_pipeline():
    assert process_running(os.getpid())
    assert not process_running(os.getpid(), created_at=1)
    assert not process_running(-1)


def test_setup_creates_defaults_without_credentials(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("TELEGRAM_CHAT", "TG_DATA_DIR", "TELEGRAM_SESSION"):
        monkeypatch.delenv(key, raising=False)
    setup.prepare_environment()
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "FangChengBaoClub" in text
    assert "API_HASH" not in text
    assert "data/fangchengbao" in text


def test_failed_document_parser_or_limit_never_counts_as_unused(tmp_path):
    path = tmp_path / "broken.docx"
    path.write_bytes(b"not a docx archive")
    with pytest.raises(ValueError, match="incomplete"):
        reprocess.source_document_text(path, ".docx")
    assert path.read_bytes() == b"not a docx archive"
    path = tmp_path / "large.txt"
    path.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="incomplete"):
        reprocess.source_document_text(path, ".txt")
    assert path.exists()
