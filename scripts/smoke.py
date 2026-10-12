"""No credentials or Telegram calls: real document processing and local CLI checks."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pymupdf
from telethon import types

from tg_knowledge.knowledge import Search, build
from tg_knowledge.reprocess import Processor, process_existing
from tg_knowledge.store import Archive
from tg_knowledge.telegram import message_record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--models", action="store_true", help="Also exercise real OCR and embeddings"
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="car-smoke-") as temporary:
        root = Path(temporary) / "Моя машина с пробелами"
        media = root / "media"
        media.mkdir(parents=True)
        path = media / "1.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((72, 72), "Battery recovery information for local search", fontsize=20)
            pdf.save(path)
        record = message_record(
            types.Message(
                id=1,
                peer_id=types.PeerChannel(123),
                date=datetime.now(UTC),
                message="Инструкция по аккумулятору из тестового документа",
            ),
            True,
        )
        record.update(
            media_type="MessageMediaDocument",
            media_json=json.dumps(
                {"id": 77, "name": "manual.pdf", "ext": ".pdf", "size": path.stat().st_size}
            ),
        )
        with Archive(root) as archive:
            archive.set("chat", {"id": -100123, "bare_id": 123, "username": "exampleclub"})
            archive.set("history_complete", True)
            archive.set("media_policy", "source")
            archive.save_message(record)
            archive.db.commit()
            archive.attachment(1, "downloaded", "media/1.pdf", path.stat().st_size)
        report = process_existing(root, prune=True, workers=1)
        assert report["status"] == "complete" and report["used"] == 1
        built = build(root, semantic=args.models)
        assert built["messages"] == 1
        hits = Search(root).search("Battery recovery", mode="lexical")
        assert hits["results"]
        assert hits["results"][0]["sources"][0]["url"] == "https://t.me/exampleclub/1"
        command = [sys.executable, "-m", "tg_knowledge.cli", "--data", str(root), "status"]
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        assert json.loads(result.stdout)["messages"] == 1
        if args.models:
            from PIL import Image, ImageDraw

            image = Image.new("RGB", (1800, 200), "white")
            ImageDraw.Draw(image).text(
                (30, 30), "Battery voltage recovery instructions", fill="black", font_size=60
            )
            image_path = root / "Изображение.png"
            image.save(image_path)
            processor = Processor(root)
            processor.check_tools()
            try:
                text = processor.ocr(image_path)
                assert "battery" in text.lower(), text
                hybrid = Search(root).search("Как восстановить аккумулятор", mode="hybrid")
                assert hybrid["results"]
            finally:
                processor.close()
        print("Smoke passed: processors, PDF source, Unicode paths, retrieval and CLI.")


if __name__ == "__main__":
    main()
