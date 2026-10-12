from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
import signal
from pathlib import Path

from .config import DEFAULT_CHAT, data_dir, load_config
from .runtime import acquire_lock, process_running
from .store import Archive, json_text


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="Архив Telegram-форума и локальный поиск")
    cli.add_argument("--data", help="Каталог архива (TG_DATA_DIR / data/fangchengbao)")
    sub = cli.add_subparsers(dest="command", required=True)
    sub.add_parser("configure", help="Локально сохранить параметры Telegram API")
    login = sub.add_parser("login", help="Подключить свой Telegram-аккаунт")
    login.add_argument("--qr", action="store_true", help="Вход сканированием QR")
    login.add_argument(
        "--password", action="store_true", help="Завершить подтверждённый QR-вход с 2FA"
    )
    sub.add_parser("dialogs", help="Показать доступные группы и их ID")
    for name in ("export", "pipeline"):
        export = sub.add_parser(
            name, help="Выгрузить сообщения" if name == "export" else "Выгрузить и построить индекс"
        )
        export.add_argument("--chat", default=os.getenv("TELEGRAM_CHAT", DEFAULT_CHAT))
        export.add_argument(
            "--no-media", action="store_true", help="Скачать текст и метаданные без файлов"
        )
        export.add_argument(
            "--media-policy",
            choices=["source", "all", "none"],
            default="source",
            help="source: только документы для поиска (по умолчанию); all: все файлы; none: без файлов",
        )
        export.add_argument(
            "--skip-history",
            action="store_true",
            help="Продолжить загрузку вложений по уже полной локальной истории",
        )
        export.add_argument(
            "--max-media-mb", type=float, help="Явный лимит одного файла; по умолчанию без лимита"
        )
        export.add_argument(
            "--full", action="store_true", help="Повторно пройти всю историю (старые правки)"
        )
        export.add_argument(
            "--wait", type=float, default=1.5, help="Пауза между страницами истории, секунды"
        )
        if name == "pipeline":
            export.add_argument(
                "--semantic", action="store_true", help="Также построить локальные эмбеддинги"
            )
            export.add_argument(
                "--process-existing",
                action="store_true",
                help="Локально обработать уже скачанные файлы: OCR, речь, PDF и ZIP",
            )
            export.add_argument(
                "--prune-unused",
                action="store_true",
                help="После обработки удалить файлы без пригодного текста",
            )
    build = sub.add_parser("build", help="Очистить архив и построить индекс")
    build.add_argument("--semantic", action="store_true")
    build.add_argument("--model", default="intfloat/multilingual-e5-small")
    for name in ("search", "context"):
        search = sub.add_parser(
            name, help="Поиск" if name == "search" else "Подготовить источники для ассистента"
        )
        search.add_argument("query")
        search.add_argument("--limit", type=int, default=8)
        search.add_argument("--topic", type=int)
        search.add_argument("--after", help="Начиная с даты YYYY-MM-DD")
        search.add_argument("--before", help="До даты YYYY-MM-DD, не включительно")
        search.add_argument("--mode", choices=["auto", "lexical", "hybrid"], default="auto")
        if name == "context":
            search.add_argument("--output", help="Сохранить Markdown с источниками")
    message = sub.add_parser("message", help="Оригинал сообщения с соседями из той же темы")
    message.add_argument("id", type=int)
    message.add_argument("--radius", type=int, default=2)
    sub.add_parser("status", help="Состояние выгрузки и индекса")
    sub.add_parser("serve", help="Локальный MCP-сервер для ассистента")
    return cli


def configure():
    from dotenv import set_key

    api_id = input("App api_id с my.telegram.org/apps: ").strip()
    api_hash = getpass.getpass("App api_hash (ввод скрыт): ").strip()
    import re

    if not api_id.isdigit() or int(api_id) <= 0 or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise ValueError("Нужны положительный api_id и api_hash из 32 шестнадцатеричных символов.")
    path = Path(".env")
    if path.is_symlink():
        raise ValueError("Не записываю секреты через символическую ссылку.")
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    set_key(path, "TELEGRAM_API_ID", api_id)
    set_key(path, "TELEGRAM_API_HASH", api_hash)
    print("Параметры сохранены локально в .env. Не пересылайте этот файл.")


