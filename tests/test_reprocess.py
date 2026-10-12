import asyncio
import json
import zipfile
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telethon import types

from tg_knowledge import reprocess
from tg_knowledge.knowledge import Search, build
from tg_knowledge.media import processed_source
from tg_knowledge.store import Archive
from tg_knowledge.telegram import download_attachments, message_record


def seed(root, *, suffix=".apk", media_type="MessageMediaDocument", text="Подпись сохранена"):
    root.mkdir(exist_ok=True)
    path = root / "media" / f"1{suffix}"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(b"original")
    record = message_record(
        types.Message(
            id=1,
            peer_id=types.PeerChannel(123),
            date=datetime.now(UTC),
            message=text,
        ),
        True,
    )
    record.update(
        media_type=media_type, media_json=json.dumps({"id": 77, "ext": suffix, "size": 8})
    )
    with Archive(root) as archive:
        archive.set("chat", {"id": -100123, "bare_id": 123, "username": "test"})
        archive.set("history_complete", True)
        archive.set("media_policy", "source")
        archive.save_message(record)
        archive.db.commit()
        archive.attachment(1, "downloaded", str(path.relative_to(root)), 8)
    return path, record


def test_prune_keeps_messages_and_persists_decision_without_redownload(tmp_path, monkeypatch):
    path, original = seed(tmp_path)
    monkeypatch.setattr(reprocess.Processor, "check_tools", lambda self: None)
    report = reprocess.process_existing(tmp_path, prune=True, workers=1)
    assert report["status"] == "complete"
    assert report["deleted_files"] == 1
    assert report["freed_bytes"] == 8
    assert not path.exists()
    with Archive(tmp_path) as archive:
        assert dict(archive.db.execute("SELECT * FROM messages").fetchone()) == original
        assert archive.stats()["attachments"] == {"discarded": 1}
        asyncio.run(download_attachments(SimpleNamespace(), None, archive, None))
    again = reprocess.process_existing(tmp_path, prune=True, workers=1)
    assert again["no_useful_text"] == 1
    assert again["deleted_files"] == 1
    assert again["freed_bytes"] == 8


def test_failed_processing_does_not_delete_original(tmp_path, monkeypatch):
    path, _ = seed(tmp_path)
    monkeypatch.setattr(reprocess.Processor, "check_tools", lambda self: None)

    def fail(self, row):
        raise OSError("temporary error")

    monkeypatch.setattr(reprocess.Processor, "file", fail)
    report = reprocess.process_existing(tmp_path, prune=True, workers=1)
    assert report["status"] == "complete_with_errors"
    assert report["errors"] == 1
    assert report["deleted_files"] == 0
    assert path.read_bytes() == b"original"


def test_image_text_is_retrievable_with_provenance_and_cache_identity(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path, record = seed(tmp_path, suffix=".jpg", media_type="MessageMediaPhoto")
    body = "Диагностика: код ЦЕЛЕВОЕРАСПОЗНАВАНИЕ. " * 90
    monkeypatch.setattr(reprocess.Processor, "check_tools", lambda self: None)
    monkeypatch.setattr(reprocess.Processor, "ocr", lambda self, path: body)
    report = reprocess.process_existing(tmp_path, prune=True, workers=1)
    assert report["used"] == 1
    assert path.exists()
    built = build(tmp_path)
    assert built["source_attachments"]["by_kind"] == {"image_ocr": 1}
    hits = Search(tmp_path).search("ЦЕЛЕВОЕРАСПОЗНАВАНИЕ", limit=8, mode="lexical")
    assert hits["results"]
    for result in hits["results"]:
        assert "Текст изображения" in result["text"]
        assert "Автоматическое распознавание" in result["text"]
        assert result["sources"][0]["url"] == "https://t.me/test/1"
        assert result["sources"][0]["attachment"]["automatic"] is True
    changed = json.loads(record["media_json"])
    changed["id"] = 78
    assert processed_source(tmp_path, 1, changed) is None


def test_zip_member_paths_are_not_extracted_to_filesystem(tmp_path, monkeypatch):
    path, record = seed(tmp_path, suffix=".zip")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../../outside.pdf", b"%PDF-document")
        archive.writestr("../../mislabeled.pdf", b"unsupported binary data")
        archive.writestr("../../outside.apk", b"executable")
    record["media_json"] = json.dumps({"id": 77, "ext": ".zip", "size": path.stat().st_size})
    (tmp_path / "processed").mkdir()
    processor = reprocess.Processor(tmp_path)

    def pdf(self, **kwargs):
        assert kwargs["data"] == b"%PDF-document"
        return [
            {
                "locator": kwargs["locator"] + ", страница 1",
                "text": "Руководство по ремонту автомобиля AITO M7",
                "method": "pdf_text",
                "automatic": False,
            }
        ]

    monkeypatch.setattr(reprocess.Processor, "pdf", pdf)
    row = {**record, "path": str(path.relative_to(tmp_path)), "saved_size": path.stat().st_size}
    result = processor.file(row)
    assert result["status"] == "used"
    assert len(result["sections"]) == 1
    assert len(result["ignored_archive_members"]) == 1
    assert result["ignored_archive_members"][0]["reason"] == "not_pdf_content"
    assert "../../outside.pdf" in result["sections"][0]["locator"]
    assert not (tmp_path.parent / "outside.pdf").exists()
    assert path.exists()


def test_delete_requires_durable_matching_result(tmp_path):
    path, record = seed(tmp_path)
    row = {**record, "path": str(path.relative_to(tmp_path)), "saved_size": 8}
    with Archive(tmp_path) as archive:
        with pytest.raises(ValueError, match="durable"):
            reprocess.prune_unused(tmp_path, row, {"status": "no_useful_text"}, archive)
    assert path.exists()


def test_damaged_pdf_fonts_and_ocr_noise_are_rejected():
    assert reprocess.damaged_pdf_text("Руководство " + "\ue004" * 20)
    assert not reprocess.damaged_pdf_text("正常的维修手册，AITO M7")
    assert not reprocess.has_useful_text("12 AA 678", ocr=True)
    assert reprocess.has_useful_text("Давление в шинах автомобиля составляет 2.6 бар", ocr=True)
