"""Resume local OCR/ASR and archive extraction; prune only confirmed unused files."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import tempfile
import threading
import unicodedata
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

from .knowledge import atomic_text, attachment_text, normalize
from .media import (
    PROCESSING_VERSION,
    SOURCE_SUFFIXES,
    attachment_suffix,
    media_identity,
    processed_source,
)
from .store import Archive, json_text
from .runtime import acquire_lock


def has_useful_text(text: str, *, ocr: bool = False) -> bool:
    text = normalize(text)
    if not text:
        return False
    letters = sum(c.isalpha() for c in text)
    chinese = sum("\u3400" <= c <= "\u9fff" for c in text)
    words = re.findall(r"[^\W\d_]{2,}", text, re.UNICODE)
    if ocr:
        return chinese >= 8 or (letters >= 25 and len(words) >= 4)
    return letters >= 15 and len(text) >= 30


def damaged_pdf_text(text: str) -> bool:
    bad = sum(unicodedata.category(c) == "Co" or c == "\ufffd" for c in text)
    return bad > max(2, len(text) * 0.01)


def source_document_text(path: Path, suffix: str) -> str:
    text, status = attachment_text(path, suffix)
    if status not in {"extracted", "extracted_cp1251"}:
        # Errors and extraction limits do not prove the document has no useful text.
        raise ValueError(f"Document extraction incomplete: {status}; original preserved")
    return text


class Processor:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.tools = self.root / "tools"
        self.ocr_readers = {}
        self.ocr_lock = threading.Lock()
        self.asr_lock = threading.Lock()
        self.whisper = None
        self.ffmpeg = None

    def check_tools(self):
        import pymupdf  # noqa: F401
        import easyocr  # noqa: F401
        import faster_whisper  # noqa: F401
        import av  # noqa: F401
        import imageio_ffmpeg

        self.tools.mkdir(parents=True, exist_ok=True)
        self.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    def ocr(self, path: Path, *, chinese=False) -> str:
        text = self._ocr_read(path, chinese=chinese)
        if not text and not chinese:
            text = self._ocr_read(path, chinese=True)
        return text

    def _ocr_read(self, path: Path, *, chinese=False) -> str:
        import easyocr
        import torch

        with self.ocr_lock:
            torch.set_num_threads(4)
            if chinese not in self.ocr_readers:
                self.ocr_readers[chinese] = easyocr.Reader(
                    ["ch_sim", "en"] if chinese else ["ru", "en"],
                    gpu=False,
                    model_storage_directory=str(self.tools / "ocr"),
                    verbose=False,
                )
            # Passing bytes also supports Cyrillic paths on Windows/OpenCV.
            lines = self.ocr_readers[chinese].readtext(path.read_bytes(), detail=1)
        text = normalize(
            "\n".join(
                text for _, text, confidence in lines if confidence >= (0.45 if chinese else 0.65)
            )
        )
        return text if has_useful_text(text, ocr=True) else ""

    def close(self):
        self.ocr_readers.clear()
        self.whisper = None

    def pdf(self, *, path=None, data=None, locator="PDF", cache: Path, scratch: Path) -> list[dict]:
        import pymupdf

        sections = []
        with pymupdf.open(path) if path else pymupdf.open(stream=data, filetype="pdf") as pdf:
            if len(pdf) > 5000:
                raise ValueError("PDF page limit exceeded")
            for number, page in enumerate(pdf, 1):
                saved = cache / f"{number}.json"
                if saved.is_file():
                    section = json.loads(saved.read_text(encoding="utf-8"))
                else:
                    text = normalize(page.get_text(sort=True))
                    method = "pdf_text"
                    if damaged_pdf_text(text) or not has_useful_text(text):
                        image = scratch / "page.png"
                        scale = min(2.5, 2000 / max(page.rect.width, page.rect.height))
                        page.get_pixmap(matrix=pymupdf.Matrix(scale, scale)).save(image)
                        chinese = sum("\u3400" <= c <= "\u9fff" for c in text + locator) >= 5
                        text = self.ocr(image, chinese=chinese)
                        method = "ocr"
                    section = {
                        "locator": f"{locator}, страница {number}",
                        "text": text,
                        "method": method,
                        "automatic": method == "ocr",
                    }
                    atomic_text(saved, json_text(section))
                if has_useful_text(section["text"]):
                    sections.append(section)
        return sections

    def archive(self, path: Path, cache: Path, scratch: Path) -> list[dict]:
        sections = []
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > 10000 or sum(m.file_size for m in members) > 20 * 1024**3:
                raise ValueError("ZIP expansion limit exceeded")
            for member in members:
                suffix = Path(member.filename).suffix.lower()
                if member.is_dir() or suffix not in SOURCE_SUFFIXES:
                    continue
                if member.file_size > 100 * 1024**2:
                    raise ValueError("ZIP document size limit exceeded")
                key = hashlib.sha256(
                    json_text([member.filename, member.CRC, member.file_size]).encode()
                ).hexdigest()
                part = cache / key
                done = part / "result.json"
                if done.is_file():
                    sections.extend(json.loads(done.read_text(encoding="utf-8")))
                    continue
                # Members are read as bytes; archive paths are never written or executed.
                data = archive.read(member)
                locator = f"Файл в ZIP: {member.filename}"
                if suffix == ".pdf":
                    if b"%PDF-" not in data[:1024]:
                        # A mislabeled binary/resource file is not a PDF document.
                        # Keep it in the original ZIP and audit the exclusion.
                        atomic_text(
                            part / "skipped.json",
                            json_text(
                                {
                                    "member": member.filename,
                                    "reason": "not_pdf_content",
                                    "bytes": member.file_size,
                                }
                            ),
                        )
                        atomic_text(done, "[]")
                        continue
                    extracted = self.pdf(
                        data=data, locator=locator, cache=part / "pages", scratch=scratch
                    )
                else:
                    document = scratch / f"document{suffix}"
                    document.write_bytes(data)
                    text = source_document_text(document, suffix)
                    extracted = (
                        [
                            {
                                "locator": locator,
                                "text": text,
                                "method": "document_text",
                                "automatic": False,
                            }
                        ]
                        if has_useful_text(text)
                        else []
                    )
                atomic_text(done, json_text(extracted))
                sections.extend(extracted)
        return sections

    def transcribe(self, path: Path, scratch: Path) -> list[dict]:
        import av
        from faster_whisper import WhisperModel

        with av.open(str(path)) as container:
            if not container.streams.audio:
                return []
        sections, seen = [], Counter()
        with self.asr_lock:
            if self.whisper is None:
                self.whisper = WhisperModel(
                    "small",
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=4,
                    download_root=str(self.tools / "whisper"),
                )
            segments, _ = self.whisper.transcribe(
                str(path), vad_filter=True, beam_size=5, condition_on_previous_text=False
            )
            for segment in segments:
                text = normalize(segment.text)
                if not has_useful_text(text) or segment.avg_logprob < -1:
                    continue
                if len(text) < 120 and re.search(
                    r"спасибо за просмотр|субтитры|подписывайтесь|продолжение следует", text, re.I
                ):
                    continue
                if seen[text.casefold()] >= 2:
                    continue
                seen[text.casefold()] += 1
                sections.append(
                    {
                        "locator": f"Речь {segment.start:.1f}–{segment.end:.1f} с",
                        "text": text,
                        "method": "faster_whisper_small",
                        "automatic": True,
                    }
                )
        return sections

    def video(self, path: Path, metadata: dict, scratch: Path) -> list[dict]:
        sections = self.transcribe(path, scratch)
        duration = float(metadata.get("duration") or 0)
        if duration <= 0:
            import av

            with av.open(str(path)) as container:
                if container.duration is None:
                    raise ValueError("Video duration is unavailable; original preserved")
                duration = float(container.duration / av.time_base)
        if not math.isfinite(duration) or duration <= 0 or duration > 24 * 3600:
            raise ValueError("Invalid or excessive video duration; original preserved")
        times = sorted(
            {0.0, min(1.0, duration / 2), duration / 2}
            | {float(t) for t in range(30, math.ceil(duration), 30)}
        )
        seen = set()
        for seconds in times:
            image = scratch / "frame.png"
            image.unlink(missing_ok=True)
            subprocess.run(
                [
                    self.ffmpeg,
                    "-v",
                    "error",
                    "-nostdin",
                    "-y",
                    "-ss",
                    str(seconds),
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=1600:-2",
                    str(image),
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
            text = self.ocr(image)
            fingerprint = normalize(text).casefold()
            if text and fingerprint not in seen:
                seen.add(fingerprint)
                sections.append(
                    {
                        "locator": f"Кадр {seconds:.1f} с (выборка каждые 30 с)",
                        "text": text,
                        "method": "ocr_sampled_video_frame",
                        "automatic": True,
                    }
                )
        return sections

    def file(self, row: dict) -> dict:
        metadata = json.loads(row["media_json"] or "{}")
        cached = processed_source(self.root, row["id"], metadata)
        if cached is not None and cached["status"] != "error":
            return cached
        path = (self.root / row["path"]).resolve()
        if not path.is_relative_to(self.root / "media") or not path.is_file():
            raise ValueError("Original attachment path is unavailable or outside media")
        identity = media_identity(metadata)
        cache_key = hashlib.sha256(json_text(identity).encode()).hexdigest()[:16]
        cache = self.root / "processed/parts" / str(row["id"]) / cache_key
        suffix = attachment_suffix(metadata)
        kind = "unsupported"
        with tempfile.TemporaryDirectory(prefix="media-", dir=self.root / "processed") as temporary:
            scratch = Path(temporary)
            if suffix == ".pdf":
                kind = "pdf"
                sections = self.pdf(path=path, cache=cache, scratch=scratch)
            elif suffix == ".zip":
                kind = "zip_documents"
                sections = self.archive(path, cache, scratch)
            elif row["media_type"] == "MessageMediaPhoto" or suffix in {
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
                ".heic",
                ".avif",
            }:
                kind = "image_ocr"
                text = self.ocr(path)
                sections = (
                    [
                        {
                            "locator": "Текст изображения",
                            "text": text,
                            "method": "ocr",
                            "automatic": True,
                        }
                    ]
                    if text
                    else []
                )
            elif suffix in {".mp4", ".mov", ".m4v", ".webm"}:
                kind = "video_asr_ocr"
                sections = self.video(path, metadata, scratch)
            elif suffix in {".m4a", ".mp3", ".aac", ".oga", ".ogg", ".wav"}:
                kind = "audio_asr"
                sections = self.transcribe(path, scratch)
            elif suffix in SOURCE_SUFFIXES:
                kind = "document"
                text = source_document_text(path, suffix)
                sections = (
                    [
                        {
                            "locator": "Текст документа",
                            "text": text,
                            "method": "document_text",
                            "automatic": False,
                        }
                    ]
                    if has_useful_text(text)
                    else []
                )
            else:
                sections = []
        result = {
            "version": PROCESSING_VERSION,
            "message_id": row["id"],
            "identity": identity,
            "status": "used" if sections else "no_useful_text",
            "kind": kind,
            "sections": sections,
            "characters": sum(len(s["text"]) for s in sections),
            "processed_at": datetime.now(UTC).isoformat(),
            "ignored_archive_members": [
                json.loads(note.read_text(encoding="utf-8")) for note in cache.rglob("skipped.json")
            ]
            if kind == "zip_documents"
            else [],
        }
        atomic_text(self.root / "processed" / f"{row['id']}.json", json_text(result))
        return result


def prune_unused(root: Path, row: dict, result: dict, archive: Archive) -> int:
    if result["status"] != "no_useful_text" or not row["path"]:
        return 0
    cached = processed_source(root, row["id"], json.loads(row["media_json"] or "{}"))
    if cached is None or cached["status"] != "no_useful_text":
        raise ValueError("Pruning requires a durable matching processing result")
    path = root / row["path"]
    if path.is_symlink() or not path.resolve().is_relative_to((root / "media").resolve()):
        raise ValueError("Unsafe pruning path")
    # The processing result is durable before the original file is removed.
    removed = path.stat().st_size if path.exists() else 0
    path.unlink(missing_ok=True)
    archive.attachment(row["id"], "discarded", size=row["saved_size"], error="no_useful_text")
    return removed


def process_existing(root: Path, *, prune=False, workers=3) -> dict:
    root = root.resolve()
    (root / "processed").mkdir(parents=True, exist_ok=True)
    lock = acquire_lock(root / ".media-process.lock")
    try:
        processor = Processor(root)
        processor.check_tools()
        with Archive(root) as archive:
            rows = [
                dict(r)
                for r in archive.db.execute("""
                SELECT m.id,m.media_type,m.media_json,a.path,a.size AS saved_size
                FROM messages m JOIN attachments a ON a.message_id=m.id
                WHERE (a.status='downloaded' AND a.path IS NOT NULL) OR a.status='discarded' ORDER BY
                    CASE WHEN a.path LIKE '%.apk' THEN 0 WHEN a.path LIKE '%.zip' THEN 1
                    WHEN a.path LIKE '%.pdf' THEN 2 ELSE 3 END,m.id
            """)
            ]
            previous = archive.get("media_processing", {})
            report = {
                "status": "running",
                "total": len(rows),
                "processed": 0,
                "used": 0,
                "no_useful_text": 0,
                "errors": 0,
                "deleted_files": previous.get("deleted_files", 0),
                "freed_bytes": previous.get("freed_bytes", 0),
                "by_kind": {},
                "started_at": datetime.now(UTC).isoformat(),
                "prune": prune,
                "deleted_partial_files": previous.get("deleted_partial_files", 0),
                "freed_partial_bytes": previous.get("freed_partial_bytes", 0),
            }
            archive.set("media_processing", report)
            counts = Counter()
            try:
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = {executor.submit(processor.file, row): row for row in rows}
                    for future in as_completed(futures):
                        row = futures[future]
                        try:
                            result = future.result()
                            if prune:
                                freed = prune_unused(root, row, result, archive)
                                if freed:
                                    report["freed_bytes"] += freed
                                    report["deleted_files"] += 1
                            if result["status"] == "used":
                                report["used"] += 1
                                counts[result["kind"]] += 1
                            else:
                                report["no_useful_text"] += 1
                        except Exception as exc:
                            report["errors"] += 1
                            print(
                                f"Обработка #{row['id']}: {type(exc).__name__}; оригинал сохранён.",
                                flush=True,
                            )
                        report["processed"] += 1
                        report["by_kind"] = dict(counts)
                        report["updated_at"] = datetime.now(UTC).isoformat()
                        archive.set("media_processing", report)
                        if report["processed"] % 25 == 0:
                            print(
                                f"Обработано {report['processed']}/{report['total']}; источников {report['used']}; удалено {report['deleted_files']}.",
                                flush=True,
                            )
            finally:
                processor.close()
            report["status"] = "complete" if not report["errors"] else "complete_with_errors"
            discarded = archive.db.execute(
                "SELECT COUNT(*),COALESCE(SUM(size),0) FROM attachments WHERE status='discarded'"
            ).fetchone()
            report["deleted_files"] = discarded[0]
            report["freed_bytes"] = discarded[1] + report["freed_partial_bytes"]
            report["finished_at"] = datetime.now(UTC).isoformat()
            archive.set("media_processing", report)
            atomic_text(root / "processed/report.json", json_text(report))
            return report

    finally:
        lock.release()


def main():
    parser = argparse.ArgumentParser(description="Обработать уже скачанное локально")
    from .config import data_dir, load_config

    load_config()
    parser.add_argument("--data")
    parser.add_argument("--prune", action="store_true")
    args = parser.parse_args()
    print(json_text(process_existing(data_dir(args.data), prune=args.prune)))


if __name__ == "__main__":
    main()
