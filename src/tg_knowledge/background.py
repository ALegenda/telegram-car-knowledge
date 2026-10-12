"""Persistent local runner for the complete export/index/media pipeline."""

import argparse
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

from .config import DEFAULT_CHAT, data_dir, load_config
from .runtime import acquire_lock, install_console_interrupt, stop_child
from .knowledge import atomic_text
from .store import json_text


def main():
    load_config()
    install_console_interrupt()
    parser = argparse.ArgumentParser(description="Фоновая полная выгрузка с повтором после сбоя")
    parser.add_argument("--data")
    parser.add_argument("--chat", default=os.getenv("TELEGRAM_CHAT", DEFAULT_CHAT))
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--media-policy", choices=["source", "all", "none"], default="source")
    parser.add_argument("--skip-history", action="store_true")
    parser.add_argument("--process-existing", action="store_true")
    parser.add_argument("--prune-unused", action="store_true")
    parser.add_argument("--log", action="store_true", help="Журнал в data/.../pipeline.log")
    args = parser.parse_args()
    root = data_dir(args.data)
    root.mkdir(parents=True, exist_ok=True)
    try:
        lock = acquire_lock(root / ".job.lock")
    except BlockingIOError:
        print("Фоновая выгрузка уже работает.", flush=True)
        return
    log = None
    try:
        atomic_text(root / "pipeline.pid", str(os.getpid()))
        if args.log:
            log = (root / "pipeline.log").open("a", encoding="utf-8")
        import psutil

        state = {
            "pid": os.getpid(),
            "process_created_at": psutil.Process().create_time(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }

        def status(value, **fields):
            state.update(status=value, updated_at=datetime.now(timezone.utc).isoformat(), **fields)
            atomic_text(root / "job.json", json_text(state))

        child = None
        try:
            for attempt in range(1, max(1, args.attempts) + 1):
                status("running", attempt=attempt)
                print(
                    f"Фоновый запуск: попытка {attempt}, продолжение с контрольной точки.",
                    flush=True,
                )
                command = [
                    sys.executable,
                    "-u",
                    "-m",
                    "tg_knowledge.cli",
                    "--data",
                    str(root),
                    "pipeline",
                    "--semantic",
                    "--chat",
                    args.chat,
                    "--media-policy",
                    args.media_policy,
                ]
                if args.skip_history:
                    command.append("--skip-history")
                if args.process_existing:
                    command.append("--process-existing")
                if args.prune_unused:
                    command.append("--prune-unused")
                options = {}
                if log is not None:
                    options.update(stdout=log, stderr=subprocess.STDOUT)
                if os.name == "nt":
                    options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                child = subprocess.Popen(command, **options)
                code = child.wait()
                if code == 0:
                    from .store import Archive

                    with Archive(root) as archive:
                        current = archive.stats()
                    selected = current["media_selection"]
                    processing = current.get("media_processing") or {}
                    processing_errors = args.process_existing and processing.get("errors", 0)
                    if (
                        selected["attachments"].get("failed", 0) or processing_errors
                    ) and attempt < args.attempts:
                        status(
                            "retrying",
                            exit_code=0,
                            reason="processing_failures"
                            if processing_errors
                            else "attachment_failures",
                        )
                        print("Повтор неудачных файлов через 30 секунд.", flush=True)
                        time.sleep(30)
                        continue
                    complete = current["history_complete"] and selected["complete"]
                    final_status = "complete" if complete else "complete_with_missing_media"
                    if processing_errors:
                        final_status = "complete_with_processing_errors"
                    status(final_status, exit_code=0)
                    print(
                        "Часть файлов не обработана; оригиналы сохранены."
                        if processing_errors
                        else "История, выбранные вложения и индекс готовы."
                        if complete
                        else "История и индекс готовы; часть вложений недоступна. См. status.",
                        flush=True,
                    )
                    return
                if code in {130, -signal.SIGINT, -signal.SIGTERM}:
                    status("stopped", exit_code=code)
                    return
                status("retrying" if attempt < args.attempts else "failed", exit_code=code)
                if attempt < args.attempts:
                    print("Сбой. Повтор через 30 секунд; прогресс сохранён.", flush=True)
                    time.sleep(30)
            raise SystemExit(1)
        except KeyboardInterrupt:
            if child is not None:
                stop_child(child)
            status("stopped")
            raise SystemExit(130) from None

    finally:
        if log is not None:
            log.close()
        lock.release()


if __name__ == "__main__":
    main()
