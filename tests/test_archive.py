import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telethon import types

from tg_knowledge.knowledge import Search, build, source_url, split_text
from tg_knowledge.store import Archive
from tg_knowledge.telegram import (
    collect_topics,
    export_stream,
    message_record,
    parse_target,
    topic_of,
)

NOW = datetime(2026, 10, 11, tzinfo=timezone.utc)


def msg(message_id, text="текст", topic=20, reply=None):
    return types.Message(
        id=message_id,
        peer_id=types.PeerChannel(123),
        date=NOW,
        message=text,
        from_id=types.PeerUser(7),
        reply_to=types.MessageReplyHeader(
            reply_to_msg_id=reply or topic, reply_to_top_id=topic, forum_topic=True
        ),
    )


@pytest.mark.parametrize(
    "value,ref,anchor",
    [
        ("https://t.me/c/123456/20/99", -100123456, 99),
        ("https://t.me/c/AITO_club/212191", "AITO_club", 212191),
        ("https://t.me/AITO_club/212191", "AITO_club", 212191),
        ("@AITO_club", "AITO_club", None),
        ("-100123456", -100123456, None),
    ],
)
def test_links(value, ref, anchor):
    assert parse_target(value) == (ref, anchor)


def test_topics_include_direct_and_nested_replies():
    assert topic_of(msg(42), True) == 20
    assert topic_of(msg(43, reply=42), True) == 20
    no_header = types.Message(id=10, peer_id=types.PeerChannel(123), date=NOW, message="hello")
    assert topic_of(no_header, True) == 1
    assert topic_of(no_header, False) is None
    assert topic_of(no_header, True, forced_topic=25) == 25


class HistoryClient:
    def __init__(self, ids, fail_after=None):
        self.ids, self.fail_after, self.kwargs = ids, fail_after, None

    def iter_messages(self, entity, **kwargs):
        self.kwargs = kwargs

        async def iterate():
            emitted = 0
            for i in self.ids:
                if kwargs.get("offset_id") and i >= kwargs["offset_id"]:
                    continue
                if i <= kwargs.get("min_id", 0):
                    continue
                if emitted == self.fail_after:
                    raise OSError("simulated disconnect")
                emitted += 1
                yield msg(i)

        return iterate()


def test_resume_partial_history_without_losing_messages(tmp_path):
    with Archive(tmp_path) as a:
        client = HistoryClient(list(range(250, 0, -1)), fail_after=123)
        with pytest.raises(OSError):
            asyncio.run(export_stream(client, SimpleNamespace(forum=True), a, None, False, 0))
        assert a.get("streams")["history"]["oldest"] == 128
        assert a.stats()["messages"] == 123
        resumed = HistoryClient(list(range(250, 0, -1)))
        asyncio.run(export_stream(resumed, SimpleNamespace(forum=True), a, None, False, 0))
        assert resumed.kwargs["offset_id"] == 128
        assert a.stats()["messages"] == 250
        assert a.get("streams")["history"]["complete"] is True


def test_interrupted_large_increment_keeps_original_watermark(tmp_path):
    with Archive(tmp_path) as a:
        a.set("streams", {"history": {"complete": True, "oldest": 1, "newest": 100}})
        client = HistoryClient(list(range(3000, 100, -1)), fail_after=150)
        with pytest.raises(OSError):
            asyncio.run(export_stream(client, SimpleNamespace(forum=True), a, None, False, 0))
        assert a.get("streams")["history"]["newest"] == 100
        resumed = HistoryClient(list(range(3000, 100, -1)))
        asyncio.run(export_stream(resumed, SimpleNamespace(forum=True), a, None, False, 0))
        assert a.stats()["messages"] == 2900
        assert a.get("streams")["history"]["newest"] == 3000


def forum_topic(topic_id, top_message):
    return types.ForumTopic(
        id=topic_id,
        date=NOW,
        peer=types.PeerChannel(123),
        title=f"Topic {topic_id}",
        icon_color=0,
        top_message=top_message,
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=0,
        unread_mentions_count=0,
        unread_reactions_count=0,
        unread_poll_votes_count=0,
        from_id=types.PeerUser(7),
        notify_settings=types.PeerNotifySettings(),
    )


def test_short_topic_pages_are_not_end_of_list(tmp_path):
    class TopicsClient:
        def __init__(self):
            self.requests = []

        async def __call__(self, request):
            self.requests.append(request)
            page = len(self.requests)
            topics = [forum_topic(page * 20, page * 50)] if page < 3 else []
            messages = [msg(page * 50)] if topics else []
            return SimpleNamespace(topics=topics, messages=messages, order_by_create_date=False)

    with Archive(tmp_path) as a:
        client = TopicsClient()
        ids = asyncio.run(collect_topics(client, SimpleNamespace(forum=True), a))
        assert ids == [1, 20, 40]
        assert len(client.requests) == 3
        assert client.requests[1].offset_topic == 20
        assert client.requests[1].offset_id == 50


def seed(root):
    with Archive(root) as a:
        a.set("chat", {"id": -100123, "bare_id": 123, "title": "Test", "forum": True})
        a.topic(20, "AITO M7 • Аккумулятор")
        a.topic(30, "AITO M9")
        body = (
            "Разрядился аккумулятор. Проверили напряжение 12 вольт и подключили зарядное устройство. "
            * 3
        )
        records = [
            msg(100, body),
            msg(101, "12.6 В", reply=100),
            msg(102, "Спасибо!"),
            msg(103, "👍"),
            msg(104, body),
            msg(105, body, topic=30),
        ]
        for m in records:
            a.save_message(message_record(m, True))
        a.db.commit()


def test_cleaning_is_reversible_and_search_keeps_reply_sources(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "archive"
    seed(root)
    report = build(root)
    assert report["messages"] == 6
    assert report["kept_messages"] == 3
    assert report["filtered"] == {"acknowledgement": 1, "emoji_only": 1, "duplicate": 1}
    assert (
        len((root / "knowledge/messages.raw.jsonl").read_text(encoding="utf-8").splitlines()) == 6
    )
    hit = Search(root).search("аккумуляторы", topic_id=20, mode="lexical")
    assert hit["results"]
    assert all(r["topic_id"] == 20 for r in hit["results"])
    reply = Search(root).search("12.6", mode="lexical")
    assert [s["message_id"] for s in reply["results"][0]["sources"]] == [100, 101]
    assert reply["results"][0]["sources"][-1]["url"] == "https://t.me/c/123/101"
    with Archive(root) as a:
        assert a.db.execute("SELECT text FROM messages WHERE id=102").fetchone()[0] == "Спасибо!"
    audit = [
        json.loads(line)
        for line in (root / "knowledge/cleaning.audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(r["message_id"] == 104 and r["duplicate_of"] == 100 for r in audit)


def test_chunking_preserves_long_message_tail():
    text = "начало " * 1000 + "КОНЕЦ ВАЖНОЙ ИНСТРУКЦИИ"
    parts = split_text(text)
    assert parts[-1].endswith("КОНЕЦ ВАЖНОЙ ИНСТРУКЦИИ")
    assert all(len(p) <= 1000 for p in parts)


def test_private_and_public_source_links():
    assert source_url({"id": -100123, "bare_id": 123}, 456) == "https://t.me/c/123/456"
    assert (
        source_url({"id": -100123, "bare_id": 123, "username": "club"}, 456)
        == "https://t.me/club/456"
    )
