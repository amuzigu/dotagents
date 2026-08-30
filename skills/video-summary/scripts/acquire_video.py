#!/usr/bin/env python3
"""Acquire and normalize evidence for video URL summaries."""

from __future__ import annotations

import argparse
import hashlib
import html
import http.client
import json
import mimetypes
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
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


def lexical_tokens(value: str) -> list[tuple[str, int, int]]:
    tokens: list[tuple[str, int, int]] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character.isascii() and character.isalnum():
            end = index + 1
            while end < len(value):
                current = value[end]
                if current.isascii() and current.isalnum():
                    end += 1
                    continue
                if (
                    current in {"'", "’"}
                    and end + 1 < len(value)
                    and value[end + 1].isascii()
                    and value[end + 1].isalnum()
                ):
                    end += 1
                    continue
                break
            token = unicodedata.normalize("NFKC", value[index:end]).casefold()
            tokens.append((token, index, end))
            index = end
            continue
        if character.isalnum():
            token = unicodedata.normalize("NFKC", character).casefold()
            tokens.append((token, index, index + 1))
        index += 1
    return tokens


def token_text_length(tokens: list[tuple[str, int, int]]) -> int:
    return sum(len(token) for token, _, _ in tokens)


def cjk_token_count(tokens: list[tuple[str, int, int]]) -> int:
    return sum(
        1 for token, _, _ in tokens
        if len(token) == 1 and "\u3400" <= token <= "\u9fff"
    )


def edge_contains(longer: list[str], shorter: list[str]) -> bool:
    if not shorter or len(shorter) >= len(longer):
        return False
    return longer[:len(shorter)] == shorter or longer[-len(shorter):] == shorter


def longest_suffix_prefix(first: list[str], second: list[str]) -> int:
    for length in range(min(len(first), len(second)) - 1, 0, -1):
        if first[-length:] == second[:length]:
            return length
    return 0


def append_after_overlap(first: str, second: str, overlap_tokens: int) -> str:
    tokens = lexical_tokens(second)
    remainder = second[tokens[overlap_tokens - 1][2]:]
    if not remainder.strip():
        return first
    if (
        first
        and remainder
        and not remainder[0].isspace()
        and first[-1].isascii()
        and first[-1].isalnum()
        and remainder[0].isascii()
        and remainder[0].isalnum()
    ):
        remainder = " " + remainder
    return normalize_caption_text(first.rstrip() + remainder)


def sentence_continuation(previous: dict, current: dict) -> str | None:
    gap = current["start_ms"] - previous["end_ms"]
    combined_duration = max(previous["end_ms"], current["end_ms"]) - previous["start_ms"]
    if gap > 750 or combined_duration > MAX_COMPACTED_DURATION_MS:
        return None
    if TERMINAL_PUNCTUATION.search(previous["text"]):
        return None
    if NON_SPEECH_CUE.match(previous["text"]) or NON_SPEECH_CUE.match(current["text"]):
        return None
    if SPEAKER_MARKER.match(current["text"]):
        return None
    merged = normalize_caption_text(f"{previous['text']} {current['text']}")
    return merged if len(merged) <= MAX_COMPACTED_CHARACTERS else None


def cue_merge(
    previous: dict,
    current: dict,
) -> tuple[str, str] | None:
    if current["start_ms"] < previous["start_ms"]:
        return None
    gap = current["start_ms"] - previous["end_ms"]
    overlap = min(previous["end_ms"], current["end_ms"]) - current["start_ms"]
    combined_duration = max(previous["end_ms"], current["end_ms"]) - previous["start_ms"]
    previous_tokens = lexical_tokens(previous["text"])
    current_tokens = lexical_tokens(current["text"])
    previous_values = [token for token, _, _ in previous_tokens]
    current_values = [token for token, _, _ in current_tokens]
    if not previous_values or not current_values:
        return None

    if previous_values == current_values and combined_duration <= MAX_COMPACTED_DURATION_MS:
        lexical_length = max(
            token_text_length(previous_tokens), token_text_length(current_tokens),
        )
        if overlap > 0 or (gap <= 250 and lexical_length >= 8):
            preferred = (
                current["text"]
                if len(current["text"]) >= len(previous["text"])
                else previous["text"]
            )
            if len(preferred) <= MAX_COMPACTED_CHARACTERS:
                return "exact", preferred

    if combined_duration <= MAX_COMPACTED_DURATION_MS and gap <= 250:
        if edge_contains(current_values, previous_values):
            coverage = len(previous_values) / len(current_values)
            enough_text = (
                token_text_length(previous_tokens) >= 6
                or cjk_token_count(previous_tokens) >= 3
            )
            if (
                coverage >= 0.25
                and enough_text
                and len(current["text"]) <= MAX_COMPACTED_CHARACTERS
            ):
                return "rolling-containment", current["text"]
        if edge_contains(previous_values, current_values):
            coverage = len(current_values) / len(previous_values)
            enough_text = (
                token_text_length(current_tokens) >= 6
                or cjk_token_count(current_tokens) >= 3
            )
            if (
                coverage >= 0.25
                and enough_text
                and len(previous["text"]) <= MAX_COMPACTED_CHARACTERS
            ):
                return "rolling-containment", previous["text"]

        overlap_length = longest_suffix_prefix(previous_values, current_values)
        if overlap_length:
            overlap_tokens = current_tokens[:overlap_length]
            overlap_characters = token_text_length(overlap_tokens)
            overlap_cjk = cjk_token_count(overlap_tokens)
            coverage = overlap_length / min(len(previous_values), len(current_values))
            merged = append_after_overlap(
                previous["text"], current["text"], overlap_length,
            )
            if (
                overlap > 0
                and overlap_length >= 2
                and coverage >= 0.4
                and (overlap_characters >= 8 or overlap_cjk >= 4)
                and len(merged) <= MAX_COMPACTED_CHARACTERS
            ):
                return "rolling-overlap", merged
    if merged := sentence_continuation(previous, current):
        return "sentence-continuation", merged
    return None


def compact_transcript_rows(
    rows: list[tuple[int, int, str]],
) -> tuple[list[dict], list[dict], dict]:
    raw_cues = []
    for source_cue, (start, end, text) in enumerate(rows):
        normalized = normalize_caption_text(text)
        if not normalized:
            continue
        start_ms = max(0, int(start))
        end_ms = max(start_ms, int(end))
        raw_cues.append({
            "cue": source_cue,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "start": format_time(start_ms),
            "end": format_time(end_ms),
            "text": normalized,
        })

    compacted: list[dict] = []
    merge_counts = {
        "exact": 0,
        "rolling-containment": 0,
        "rolling-overlap": 0,
        "sentence-continuation": 0,
    }
    for cue in raw_cues:
        current = {
            "start_ms": cue["start_ms"],
            "end_ms": cue["end_ms"],
            "text": cue["text"],
            "source_cue_start": cue["cue"],
            "source_cue_end": cue["cue"],
        }
        decision = cue_merge(compacted[-1], current) if compacted else None
        if decision is None:
            compacted.append(current)
            continue
        reason, merged_text = decision
        previous = compacted[-1]
        previous["end_ms"] = max(previous["end_ms"], current["end_ms"])
        previous["text"] = merged_text
        previous["source_cue_end"] = current["source_cue_end"]
        merge_counts[reason] += 1

    segments = []
    for index, compacted_row in enumerate(compacted):
        segment = {
            "segment": index,
            "start_ms": compacted_row["start_ms"],
            "end_ms": compacted_row["end_ms"],
            "start": format_time(compacted_row["start_ms"]),
            "end": format_time(compacted_row["end_ms"]),
            "text": compacted_row["text"],
        }
        if compacted_row["source_cue_start"] != compacted_row["source_cue_end"]:
            segment["source_cues"] = {
                "start": compacted_row["source_cue_start"],
                "end": compacted_row["source_cue_end"],
            }
        segments.append(segment)

    raw_characters = sum(len(cue["text"]) for cue in raw_cues)
    compacted_characters = sum(len(segment["text"]) for segment in segments)
    raw_jsonl_characters = sum(
        len(json.dumps(cue, ensure_ascii=False)) + 1 for cue in raw_cues
    )
    compacted_jsonl_characters = sum(
        len(json.dumps(segment, ensure_ascii=False)) + 1 for segment in segments
    )
    stats = {
        "raw_cue_count": len(raw_cues),
        "segment_count": len(segments),
        "segments_removed": len(raw_cues) - len(segments),
        "segment_reduction_ratio": (
            round((len(raw_cues) - len(segments)) / len(raw_cues), 4)
            if raw_cues else 0
        ),
        "raw_character_count": raw_characters,
        "character_count": compacted_characters,
        "characters_removed": max(0, raw_characters - compacted_characters),
        "character_reduction_ratio": (
            round(max(0, raw_characters - compacted_characters) / raw_characters, 4)
            if raw_characters else 0
        ),
        "raw_jsonl_character_count": raw_jsonl_characters,
        "jsonl_character_count": compacted_jsonl_characters,
        "jsonl_character_reduction_ratio": (
            round(
                max(0, raw_jsonl_characters - compacted_jsonl_characters)
                / raw_jsonl_characters,
                4,
            )
            if raw_jsonl_characters else 0
        ),
        "merge_counts": merge_counts,
    }
    return raw_cues, segments, stats


