from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import signal
import sys
from datetime import UTC, datetime

import httpx

from ..knowledge import ACK, atomic_text, normalize, source_url
from ..media import MAX_TEXT_BYTES, TEXT_SUFFIXES, attachment_suffix, media_skip_reason
from ..runtime import acquire_lock
from .api import APIError, OpenAI, Telegram
from .config import Settings
from .store import Store

LOG = logging.getLogger(__name__)
HELP = (
    "Добавьте меня в группу или администратором в канал. Администратор включает базу "
    "командой /kb_on, затем можно спросить: @tg_channel_parse_bot ваш вопрос. "
    "В группе также работают ответы на мои сообщения и /ask.\n\n"
    "/kb_status — состояние, /kb_off — остановить сбор.\n"
    "Сохраняю только сообщения, которые Telegram передал после включения. "
    "Для старой истории нужен отдельный импорт. В канале вопросы публикуют администраторы; "
    "остальным нужна группа обсуждения. Базы канала и обсуждения раздельные.\n\n"
    "Переписка и выбранные PDF/DOCX/текстовые документы сохраняются на сервере. "
    "Тексты для поиска и фрагменты для ответа отправляются в OpenAI API. "
    "Фото, видео и голосовые не скачиваю; сканы без текстового слоя не распознаю."
)


def addressed(message: dict, username: str, bot_id: int) -> tuple[str | None, str]:
    text = message.get("text", message.get("caption", ""))
    command = re.match(r"^/(\w+)(?:@([A-Za-z0-9_]+))?(?:\s|$)", text)
    if command:
        if command[2] and command[2].lower() != username.lower():
            return None, ""
        return command[1].lower(), text[command.end():].strip()
    # Telegram entity offsets use UTF-16, not Python character positions.
    encoded = text.encode("utf-16-le")
    cuts = []
    for entity in message.get("entities", message.get("caption_entities", [])):
        start, end = entity["offset"] * 2, (entity["offset"] + entity["length"]) * 2
        value = encoded[start:end].decode("utf-16-le", errors="ignore")
        if (entity["type"] == "mention" and value.lower() == "@" + username.lower()) or (
            entity["type"] == "text_mention" and entity.get("user", {}).get("id") == bot_id
        ):
            cuts.append((start, end))
    if cuts:
        for start, end in sorted(cuts, reverse=True):
            encoded = encoded[:start] + encoded[end:]
        return "ask", encoded.decode("utf-16-le").strip(" ,:—\n")
    if message.get("reply_to_message", {}).get("from", {}).get("id") == bot_id:
        return "ask", text.strip()
    return None, ""


