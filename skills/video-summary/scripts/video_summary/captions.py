from __future__ import annotations

import argparse
import json
from pathlib import Path

from .core import (
    ORIGINAL_MARKERS,
    SUPPORTED_SUBTITLE_EXTENSIONS,
    TRANSLATION_MARKERS,
    cookie_arguments,
    language_matches,
    normalize_language,
    primary_language,
    require_program,
    run,
    update_acquisition,
)
from .transcript import (
    convert_subtitle,
    publish_active_transcript,
)


def caption_name(entries: list[dict]) -> str:
    names = [
        str(entry.get("name", "")).strip() for entry in entries if entry.get("name")
    ]
    return names[0] if names else ""


def caption_is_usable(code: str, entries: list[dict]) -> bool:
    if code in {"danmaku", "live_chat"}:
        return False
    extensions = {str(entry.get("ext", "")).lower() for entry in entries}
    return bool(
        extensions.intersection(SUPPORTED_SUBTITLE_EXTENSIONS) or not extensions
    )


def collect_caption_candidates(info: dict) -> list[dict]:
    candidates: list[dict] = []
    for source, field in (("manual", "subtitles"), ("automatic", "automatic_captions")):
        for code, raw_entries in (info.get(field) or {}).items():
            entries = raw_entries if isinstance(raw_entries, list) else []
            if not entries or not caption_is_usable(code, entries):
                continue
            name = caption_name(entries)
            lowered_name = name.lower()
            candidates.append(
                {
                    "code": code,
                    "language": normalize_language(code),
                    "source": source,
                    "name": name,
                    "original_marker": code.lower().endswith("-orig")
                    or any(marker in lowered_name for marker in ORIGINAL_MARKERS),
                }
            )
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
        candidate["language"]
        for candidate in candidates
        if candidate["original_marker"] and candidate["language"]
    }
    if len(original_candidates) == 1:
        return original_candidates.pop(), "caption marked original", "high"

    manual_languages = {
        candidate["language"]
        for candidate in candidates
        if candidate["source"] == "manual"
        and candidate["language"]
        and not any(
            marker in candidate["name"].lower() for marker in TRANSLATION_MARKERS
        )
    }
    if len(manual_languages) == 1:
        return manual_languages.pop(), "single manual caption language", "medium"
    return None, None, "none"


def preferred_language_bonus(code: str, preferred_languages: list[str]) -> int:
    normalized_code = normalize_language(code)
    for index, preferred in enumerate(preferred_languages):
        if normalized_code == normalize_language(preferred):
            return max(3, 15 - index * 3)
    if any(
        primary_language(code) == primary_language(preferred)
        for preferred in preferred_languages
    ):
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
        scored.append(
            {
                **candidate,
                "native_language": native,
                "translated": translated,
                "score": score,
            }
        )
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
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def find_selected_caption_file(output: Path, language_code: str) -> Path:
    prefix = f"media.{language_code}."
    candidates = [
        path
        for path in output.iterdir()
        if path.is_file()
        and path.name.startswith(prefix)
        and path.suffix.removeprefix(".") in SUPPORTED_SUBTITLE_EXTENSIONS
    ]
    format_rank = {".json3": 3, ".srt": 2, ".vtt": 1}
    if candidates:
        return max(candidates, key=lambda path: format_rank.get(path.suffix, 0))
    raise SystemExit(
        f"Unable to identify the downloaded caption track: {language_code}"
    )


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
        language.strip()
        for language in args.preferred_languages.split(",")
        if language.strip()
    ]
    raw_candidates = collect_caption_candidates(info)
    original_language, original_source, original_confidence = detect_original_language(
        info,
        raw_candidates,
        args.original_language,
    )
    candidates = score_caption_candidates(
        raw_candidates,
        original_language,
        preferred_languages,
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

    caption_flag = (
        "--write-subs" if selected["source"] == "manual" else "--write-auto-subs"
    )
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
