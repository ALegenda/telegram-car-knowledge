from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from .config import Settings
from .importer import import_archive
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="Telegram-бот с отдельной базой каждого чата")
    parser.add_argument("--env", type=Path, default=Path(".env.bot"))
    parser.add_argument("--data", type=Path, help="Отдельная папка данных бота")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="Запустить long polling и обработку")
    status = commands.add_parser("status", help="Локальное состояние без вызовов API")
    status.add_argument("--health", action="store_true", help="Код 1, если нет успешного polling за 90 с")
    importer = commands.add_parser("import-archive", help="Прочитать архив исходного проекта")
    importer.add_argument("archive", type=Path)
    importer.add_argument("--chat-id", required=True, type=int)
    commands.add_parser("retry-failed", help="Повторить обновления с исчерпанными попытками")
    args = parser.parse_args()
    os.umask(0o077)
    load_dotenv(args.env, override=False)
    if args.data:
        os.environ["BOT_DATA_DIR"] = str(args.data.resolve())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # HTTP request loggers include the Telegram token in URLs.
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    try:
        settings = Settings.from_env(credentials=args.command == "run")
        if args.command == "run":
            from .service import serve

            asyncio.run(serve(settings))
            return
        if args.command == "status" and not (settings.root / "bot.sqlite3").is_file():
            print(json.dumps({"running": False, "messages": 0}))
            if args.health:
                raise SystemExit(1)
            return
        if args.command == "import-archive":
            from ..runtime import acquire_lock

            lock = acquire_lock(settings.root / ".bot.lock")
        else:
            lock = None
        store = Store(settings.root, readonly=args.command == "status")
        try:
            if args.command == "status":
                result = store.stats()
                result["embedding_error"] = store.get("embedding_error")
                print(json.dumps(result, ensure_ascii=False, indent=2))
                if args.health:
                    last = result["last_poll"]
                    if not last or (datetime.now(UTC) - datetime.fromisoformat(last)).total_seconds() > 90:
                        raise SystemExit(1)
            elif args.command == "import-archive":
                print(json.dumps({"imported_messages": import_archive(store, args.archive, args.chat_id)}))
            elif args.command == "retry-failed":
                with store.db:
                    cursor = store.db.execute(
                        "UPDATE inbox SET status='pending',attempts=0,next_attempt=0,error=NULL "
                        "WHERE status='failed'"
                    )
                print(json.dumps({"retrying": cursor.rowcount}))
        finally:
            store.close()
            if lock:
                lock.release()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Do not leak private data via CLI traceback / HTTP exception strings.
        logging.error("Операция не завершена: %s", type(exc).__name__)
        if isinstance(exc, (ValueError, BlockingIOError)):
            logging.error("%s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
