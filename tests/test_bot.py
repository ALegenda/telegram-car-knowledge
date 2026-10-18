import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from tg_knowledge.bot.api import APIError, OpenAI, Telegram, telegram_text
from tg_knowledge.bot.config import Settings
from tg_knowledge.bot.extract import extract
from tg_knowledge.bot.importer import import_archive
from tg_knowledge.bot.service import Service, addressed
from tg_knowledge.bot.store import Store
from tg_knowledge.store import Archive

DATE = "2026-10-18T10:00:00+00:00"
CHAT = -100123456


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "bot")
    yield value
    value.close()


def register(store, chat_id=CHAT):
    store.register({"id": chat_id, "type": "supergroup", "title": "Test"})
    store.enable(chat_id, True)


def save(store, chat_id, message_id, text, raw=None):
    store.replace_message(chat_id, message_id, DATE, text, raw or {}, [("Сообщение", text)])


def test_retrieval_and_context_never_cross_chat_boundary(store):
    register(store)
    register(store, CHAT - 1)
    save(store, CHAT, 1, "Аккумулятор следует заряжать", {"message_thread_id": 5})
    save(store, CHAT - 1, 1, "Аккумулятор чужого чата — секрет")
    save(store, CHAT, 2, "Аккумулятор 12V", {
        "message_thread_id": 5, "reply_to_message": {"message_id": 1},
    })
    save(store, CHAT, 3, "Другая тема", {"message_thread_id": 6})
    rows = store.pending_vectors("test")
    store.save_vectors(rows, [[1, 0]] * len(rows), "test")
    hits = store.search(CHAT, "зарядка аккумулятора", [1, 0], "test")
    assert hits and all(hit["chat_id"] == CHAT for hit in hits)
    assert not any("секрет" in hit["text"] for hit in hits)
    assert [r["message_id"] for r in store.context(CHAT, 2)] == [1]
    assert store.context(CHAT, 3) == []


def test_edit_invalidates_old_lexical_and_semantic_chunks(store):
    register(store)
    save(store, CHAT, 1, "Аккумулятор", {"_update_id": 10})
    store.save_vectors(store.pending_vectors("test"), [[1, 0]], "test")
    save(store, CHAT, 1, "Коробка передач", {"_update_id": 12})
    assert store.search(CHAT, "аккумулятор", [0, 0], "test") == []
    assert store.stats(CHAT)["embedded"] == 0
    # A retried older update cannot restore stale text after a newer edit.
    save(store, CHAT, 1, "Аккумулятор", {"_update_id": 11})
    assert store.search(CHAT, "коробка", [0, 0], "test")


def test_inbox_survives_restart_and_deduplicates(tmp_path):
    root = tmp_path / "bot"
    store = Store(root)
    store.enqueue([{"update_id": 9, "message": {"text": "тест"}}] * 2)
    store.close()
    store = Store(root)
    try:
        assert store.get("offset") == "10"
        assert store.stats()["inbox"] == {"pending": 1}
        assert json.loads(store.next_update()["payload"])["message"]["text"] == "тест"
        store.finish(9)
        assert store.next_update() is None
    finally:
        store.close()


def test_utf16_mention_and_reply_routing():
    name = "tg_channel_parse_bot"
    mention = "@" + name
    message = {"text": "🚙 " + mention + " аккумулятор?", "entities": [
        {"type": "mention", "offset": 3, "length": len(mention)},
    ]}
    assert addressed(message, name, 42) == ("ask", "🚙  аккумулятор?")
    assert addressed({"text": "/ask@OtherBot test"}, name, 42) == (None, "")
    assert addressed({"text": "/ask@TG_CHANNEL_PARSE_BOT test"}, name, 42) == ("ask", "test")
    assert addressed({"text": "вопрос", "reply_to_message": {"from": {"id": 42}}}, name, 42) == ("ask", "вопрос")
    assert addressed({"text": "@tg_channel_parse_bot_fake вопрос"}, name, 42) == (None, "")


