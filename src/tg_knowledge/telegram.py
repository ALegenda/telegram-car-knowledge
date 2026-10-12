from __future__ import annotations

import asyncio
import getpass
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from telethon import TelegramClient, errors, functions, types, utils

from .config import client_config
from .store import Archive, json_text


def make_client() -> TelegramClient:
    api_id, api_hash, session = client_config()
    return TelegramClient(
        str(session),
        api_id,
        api_hash,
        device_model="Local Telegram Knowledge Archive",
        app_version="0.1.0",
        flood_sleep_threshold=0,
        request_retries=5,
    )


async def retry(call):
    while True:
        try:
            return await call()
        except errors.FloodWaitError as exc:
            print(f"Telegram просит паузу {exc.seconds} с; прогресс сохранён.", file=sys.stderr)
            await asyncio.sleep(exc.seconds + 1)


async def login(qr: bool = False, password_only: bool = False) -> None:
    client = make_client()
    await client.connect()
    try:
        if await client.is_user_authorized():
            print("Telegram уже подключён.")
            return
        if password_only:
            password = await asyncio.to_thread(
                getpass.getpass, "Пароль Telegram 2FA (ввод скрыт): "
            )
            await client.sign_in(password=password)
        elif qr:
            import qrcode

            path = Path("secrets/login-qr.png").resolve()
            for _ in range(10):
                token = await client.qr_login()
                qrcode.make(token.url).save(path)
                print(f"QR обновлён: {path}", flush=True)
                try:
                    await token.wait()
                    break
                except TimeoutError:
                    continue
                except errors.SessionPasswordNeededError:
                    password = await asyncio.to_thread(getpass.getpass, "Пароль 2FA (ввод скрыт): ")
                    await client.sign_in(password=password)
                    break
            else:
                raise ValueError("QR не подтверждён. Повторите команду login --qr.")
        else:
            phone = (
                await asyncio.to_thread(input, "Номер Telegram в международном формате: ")
            ).strip()
            sent = await retry(lambda: client.send_code_request(phone))
            try:
                await client.sign_in(
                    phone=phone,
                    code=await asyncio.to_thread(getpass.getpass, "Код из Telegram (ввод скрыт): "),
                    phone_code_hash=sent.phone_code_hash,
                )
            except errors.SessionPasswordNeededError:
                password = await asyncio.to_thread(getpass.getpass, "Пароль 2FA (ввод скрыт): ")
                await client.sign_in(password=password)
        print("Вход подтверждён. Сессия сохранена локально.")
    finally:
        await client.disconnect()
        Path("secrets/login-qr.png").unlink(missing_ok=True)


def parse_target(value: str) -> tuple[str | int, int | None]:
    value = value.strip()
    if value.lstrip("-").isdigit():
        return int(value), None
    if value.startswith(("https://", "http://", "t.me/")):
        url = urlparse(value if "://" in value else "https://" + value)
        if url.hostname not in {"t.me", "telegram.me", "www.t.me"}:
            raise ValueError("Ожидается ссылка t.me или имя/ID чата.")
        parts = [p for p in url.path.split("/") if p]
        if not parts or parts[0].startswith("+") or parts[0] == "joinchat":
            raise ValueError(
                "Invite-ссылка не подходит. Укажите ID или название уже доступного чата."
            )
        if parts[0] == "c":
            if len(parts) < 2:
                raise ValueError("В ссылке отсутствует ID чата.")
            # Some copied links incorrectly combine /c/ with a public username.
            ref = int("-100" + parts[1]) if parts[1].isdigit() else parts[1]
            rest = parts[2:]
        else:
            ref, rest = parts[0], parts[1:]
        return ref, int(rest[-1]) if rest and rest[-1].isdigit() else None
    return value.removeprefix("@"), None


