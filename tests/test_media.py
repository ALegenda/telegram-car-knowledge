import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from telethon import types

from tg_knowledge.store import Archive
from tg_knowledge.telegram import download_attachments, message_record
from tg_knowledge.media import MAX_DOCUMENT_BYTES, MAX_TEXT_BYTES


def test_partial_download_is_retried_and_verified(tmp_path):
    class MediaClient:
        def __init__(self, complete):
            self.complete, self.calls = complete, 0

        async def get_messages(self, entity, ids):
            self.calls += 1
            return SimpleNamespace(id=ids)

        async def download_media(self, message, file):
            Path(file).write_bytes(b"test" if self.complete else b"te")
            return file

    with Archive(tmp_path) as a:
        message = types.Message(
            id=123,
            peer_id=types.PeerChannel(123),
            date=datetime.now(timezone.utc),
            message="test",
            reply_to=types.MessageReplyHeader(reply_to_msg_id=20, forum_topic=True),
        )
        record = message_record(message, True)
        record["media_type"] = "MessageMediaDocument"
        record["media_json"] = json.dumps({"id": 99, "size": 4, "ext": ".txt"})
        a.save_message(record)
        a.db.commit()
        asyncio.run(download_attachments(MediaClient(False), None, a, None))
        assert a.stats()["attachments"] == {"failed": 1}
        assert not (tmp_path / "media/20/123.txt").exists()
        good = MediaClient(True)
        asyncio.run(download_attachments(good, None, a, None))
        assert a.stats()["media_complete"] is True
        assert (tmp_path / "media/20/123.txt").read_bytes() == b"test"
        again = MediaClient(True)
        asyncio.run(download_attachments(again, None, a, None))
        assert again.calls == 0
        record["media_json"] = json.dumps({"id": 100, "size": 4, "ext": ".txt"})
        a.save_message(record)
        a.db.commit()
        assert a.stats()["media_complete"] is False
        asyncio.run(download_attachments(good, None, a, None))
        assert good.calls == 2


def test_source_download_skips_unusable_files_without_fetching_and_preserves_originals(tmp_path):
    class Client:
        def __init__(self):
            self.fetched = []

        async def get_messages(self, entity, ids):
            self.fetched.append(ids)
            return SimpleNamespace(id=ids)

        async def download_media(self, message, file):
            Path(file).write_bytes(b"test")
            return file

    cases = [
        (1, "MessageMediaPhoto", {"ext": ".jpg", "size": 4}),
        (2, "MessageMediaDocument", {"ext": ".mp4", "size": 4}),
        (3, "MessageMediaDocument", {"ext": ".apk", "size": 4}),
        (4, "MessageMediaDocument", {"ext": ".pdf", "size": MAX_DOCUMENT_BYTES + 1}),
        (5, "MessageMediaDocument", {"ext": ".txt", "size": MAX_TEXT_BYTES + 1}),
        (6, "MessageMediaDocument", {"ext": ".bin", "name": "../../manual.txt", "size": 4}),
        (7, "MessageMediaDocument", {"ext": ".pdf", "size": None}),
        (8, "MessageMediaWebPage", {"ext": ".pdf", "size": 4}),
    ]
    with Archive(tmp_path) as a:
        for ident, media_type, meta in cases:
            record = message_record(
                types.Message(
                    id=ident,
                    peer_id=types.PeerChannel(123),
                    date=datetime.now(timezone.utc),
                    message="caption",
                ),
                True,
            )
            record.update(media_type=media_type, media_json=json.dumps(meta))
            a.save_message(record)
        a.db.commit()
        old_video = tmp_path / "media/old.mp4"
        old_video.parent.mkdir()
        old_video.write_bytes(b"original")
        a.attachment(2, "downloaded", "media/old.mp4", 8)
        client = Client()
        asyncio.run(download_attachments(client, None, a, None))
        assert client.fetched == [6]
        assert (tmp_path / "media/1/6.txt").read_bytes() == b"test"
        selection = a.stats()["media_selection"]
        assert selection["policy"] == "source"
        assert selection["total"] == selection["downloaded"] == 1
        assert selection["complete"] is True
        assert selection["excluded"] == 6
        assert a.stats()["media_complete"] is False
        assert old_video.read_bytes() == b"original"
        again = Client()
        asyncio.run(download_attachments(again, None, a, None))
        assert again.fetched == []


def test_source_index_uses_document_text_but_does_not_read_other_saved_media(tmp_path, monkeypatch):
    from pypdf import PdfWriter

    from tg_knowledge import knowledge

    monkeypatch.chdir(tmp_path)
    body = "Для диагностики используется уникальный код ЦЕЛЕВОЙДОКУМЕНТ."
    document = tmp_path / "media/manual.bin"
    document.parent.mkdir()
    document.write_text(body, encoding="utf-8")
    video = tmp_path / "media/old.mp4"
    video.write_bytes(b"preserved original")
    blank = tmp_path / "media/scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(blank)
    with Archive(tmp_path) as a:
        a.set("chat", {"id": -100123, "bare_id": 123, "username": "test"})
        a.set("history_complete", True)
        a.set("media_policy", "source")
        for ident, path, meta in [
            (1, document, {"name": "manual.txt", "ext": ".bin"}),
            (2, video, {"ext": ".mp4"}),
            (3, blank, {"ext": ".pdf"}),
        ]:
            meta["size"] = path.stat().st_size
            record = message_record(
                types.Message(
                    id=ident,
                    peer_id=types.PeerChannel(123),
                    date=datetime.now(timezone.utc),
                    message="Сохранённая подпись к вложению",
                ),
                True,
            )
            record.update(media_type="MessageMediaDocument", media_json=json.dumps(meta))
            a.save_message(record)
            a.db.commit()
            a.attachment(ident, "downloaded", str(path.relative_to(tmp_path)), path.stat().st_size)
    original = knowledge.attachment_text
    read_paths = []

    def extract(path, suffix=None):
        read_paths.append(path)
        return original(path, suffix)

    monkeypatch.setattr(knowledge, "attachment_text", extract)
    report = knowledge.build(tmp_path)
    assert read_paths == [document, blank]
    assert report["source_documents"]["with_text"] == 1
    assert report["source_documents"]["statuses"] == {"extracted": 1, "no_text": 1}
    assert report["media_selection"]["complete"] is True
    assert (
        report["media_complete"] is True
    )  # All three originals were already saved in this fixture.
    result = knowledge.Search(tmp_path).search("ЦЕЛЕВОЙДОКУМЕНТ", mode="lexical")
    assert result["results"][0]["sources"][0]["message_id"] == 1
    assert video.read_bytes() == b"preserved original"