def test_utf16_reply_limit_never_breaks_emoji():
    text = telegram_text("🚙" * 3000)
    assert len(text.encode("utf-16-le")) <= 8000
    assert "�" not in text


class FakeTelegram:
    def __init__(self, admin=True):
        self.is_admin, self.replies, self.downloads = admin, [], []

    async def call(self, method, **kwargs):
        if method == "getChatMember":
            return {"status": "administrator" if self.is_admin else "member"}
        raise AssertionError(method)

    async def reply(self, message, text):
        self.replies.append((message, text))
        return {"message_id": 10000 + len(self.replies)}

    async def download(self, *args):
        self.downloads.append(args)
        raise AssertionError("Unsupported media must never be downloaded")


class FakeAI:
    def __init__(self):
        self.sources = []

    async def embed(self, texts):
        return [[0, 0] for _ in texts]

    async def answer(self, question, sources):
        self.sources = sources
        return "Проверьте аккумулятор [1]."


def message(text, mid=10, **kwargs):
    return {
        "message_id": mid, "chat": {"id": CHAT, "type": "supergroup", "title": "Test"},
        "from": {"id": 7}, "date": 1792317600, "text": text, **kwargs,
    }


def service(store, tmp_path, admin=True):
    telegram, ai = FakeTelegram(admin), FakeAI()
    settings = Settings(tmp_path / "bot", "token", "key")
    result = Service(settings, store, telegram, ai)
    result.bot_id = 42
    return result, telegram, ai


def test_admin_activation_and_opt_out(store, tmp_path):
    svc, tg, ai = service(store, tmp_path, admin=False)
    asyncio.run(svc.handle({"update_id": 1, "message": message("/kb_on")}))
    assert not store.chat(CHAT)["enabled"]
    asyncio.run(svc.handle({"update_id": 2, "message": message("аккумулятор", 11)}))
    assert store.stats(CHAT)["messages"] == 0
    tg.is_admin = True
    asyncio.run(svc.handle({"update_id": 3, "message": message("/kb_on", 12)}))
    asyncio.run(svc.handle({"update_id": 4, "message": message("аккумулятор", 13)}))
    assert store.stats(CHAT)["chunks"] == 1
    asyncio.run(svc.handle({"update_id": 5, "message": message("/kb_off", 14)}))
    asyncio.run(svc.handle({"update_id": 6, "message": message("новое сообщение", 15)}))
    assert store.stats(CHAT)["messages"] == 1


def test_answers_use_current_chat_and_preserve_topic_reply(store, tmp_path):
    register(store)
    register(store, CHAT - 1)
    save(store, CHAT, 1, "Аккумулятор 12V заряжается отдельно")
    save(store, CHAT - 1, 2, "Аккумулятор — другой секрет")
    svc, tg, ai = service(store, tmp_path)
    request = message("/ask аккумулятор", message_thread_id=20)
    asyncio.run(svc.handle({"update_id": 10, "message": request}))
    reply_to, text = tg.replies[-1]
    assert reply_to["message_thread_id"] == 20
    assert "https://t.me/c/123456/1" in text
    assert "секрет" not in json.dumps(ai.sources, ensure_ascii=False)
    assert store.stats(CHAT)["chunks"] == 1  # A question is not an answer source.
    asyncio.run(svc.handle({"update_id": 10, "message": request}))
    assert len(tg.replies) == 1


def test_private_chat_cannot_query_group_archive(store, tmp_path):
    register(store)
    save(store, CHAT, 1, "Аккумулятор — секрет")
    svc, tg, ai = service(store, tmp_path)
    request = message("/ask аккумулятор", chat={"id": 7, "type": "private"})
    asyncio.run(svc.handle({"update_id": 10, "message": request}))
    assert not ai.sources
    assert "секрет" not in tg.replies[-1][1]


