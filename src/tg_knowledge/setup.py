"""Interactive first run; secrets are entered in the local console only."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv, set_key

from .config import DEFAULT_CHAT, DEFAULT_DATA, client_config, data_dir, load_config
from .runtime import acquire_lock, install_console_interrupt, stop_child


def prepare_environment():
    env = Path(".env")
    if env.is_symlink():
        raise ValueError("Не записываю настройки через символическую ссылку.")
    env.touch(mode=0o600, exist_ok=True)
    load_dotenv(env)
    defaults = {
        "TELEGRAM_CHAT": DEFAULT_CHAT,
        "TG_DATA_DIR": DEFAULT_DATA,
        "TELEGRAM_SESSION": "secrets/telegram",
    }
    for key, value in defaults.items():
        if not os.getenv(key):
            set_key(env, key, value)
            os.environ[key] = value


def is_ready(root: Path) -> bool:
    import json

    from .store import Archive

    if not (root / "archive.sqlite3").is_file():
        return False
    with Archive(root) as archive:
        status = archive.stats()
    report = root / "knowledge/report.json"
    if not report.is_file():
        return False
    knowledge = json.loads(report.read_text(encoding="utf-8"))
    job_file = root / "job.json"
    if not job_file.is_file():
        return False
    job = json.loads(job_file.read_text(encoding="utf-8"))
    return (
        job.get("status") == "complete"
        and status["history_complete"]
        and status["media_selection"]["complete"]
        and (status.get("media_processing") or {}).get("status") == "complete"
        and knowledge.get("history_complete") is True
        and knowledge.get("semantic_ready") is True
        and knowledge.get("media_selection", {}).get("complete") is True
        and knowledge.get("media_processing", {}).get("status") == "complete"
        and knowledge.get("messages") == status["messages"]
    )


def main():
    load_config()
    install_console_interrupt()
    prepare_environment()
    # A second launcher must not open the same Telegram session or prompt for secrets.
    root = data_dir()
    try:
        lock = acquire_lock(root / ".setup.lock")
    except BlockingIOError:
        print("Настройка или выгрузка уже запущена. Дождитесь её завершения.")
        return
    try:
        try:
            job_lock = acquire_lock(root / ".job.lock")
        except BlockingIOError:
            print("Выгрузка уже работает. Откройте Status.cmd для проверки.")
            return
        job_lock.release()
        if is_ready(root):
            print("База уже готова. Откройте проект в Codex и задавайте вопросы.")
            print("Для получения новых сообщений запустите Update.cmd.")
            return
        print("Telegram → локальная база знаний об автомобиле")
        print("Клуб: " + os.getenv("TELEGRAM_CHAT", DEFAULT_CHAT))
        print("Скачиваются сообщения всех тем и выбранные документы; внешние видео не скачиваются.")
        try:
            client_config()
        except ValueError:
            print("\nОткройте https://my.telegram.org/apps и войдите своим номером Telegram.")
            print("В API development tools создайте приложение для личного использования.")
            print(
                "Подробности: docs/START-WINDOWS.md. Вводите ключи только здесь, не в чате Codex."
            )
            from .cli import configure

            configure()
            load_dotenv(Path(".env"), override=True)
        from .telegram import login

        # The phone/code path uses an ordinary visible console and hidden password input.
        asyncio.run(login())
        print("\nНачинаю выгрузку. Оставьте окно открытым и отключите сон компьютера.")
        print("Прогресс: Status.cmd. Подробный журнал: " + str(root / "pipeline.log"))
        # Remain in the console: Ctrl+C stops safely; launching again resumes checkpoints.
        options = {}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        child = subprocess.Popen(
            [
                sys.executable,
                "-u",
                "-m",
                "tg_knowledge.background",
                "--media-policy",
                "source",
                "--process-existing",
                "--prune-unused",
                "--log",
            ],
            **options,
        )
        try:
            code = child.wait()
        except KeyboardInterrupt:
            stop_child(child, timeout=45)
            raise
        if code:
            raise SystemExit(code)
    finally:
        lock.release()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nОстановлено. Запустите Start.cmd ещё раз, чтобы продолжить.")
        raise SystemExit(130) from None
    except (ValueError, OSError, ImportError) as exc:
        print(
            f"Ошибка: {type(exc).__name__}. Подробности могут помочь Codex; ключи не пересылайте."
        )
        raise SystemExit(1) from None
