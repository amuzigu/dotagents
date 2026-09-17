from __future__ import annotations

import argparse
import json
import re
import shutil
import unicodedata
from pathlib import Path

from .core import (
    ACTIVE_TRANSCRIPT_MARKDOWN,
    MAX_COMPACTED_CHARACTERS,
    MAX_COMPACTED_DURATION_MS,
    NON_SPEECH_CUE,
    SPEAKER_MARKER,
    TERMINAL_PUNCTUATION,
    TRANSCRIPT_CHUNK_CHARACTERS,
    TRANSCRIPT_CHUNK_SEGMENTS,
    format_time,
    indexed_chapters,
    load_json_object,
    normalize_caption_text,
    parse_clock,
    transcript_bundle_paths,
    transcript_raw_path,
    write_json_atomic,
)


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
        1 for token, _, _ in tokens if len(token) == 1 and "\u3400" <= token <= "\u9fff"
    )


def edge_contains(longer: list[str], shorter: list[str]) -> bool:
    if not shorter or len(shorter) >= len(longer):
        return False
    return longer[: len(shorter)] == shorter or longer[-len(shorter) :] == shorter


def longest_suffix_prefix(first: list[str], second: list[str]) -> int:
    for length in range(min(len(first), len(second)) - 1, 0, -1):
        if first[-length:] == second[:length]:
            return length
    return 0


def append_after_overlap(first: str, second: str, overlap_tokens: int) -> str:
    tokens = lexical_tokens(second)
    remainder = second[tokens[overlap_tokens - 1][2] :]
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
    combined_duration = (
        max(previous["end_ms"], current["end_ms"]) - previous["start_ms"]
    )
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
    combined_duration = (
        max(previous["end_ms"], current["end_ms"]) - previous["start_ms"]
    )
    previous_tokens = lexical_tokens(previous["text"])
    current_tokens = lexical_tokens(current["text"])
    previous_values = [token for token, _, _ in previous_tokens]
    current_values = [token for token, _, _ in current_tokens]
    if not previous_values or not current_values:
        return None

    if (
        previous_values == current_values
        and combined_duration <= MAX_COMPACTED_DURATION_MS
    ):
        lexical_length = max(
            token_text_length(previous_tokens),
            token_text_length(current_tokens),
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
                previous["text"],
                current["text"],
                overlap_length,
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
        raw_cues.append(
            {
                "cue": source_cue,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "start": format_time(start_ms),
                "end": format_time(end_ms),
                "text": normalized,
            }
        )

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
            round((len(raw_cues) - len(segments)) / len(raw_cues), 4) if raw_cues else 0
        ),
        "raw_character_count": raw_characters,
        "character_count": compacted_characters,
        "characters_removed": max(0, raw_characters - compacted_characters),
        "character_reduction_ratio": (
            round(max(0, raw_characters - compacted_characters) / raw_characters, 4)
            if raw_characters
            else 0
        ),
        "raw_jsonl_character_count": raw_jsonl_characters,
        "jsonl_character_count": compacted_jsonl_characters,
        "jsonl_character_reduction_ratio": (
            round(
                max(0, raw_jsonl_characters - compacted_jsonl_characters)
                / raw_jsonl_characters,
                4,
            )
            if raw_jsonl_characters
            else 0
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
        chunks.append(
            {
                "chunk": len(chunks),
                "segment_start": current[0]["segment"],
                "segment_end": current[-1]["segment"],
                "start_ms": current[0]["start_ms"],
                "end_ms": current[-1]["end_ms"],
                "segment_count": len(current),
                "character_count": character_count,
            }
        )
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
    index_path.write_text(
        json.dumps(
            {
                "version": 1,
                "source": path.name,
                "markdown": output.name,
                "jsonl": jsonl_path.name,
                "raw_jsonl": raw_path.name,
                "segment_count": len(segments),
                "character_count": sum(len(segment["text"]) for segment in segments),
                "raw_cue_count": len(raw_cues),
                "start_ms": segments[0]["start_ms"] if segments else None,
                "end_ms": segments[-1]["end_ms"] if segments else None,
                "chunks": chunks,
                "compaction": compaction,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return output


def rows_from_json3(path: Path) -> list[tuple[int, int, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows: list[tuple[int, int, str]] = []
    for event in data.get("events", []):
        segments = event.get("segs") or []
        text = normalize_caption_text(
            "".join(segment.get("utf8", "") for segment in segments)
        )
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
        time_index = next(
            (index for index, line in enumerate(lines) if "-->" in line), None
        )
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
                end = round(
                    seconds_value(segment.get("end", segment.get("start", 0))) * 1000
                )
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
                end = round(
                    seconds_value(timestamps.get("to", timestamps.get("from", 0)))
                    * 1000
                )
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
    index.update(
        {
            "version": 1,
            "source": transcript.name,
            "markdown": active.name,
            "jsonl": active_jsonl.name,
            "raw_jsonl": active_raw.name if active_raw.is_file() else None,
            "transcript_source": source,
            "language": language,
            "original_language": original_language,
            "chapters": indexed_chapters(media_info),
        }
    )
    write_json_atomic(active_index, index)
    duration = media_info.get("duration")
    duration_ms = (
        round(float(duration) * 1000) if isinstance(duration, (int, float)) else None
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
        "character_count": index.get("character_count"),
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


def group_words(words: list[dict]) -> list[tuple[int, int, str]]:
    rows: list[tuple[int, int, str]] = []
    current: list[dict] = []
    for word in words:
        if current:
            gap = float(word.get("start", 0)) - float(current[-1].get("end", 0))
            duration = float(current[-1].get("end", 0)) - float(
                current[0].get("start", 0)
            )
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


def load_transcript_jsonl(path: Path) -> list[dict]:
    segments: list[dict] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip():
            continue
        try:
            segment = json.loads(line)
        except json.JSONDecodeError as error:
            raise SystemExit(
                f"Invalid transcript JSONL at line {line_number}: {error}"
            ) from error
        required = {"start_ms", "end_ms", "text"}
        has_identifier = isinstance(segment, dict) and (
            "segment" in segment or "cue" in segment
        )
        if (
            not isinstance(segment, dict)
            or not required.issubset(segment)
            or not has_identifier
        ):
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
    if all(
        value is None
        for value in (
            args.start,
            args.end,
            args.segment_start,
            args.segment_end,
            cue_start,
            cue_end,
        )
    ):
        raise SystemExit(
            "Provide a time, segment, or cue boundary for transcript-slice."
        )
    segments = load_transcript_jsonl(source)
    raw_cues = bool(segments and "cue" in segments[0] and "segment" not in segments[0])
    if raw_cues and (args.segment_start is not None or args.segment_end is not None):
        raise SystemExit("Use --cue-start/--cue-end with a raw cue JSONL file.")
    if not raw_cues and (cue_start is not None or cue_end is not None):
        raise SystemExit(
            "Use --segment-start/--segment-end with a compacted transcript JSONL file."
        )
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
        (
            f"- {'Cues' if raw_cues else 'Segments'}: "
            f"`{selected[0][identifier]}–{selected[-1][identifier]}` of `{len(segments)}`"
        ),
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