class Service:
    def __init__(self, settings: Settings, store: Store, telegram, ai):
        self.settings, self.store, self.telegram, self.ai = settings, store, telegram, ai
        self.bot_id = 0
        self.stop = asyncio.Event()
        self.read_all = False

    @property
    def help(self):
        if self.ai is not None:
            return HELP
        return HELP.replace(
            "Тексты для поиска и фрагменты для ответа отправляются в OpenAI API. ",
            "Сейчас включён поиск по словам: в ответе будут найденные фрагменты и ссылки. "
            "Поиск выполняется на этом сервере. Генерация ответов пока не подключена. ",
        )

    async def reply(self, message: dict, text: str):
        result = await self.telegram.reply(message, text)
        if isinstance(result, dict) and result.get("message_id"):
            self.store.set(f"outgoing:{message['chat']['id']}:{result['message_id']}", "1")
        return result

    async def admin(self, message: dict) -> bool:
        chat_id = message["chat"]["id"]
        if message.get("sender_chat", {}).get("id") == chat_id:
            return True  # Channel posts / anonymous administrators of this same chat.
        sender = message.get("from", {})
        if not sender.get("id") or sender.get("is_bot"):
            return False
        membership = await self.telegram.call("getChatMember", chat_id=chat_id, user_id=sender["id"])
        return membership.get("status") in {"creator", "administrator"}

    async def document(self, message: dict):
        doc = message.get("document")
        if not doc:
            return [], "none"
        metadata = {"name": doc.get("file_name", ""), "size": doc.get("file_size")}
        reason = media_skip_reason("MessageMediaDocument", metadata, "source", max_mb=20)
        if reason:
            return [], "skipped:" + reason
        suffix = attachment_suffix(metadata)
        limit = MAX_TEXT_BYTES if suffix in TEXT_SUFFIXES else 20 * 1024**2
        identity = hashlib.sha256(doc["file_id"].encode()).hexdigest()[:24]
        folder = self.settings.root / "media" / str(message["chat"]["id"])
        path = folder / str(message["message_id"]) / (identity + suffix)
        cache = path.with_suffix(".extracted.json")
        if cache.is_file():
            result = json.loads(cache.read_text(encoding="utf-8"))
            return result["sections"], result["status"]
        if not path.is_file():
            await self.telegram.download(doc["file_id"], path, limit)
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "tg_knowledge.bot.extract", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=45)
        except (TimeoutError, asyncio.CancelledError) as exc:
            process.kill()
            await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            result = {"status": "extraction_timeout", "sections": []}
        else:
            try:
                result = json.loads(output) if process.returncode == 0 else {}
            except ValueError:
                result = {}
            if "sections" not in result:
                result = {"status": "extraction_failed", "sections": []}
        # Keep original documents even when extraction fails or finds no text.
        atomic_text(cache, json.dumps(result, ensure_ascii=False))
        return result["sections"], result["status"]

    async def handle(self, update: dict):
        membership = update.get("my_chat_member")
        if membership and membership["new_chat_member"]["status"] in {"left", "kicked"}:
            self.store.enable(membership["chat"]["id"], False)
            return
        message = next((update[k] for k in (
            "message", "edited_message", "channel_post", "edited_channel_post"
        ) if k in update), None)
        if not message:
            return
        message = {**message, "_update_id": update["update_id"]}
        chat = message["chat"]
        command, question = addressed(message, self.settings.username, self.bot_id)
        if chat["type"] == "private":
            if command in {"start", "help", "ask", "kb_status"}:
                await self.reply(message, self.help)
            return
        if chat["type"] not in {"group", "supergroup", "channel"}:
            return
        if self.settings.allowed_chats and chat["id"] not in self.settings.allowed_chats:
            return
        # Ignore all bot-authored messages; don't index generated answers or trigger loops.
        if self.store.get(f"outgoing:{chat['id']}:{message['message_id']}"):
            return
        if message.get("from", {}).get("is_bot") and not message.get("sender_chat"):
            return
        self.store.register(chat)
        if command in {"kb_on", "kb_off"}:
            if not await self.admin(message):
                await self.reply(message, "Эту команду может выполнить администратор чата.")
                return
            enabled = command == "kb_on"
            self.store.enable(chat["id"], enabled)
            text = self.help if enabled else "Сбор сообщений и ответы в этом чате остановлены."
            if enabled and chat["type"] != "channel" and not self.read_all:
                text += (
                    "\n\nЧтобы видеть обычные сообщения, дайте мне права администратора "
                    "или отключите Group Privacy через /setprivacy в @BotFather "
                    "и добавьте бота в группу заново."
                )
            await self.reply(message, text)
            return
        if command in {"start", "help"}:
            await self.reply(message, self.help)
            return
        if command == "kb_status":
            status = self.store.stats(chat["id"])
            state = "включён" if self.store.chat(chat["id"])["enabled"] else "выключен"
            pending = max(0, status["chunks"] - status["embedded"])
            warnings = sum(count for name, count in status["documents"].items() if name in {
                "needs_ocr", "no_text", "extraction_timeout", "extraction_failed", "partial_text_limit",
                "pdf_over_300_pages",
            } or name.startswith("extraction_error:"))
            mode = (
                f"Ожидают смыслового поиска: {pending} фрагментов.\n"
                if self.ai is not None else
                "Режим: поиск по словам, ответ найденными фрагментами. Генерация пока не подключена.\n"
            )
            await self.reply(message, (
                f"Чат {chat['id']}: сбор {state}.\n"
                f"Сохранено сообщений: {status['messages']}.\n{mode}"
                f"Документов без полного извлечения текста: {warnings}.\n"
                "Это доступная боту история; полную старую переписку Bot API не передаёт. "
                "Удаления сообщений Telegram тоже не передаёт: сохранённые копии остаются."
            ))
            return
        if not self.store.chat(chat["id"])["enabled"]:
            if command == "ask":
                await self.reply(message, "Сначала администратор должен включить /kb_on.")
            return
        text = normalize(message.get("text", message.get("caption", "")))
        date = datetime.fromtimestamp(message["date"], UTC).isoformat()
        if command:
            # An edit may turn an existing source into a question/command: remove its old chunks.
            self.store.replace_message(chat["id"], message["message_id"], date, text, message, [])
            if command == "ask" and "edited_message" not in update and "edited_channel_post" not in update:
                await self.ask(message, question)
            return
        sections = [("Сообщение", text)] if text and not ACK.fullmatch(text) else []
        # Persist the source before downloading; an interrupted document remains retryable in inbox.
        self.store.replace_message(chat["id"], message["message_id"], date, text, message, sections)
        documents, status = await self.document(message)
        if message.get("document"):
            self.store.replace_message(
                chat["id"], message["message_id"], date, text, message,
                sections + documents, status,
            )

    async def ask(self, message: dict, question: str):
        if not question or len(question) > 1200:
            await self.reply(message, "Укажите вопрос после @имени бота или /ask (до 1200 символов).")
            return
        key = f"{message['chat']['id']}:{message.get('from', {}).get('id', 'channel')}"
        # Keep the same update retryable, but throttle separate questions from the same sender.
        retry_key = f"question:{message['chat']['id']}:{message['message_id']}"
        if not self.store.get(retry_key):
            if not self.store.rate_limit(key):
                await self.reply(message, "Подождите 15 секунд между вопросами.")
                return
            self.store.set(retry_key, "started")
        chat_id = message["chat"]["id"]
        if not self.store.stats(chat_id)["chunks"]:
            await self.reply(message, "В базе этого чата пока нет сообщений. Старую историю нужно импортировать отдельно.")
            self.store.set(retry_key, "done")
            return
        if self.store.get(retry_key) == "done":
            return
        try:
            vector = (await self.ai.embed([question]))[0] if self.ai is not None else []
            hits = self.store.search(chat_id, question, vector, self.settings.vector_model, limit=4)
            if not hits:
                await self.reply(message, "В сохранённых сообщениях этого чата ответ не нашёл. Попробуйте уточнить вопрос.")
                self.store.set(retry_key, "done")
                return
            seen = {hit["message_id"] for hit in hits}
            for hit in list(hits):
                for context in self.store.context(chat_id, hit["message_id"]):
                    if context["message_id"] not in seen and len(hits) < 6:
                        hits.append(context)
                        seen.add(context["message_id"])
            sources = []
            for number, hit in enumerate(hits, 1):
                sources.append({
                    "number": number, "date": hit["date"], "locator": hit["locator"],
                    "text": hit["text"],
                })
            if self.ai is not None:
                answer = await self.ai.answer(question, sources)
            else:
                answer = "Найденные фрагменты из сохранённых сообщений:\n\n" + "\n\n".join(
                    f"[{number}] " + hit["text"][:260].strip() + ("…" if len(hit["text"]) > 260 else "")
                    for number, hit in enumerate(hits, 1)
                )
        except APIError as exc:
            LOG.warning("Answer API unavailable: %s status=%s", exc.operation, exc.status)
            await self.reply(message, "Сервис ответов временно недоступен. Сообщения сохраняются; попробуйте позднее.")
            self.store.set(retry_key, "done")
            return
        chat = dict(self.store.chat(chat_id))
        chat["bare_id"] = int(str(chat_id)[4:]) if str(chat_id).startswith("-100") else abs(chat_id)
        references = ["\nИсточники:"]
        for i, hit in enumerate(hits, 1):
            references.append(
                f"[{i}] {hit['date'][:10]}, {hit['locator'][:70]}: "
                f"{source_url(chat, hit['message_id'])}"
            )
        await self.reply(message, answer + "\n" + "\n".join(references))
        self.store.set(retry_key, "done")

    async def pause(self, seconds: float):
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def poll(self):
        first = True
        while not self.stop.is_set():
            if self.store.stats()["inbox"].get("pending", 0) >= 5000:
                await self.pause(5)
                continue
            try:
                updates = await self.telegram.call(
                    "getUpdates", offset=int(self.store.get("offset", "0")), timeout=0 if first else 30,
                    allowed_updates=["message", "edited_message", "channel_post",
                                     "edited_channel_post", "my_chat_member"],
                )
                self.store.enqueue(updates)
                self.store.set("last_poll", datetime.now(UTC).isoformat())
                first = False
            except APIError as exc:
                if exc.status in {401, 404, 409}:
                    raise
                LOG.warning("Polling unavailable: status=%s", exc.status)
                await self.pause(max(5, exc.retry_after))

    async def work(self):
        while not self.stop.is_set():
            row = self.store.next_update()
            if row is None:
                await self.pause(0.5)
                continue
            try:
                await self.handle(json.loads(row["payload"]))
            except Exception as exc:
                # Exception messages/tracebacks may contain request tokens or private source text.
                LOG.warning("Update failed: id=%s kind=%s", row["id"], type(exc).__name__)
                self.store.fail(row["id"], type(exc).__name__, getattr(exc, "retry_after", 0))
            else:
                self.store.finish(row["id"])

    async def embeddings(self):
        if self.ai is None:
            await self.stop.wait()
            return
        while not self.stop.is_set():
            rows = self.store.pending_vectors(self.settings.vector_model)
            if not rows:
                await self.pause(2)
                continue
            try:
                vectors = await self.ai.embed([row["text"] for row in rows])
                self.store.save_vectors(rows, vectors, self.settings.vector_model)
                self.store.set("embedding_error", "")
            except Exception as exc:
                self.store.set("embedding_error", type(exc).__name__)
                LOG.warning("Embedding unavailable: kind=%s", type(exc).__name__)
                await self.pause(60)

    async def run(self):
        me = await self.telegram.call("getMe")
        if me.get("username", "").lower() != self.settings.username.lower():
            raise ValueError("Токен принадлежит другому боту. Проверьте BOT_USERNAME.")
        self.bot_id, self.read_all = me["id"], me.get("can_read_all_group_messages", False)
        webhook = await self.telegram.call("getWebhookInfo")
        if webhook.get("url"):
            raise ValueError("У бота уже настроен webhook; polling не запущен.")
        LOG.info("Bot connected: @%s", self.settings.username)
        tasks = [asyncio.create_task(coro()) for coro in (self.poll, self.work, self.embeddings)]
        stop_task = asyncio.create_task(self.stop.wait())
        try:
            done, _ = await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is not stop_task:
                    task.result()
        finally:
            for task in [*tasks, stop_task]:
                task.cancel()
            await asyncio.gather(*tasks, stop_task, return_exceptions=True)


async def serve(settings: Settings):
    lock = acquire_lock(settings.root / ".bot.lock")
    store = Store(settings.root)
    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            ai = OpenAI(settings, client) if settings.openai_key else None
            service = Service(settings, store, Telegram(settings.telegram_token, client), ai)
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(signum, service.stop.set)
                except NotImplementedError:
                    pass
            await service.run()
    finally:
        store.close()
        lock.release()