async def resolve_chat(client, value: str):
    ref, _ = parse_target(value)
    # Cache joined private peers and support exact human-readable forum names.
    candidates = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(ref, int) and utils.get_peer_id(entity) == ref:
            return entity
        if isinstance(ref, str) and (
            dialog.name.casefold() == ref.casefold()
            or (getattr(entity, "username", None) or "").casefold() == ref.casefold()
        ):
            candidates.append(entity)
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise ValueError("Несколько чатов с таким названием. Используйте ID из команды dialogs.")
    try:
        return await client.get_entity(ref)
    except (ValueError, errors.RPCError) as exc:
        raise ValueError(
            "Чат не найден. Выполните dialogs и укажите его точный ID или название."
        ) from exc


async def collect_topics(client, entity, archive: Archive) -> list[int]:
    if not getattr(entity, "forum", False):
        return []
    archive.topic(1, "General / Общая тема")
    offsets = (0, 0, 0)
    seen = set()
    while True:
        if hasattr(functions.messages, "GetForumTopicsRequest"):
            request = functions.messages.GetForumTopicsRequest(
                peer=entity,
                offset_date=offsets[0],
                offset_id=offsets[1],
                offset_topic=offsets[2],
                limit=100,
            )
        else:
            request = functions.channels.GetForumTopicsRequest(
                channel=entity,
                offset_date=offsets[0],
                offset_id=offsets[1],
                offset_topic=offsets[2],
                limit=100,
            )
        result = await retry(lambda request=request: client(request))
        topics = [t for t in result.topics if isinstance(t, types.ForumTopic)]
        if not topics:
            break
        fresh = [t for t in topics if t.id not in seen]
        if not fresh:
            raise ValueError("Пагинация тем остановилась; полная выгрузка не подтверждена.")
        for topic in topics:
            seen.add(topic.id)
            archive.topic(topic.id, topic.title, topic.to_dict())
        archive.db.commit()
        last = topics[-1]
        if getattr(result, "order_by_create_date", False):
            offset_date = last.date
        else:
            msg = next((m for m in result.messages if m.id == last.top_message), None)
            if msg is None:
                msg = await retry(
                    lambda msg_id=last.top_message: client.get_messages(entity, ids=msg_id)
                )
            if msg is None:
                raise ValueError("Не удалось определить смещение тем. Повторите выгрузку.")
            offset_date = msg.date
        offsets = (offset_date, last.top_message, last.id)
        # A short server page is not an end-of-list signal: always request the next page.
    return sorted(seen | {1})


def topic_of(message, forum: bool, forced_topic: int | None = None) -> int | None:
    action = getattr(message, "action", None)
    if isinstance(action, types.MessageActionTopicCreate):
        return message.id
    header = getattr(message, "reply_to", None)
    if header and getattr(header, "forum_topic", False):
        return getattr(header, "reply_to_top_id", None) or header.reply_to_msg_id or 1
    return forced_topic or (1 if forum else None)


def message_record(message, forum: bool, forced_topic: int | None = None) -> dict:
    sender = getattr(message, "sender", None)
    name = utils.get_display_name(sender) if sender else None
    action = getattr(message, "action", None)
    file = getattr(message, "file", None)
    media = getattr(message, "media", None)
    return {
        "id": message.id,
        "topic_id": topic_of(message, forum, forced_topic),
        "date": message.date.isoformat(),
        "edit_date": message.edit_date.isoformat() if getattr(message, "edit_date", None) else None,
        "sender_id": getattr(message, "sender_id", None),
        "sender_name": name,
        "sender_username": getattr(sender, "username", None),
        "reply_id": getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None),
        "text": getattr(message, "message", None) or "",
        "service": type(action).__name__ if action else None,
        "media_type": type(media).__name__ if media else None,
        "media_json": json_text(
            {
                "id": getattr(getattr(message, "document", None), "id", None)
                or getattr(getattr(message, "photo", None), "id", None),
                "name": file.name,
                "size": file.size,
                "mime_type": file.mime_type,
                "ext": file.ext,
                "duration": file.duration,
            }
        )
        if file
        else None,
        "raw_json": json_text(message.to_dict()),
    }