def test_channel_post_activation_and_own_answer_not_reindexed(store, tmp_path):
    svc, tg, ai = service(store, tmp_path)
    post = message("/kb_on", chat={"id": CHAT, "type": "channel", "title": "Channel"},
                   sender_chat={"id": CHAT}, **{"from": {"id": 1087968824, "is_bot": True}})
    asyncio.run(svc.handle({"update_id": 1, "channel_post": post}))
    assert store.chat(CHAT)["enabled"]
    answer = message("Сгенерированный ответ", mid=10001, chat=post["chat"], sender_chat={"id": CHAT})
    asyncio.run(svc.handle({"update_id": 2, "channel_post": answer}))
    assert store.stats(CHAT)["messages"] == 0


def test_allowlist_excludes_unapproved_chat(store, tmp_path):
    svc, tg, ai = service(store, tmp_path)
    svc.settings = replace(svc.settings, allowed_chats=frozenset({CHAT - 1}))
    asyncio.run(svc.handle({"update_id": 1, "message": message("/kb_on")}))
    assert store.chat(CHAT) is None
    assert not tg.replies


def test_unsupported_or_oversized_files_are_not_downloaded(store, tmp_path):
    svc, tg, ai = service(store, tmp_path)
    for name, size in [("video.mp4", 1), ("file.pdf", 21 * 1024**2), ("file.txt", 2 * 1024**2)]:
        sections, status = asyncio.run(svc.document(message("", document={
            "file_name": name, "file_size": size, "file_id": "id",
        })))
        assert not sections and status.startswith("skipped:")
    assert not tg.downloads


def test_import_refuses_another_chat_and_preserves_original(store, tmp_path):
    root = tmp_path / "original"
    with Archive(root) as archive:
        archive.set("chat", {"id": CHAT, "title": "Original"})
        archive.save_message({
            "id": 1, "date": DATE, "text": "Инструкция аккумулятора", "raw_json": "{}",
            "media_json": None, "media_type": None,
        })
        archive.db.commit()
    original = (root / "archive.sqlite3").read_bytes()
    with pytest.raises(ValueError, match="не совпадает"):
        import_archive(store, root, CHAT - 1)
    assert import_archive(store, root, CHAT) == 1
    assert store.search(CHAT, "аккумулятор", [0, 0], "test")
    assert (root / "archive.sqlite3").read_bytes() == original
    assert not store.chat(CHAT)["enabled"]


def test_document_extraction_retains_page_locator_and_original(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    path = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(path)
    assert extract(path)["status"] == "needs_ocr"
    assert path.exists()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 10 10 Td (Battery instructions) Tj ET")
    page[NameObject("/Contents")] = stream
    writer.write(path)
    assert extract(path)["sections"] == [["PDF, страница 2", "Battery instructions"]]
    path = tmp_path / "note.txt"
    path.write_text("Текст инструкции аккумулятора", encoding="utf-8")
    assert extract(path)["sections"] == [["Документ", "Текст инструкции аккумулятора"]]


def test_http_failures_never_expose_tokens_and_download_rejects_traversal(tmp_path):
    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(401, json={"ok": False, "error_code": 401})
        )) as client:
            with pytest.raises(APIError) as error:
                await Telegram("SECRET_TOKEN", client).call("getMe")
            assert "SECRET" not in str(error.value)
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"ok": True, "result": {"file_path": "../secret"}})
        )) as client:
            with pytest.raises(ValueError, match="путь"):
                await Telegram("SECRET_TOKEN", client).download("id", tmp_path / "target", 100)
    asyncio.run(check())


def test_openai_response_payload_and_validated_citations(tmp_path):
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"output": [{"type": "message", "content": [
            {"type": "output_text", "text": "Совет [1], выдумка [99] https://bad.example"},
        ]}]})

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            ai = OpenAI(Settings(tmp_path, "token", "key"), client)
            answer = await ai.answer("вопрос", [{"text": "источник"}])
            assert "[1]" in answer and "99" not in answer and "bad.example" not in answer
    asyncio.run(check())
    assert requests[0]["store"] is False
    assert "недоверенные" in requests[0]["instructions"]


def test_local_status_connection_is_readonly(store, tmp_path):
    import sqlite3

    reader = Store(tmp_path / "bot", readonly=True)
    try:
        assert reader.stats()["messages"] == 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.set("test", "value")
    finally:
        reader.close()
