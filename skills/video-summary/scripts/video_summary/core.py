from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

DEFAULT_FALLBACK_LANGUAGES = ("en", "zh-Hans", "zh-Hant", "zh")

SUPPORTED_SUBTITLE_EXTENSIONS = ("json3", "srt", "vtt")

ACQUISITION_MANIFEST = "acquisition.json"

ACTIVE_TRANSCRIPT_MARKDOWN = "transcript.active.md"

ORIGINAL_MARKERS = ("original", "原始", "原文")

TRANSLATION_MARKERS = (" from ", "translated", "translation", "翻译", "翻譯")

LANGUAGE_ALIASES = {
    "zh-cn": "zh-hans",
    "zh-sg": "zh-hans",
    "zh-tw": "zh-hant",
    "zh-hk": "zh-hant",
    "zh-mo": "zh-hant",
}

PUNCTUATION = re.compile(r"\s+([,.;:!?，。！？；：])")

CHINESE_GAP = re.compile(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])")

MAX_COMPACTED_DURATION_MS = 20_000

MAX_COMPACTED_CHARACTERS = 400

TRANSCRIPT_CHUNK_SEGMENTS = 200

TRANSCRIPT_CHUNK_CHARACTERS = 12_000

TERMINAL_PUNCTUATION = re.compile(r"[.!?。！？][\"'’”）\]\}]*$")

NON_SPEECH_CUE = re.compile(r"^\s*(\[[^\]]+\]|\([^\)]+\)|[♪♫]+)\s*$")

SPEAKER_MARKER = re.compile(r"^\s*(>>|\[(speaker\s*)?[^\]]+\])", re.IGNORECASE)

LOCAL_ASR_CLIS = (
    {
        "command": "mlx_whisper",
        "engine": "MLX Whisper",
        "role": "preferred on Apple Silicon",
        "cache": "huggingface",
    },
    {
        "command": "whisper-ctranslate2",
        "engine": "faster-whisper via CTranslate2",
        "role": "general-purpose fast transcription",
        "cache": "huggingface",
    },
    {
        "command": "whisperx",
        "engine": "faster-whisper plus alignment",
        "role": "word timestamps and speaker diarization",
        "cache": "huggingface-and-whisper",
    },
    {
        "command": "whisper-cli",
        "engine": "whisper.cpp",
        "role": "lightweight offline transcription",
        "cache": "whisper-cpp",
    },
    {
        "command": "stable-ts",
        "engine": "Stable-ts",
        "role": "timestamp refinement",
        "cache": "whisper-or-huggingface",
    },
    {
        "command": "insanely-fast-whisper",
        "engine": "Transformers Whisper",
        "role": "high-throughput CUDA or MPS transcription",
        "cache": "huggingface",
    },
    {
        "command": "whisper",
        "engine": "OpenAI Whisper",
        "role": "reference implementation fallback",
        "cache": "whisper",
    },
)


def require_program(name: str) -> None:
    if shutil.which(name) is None:
        raise SystemExit(f"Required program is unavailable: {name}")


def run(command: list[str]) -> str:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout, file=sys.stderr, end="")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        raise SystemExit(result.returncode)
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    return result.stdout


def cookie_arguments(args: argparse.Namespace) -> list[str]:
    browser = getattr(args, "cookies_from_browser", None)
    return ["--cookies-from-browser", browser] if browser else []


def format_time(milliseconds: int) -> str:
    total_seconds = max(0, milliseconds) // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def parse_clock(value: str) -> int:
    parts = value.strip().replace(",", ".").split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError(f"Invalid timestamp: {value}")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return round(seconds * 1000)


def normalize_caption_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", html.unescape(value)).replace("\n", " ")
    value = re.sub(r"\s+", " ", value).strip()
    value = PUNCTUATION.sub(r"\1", value)
    return CHINESE_GAP.sub("", value)


def transcript_bundle_paths(markdown: Path) -> tuple[Path, Path]:
    stem = markdown.name.removesuffix(".md")
    return (
        markdown.with_name(f"{stem}.jsonl"),
        markdown.with_name(f"{stem}.index.json"),
    )


def transcript_raw_path(markdown: Path) -> Path:
    stem = markdown.name.removesuffix(".md")
    return markdown.with_name(f"{stem}.raw.jsonl")


def load_json_object(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def normalize_language(code: str | None) -> str | None:
    if not code:
        return None
    normalized = code.strip().replace("_", "-").lower()
    if normalized in {"und", "unknown", "none"}:
        return None
    normalized = normalized.removesuffix("-orig")
    return LANGUAGE_ALIASES.get(normalized, normalized)


def primary_language(code: str | None) -> str | None:
    normalized = normalize_language(code)
    return normalized.split("-", 1)[0] if normalized else None


def language_matches(first: str | None, second: str | None) -> bool:
    first_normalized = normalize_language(first)
    second_normalized = normalize_language(second)
    if not first_normalized or not second_normalized:
        return False
    return first_normalized == second_normalized or primary_language(
        first_normalized
    ) == primary_language(second_normalized)


def indexed_chapters(media_info: dict) -> list[dict]:
    chapters = []
    for chapter in media_info.get("chapters") or []:
        if not isinstance(chapter, dict):
            continue
        try:
            start_ms = round(float(chapter.get("start_time", 0)) * 1000)
            end_ms = round(
                float(chapter.get("end_time", chapter.get("start_time", 0))) * 1000
            )
        except (TypeError, ValueError):
            continue
        chapters.append(
            {
                "title": chapter.get("title"),
                "start_ms": start_ms,
                "end_ms": end_ms,
            }
        )
    return chapters


def platform_label(info: dict) -> str:
    marker = " ".join(
        str(info.get(field) or "")
        for field in ("extractor_key", "extractor", "webpage_url_domain")
    ).lower()
    if "youtube" in marker:
        return "youtube"
    if "bilibili" in marker:
        return "bilibili"
    if any(value in marker for value in ("twitter", "x.com")):
        return "x"
    return str(info.get("extractor_key") or info.get("extractor") or "unknown").lower()


def media_acquisition_fields(info: dict, requested_url: str | None) -> dict:
    duration = info.get("duration")
    duration_ms = (
        round(float(duration) * 1000) if isinstance(duration, (int, float)) else None
    )
    return {
        "url": info.get("webpage_url") or info.get("original_url") or requested_url,
        "platform": platform_label(info),
        "media": {
            "id": info.get("id"),
            "title": info.get("title"),
            "uploader": info.get("uploader"),
            "channel": info.get("channel"),
            "duration_ms": duration_ms,
            "upload_date": info.get("upload_date"),
            "original_language": normalize_language(info.get("language")),
            "chapters": indexed_chapters(info),
        },
        "paths": {"media_info": "media.info.json"},
    }


def update_acquisition(
    output: Path,
    *,
    requested_url: str | None = None,
    info: dict | None = None,
    transcript: dict | None = None,
    audio: dict | None = None,
    next_action: str | None = None,
) -> Path:
    path = output / ACQUISITION_MANIFEST
    manifest = load_json_object(path)
    manifest["version"] = 1
    if info is not None:
        manifest.update(media_acquisition_fields(info, requested_url))
    elif requested_url and not manifest.get("url"):
        manifest["url"] = requested_url
    if transcript is not None:
        manifest["transcript"] = transcript
    if audio is not None:
        manifest["audio"] = audio
    if next_action is not None:
        manifest["next_action"] = next_action
    write_json_atomic(path, manifest)
    return path


def write_json_atomic(path: Path, value: object) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temp, path)