async def export_stream(client, entity, archive: Archive, topic_id, full: bool, wait: float):
    key = "history" if topic_id is None else f"topic:{topic_id}"
    streams = archive.get("streams", {})
    if full:
        streams.pop(key, None)
    state = streams.get(key, {"complete": False, "oldest": 0, "newest": 0})
    was_complete = state["complete"]
    kwargs = {"limit": None, "wait_time": wait}
    if topic_id is not None and topic_id != 1:
        kwargs["reply_to"] = topic_id
    if was_complete:
        # Refresh a bounded recent overlap to pick up recent edits as well as new posts.
        kwargs["min_id"] = max(0, state["newest"] - 1000)
    elif state["oldest"]:
        kwargs["offset_id"] = state["oldest"]
    count = 0
    newest_seen = state["newest"]

    def checkpoint():
        archive.db.commit()
        streams[key] = state
        archive.set("streams", streams)

    while True:
        try:
            async for message in client.iter_messages(entity, **kwargs):
                if isinstance(message, types.MessageEmpty):
                    continue
                record = message_record(message, bool(getattr(entity, "forum", False)), topic_id)
                archive.save_message(record)
                action = getattr(message, "action", None)
                if isinstance(action, types.MessageActionTopicCreate):
                    archive.db.execute(
                        "INSERT OR IGNORE INTO topics VALUES (?,?,?)",
                        (message.id, action.title, json_text(action.to_dict())),
                    )
                if isinstance(action, types.MessageActionTopicEdit) and action.title:
                    archive.db.execute(
                        "INSERT OR IGNORE INTO topics VALUES (?,?,?)",
                        (record["topic_id"], action.title, json_text(action.to_dict())),
                    )
                newest_seen = max(newest_seen, message.id)
                if not was_complete:
                    state["newest"] = newest_seen
                    state["oldest"] = min(state["oldest"] or message.id, message.id)
                kwargs["offset_id"] = message.id
                count += 1
                if count % 100 == 0:
                    checkpoint()
                    print(f"{key}: сохранено {count} сообщений", file=sys.stderr, flush=True)
            state["complete"] = True
            state["newest"] = newest_seen
            checkpoint()
            return
        except errors.FloodWaitError as exc:
            checkpoint()
            print(f"{key}: пауза Telegram {exc.seconds} с", file=sys.stderr, flush=True)
            await asyncio.sleep(exc.seconds + 1)
        except BaseException:
            checkpoint()
            raise


async def download_attachments(
    client, entity, archive: Archive, max_mb: float | None, policy: str = "source"
):
    from .media import attachment_suffix, media_skip_reason

    archive.set("media_policy", policy)
    archive.set("max_media_mb", max_mb)
    rows = archive.db.execute("""
        SELECT m.id,m.topic_id,m.media_type,m.media_json,a.path,a.status FROM messages m
        LEFT JOIN attachments a ON a.message_id=m.id
        WHERE m.media_type IN ('MessageMediaPhoto','MessageMediaDocument') ORDER BY m.id
    """).fetchall()
    selected = [
        row
        for row in rows
        if not (policy == "source" and row["status"] == "discarded")
        and media_skip_reason(
            row["media_type"], json.loads(row["media_json"] or "{}"), policy, max_mb
        )
        is None
    ]
    print(
        f"Вложения ({policy}): выбрано {len(selected)}, исключено {len(rows) - len(selected)}.",
        file=sys.stderr,
        flush=True,
    )
    for i, row in enumerate(selected, 1):
        if row["status"] == "downloaded" and row["path"]:
            saved = archive.root / row["path"]
            if saved.is_file() and saved.stat().st_size > 0:
                continue
        metadata = json.loads(row["media_json"] or "{}")
        size = metadata.get("size")
        folder = archive.root / "media" / str(row["topic_id"] or "main")
        folder.mkdir(parents=True, exist_ok=True)
        ext = attachment_suffix(metadata)
        target = folder / f"{row['id']}{ext}"
        partial = target.with_name(target.name + ".part")
        try:
            message = await retry(lambda msg_id=row["id"]: client.get_messages(entity, ids=msg_id))
            if message is None:
                archive.attachment(row["id"], "unavailable", error="Сообщение больше не доступно")
                continue
            # Explicit filename prevents trusting filenames supplied in the remote document.
            result = await retry(
                lambda message=message, partial=partial: client.download_media(
                    message, file=str(partial)
                )
            )
            if result is None or not partial.is_file() or partial.stat().st_size == 0:
                raise ValueError("Telegram не вернул файл")
            actual_size = partial.stat().st_size
            if size and actual_size != size:
                raise ValueError(f"Неполный файл: {actual_size} из {size} байт")
            partial.replace(target)
            archive.attachment(
                row["id"], "downloaded", str(target.relative_to(archive.root)), actual_size
            )
        except (errors.RPCError, OSError, ValueError) as exc:
            archive.attachment(row["id"], "failed", error=type(exc).__name__)
            partial.unlink(missing_ok=True)
        if i % 50 == 0:
            print(f"Вложения: проверено {i}/{len(selected)}", file=sys.stderr, flush=True)