def transcript_chunks(segments: list[dict]) -> list[dict]:
    chunks = []
    current: list[dict] = []
    character_count = 0

    def finish() -> None:
        nonlocal current, character_count
        if not current:
            return
        chunks.append({
            "chunk": len(chunks),
            "segment_start": current[0]["segment"],
            "segment_end": current[-1]["segment"],
            "start_ms": current[0]["start_ms"],
            "end_ms": current[-1]["end_ms"],
            "segment_count": len(current),
            "character_count": character_count,
        })
        current = []
        character_count = 0

    for segment in segments:
        next_characters = len(segment["text"])
        if current and (
            len(current) >= TRANSCRIPT_CHUNK_SEGMENTS
            or character_count + next_characters > TRANSCRIPT_CHUNK_CHARACTERS
        ):
            finish()
        current.append(segment)
        character_count += next_characters
    finish()
    return chunks


def write_transcript(path: Path, rows: list[tuple[int, int, str]]) -> Path:
    output = path.with_suffix(".transcript.md")
    jsonl_path, index_path = transcript_bundle_paths(output)
    raw_path = transcript_raw_path(output)
    title = path.name.rsplit(".", 1)[0]
    raw_cues, segments, compaction = compact_transcript_rows(rows)
    lines = [f"# Transcript: {title}", ""]
    lines.extend(
        f"- [{segment['start']}–{segment['end']}] {segment['text']}"
        for segment in segments
    )
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    jsonl_path.write_text(
        "".join(json.dumps(segment, ensure_ascii=False) + "\n" for segment in segments),
        encoding="utf-8",
    )
    raw_path.write_text(
        "".join(json.dumps(cue, ensure_ascii=False) + "\n" for cue in raw_cues),
        encoding="utf-8",
    )
    chunks = transcript_chunks(segments)
    index_path.write_text(json.dumps({
        "version": 1,
        "source": path.name,
        "markdown": output.name,
        "jsonl": jsonl_path.name,
        "raw_jsonl": raw_path.name,
        "segment_count": len(segments),
        "raw_cue_count": len(raw_cues),
        "start_ms": segments[0]["start_ms"] if segments else None,
        "end_ms": segments[-1]["end_ms"] if segments else None,
        "chunks": chunks,
        "compaction": compaction,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


def rows_from_json3(path: Path) -> list[tuple[int, int, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows: list[tuple[int, int, str]] = []
    for event in data.get("events", []):
        segments = event.get("segs") or []
        text = normalize_caption_text("".join(segment.get("utf8", "") for segment in segments))
        if not text:
            continue
        start = int(event.get("tStartMs", 0))
        end = start + int(event.get("dDurationMs", 0))
        rows.append((start, end, text))
    return rows


def convert_json3(path: Path) -> Path:
    return write_transcript(path, rows_from_json3(path))


def rows_from_timed_text(path: Path) -> list[tuple[int, int, str]]:
    content = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    rows: list[tuple[int, int, str]] = []
    for block in re.split(r"\n\s*\n", content):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        time_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if time_index is None:
            continue
        start_raw, end_raw = lines[time_index].split("-->", 1)
        end_raw = end_raw.strip().split()[0]
        try:
            start, end = parse_clock(start_raw), parse_clock(end_raw)
        except ValueError:
            continue
        text = normalize_caption_text(" ".join(lines[time_index + 1 :]))
        if not text:
            continue
        rows.append((start, end, text))
    return rows


def convert_timed_text(path: Path) -> Path:
    return write_transcript(path, rows_from_timed_text(path))


def seconds_value(value: object) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return parse_clock(value) / 1000
    raise ValueError(f"Invalid timestamp value: {value}")


def rows_from_asr_json(path: Path) -> list[tuple[int, int, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    segments = data.get("segments") if isinstance(data, dict) else None
    rows: list[tuple[int, int, str]] = []
    if isinstance(segments, list):
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            text = normalize_caption_text(str(segment.get("text", "")))
            if not text:
                continue
            speaker = segment.get("speaker")
            if speaker is not None:
                text = f"[{speaker}] {text}"
            try:
                start = round(seconds_value(segment.get("start", 0)) * 1000)
                end = round(seconds_value(segment.get("end", segment.get("start", 0))) * 1000)
            except ValueError:
                continue
            rows.append((start, max(start, end), text))
        return rows

    transcription = data.get("transcription") if isinstance(data, dict) else None
    if isinstance(transcription, list):
        for segment in transcription:
            if not isinstance(segment, dict):
                continue
            timestamps = segment.get("timestamps") or segment.get("offsets") or {}
            text = normalize_caption_text(str(segment.get("text", "")))
            if not text or not isinstance(timestamps, dict):
                continue
            try:
                start = round(seconds_value(timestamps.get("from", 0)) * 1000)
                end = round(seconds_value(timestamps.get("to", timestamps.get("from", 0))) * 1000)
            except ValueError:
                continue
            rows.append((start, max(start, end), text))
        return rows

    words = data.get("words") if isinstance(data, dict) else None
    if isinstance(words, list) and words:
        return group_words(words)
    return rows


def convert_subtitle(path: Path) -> Path:
    if path.suffix == ".json3":
        return convert_json3(path)
    if path.suffix in {".srt", ".vtt"}:
        return convert_timed_text(path)
    raise ValueError(f"Unsupported subtitle format: {path.suffix}")


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
    return (
        first_normalized == second_normalized
        or primary_language(first_normalized) == primary_language(second_normalized)
    )


def caption_name(entries: list[dict]) -> str:
    names = [str(entry.get("name", "")).strip() for entry in entries if entry.get("name")]
    return names[0] if names else ""


def caption_is_usable(code: str, entries: list[dict]) -> bool:
    if code in {"danmaku", "live_chat"}:
        return False
    extensions = {str(entry.get("ext", "")).lower() for entry in entries}
    return bool(extensions.intersection(SUPPORTED_SUBTITLE_EXTENSIONS) or not extensions)


def collect_caption_candidates(info: dict) -> list[dict]:
    candidates: list[dict] = []
    for source, field in (("manual", "subtitles"), ("automatic", "automatic_captions")):
        for code, raw_entries in (info.get(field) or {}).items():
            entries = raw_entries if isinstance(raw_entries, list) else []
            if not entries or not caption_is_usable(code, entries):
                continue
            name = caption_name(entries)
            lowered_name = name.lower()
            candidates.append({
                "code": code,
                "language": normalize_language(code),
                "source": source,
                "name": name,
                "original_marker": code.lower().endswith("-orig")
                or any(marker in lowered_name for marker in ORIGINAL_MARKERS),
            })
    return candidates


def detect_original_language(
    info: dict,
    candidates: list[dict],
    override: str | None,
) -> tuple[str | None, str | None, str]:
    if normalized := normalize_language(override):
        return normalized, "command-line override", "high"
    if normalized := normalize_language(info.get("language")):
        return normalized, "media metadata", "high"

    original_candidates = {
        candidate["language"] for candidate in candidates
        if candidate["original_marker"] and candidate["language"]
    }
    if len(original_candidates) == 1:
        return original_candidates.pop(), "caption marked original", "high"

    manual_languages = {
        candidate["language"] for candidate in candidates
        if candidate["source"] == "manual"
        and candidate["language"]
        and not any(marker in candidate["name"].lower() for marker in TRANSLATION_MARKERS)
    }
    if len(manual_languages) == 1:
        return manual_languages.pop(), "single manual caption language", "medium"
    return None, None, "none"


def preferred_language_bonus(code: str, preferred_languages: list[str]) -> int:
    normalized_code = normalize_language(code)
    for index, preferred in enumerate(preferred_languages):
        if normalized_code == normalize_language(preferred):
            return max(3, 15 - index * 3)
    if any(primary_language(code) == primary_language(preferred) for preferred in preferred_languages):
        return 3
    return 0


def score_caption_candidates(
    candidates: list[dict],
    original_language: str | None,
    preferred_languages: list[str],
) -> list[dict]:
    scored: list[dict] = []
    for candidate in candidates:
        native = language_matches(candidate["language"], original_language)
        lowered_name = candidate["name"].lower()
        translated = bool(
            any(marker in lowered_name for marker in TRANSLATION_MARKERS)
            or (original_language and not native and not candidate["original_marker"])
        )
        score = 100 if candidate["source"] == "manual" else 50
        if native:
            score += 60
        if candidate["original_marker"]:
            score += 20
        score += preferred_language_bonus(candidate["language"], preferred_languages)
        if translated:
            score -= 40
        scored.append({
            **candidate,
            "native_language": native,
            "translated": translated,
            "score": score,
        })
    return sorted(
        scored,
        key=lambda candidate: (
            candidate["score"],
            candidate["source"] == "manual",
            candidate["original_marker"],
            candidate["code"],
        ),
        reverse=True,
    )


def write_caption_selection(
    output: Path,
    info: dict,
    candidates: list[dict],
    selected: dict | None,
    original_language: str | None,
    original_language_source: str | None,
    original_language_confidence: str,
    preferred_languages: list[str],
) -> Path:
    report = {
        "title": info.get("title"),
        "original_language": original_language,
        "original_language_source": original_language_source,
        "original_language_confidence": original_language_confidence,
        "preferred_fallback_languages": preferred_languages,
        "selected": selected,
        "candidates": candidates,
        "recommend_asr": bool(
            selected is None
            or selected.get("translated")
            or (
                selected.get("source") == "automatic"
                and not selected.get("native_language")
                and not selected.get("original_marker")
            )
        ),
        "asr_language_hint": (
            original_language if original_language_confidence == "high" else None
        ),
    }
    path = output / "caption-selection.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def find_selected_caption_file(output: Path, language_code: str) -> Path:
    prefix = f"media.{language_code}."
    candidates = [
        path for path in output.iterdir()
        if path.is_file()
        and path.name.startswith(prefix)
        and path.suffix.removeprefix(".") in SUPPORTED_SUBTITLE_EXTENSIONS
    ]
    format_rank = {".json3": 3, ".srt": 2, ".vtt": 1}
    if candidates:
        return max(candidates, key=lambda path: format_rank.get(path.suffix, 0))
    raise SystemExit(f"Unable to identify the downloaded caption track: {language_code}")


def indexed_chapters(media_info: dict) -> list[dict]:
    chapters = []
    for chapter in media_info.get("chapters") or []:
        if not isinstance(chapter, dict):
            continue
        try:
            start_ms = round(float(chapter.get("start_time", 0)) * 1000)
            end_ms = round(float(chapter.get("end_time", chapter.get("start_time", 0))) * 1000)
        except (TypeError, ValueError):
            continue
        chapters.append({
            "title": chapter.get("title"),
            "start_ms": start_ms,
            "end_ms": end_ms,
        })
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
        round(float(duration) * 1000)
        if isinstance(duration, (int, float))
        else None
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


def publish_active_transcript(
    output: Path,
    transcript: Path,
    *,
    source: str,
    language: str | None,
    original_language: str | None,
    media_info: dict,
    details: dict | None = None,
) -> tuple[Path, dict]:
    source_lines = transcript.read_text(encoding="utf-8").splitlines()
    if source_lines and source_lines[0].startswith("# "):
        source_lines = source_lines[2:]
    active = output / ACTIVE_TRANSCRIPT_MARKDOWN
    header = [
        "# Active transcript",
        "",
        f"- Source: `{source}`",
        f"- Language: `{language or 'unknown'}`",
        f"- Original language: `{original_language or 'unknown'}`",
        "",
    ]
    active.write_text(
        "\n".join([*header, *source_lines]).rstrip() + "\n",
        encoding="utf-8",
    )
    source_jsonl, source_index = transcript_bundle_paths(transcript)
    active_jsonl, active_index = transcript_bundle_paths(active)
    shutil.copyfile(source_jsonl, active_jsonl)
    source_raw = transcript_raw_path(transcript)
    active_raw = transcript_raw_path(active)
    if source_raw.is_file():
        shutil.copyfile(source_raw, active_raw)
    index = load_json_object(source_index)
    index.update({
        "version": 1,
        "source": transcript.name,
        "markdown": active.name,
        "jsonl": active_jsonl.name,
        "raw_jsonl": active_raw.name if active_raw.is_file() else None,
        "transcript_source": source,
        "language": language,
        "original_language": original_language,
        "chapters": indexed_chapters(media_info),
    })
    write_json_atomic(active_index, index)
    duration = media_info.get("duration")
    duration_ms = (
        round(float(duration) * 1000)
        if isinstance(duration, (int, float))
        else None
    )
    end_ms = index.get("end_ms")
    coverage = (
        round(min(1.0, max(0.0, float(end_ms) / duration_ms)), 4)
        if isinstance(end_ms, (int, float)) and duration_ms
        else None
    )
    manifest = {
        "status": "ready",
        "source": source,
        "language": language,
        "original_language": original_language,
        "segment_count": index.get("segment_count"),
        "raw_cue_count": index.get("raw_cue_count"),
        "start_ms": index.get("start_ms"),
        "end_ms": end_ms,
        "coverage": coverage,
        "paths": {
            "markdown": active.name,
            "jsonl": active_jsonl.name,
            "index": active_index.name,
            "raw_jsonl": active_raw.name if active_raw.is_file() else None,
        },
        "compaction": index.get("compaction"),
    }
    if details:
        manifest["details"] = details
    return active, manifest


def acquire_captions(args: argparse.Namespace) -> None:
    require_program("yt-dlp")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    metadata_command = [
        "yt-dlp",
        "--no-playlist",
        "--skip-download",
        "--dump-single-json",
        *cookie_arguments(args),
        args.url,
    ]
    info = json.loads(run(metadata_command))
    (output / "media.info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    update_acquisition(
        output,
        requested_url=args.url,
        info=info,
        transcript={"status": "selecting"},
        next_action="select_transcript",
    )
    preferred_languages = [
        language.strip() for language in args.preferred_languages.split(",") if language.strip()
    ]
    raw_candidates = collect_caption_candidates(info)
    original_language, original_source, original_confidence = detect_original_language(
        info, raw_candidates, args.original_language,
    )
    candidates = score_caption_candidates(
        raw_candidates, original_language, preferred_languages,
    )
    selected = candidates[0] if candidates else None
    report_path = write_caption_selection(
        output,
        info,
        candidates,
        selected,
        original_language,
        original_source,
        original_confidence,
        preferred_languages,
    )
    if selected is None:
        acquisition_path = update_acquisition(
            output,
            requested_url=args.url,
            info=info,
            transcript={
                "status": "needs_asr",
                "source": None,
                "language": None,
                "original_language": original_language,
                "asr_language_hint": (
                    original_language if original_confidence == "high" else None
                ),
                "details": {"caption_selection": report_path.name},
            },
            next_action="download_audio",
        )
        print(acquisition_path)
        return

    caption_flag = "--write-subs" if selected["source"] == "manual" else "--write-auto-subs"
    download_command = [
        "yt-dlp",
        "--no-playlist",
        "--skip-download",
        caption_flag,
        "--sub-format",
        "json3/srt/vtt/best",
        "--sub-langs",
        selected["code"],
        "--output",
        str(output / "media.%(ext)s"),
        *cookie_arguments(args),
        args.url,
    ]
    run(download_command)
    caption_file = find_selected_caption_file(output, selected["code"])
    transcript = convert_subtitle(caption_file)
    _, transcript_manifest = publish_active_transcript(
        output,
        transcript,
        source=f"{selected['source']}-caption",
        language=selected["code"],
        original_language=original_language,
        media_info=info,
        details={
            "caption_selection": report_path.name,
            "selection_score": selected["score"],
        },
    )
    acquisition_path = update_acquisition(
        output,
        requested_url=args.url,
        info=info,
        transcript=transcript_manifest,
        next_action="summarize",
    )
    print(acquisition_path)


def find_downloaded_media(output: Path, command_output: str, stem: str) -> Path:
    for line in reversed(command_output.splitlines()):
        candidate = Path(line.strip())
        if candidate.is_file():
            return candidate
    excluded = {".json", ".json3", ".part", ".ytdl", ".jpg", ".webp", ".md"}
    candidates = [
        path for path in output.glob(f"{stem}.*")
        if path.is_file() and path.suffix.lower() not in excluded
    ]
    if len(candidates) == 1:
        return candidates[0]
    raise SystemExit(f"Unable to identify downloaded {stem} file.")


def audio_format_candidates(info: dict) -> list[dict]:
    candidates = []
    for candidate in info.get("formats") or []:
        if not isinstance(candidate, dict) or candidate.get("has_drm"):
            continue
        acodec = str(candidate.get("acodec") or "none").lower()
        vcodec = str(candidate.get("vcodec") or "none").lower()
        url = str(candidate.get("url") or "")
        if acodec == "none" or vcodec != "none" or not url.startswith(("http://", "https://")):
            continue
        candidates.append(candidate)
    protocol_rank = {
        "https": 5,
        "http": 5,
        "m3u8_native": 4,
        "m3u8": 4,
        "http_dash_segments": 3,
        "dash": 3,
    }
    return sorted(
        candidates,
        key=lambda candidate: (
            float(candidate.get("abr") or candidate.get("tbr") or 0),
            protocol_rank.get(protocol_name(candidate), 1),
            float(candidate.get("quality") or 0),
        ),
        reverse=True,
    )


def audio_candidate_summary(candidate: dict, duration: object) -> dict:
    return {
        "format_id": candidate.get("format_id"),
        "protocol": protocol_name(candidate),
        "ext": candidate.get("ext"),
        "acodec": candidate.get("acodec"),
        "abr": candidate.get("abr"),
        "estimated_bytes": format_size_estimate(candidate, duration),
    }


def serialized_http_headers(headers: dict[str, str]) -> str:
    return "".join(f"{key}: {value}\r\n" for key, value in headers.items())


def ffmpeg_audio_command(
    info: dict,
    candidate: dict,
    target: Path,
    requested_format: str,
    timeout: float,
) -> dict:
    command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
    headers = merged_http_headers(info, candidate)
    if headers:
        command.extend(["-headers", serialized_http_headers(headers)])
    command.extend(["-i", str(candidate["url"]), "-vn"])
    acodec = str(candidate.get("acodec") or "").lower()
    if requested_format == "m4a":
        if any(marker in acodec for marker in ("aac", "mp4a")):
            command.extend(["-c:a", "copy"])
        else:
            command.extend(["-c:a", "aac", "-b:a", "128k"])
    elif requested_format == "wav":
        command.extend(["-c:a", "pcm_s16le", "-ar", "16000", "-ac", "1"])
    elif requested_format == "mp3":
        command.extend(["-c:a", "libmp3lame", "-b:a", "128k"])
    elif requested_format == "opus":
        command.extend(["-c:a", "libopus", "-b:a", "96k"])
    command.append(str(target))
    return run_bounded(command, timeout)


def yt_dlp_audio_command(
    args: argparse.Namespace,
    temp_dir: Path,
    candidate: dict | None,
) -> dict:
    selector = str(candidate.get("format_id")) if candidate and candidate.get("format_id") else "bestaudio/best"
    command = [
        "yt-dlp",
        "--no-playlist",
        "--force-overwrites",
        "--format",
        selector,
        "--extract-audio",
        "--audio-format",
        args.format,
        "--output",
        str(temp_dir / "media.%(ext)s"),
        "--print",
        "after_move:filepath",
        *cookie_arguments(args),
        args.url,
    ]
    return run_bounded(command, args.timeout)


def require_audio_output(outcome: dict, target: Path | None) -> dict:
    if outcome["returncode"] != 0:
        return outcome
    if target is not None and target.is_file() and target.stat().st_size > 0:
        return outcome
    failed = dict(outcome)
    failed["returncode"] = 1
    failed["stderr"] = "Audio command completed without a non-empty output file."
    return failed


def acquire_audio(args: argparse.Namespace) -> None:
    require_program("yt-dlp")
    require_program("ffmpeg")
    if args.timeout <= 0:
        raise SystemExit("Audio process timeout must be positive.")
    if not re.fullmatch(r"[a-zA-Z0-9]+", args.format):
        raise SystemExit("Audio format must contain only letters and digits.")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    audio_dir = output / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    result_path = audio_dir / "result.json"
    temp_dir = output / f".audio-{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    report = {
        "version": 1,
        "url": args.url,
        "status": "failed",
        "requested_format": args.format,
        "metadata_refreshed": False,
        "attempts": [],
    }
    info = load_json_object(output / "media.info.json")
    if info:
        report["metadata_source"] = "cache"
    else:
        info, metadata_outcome = refresh_media_info(args, output)
        report["attempts"].append(process_attempt("metadata", metadata_outcome))
        report["metadata_source"] = "fresh" if info else "unavailable"
    candidate: dict | None = None
    source: Path | None = None
    strategy: str | None = None
    try:
        if info and platform_label(info) == "bilibili":
            candidates = audio_format_candidates(info)
            candidate = candidates[0] if candidates else None
            if candidate:
                direct_target = temp_dir / f"media.{args.format}"
                direct = ffmpeg_audio_command(
                    info, candidate, direct_target, args.format, args.timeout,
                )
                direct = require_audio_output(direct, direct_target)
                summary = audio_candidate_summary(candidate, info.get("duration"))
                report["attempts"].append(process_attempt("direct_url", direct, summary))
                if direct["returncode"] == 0:
                    source, strategy = direct_target, "direct_url"
                elif failure_code(direct) == "url-or-auth":
                    refreshed_info, metadata_outcome = refresh_media_info(args, output)
                    report["metadata_refreshed"] = True
                    report["attempts"].append(process_attempt("metadata_refresh", metadata_outcome))
                    if refreshed_info:
                        info = refreshed_info
                        candidates = audio_format_candidates(info)
                        candidate = candidates[0] if candidates else None
                        if candidate:
                            refreshed_target = temp_dir / f"media-refreshed.{args.format}"
                            retried = ffmpeg_audio_command(
                                info, candidate, refreshed_target, args.format, args.timeout,
                            )
                            retried = require_audio_output(retried, refreshed_target)
                            summary = audio_candidate_summary(candidate, info.get("duration"))
                            report["attempts"].append(
                                process_attempt("direct_url_refreshed", retried, summary)
                            )
                            if retried["returncode"] == 0:
                                source, strategy = refreshed_target, "direct_url_refreshed"

        if source is None:
            fallback = yt_dlp_audio_command(args, temp_dir, candidate)
            fallback_source: Path | None = None
            if fallback["returncode"] == 0:
                try:
                    fallback_source = find_downloaded_media(
                        temp_dir, fallback["stdout"], "media",
                    )
                except SystemExit as error:
                    fallback = dict(fallback)
                    fallback["returncode"] = 1
                    fallback["stderr"] = str(error)
                fallback = require_audio_output(fallback, fallback_source)
            summary = (
                audio_candidate_summary(candidate, info.get("duration"))
                if candidate and info else None
            )
            report["attempts"].append(process_attempt("yt_dlp", fallback, summary))
            if fallback["returncode"] == 0:
                source = fallback_source
                strategy = "yt_dlp"

        if source is None or strategy is None:
            report.update({
                "status": "failed",
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "error_code": (
                    report["attempts"][-1].get("error_code")
                    if report["attempts"] else "media-unavailable"
                ),
            })
            write_json_atomic(result_path, report)
            update_acquisition(
                output,
                requested_url=args.url,
                info=info or None,
                audio={
                    "status": "failed",
                    "result": str(result_path.relative_to(output)),
                    "error_code": report.get("error_code"),
                },
                next_action="resolve_audio_access",
            )
            raise SystemExit(f"Audio acquisition failed. Read {result_path}.")

        target = output / f"media.{args.format}"
        os.replace(source, target)
        report.update({
            "status": "success",
            "strategy": strategy,
            "path": str(target.relative_to(output)),
            "bytes": target.stat().st_size,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
        })
        write_json_atomic(result_path, report)
        transcript_status = load_json_object(output / ACQUISITION_MANIFEST).get("transcript", {})
        acquisition_path = update_acquisition(
            output,
            requested_url=args.url,
            info=info or None,
            audio={
                "status": "ready",
                "path": str(target.relative_to(output)),
                "format": args.format,
                "bytes": target.stat().st_size,
                "strategy": strategy,
                "result": str(result_path.relative_to(output)),
            },
            next_action=(
                "summarize" if transcript_status.get("status") == "ready" else "transcribe"
            ),
        )
        print(acquisition_path)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def timestamp_label(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}-{minutes:02d}-{whole_seconds:02d}-{millis:03d}"


def terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            pass


def run_bounded(command: list[str], timeout: float) -> dict:
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process(process)
        stdout, stderr = process.communicate()
    except KeyboardInterrupt:
        terminate_process(process)
        process.communicate()
        raise
    return {
        "returncode": process.returncode,
        "stdout": stdout or "",
        "stderr": stderr or "",
        "timed_out": timed_out,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
    }


def redact_diagnostic(value: str, limit: int = 1600) -> str:
    value = re.sub(r"https?://\S+", "<redacted-url>", value)
    value = re.sub(
        r"(?im)^(authorization|cookie|set-cookie):.*$",
        r"\1: <redacted>",
        value,
    )
    return value.strip()[-limit:]


def failure_code(outcome: dict) -> str:
    if outcome.get("timed_out"):
        return "timeout"
    diagnostic = outcome.get("stderr", "").lower()
    if any(marker in diagnostic for marker in (
        "401", "403", "412", "forbidden", "unauthorized", "expired", "precondition failed",
    )):
        return "url-or-auth"
    if any(marker in diagnostic for marker in ("connection", "timed out", "network is unreachable")):
        return "network"
    return "process-failed"


def refresh_media_info(args: argparse.Namespace, output: Path) -> tuple[dict | None, dict]:
    command = [
        "yt-dlp", "--no-playlist", "--skip-download", "--dump-single-json",
        *cookie_arguments(args), args.url,
    ]
    outcome = run_bounded(command, args.timeout)
    if outcome["returncode"] != 0:
        return None, outcome
    try:
        info = json.loads(outcome["stdout"])
    except json.JSONDecodeError as error:
        outcome["stderr"] = f"Invalid yt-dlp metadata JSON: {error}"
        outcome["returncode"] = 1
        return None, outcome
    (output / "media.info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return info, outcome


def protocol_name(candidate: dict) -> str:
    protocol = str(candidate.get("protocol") or "").lower()
    if protocol:
        return protocol
    url = str(candidate.get("url") or "")
    return url.split(":", 1)[0].lower() if ":" in url else "unknown"


def format_size_estimate(candidate: dict, duration: object) -> int | None:
    for field in ("filesize", "filesize_approx"):
        value = candidate.get(field)
        if isinstance(value, (int, float)) and value > 0:
            return round(value)
    tbr = candidate.get("tbr")
    if isinstance(tbr, (int, float)) and isinstance(duration, (int, float)):
        return round(tbr * 1000 / 8 * duration)
    return None


def frame_format_candidates(info: dict, height: int) -> list[dict]:
    candidates = []
    for candidate in info.get("formats") or []:
        if not isinstance(candidate, dict) or candidate.get("has_drm"):
            continue
        if str(candidate.get("vcodec") or "none").lower() in {"none", "images"}:
            continue
        url = str(candidate.get("url") or "")
        if not url.startswith(("http://", "https://")):
            continue
        note = f"{candidate.get('format_note', '')} {candidate.get('format_id', '')}".lower()
        if "storyboard" in note:
            continue
        candidates.append(candidate)
    within = [
        candidate for candidate in candidates
        if isinstance(candidate.get("height"), (int, float)) and candidate["height"] <= height
    ]
    protocol_rank = {
        "https": 5,
        "http": 5,
        "m3u8_native": 4,
        "m3u8": 4,
        "http_dash_segments": 3,
        "dash": 3,
    }
    if within:
        return sorted(
            within,
            key=lambda candidate: (
                float(candidate.get("height") or 0),
                protocol_rank.get(protocol_name(candidate), 1),
                float(candidate.get("tbr") or 0),
            ),
            reverse=True,
        )
    return sorted(
        candidates,
        key=lambda candidate: (
            float(candidate.get("height") or 1_000_000),
            -protocol_rank.get(protocol_name(candidate), 1),
        ),
    )


def candidate_summary(candidate: dict, duration: object) -> dict:
    return {
        "format_id": candidate.get("format_id"),
        "protocol": protocol_name(candidate),
        "ext": candidate.get("ext"),
        "width": candidate.get("width"),
        "height": candidate.get("height"),
        "estimated_bytes": format_size_estimate(candidate, duration),
    }


def merged_http_headers(info: dict, candidate: dict) -> dict[str, str]:
    headers: dict[str, str] = {}
    for source in (info.get("http_headers"), candidate.get("http_headers")):
        if isinstance(source, dict):
            headers.update({str(key): str(value) for key, value in source.items()})
    return headers


def load_frame_requests(path: Path, args: argparse.Namespace) -> list[dict]:
    if not path.is_file():
        raise SystemExit(f"Frame request manifest is unavailable: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 2:
        raise SystemExit("frame-requests.json must use version 2.")
    raw_requests = data.get("requests")
    if not isinstance(raw_requests, list) or not raw_requests:
        raise SystemExit("frame-requests.json must contain at least one request.")
    requests = []
    seen: set[str] = set()
    purposes = {"evidence", "explanation", "example", "comparison"}
    for index, raw in enumerate(raw_requests):
        if not isinstance(raw, dict):
            raise SystemExit(f"Frame request {index} must be an object.")
        request_id = str(raw.get("id") or "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", request_id):
            raise SystemExit(f"Invalid frame request id: {request_id!r}")
        if request_id in seen:
            raise SystemExit(f"Duplicate frame request id: {request_id}")
        seen.add(request_id)
        purpose = str(raw.get("purpose") or "").strip()
        if purpose not in purposes:
            raise SystemExit(
                f"Frame request {request_id} needs purpose: "
                "evidence, explanation, example, or comparison."
            )
        timestamp_ms = raw.get("timestamp_ms")
        if timestamp_ms is None and raw.get("timestamp") is not None:
            timestamp_ms = parse_clock(str(raw["timestamp"]))
        if not isinstance(timestamp_ms, (int, float)) or timestamp_ms < 0:
            raise SystemExit(f"Frame request {request_id} needs a valid timestamp_ms.")
        height = raw.get("height", args.height)
        if not isinstance(height, int) or not 1 <= height <= 4320:
            raise SystemExit(f"Frame request {request_id} has an invalid height.")
        image_format = raw.get("image_format", args.image_format)
        if image_format not in {"jpg", "png"}:
            raise SystemExit(f"Frame request {request_id} has an invalid image_format.")
        expected = str(raw.get("expected_observation") or "").strip()
        claim_id = str(raw.get("claim_id") or "").strip()
        if not expected or not claim_id:
            raise SystemExit(f"Frame request {request_id} needs claim_id and expected_observation.")
        requests.append({
            "id": request_id,
            "claim_id": claim_id,
            "timestamp_ms": round(timestamp_ms),
            "height": height,
            "image_format": image_format,
            "crop": str(raw.get("crop") or "").strip() or None,
            "expected_observation": expected,
            "purpose": purpose,
            "user_requested": bool(raw.get("user_requested")),
        })
    return requests


def ffmpeg_frame_command(
    source: str,
    seconds: float,
    target: Path,
    request: dict,
    timeout: float,
    headers: dict[str, str] | None = None,
) -> dict:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if source.startswith(("http://", "https://")):
        command.extend(["-rw_timeout", str(round(timeout * 1_000_000))])
        if headers:
            rendered_headers = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
            command.extend(["-headers", rendered_headers])
    command.extend(["-ss", f"{seconds:.3f}", "-i", source, "-frames:v", "1", "-an"])
    filters = []
    if request.get("crop"):
        filters.append(f"crop={request['crop']}")
    filters.append(f"scale=-2:min({request['height']}\\,ih)")
    command.extend(["-vf", ",".join(filters)])
    if request["image_format"] == "jpg":
        command.extend(["-q:v", "2"])
    else:
        command.extend(["-compression_level", "3"])
    command.extend(["-y", str(target)])
    return run_bounded(command, timeout)


def image_dimensions(path: Path) -> tuple[int | None, int | None]:
    if shutil.which("ffprobe") is None:
        return None, None
    outcome = run_bounded([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "json", str(path),
    ], 10)
    if outcome["returncode"] != 0:
        return None, None
    try:
        stream = json.loads(outcome["stdout"])["streams"][0]
        return int(stream["width"]), int(stream["height"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None, None


def finalize_frame(temp: Path, target: Path, request: dict, result: dict) -> None:
    if not temp.is_file() or temp.stat().st_size == 0:
        raise RuntimeError("FFmpeg completed without a frame file.")
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temp, target)
    width, height = image_dimensions(target)
    result.update({
        "status": "success",
        "output": str(target),
        "width": width,
        "height": height,
        "bytes": target.stat().st_size,
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "expected_observation": request["expected_observation"],
    })


def process_attempt(strategy: str, outcome: dict, candidate: dict | None = None) -> dict:
    attempt = {
        "strategy": strategy,
        "status": "success" if outcome["returncode"] == 0 else "failed",
        "elapsed_ms": outcome["elapsed_ms"],
    }
    if candidate:
        attempt.update(candidate)
    if outcome["returncode"] != 0:
        attempt.update({
            "error_code": failure_code(outcome),
            "diagnostic": redact_diagnostic(outcome.get("stderr", "")),
        })
    return attempt


def try_remote_frame(
    request: dict,
    result: dict,
    info: dict,
    temp_dir: Path,
    frames_dir: Path,
    timeout: float,
) -> tuple[bool, bool]:
    candidates = frame_format_candidates(info, request["height"])[:2]
    saw_expired = False
    for candidate in candidates:
        temp = temp_dir / f"{request['id']}-{uuid.uuid4().hex}.{request['image_format']}"
        outcome = ffmpeg_frame_command(
            str(candidate["url"]), request["timestamp_ms"] / 1000, temp,
            request, timeout, merged_http_headers(info, candidate),
        )
        summary = candidate_summary(candidate, info.get("duration"))
        result["attempts"].append(process_attempt("remote_seek", outcome, summary))
        if outcome["returncode"] == 0:
            target = frames_dir / (
                f"{request['id']}-{timestamp_label(request['timestamp_ms'] / 1000)}."
                f"{request['image_format']}"
            )
            finalize_frame(temp, target, request, result)
            return True, False
        saw_expired = saw_expired or failure_code(outcome) == "url-or-auth"
    return False, saw_expired


def try_partial_section(
    args: argparse.Namespace,
    request: dict,
    result: dict,
    temp_dir: Path,
    frames_dir: Path,
) -> bool:
    seconds = request["timestamp_ms"] / 1000
    section_start = max(0.0, seconds - 2.0)
    section_end = seconds + 2.0
    stem = f"section-{request['id']}"
    command = [
        "yt-dlp", "--no-playlist", "--force-overwrites",
        "--format", f"bestvideo*[height<={request['height']}]/best[height<={request['height']}]",
        "--download-sections", f"*{section_start:.3f}-{section_end:.3f}",
        "--output", str(temp_dir / f"{stem}.%(ext)s"),
        "--print", "after_move:filepath",
        *cookie_arguments(args), args.url,
    ]
    download = run_bounded(command, max(args.timeout * 2, 90))
    result["attempts"].append(process_attempt("partial_section_download", download))
    if download["returncode"] != 0:
        return False
    try:
        section = find_downloaded_media(temp_dir, download["stdout"], stem)
    except SystemExit as error:
        result["attempts"].append({
            "strategy": "partial_section_extract",
            "status": "failed",
            "elapsed_ms": 0,
            "error_code": "missing-section",
            "diagnostic": str(error),
        })
        return False
    temp = temp_dir / f"{request['id']}-partial.{request['image_format']}"
    extract = ffmpeg_frame_command(
        str(section), seconds - section_start, temp, request, args.timeout,
    )
    result["attempts"].append(process_attempt("partial_section_extract", extract))
    if extract["returncode"] != 0:
        return False
    target = frames_dir / (
        f"{request['id']}-{timestamp_label(seconds)}.{request['image_format']}"
    )
    finalize_frame(temp, target, request, result)
    return True


def full_download_fallback(
    args: argparse.Namespace,
    pending: list[tuple[dict, dict]],
    info: dict,
    temp_dir: Path,
    frames_dir: Path,
) -> dict:
    report = {"attempted": False, "status": "skipped"}
    if not pending or args.max_full_download_mib <= 0:
        return report
    max_height = max(request["height"] for request, _ in pending)
    candidates = frame_format_candidates(info, max_height)
    if not candidates:
        report.update({"status": "blocked", "reason": "no-video-format"})
        return report
    candidate = candidates[0]
    summary = candidate_summary(candidate, info.get("duration"))
    estimate = summary["estimated_bytes"]
    budget = round(args.max_full_download_mib * 1024 * 1024)
    report.update({"format": summary, "budget_bytes": budget})
    if estimate is None:
        report.update({"status": "blocked", "reason": "size-unknown"})
        return report
    if estimate > budget:
        report.update({"status": "blocked", "reason": "over-budget"})
        return report
    report["attempted"] = True
    stem = "fallback-source"
    command = [
        "yt-dlp", "--no-playlist", "--force-overwrites",
        "--format", str(candidate.get("format_id") or (
            f"bestvideo*[height<={max_height}]/best[height<={max_height}]"
        )),
        "--max-filesize", str(budget),
        "--output", str(temp_dir / f"{stem}.%(ext)s"),
        "--print", "after_move:filepath",
        *cookie_arguments(args), args.url,
    ]
    download = run_bounded(command, max(args.timeout * 4, 180))
    report.update({
        "elapsed_ms": download["elapsed_ms"],
        "status": "downloaded" if download["returncode"] == 0 else "failed",
    })
    if download["returncode"] != 0:
        report.update({
            "error_code": failure_code(download),
            "diagnostic": redact_diagnostic(download.get("stderr", "")),
        })
        return report
    try:
        source = find_downloaded_media(temp_dir, download["stdout"], stem)
    except SystemExit as error:
        report.update({"status": "failed", "reason": "missing-download", "diagnostic": str(error)})
        return report
    report["actual_bytes"] = source.stat().st_size
    for request, result in pending:
        temp = temp_dir / f"{request['id']}-full.{request['image_format']}"
        extract = ffmpeg_frame_command(
            str(source), request["timestamp_ms"] / 1000, temp, request, args.timeout,
        )
        result["attempts"].append(process_attempt("full_download_extract", extract, summary))
        if extract["returncode"] == 0:
            target = frames_dir / (
                f"{request['id']}-{timestamp_label(request['timestamp_ms'] / 1000)}."
                f"{request['image_format']}"
            )
            finalize_frame(temp, target, request, result)
    return report


def write_json_atomic(path: Path, value: object) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def acquire_frames(args: argparse.Namespace) -> None:
    require_program("yt-dlp")
    require_program("ffmpeg")
    if not 1 <= args.height <= 4320:
        raise SystemExit("Default frame height must be between 1 and 4320.")
    if args.timeout <= 0:
        raise SystemExit("Frame process timeout must be positive.")
    if args.max_full_download_mib < 0:
        raise SystemExit("Full-download fallback budget must be zero or positive.")
    output = args.output.resolve()
    frames_dir = output / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.manifest.resolve()
    result_path = frames_dir / "result.json"
    observations_path = frames_dir / "observations.json"
    started = time.monotonic()
    report = {
        "version": 1,
        "video_url": args.url,
        "manifest": str(manifest_path),
        "status": "running",
        "metadata": [],
        "metadata_refresh_attempted": False,
        "metadata_refreshed": False,
        "full_download": {"attempted": False, "status": "pending"},
        "results": [],
        "observation_file": str(observations_path),
    }
    temp_dir = frames_dir / f".tmp-{uuid.uuid4().hex}"
    temp_dir.mkdir(parents=True, exist_ok=False)
    fatal: str | None = None
    try:
        requests = load_frame_requests(manifest_path, args)
        for request in requests:
            report["results"].append({
                "id": request["id"],
                "claim_id": request["claim_id"],
                "purpose": request["purpose"],
                "timestamp_ms": request["timestamp_ms"],
                "requested_height": request["height"],
                "image_format": request["image_format"],
                "status": "pending",
                "attempts": [],
            })
        info_path = output / "media.info.json"
        info = None
        if info_path.is_file():
            try:
                loaded = json.loads(info_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and loaded.get("formats"):
                    info = loaded
                    report["metadata"].append({"source": "cached", "status": "success"})
            except json.JSONDecodeError:
                pass
        if info is None:
            info, outcome = refresh_media_info(args, output)
            report["metadata"].append(process_attempt("metadata_fetch", outcome))
        if info is None:
            raise RuntimeError("Unable to acquire media metadata for frame extraction.")

        pending: list[tuple[dict, dict]] = []
        refreshed = False
        for request, result in zip(requests, report["results"]):
            success, expired = try_remote_frame(
                request, result, info, temp_dir, frames_dir, args.timeout,
            )
            if expired and not refreshed:
                refreshed_info, outcome = refresh_media_info(args, output)
                report["metadata"].append(process_attempt("metadata_refresh", outcome))
                refreshed = True
                report["metadata_refresh_attempted"] = True
                if refreshed_info is not None:
                    info = refreshed_info
                    report["metadata_refreshed"] = True
                    success, _ = try_remote_frame(
                        request, result, info, temp_dir, frames_dir, args.timeout,
                    )
            if success:
                print(result["output"])
                continue
            if try_partial_section(args, request, result, temp_dir, frames_dir):
                print(result["output"])
                continue
            pending.append((request, result))

        report["full_download"] = full_download_fallback(
            args, pending, info, temp_dir, frames_dir,
        )
        for request, result in pending:
            if result["status"] == "success":
                print(result["output"])
                continue
            result.update({
                "status": "failed",
                "failure_reason": report["full_download"].get("reason")
                or report["full_download"].get("error_code")
                or "all-frame-strategies-failed",
            })
        success_count = sum(result["status"] == "success" for result in report["results"])
        report["status"] = (
            "success" if success_count == len(report["results"])
            else "partial" if success_count
            else "failed"
        )
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        fatal = "Frame extraction was interrupted."
    except SystemExit as error:
        report["status"] = "failed"
        report["fatal_error"] = str(error)
        fatal = str(error)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        report["status"] = "failed"
        report["fatal_error"] = str(error)
        fatal = str(error)
    finally:
        report["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        write_json_atomic(result_path, report)
        shutil.rmtree(temp_dir, ignore_errors=True)
    if not observations_path.exists():
        write_json_atomic(observations_path, {"version": 1, "observations": []})
    print(result_path)
    if fatal or report["status"] != "success":
        raise SystemExit(fatal or "Some requested frames could not be extracted; inspect result.json.")


def record_frame_observation(args: argparse.Namespace) -> None:
    result_path = args.result.resolve()
    if not result_path.is_file():
        raise SystemExit(f"Frame result is unavailable: {result_path}")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    frame = next(
        (item for item in result.get("results", []) if item.get("id") == args.frame_id),
        None,
    )
    if frame is None or frame.get("status") != "success":
        raise SystemExit(f"Successful frame id is unavailable: {args.frame_id}")
    observations_path = result_path.parent / "observations.json"
    if observations_path.is_file():
        observations = json.loads(observations_path.read_text(encoding="utf-8"))
    else:
        observations = {"version": 1, "observations": []}
    entry = {
        "frame_id": args.frame_id,
        "claim_id": frame.get("claim_id"),
        "purpose": frame.get("purpose"),
        "output": frame.get("output"),
        "readability": args.readability,
        "visible_facts": args.visible_fact,
        "supports_claim": args.supports_claim,
        "note": args.note,
    }
    items = [
        item for item in observations.get("observations", [])
        if item.get("frame_id") != args.frame_id
    ]
    items.append(entry)
    observations["observations"] = items
    write_json_atomic(observations_path, observations)
    print(observations_path)


def multipart_parts(boundary: str, fields: list[tuple[str, str]], audio: Path) -> tuple[bytes, bytes]:
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n".encode()
        )
    filename = audio.name.replace('"', "_")
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    chunks.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{filename}\"\r\nContent-Type: {media_type}\r\n\r\n".encode()
    )
    return b"".join(chunks), f"\r\n--{boundary}--\r\n".encode()


def call_xai_stt(audio: Path, fields: list[tuple[str, str]], api_key: str) -> dict:
    boundary = f"codex-{uuid.uuid4().hex}"
    prefix, suffix = multipart_parts(boundary, fields, audio)
    connection = http.client.HTTPSConnection("api.x.ai", timeout=600)
    connection.putrequest("POST", "/v1/stt")
    connection.putheader("Authorization", f"Bearer {api_key}")
    connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
    connection.putheader("Content-Length", str(len(prefix) + audio.stat().st_size + len(suffix)))
    connection.endheaders()
    connection.send(prefix)
    with audio.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            connection.send(chunk)
    connection.send(suffix)
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    if not 200 <= response.status < 300:
        message = payload.decode("utf-8", errors="replace")[:2000]
        raise SystemExit(f"xAI STT failed with HTTP {response.status}: {message}")
    return json.loads(payload)


def group_words(words: list[dict]) -> list[tuple[int, int, str]]:
    rows: list[tuple[int, int, str]] = []
    current: list[dict] = []
    for word in words:
        if current:
            gap = float(word.get("start", 0)) - float(current[-1].get("end", 0))
            duration = float(current[-1].get("end", 0)) - float(current[0].get("start", 0))
            speaker_changed = word.get("speaker") != current[-1].get("speaker")
            if gap > 1.2 or duration >= 30 or speaker_changed:
                rows.append(words_to_row(current))
                current = []
        current.append(word)
    if current:
        rows.append(words_to_row(current))
    return rows


def words_to_row(words: list[dict]) -> tuple[int, int, str]:
    text = normalize_caption_text(" ".join(str(word.get("text", "")) for word in words))
    speaker = words[0].get("speaker")
    if speaker is not None:
        text = f"[Speaker {speaker}] {text}"
    start = round(float(words[0].get("start", 0)) * 1000)
    end = round(float(words[-1].get("end", 0)) * 1000)
    return start, end, text


def infer_asr_language(output: Path) -> tuple[str | None, str]:
    acquisition = load_json_object(output / ACQUISITION_MANIFEST)
    transcript = acquisition.get("transcript")
    if isinstance(transcript, dict):
        if language := normalize_language(transcript.get("asr_language_hint")):
            return language, "acquisition manifest"
    report_path = output / "caption-selection.json"
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if language := normalize_language(report.get("asr_language_hint")):
            return language, "caption selection"
    info_path = output / "media.info.json"
    if info_path.is_file():
        info = json.loads(info_path.read_text(encoding="utf-8"))
        if language := normalize_language(info.get("language")):
            return language, "media metadata"
    return None, "automatic detection"


def transcribe_xai(args: argparse.Namespace) -> None:
    if not args.confirm_paid_api:
        raise SystemExit("Pass --confirm-paid-api after the user approves xAI API usage.")
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        raise SystemExit("XAI_API_KEY is unavailable.")
    audio = args.audio.resolve()
    if not audio.is_file():
        raise SystemExit(f"Audio file is unavailable: {audio}")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    fields: list[tuple[str, str]] = []
    if args.language and args.language.lower() != "auto":
        language, language_source = normalize_language(args.language), "command-line override"
    else:
        language, language_source = infer_asr_language(output)
    if language:
        fields.extend([("language", language), ("format", "true")])
    if args.diarize:
        fields.append(("diarize", "true"))
    fields.extend(("keyterm", keyterm) for keyterm in args.keyterm)
    result = call_xai_stt(audio, fields, api_key)
    raw_path = output / "xai-stt.json"
    raw_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    request_path = output / "xai-stt-request.json"
    request_path.write_text(json.dumps({
        "requested_language": language,
        "language_source": language_source,
        "diarize": args.diarize,
        "keyterms": args.keyterm,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    words = result.get("words") or []
    if words:
        rows = group_words(words)
    else:
        duration = round(float(result.get("duration", 0)) * 1000)
        rows = [(0, duration, normalize_caption_text(result.get("text", "")))]
    transcript_path = write_transcript(output / "xai-stt.json", rows)
    media_info = load_json_object(output / "media.info.json")
    actual_language = normalize_language(result.get("language")) or language
    acquisition = load_json_object(output / ACQUISITION_MANIFEST)
    original_language = normalize_language(
        (acquisition.get("media") or {}).get("original_language")
    )
    _, transcript_manifest = publish_active_transcript(
        output,
        transcript_path,
        source="xai-stt",
        language=actual_language,
        original_language=original_language or actual_language,
        media_info=media_info,
        details={
            "request": request_path.name,
            "raw": raw_path.name,
            "language_source": language_source,
        },
    )
    acquisition_path = update_acquisition(
        output,
        info=media_info or None,
        transcript=transcript_manifest,
        next_action="summarize",
    )
    print(acquisition_path)


def probe_local_asr_cli(spec: dict) -> dict:
    command = spec["command"]
    executable = shutil.which(command)
    result = {**spec, "path": executable, "status": "missing"}
    if executable is None:
        return result
    try:
        probe = subprocess.run(
            [executable, "--help"],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        result.update({"status": "broken", "diagnostic": str(error)})
        return result
    combined = "\n".join(part for part in (probe.stdout, probe.stderr) if part).strip()
    help_rendered = bool(re.search(r"(^|\n)\s*usage:", combined, re.IGNORECASE))
    diagnostic = combined.splitlines()[-1][:500] if combined else None
    result.update({
        "status": "ready" if probe.returncode == 0 or help_rendered else "broken",
        "returncode": probe.returncode,
        "diagnostic": diagnostic,
    })
    return result


def cache_roots() -> list[tuple[str, Path]]:
    user_cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    huggingface_home = Path(os.environ.get("HF_HOME", user_cache / "huggingface"))
    roots: list[tuple[str, Path]] = [
        ("huggingface", huggingface_home / "hub"),
        ("huggingface", Path.home() / "Library/Caches/huggingface/hub"),
        ("whisper", user_cache / "whisper"),
        ("whisper-cpp", Path("/opt/homebrew/share/whisper.cpp/models")),
        ("whisper-cpp", Path("/usr/local/share/whisper.cpp/models")),
    ]
    for variable, kind in (
        ("WHISPER_MODEL_DIR", "whisper"),
        ("WHISPER_CPP_MODEL_DIR", "whisper-cpp"),
    ):
        if value := os.environ.get(variable):
            roots.append((kind, Path(value).expanduser()))
    unique: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for kind, path in roots:
        resolved = path.resolve()
        if resolved not in seen:
            unique.append((kind, resolved))
            seen.add(resolved)
    return unique


def cached_models(kind: str, root: Path) -> list[str]:
    if not root.is_dir():
        return []
    if kind == "huggingface":
        return sorted(
            entry.name for entry in root.iterdir()
            if entry.is_dir() and entry.name.startswith("models--") and "whisper" in entry.name.lower()
        )
    patterns = ("*.pt",) if kind == "whisper" else ("ggml-*.bin", "ggml-*.gguf")
    return sorted({entry.name for pattern in patterns for entry in root.glob(pattern) if entry.is_file()})


def cli_has_compatible_cache(command: str, cache_report: list[dict]) -> bool:
    models_by_kind = {
        kind: [model for cache in cache_report if cache["kind"] == kind for model in cache["models"]]
        for kind in {cache["kind"] for cache in cache_report}
    }
    hf_models = [model.lower() for model in models_by_kind.get("huggingface", [])]
    if command == "mlx_whisper":
        return any("mlx-community--whisper" in model for model in hf_models)
    if command in {"whisper-ctranslate2", "whisperx"}:
        return any("faster-whisper" in model for model in hf_models)
    if command == "whisper-cli":
        return bool(models_by_kind.get("whisper-cpp"))
    if command == "whisper":
        return bool(models_by_kind.get("whisper"))
    if command == "stable-ts":
        return bool(models_by_kind.get("whisper") or hf_models)
    if command == "insanely-fast-whisper":
        return bool(hf_models)
    return False


def detect_local_asr(args: argparse.Namespace) -> None:
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    clis = [probe_local_asr_cli(spec) for spec in LOCAL_ASR_CLIS]
    caches = [
        {"kind": kind, "path": str(root), "models": models}
        for kind, root in cache_roots()
        if (models := cached_models(kind, root))
    ]
    for cli in clis:
        cli["compatible_cache_entry_found"] = cli_has_compatible_cache(cli["command"], caches)

    ready = [cli for cli in clis if cli["status"] == "ready"]
    apple_silicon = platform.system() == "Darwin" and platform.machine() == "arm64"
    preferred_order = [
        "mlx_whisper" if apple_silicon else "whisper-ctranslate2",
        "whisper-ctranslate2",
        "whisperx",
        "whisper-cli",
        "stable-ts",
        "insanely-fast-whisper",
        "whisper",
    ]
    recommendation = next(
        (
            cli for command in preferred_order for cli in ready
            if cli["command"] == command and cli["compatible_cache_entry_found"]
        ),
        None,
    )
    cached_candidate = next(
        (
            cli for command in preferred_order for cli in clis
            if cli["command"] == command and cli["compatible_cache_entry_found"]
        ),
        None,
    )
    report = {
        "architecture": platform.machine(),
        "platform": platform.system(),
        "clis": clis,
        "model_caches": caches,
        "recommended_cached_cli": recommendation["command"] if recommendation else None,
        "preferred_cached_candidate": cached_candidate["command"] if cached_candidate else None,
        "healthy_cli_count": len(ready),
        "repair_or_install_for_cached_model": recommendation is None and cached_candidate is not None,
        "download_model_for_healthy_cli": recommendation is None and bool(ready),
    }
    report_path = output / "local-asr-capabilities.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(report_path)


def normalize_asr(args: argparse.Namespace) -> None:
    source = args.input.resolve()
    if not source.is_file():
        raise SystemExit(f"ASR output is unavailable: {source}")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower()
    if suffix == ".json3":
        rows = rows_from_json3(source)
    elif suffix in {".srt", ".vtt"}:
        rows = rows_from_timed_text(source)
    elif suffix == ".json":
        rows = rows_from_asr_json(source)
    else:
        raise SystemExit(f"Unsupported ASR output format: {suffix}")
    if not rows:
        raise SystemExit("No timestamped ASR segments were found in the input.")
    transcript_path = write_transcript(output / "asr.normalized.json", rows)
    media_info = load_json_object(output / "media.info.json")
    source_data = load_json_object(source) if suffix == ".json" else {}
    language = normalize_language(source_data.get("language"))
    language_source = "ASR output"
    if language is None:
        language, language_source = infer_asr_language(output)
    acquisition = load_json_object(output / ACQUISITION_MANIFEST)
    original_language = normalize_language(
        (acquisition.get("media") or {}).get("original_language")
    )
    manifest_path = output / "asr.source.json"
    write_json_atomic(manifest_path, {
        "source": str(source),
        "format": suffix.removeprefix("."),
        "segments": len(rows),
        "language": language,
        "language_source": language_source,
    })
    _, transcript_manifest = publish_active_transcript(
        output,
        transcript_path,
        source="local-asr",
        language=language,
        original_language=original_language or language,
        media_info=media_info,
        details={"source_manifest": manifest_path.name},
    )
    acquisition_path = update_acquisition(
        output,
        info=media_info or None,
        transcript=transcript_manifest,
        next_action="summarize",
    )
    print(acquisition_path)


def load_transcript_jsonl(path: Path) -> list[dict]:
    segments: list[dict] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            segment = json.loads(line)
        except json.JSONDecodeError as error:
            raise SystemExit(f"Invalid transcript JSONL at line {line_number}: {error}") from error
        required = {"start_ms", "end_ms", "text"}
        has_identifier = isinstance(segment, dict) and (
            "segment" in segment or "cue" in segment
        )
        if not isinstance(segment, dict) or not required.issubset(segment) or not has_identifier:
            raise SystemExit(f"Invalid transcript segment at line {line_number}.")
        segments.append(segment)
    return segments


def timestamp_ms_argument(value: str) -> int:
    try:
        return parse_clock(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def transcript_slice(args: argparse.Namespace) -> None:
    source = args.input.resolve()
    if not source.is_file():
        raise SystemExit(f"Transcript JSONL is unavailable: {source}")
    cue_start = getattr(args, "cue_start", None)
    cue_end = getattr(args, "cue_end", None)
    if all(value is None for value in (
        args.start, args.end, args.segment_start, args.segment_end,
        cue_start, cue_end,
    )):
        raise SystemExit("Provide a time, segment, or cue boundary for transcript-slice.")
    segments = load_transcript_jsonl(source)
    raw_cues = bool(segments and "cue" in segments[0] and "segment" not in segments[0])
    if raw_cues and (args.segment_start is not None or args.segment_end is not None):
        raise SystemExit("Use --cue-start/--cue-end with a raw cue JSONL file.")
    if not raw_cues and (cue_start is not None or cue_end is not None):
        raise SystemExit("Use --segment-start/--segment-end with a compacted transcript JSONL file.")
    start_boundary = cue_start if raw_cues else args.segment_start
    end_boundary = cue_end if raw_cues else args.segment_end
    identifier = "cue" if raw_cues else "segment"
    selected = []
    for segment in segments:
        index = int(segment[identifier])
        if start_boundary is not None and index < start_boundary:
            continue
        if end_boundary is not None and index > end_boundary:
            continue
        if args.start is not None and int(segment["end_ms"]) < args.start:
            continue
        if args.end is not None and int(segment["start_ms"]) > args.end:
            continue
        selected.append(segment)
    if not selected:
        raise SystemExit("No transcript segments matched the requested slice.")
    lines = [
        "# Transcript slice",
        "",
        f"- Source: `{source}`",
        f"- {'Cues' if raw_cues else 'Segments'}: "
        f"`{selected[0][identifier]}–{selected[-1][identifier]}` of `{len(segments)}`",
        f"- Time: `{selected[0]['start']}–{selected[-1]['end']}`",
        "",
    ]
    lines.extend(
        f"- [{segment['start']}–{segment['end']}] {segment['text']}"
        for segment in selected
    )
    rendered = "\n".join(lines) + "\n"
    if args.output:
        target = args.output.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")
        print(target)
    else:
        print(rendered, end="")


def add_cookie_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cookies-from-browser",
        help="Browser name passed to yt-dlp after the user approves account-cookie access",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    captions = subparsers.add_parser("captions", help="Download captions and metadata")
    captions.add_argument("url")
    captions.add_argument("--output", type=Path, required=True)
    captions.add_argument(
        "--preferred-languages",
        default=",".join(DEFAULT_FALLBACK_LANGUAGES),
        help="Comma-separated fallback languages used after the original language",
    )
    captions.add_argument(
        "--original-language",
        help="Original-language override used when platform metadata is missing or wrong",
    )
    add_cookie_option(captions)
    captions.set_defaults(func=acquire_captions)

    audio = subparsers.add_parser("audio", help="Download an audio track for transcription")
    audio.add_argument("url")
    audio.add_argument("--output", type=Path, required=True)
    audio.add_argument("--format", default="m4a")
    audio.add_argument(
        "--timeout", type=float, default=900,
        help="Per-attempt timeout in seconds for metadata and audio download",
    )
    add_cookie_option(audio)
    audio.set_defaults(func=acquire_audio)

    frames = subparsers.add_parser(
        "frames",
        help="Extract manifest-approved frames with remote seek and bounded fallback",
    )
    frames.add_argument("url")
    frames.add_argument("--output", type=Path, required=True)
    frames.add_argument("--manifest", type=Path, required=True)
    frames.add_argument(
        "--height", type=int, default=720,
        help="Default maximum height; individual manifest requests may override it",
    )
    frames.add_argument(
        "--image-format", choices=("jpg", "png"), default="jpg",
        help="Default frame format; individual manifest requests may override it",
    )
    frames.add_argument(
        "--timeout", type=float, default=60,
        help="Per-process timeout in seconds for metadata, seek, and extraction",
    )
    frames.add_argument(
        "--max-full-download-mib", type=float, default=200,
        help="Full-download fallback budget after remote and partial strategies; 0 disables it",
    )
    add_cookie_option(frames)
    frames.set_defaults(func=acquire_frames)

    observe = subparsers.add_parser(
        "record-frame-observation",
        help="Record what was actually visible after viewing a generated frame",
    )
    observe.add_argument("--result", type=Path, required=True)
    observe.add_argument("--frame-id", required=True)
    observe.add_argument(
        "--readability", choices=("readable", "partial", "unreadable"), required=True,
    )
    observe.add_argument("--visible-fact", action="append", default=[])
    observe.add_argument(
        "--supports-claim", choices=("yes", "partial", "no", "uncertain"), required=True,
    )
    observe.add_argument("--note")
    observe.set_defaults(func=record_frame_observation)

    xai = subparsers.add_parser("transcribe-xai", help="Transcribe audio with xAI Speech-to-Text")
    xai.add_argument("--audio", type=Path, required=True)
    xai.add_argument("--output", type=Path, required=True)
    xai.add_argument(
        "--language",
        help="BCP-47 override; omit or pass auto to use a high-confidence inferred language",
    )
    xai.add_argument("--diarize", action="store_true")
    xai.add_argument("--keyterm", action="append", default=[])
    xai.add_argument("--confirm-paid-api", action="store_true")
    xai.set_defaults(func=transcribe_xai)

    detect_asr = subparsers.add_parser(
        "detect-local-asr",
        help="Probe installed ASR CLIs and reusable local model caches",
    )
    detect_asr.add_argument("--output", type=Path, required=True)
    detect_asr.set_defaults(func=detect_local_asr)

    normalize = subparsers.add_parser(
        "normalize-asr",
        help="Normalize timestamped SRT, VTT, JSON3, or Whisper JSON output",
    )
    normalize.add_argument("--input", type=Path, required=True)
    normalize.add_argument("--output", type=Path, required=True)
    normalize.set_defaults(func=normalize_asr)

    slice_parser = subparsers.add_parser(
        "transcript-slice",
        help="Read a continuous time or segment range from transcript JSONL",
    )
    slice_parser.add_argument("--input", type=Path, required=True)
    slice_parser.add_argument("--start", type=timestamp_ms_argument)
    slice_parser.add_argument("--end", type=timestamp_ms_argument)
    slice_parser.add_argument("--segment-start", type=int)
    slice_parser.add_argument("--segment-end", type=int)
    slice_parser.add_argument("--cue-start", type=int)
    slice_parser.add_argument("--cue-end", type=int)
    slice_parser.add_argument("--output", type=Path)
    slice_parser.set_defaults(func=transcript_slice)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
