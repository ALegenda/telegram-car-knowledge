import json
from unittest.mock import Mock

from tg_knowledge import background
from tg_knowledge.store import Archive


def test_background_retries_failed_media_after_successful_text_export(tmp_path, monkeypatch):
    with Archive(tmp_path) as a:
        a.set("history_complete", True)
        a.save_message(
            {
                "id": 1,
                "topic_id": 1,
                "date": "2026-10-11",
                "edit_date": None,
                "sender_id": 1,
                "sender_name": "Test",
                "sender_username": None,
                "reply_id": None,
                "text": "",
                "service": None,
                "media_type": "MessageMediaPhoto",
                "media_json": "{}",
                "raw_json": "{}",
            }
        )
        a.attachment(1, "failed", error="OSError")

    calls = []

    def popen(*args, **kwargs):
        calls.append(args)
        if len(calls) == 2:
            with Archive(tmp_path) as a:
                a.attachment(1, "downloaded", "media/1.jpg", 123)
        return Mock(wait=Mock(return_value=0))

    monkeypatch.setattr(background, "load_config", lambda: None)
    monkeypatch.setattr(background.subprocess, "Popen", popen)
    monkeypatch.setattr(background.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        "sys.argv",
        ["background", "--data", str(tmp_path), "--attempts", "2", "--media-policy", "all"],
    )
    background.main()
    assert len(calls) == 2
    state = json.loads((tmp_path / "job.json").read_text(encoding="utf-8"))
    assert state["status"] == "complete"
    assert state["attempt"] == 2


def test_background_ignores_failed_files_outside_source_selection(tmp_path, monkeypatch):
    with Archive(tmp_path) as a:
        a.set("history_complete", True)
        a.set("media_policy", "source")
        a.save_message(
            {
                "id": 1,
                "topic_id": 1,
                "date": "2026-10-11",
                "edit_date": None,
                "sender_id": 1,
                "sender_name": "Test",
                "sender_username": None,
                "reply_id": None,
                "text": "Caption",
                "service": None,
                "media_type": "MessageMediaDocument",
                "media_json": json.dumps({"ext": ".mp4", "size": 4}),
                "raw_json": "{}",
            }
        )
        a.db.commit()
        a.attachment(1, "failed", error="OSError")
    popen = Mock(return_value=Mock(wait=Mock(return_value=0)))
    monkeypatch.setattr(background, "load_config", lambda: None)
    monkeypatch.setattr(background.subprocess, "Popen", popen)
    monkeypatch.setattr(background.time, "sleep", lambda seconds: None)
    monkeypatch.setattr("sys.argv", ["background", "--data", str(tmp_path), "--skip-history"])
    background.main()
    assert popen.call_count == 1
    command = popen.call_args.args[0]
    assert command[-3:] == ["--media-policy", "source", "--skip-history"]
    assert json.loads((tmp_path / "job.json").read_text(encoding="utf-8"))["status"] == "complete"
    with Archive(tmp_path) as a:
        assert a.stats()["media_complete"] is False


def test_background_retries_processing_errors_before_reporting_complete(tmp_path, monkeypatch):
    with Archive(tmp_path) as archive:
        archive.set("history_complete", True)
        archive.set("media_policy", "source")
    calls = []

    def popen(command, **kwargs):
        calls.append(command)
        with Archive(tmp_path) as archive:
            archive.set("media_processing", {"errors": 1 if len(calls) == 1 else 0})
        return Mock(wait=Mock(return_value=0))

    monkeypatch.setattr(background, "load_config", lambda: None)
    monkeypatch.setattr(background.subprocess, "Popen", popen)
    monkeypatch.setattr(background.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(
        "sys.argv", ["background", "--data", str(tmp_path), "--process-existing", "--prune-unused"]
    )
    background.main()
    assert len(calls) == 2
    assert calls[0][-2:] == ["--process-existing", "--prune-unused"]
    state = json.loads((tmp_path / "job.json").read_text(encoding="utf-8"))
    assert state["status"] == "complete"
    assert state["attempt"] == 2