def main():
    load_config()
    if hasattr(signal, "SIGBREAK"):

        def interrupt(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGBREAK, interrupt)
    args = parser().parse_args()
    root = data_dir(args.data)
    export_lock = None
    try:
        if args.command in {"export", "pipeline"}:
            root.mkdir(parents=True, exist_ok=True)
            try:
                export_lock = acquire_lock(root / ".export.lock")
            except BlockingIOError as exc:
                raise ValueError("Выгрузка уже запущена для этой папки.") from exc
        if args.command == "configure":
            configure()
        elif args.command in {"login", "dialogs", "export", "pipeline"}:
            from .telegram import dialogs, export_chat, login

            if args.command == "login":
                asyncio.run(login(args.qr, args.password))
            elif args.command == "dialogs":
                asyncio.run(dialogs())
            else:
                if args.wait < 0 or (args.max_media_mb is not None and args.max_media_mb <= 0):
                    raise ValueError(
                        "Пауза должна быть неотрицательной, лимит файла — положительным."
                    )
                media_policy = "none" if args.no_media else args.media_policy
                if args.full and args.skip_history:
                    raise ValueError("--full несовместим с --skip-history.")
                if not args.skip_history or args.command == "export":
                    asyncio.run(
                        export_chat(
                            root,
                            args.chat,
                            args.command == "export" and media_policy != "none",
                            args.max_media_mb,
                            args.full,
                            args.wait,
                            media_policy,
                            args.skip_history,
                        )
                    )
                elif not (root / "archive.sqlite3").is_file():
                    raise ValueError("Архив отсутствует.")
                else:
                    with Archive(root) as archive:
                        if not archive.get("history_complete", False):
                            raise ValueError("Нельзя пропустить историю: архив ещё не полный.")
                        archive.set("media_policy", media_policy)
                        archive.set("max_media_mb", args.max_media_mb)
                if args.command == "pipeline":
                    from .knowledge import build

                    if args.prune_unused and not args.process_existing:
                        raise ValueError("Удаление требует --process-existing.")

                    if not args.skip_history or media_policy == "none":
                        print(json_text(build(root, args.semantic)))
                    if media_policy != "none":
                        asyncio.run(
                            export_chat(
                                root,
                                args.chat,
                                True,
                                args.max_media_mb,
                                False,
                                args.wait,
                                media_policy,
                                True,
                            )
                        )
                    if args.process_existing:
                        from .reprocess import process_existing

                        print(json_text(process_existing(root, prune=args.prune_unused)))
                    if media_policy != "none" or args.process_existing:
                        print(json_text(build(root, args.semantic)))
        elif args.command == "build":
            from .knowledge import build

            print(json_text(build(root, args.semantic, args.model)))
        elif args.command in {"search", "context", "message"}:
            from .knowledge import Search, atomic_text, context_markdown

            search = Search(root)
            if args.command == "message":
                print(json_text(search.message(args.id, args.radius)))
            else:
                result = search.search(
                    args.query, args.limit, args.topic, args.after, args.before, args.mode
                )
                if args.command == "search":
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    text = context_markdown(result)
                    if args.output:
                        atomic_text(Path(args.output).resolve(), text)
                        print(f"Источники сохранены: {Path(args.output).resolve()}")
                    else:
                        print(text)
        elif args.command == "status":
            if not (root / "archive.sqlite3").exists():
                raise ValueError("Архив ещё не создан.")
            with Archive(root) as archive:
                status = archive.stats()
            report = root / "knowledge/report.json"
            if report.exists():
                status["knowledge"] = json.loads(report.read_text(encoding="utf-8"))
            job_file = root / "job.json"
            if job_file.exists():
                status["job"] = json.loads(job_file.read_text(encoding="utf-8"))
            pid_file = root / "pipeline.pid"
            if pid_file.is_file():
                try:
                    pid = int(pid_file.read_text(encoding="utf-8"))
                    status["pipeline_running"] = process_running(
                        pid, status.get("job", {}).get("process_created_at")
                    )
                except ValueError:
                    status["pipeline_running"] = False
            print(json.dumps(status, ensure_ascii=False, indent=2))
        elif args.command == "serve":
            from .server import serve

            serve(root)
    except KeyboardInterrupt:
        print("\nОстановлено. Для продолжения повторите команду.", file=sys.stderr)
        raise SystemExit(130) from None
    except (ValueError, OSError, ImportError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        if export_lock is not None:
            export_lock.release()


if __name__ == "__main__":
    main()