async def export_chat(
    root: Path,
    target: str,
    media: bool = True,
    max_mb=None,
    full=False,
    wait=1.5,
    media_policy="source",
    skip_history=False,
):
    client = make_client()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise ValueError("Сначала подтвердите вход: uv run tg-knowledge login --qr")
        entity = await resolve_chat(client, target)
        if not isinstance(entity, (types.Chat, types.Channel)):
            raise ValueError("Цель должна быть группой или каналом.")
        peer_id = utils.get_peer_id(entity)
        with Archive(root) as archive:
            previous = archive.get("chat")
            if previous and previous["id"] != peer_id:
                raise ValueError("Эта папка содержит другой чат. Задайте отдельную --data.")
            if skip_history and not archive.get("history_complete", False):
                raise ValueError("Нельзя пропустить историю: архив ещё не полный.")
            archive.set("media_policy", media_policy)
            archive.set("max_media_mb", max_mb)
            if skip_history:
                if media:
                    await download_attachments(client, entity, archive, max_mb, media_policy)
                print(json_text(archive.stats()))
                return
            archive.set(
                "chat",
                {
                    "id": peer_id,
                    "bare_id": entity.id,
                    "title": entity.title,
                    "username": getattr(entity, "username", None),
                    "forum": bool(getattr(entity, "forum", False)),
                    "source": target,
                },
            )
            archive.set("history_complete", False)
            topics = await collect_topics(client, entity, archive)
            print(f"Выгрузка: {entity.title}; тем: {len(topics)}", file=sys.stderr, flush=True)
            # General history plus each topic explicitly: do not assume getHistory covers a forum.
            await export_stream(client, entity, archive, None, full, wait)
            for topic_id in sorted(set(topics) - {1}):
                # A deleted topic cannot be read separately; its visible messages are in history.
                try:
                    await export_stream(client, entity, archive, topic_id, full, wait)
                except errors.RPCError as exc:
                    archive.set(f"topic_error:{topic_id}", type(exc).__name__)
                    raise ValueError(
                        f"Тема {topic_id} недоступна: {type(exc).__name__}; архив неполный"
                    ) from exc
            archive.set("history_complete", True)
            archive.set("last_sync", datetime.now(UTC).isoformat())
            if media:
                await download_attachments(client, entity, archive, max_mb, media_policy)
            print(json_text(archive.stats()))
    finally:
        await client.disconnect()


async def dialogs():
    client = make_client()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise ValueError("Сначала выполните login.")
        async for dialog in client.iter_dialogs():
            if isinstance(dialog.entity, (types.Channel, types.Chat)):
                print(
                    json_text(
                        {
                            "id": utils.get_peer_id(dialog.entity),
                            "title": dialog.name,
                            "forum": bool(getattr(dialog.entity, "forum", False)),
                        }
                    )
                )
    finally:
        await client.disconnect()
