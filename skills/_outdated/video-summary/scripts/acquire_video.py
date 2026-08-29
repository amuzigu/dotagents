#!/usr/bin/env python3
"""Acquire and normalize evidence for video URL summaries."""

from __future__ import annotations

import argparse
import html
import http.client
import json
import mimetypes
import os
import platform
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path


DEFAULT_FALLBACK_LANGUAGES = ("en", "zh-Hans", "zh-Hant", "zh")
SUPPORTED_SUBTITLE_EXTENSIONS = ("json3", "srt", "vtt")
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
    result = subprocess.run(command, text=True, capture_output=True)
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


def write_transcript(path: Path, rows: list[tuple[int, int, str]]) -> Path:
    output = path.with_suffix(".transcript.md")
    title = path.name.rsplit(".", 1)[0]
    lines = [f"# Transcript: {title}", ""]
    lines.extend(
        f"- [{format_time(start)}–{format_time(end)}] {text}"
        for start, end, text in rows
    )
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output


def rows_from_json3(path: Path) -> list[tuple[int, int, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows: list[tuple[int, int, str]] = []
    previous = ""
    for event in data.get("events", []):
        segments = event.get("segs") or []
        text = normalize_caption_text("".join(segment.get("utf8", "") for segment in segments))
        if not text or text == previous:
            continue
        start = int(event.get("tStartMs", 0))
        end = start + int(event.get("dDurationMs", 0))
        rows.append((start, end, text))
        previous = text
    return rows


def convert_json3(path: Path) -> Path:
    return write_transcript(path, rows_from_json3(path))


def rows_from_timed_text(path: Path) -> list[tuple[int, int, str]]:
    content = path.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    rows: list[tuple[int, int, str]] = []
    previous = ""
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
        if not text or text == previous:
            continue
        rows.append((start, end, text))
        previous = text
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


def write_selected_transcript(
    output: Path,
    transcript: Path,
    selected: dict,
    original_language: str | None,
) -> Path:
    body = transcript.read_text(encoding="utf-8").splitlines()
    if body and body[0].startswith("# "):
        body = body[2:]
    target = output / "selected.transcript.md"
    lines = [
        "# Selected transcript",
        "",
        f"- Language: `{selected['code']}`",
        f"- Source: `{selected['source']}`",
        f"- Original language: `{original_language or 'unknown'}`",
        f"- Selection score: `{selected['score']}`",
        "",
        *body,
    ]
    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return target


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
        print(report_path)
        raise SystemExit("No usable embedded captions were found. Continue with audio transcription.")

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
    selected_transcript = write_selected_transcript(
        output, transcript, selected, original_language,
    )
    print(selected_transcript)
    print(report_path)


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


def acquire_audio(args: argparse.Namespace) -> None:
    require_program("yt-dlp")
    require_program("ffmpeg")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    command = [
        "yt-dlp",
        "--no-playlist",
        "--format",
        "bestaudio/best",
        "--extract-audio",
        "--audio-format",
        args.format,
        "--write-info-json",
        "--output",
        str(output / "media.%(ext)s"),
        "--print",
        "after_move:filepath",
        *cookie_arguments(args),
        args.url,
    ]
    print(find_downloaded_media(output, run(command), "media"))


def parse_timestamp(value: str) -> float:
    try:
        milliseconds = parse_clock(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    if milliseconds < 0:
        raise argparse.ArgumentTypeError(f"Invalid timestamp: {value}")
    return milliseconds / 1000


def timestamp_label(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}-{minutes:02d}-{whole_seconds:02d}-{millis:03d}"


def acquire_frames(args: argparse.Namespace) -> None:
    require_program("yt-dlp")
    require_program("ffmpeg")
    output = args.output.resolve()
    frames_dir = output / "frames"
    output.mkdir(parents=True, exist_ok=True)
    frames_dir.mkdir(parents=True, exist_ok=True)
    command = [
        "yt-dlp",
        "--no-playlist",
        "--format",
        f"bestvideo*[height<={args.height}]/best[height<={args.height}]",
        "--output",
        str(output / "source.%(ext)s"),
        "--print",
        "after_move:filepath",
        *cookie_arguments(args),
        args.url,
    ]
    video_path = find_downloaded_media(output, run(command), "source")
    timestamps = [parse_timestamp(item) for item in args.timestamps.split(",") if item.strip()]
    if not timestamps:
        raise SystemExit("Provide at least one timestamp.")
    manifest = ["# Extracted frames", "", f"Source: `{video_path}`", ""]
    for seconds in timestamps:
        label = timestamp_label(seconds)
        frame_path = frames_dir / f"frame-{label}.jpg"
        run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(seconds),
            "-i", str(video_path), "-frames:v", "1", "-vf",
            "scale=min(1280\\,iw):-2", "-q:v", "2", "-y", str(frame_path),
        ])
        manifest.append(f"- `{label}`: `{frame_path}`")
        print(frame_path)
    (frames_dir / "manifest.md").write_text("\n".join(manifest) + "\n", encoding="utf-8")


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
    print(request_path)
    print(raw_path)
    print(transcript_path)


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
    transcript_path = write_transcript(output / "local-asr.json", rows)
    manifest_path = output / "local-asr-source.json"
    manifest_path.write_text(json.dumps({
        "source": str(source),
        "format": suffix.removeprefix("."),
        "segments": len(rows),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(manifest_path)
    print(transcript_path)


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
    add_cookie_option(audio)
    audio.set_defaults(func=acquire_audio)

    frames = subparsers.add_parser("frames", help="Download video and extract frames")
    frames.add_argument("url")
    frames.add_argument("--output", type=Path, required=True)
    frames.add_argument("--timestamps", required=True, help="Comma-separated HH:MM:SS values")
    frames.add_argument("--height", type=int, default=720)
    add_cookie_option(frames)
    frames.set_defaults(func=acquire_frames)

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

    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
