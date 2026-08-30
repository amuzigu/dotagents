from __future__ import annotations

import argparse
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from .core import (
    ACQUISITION_MANIFEST,
    cookie_arguments,
    load_json_object,
    platform_label,
    require_program,
    update_acquisition,
    write_json_atomic,
)
from .transport import (
    failure_code,
    find_downloaded_media,
    format_size_estimate,
    merged_http_headers,
    process_attempt,
    protocol_name,
    refresh_media_info,
    run_bounded,
    serialized_http_headers,
)


def audio_format_candidates(info: dict) -> list[dict]:
    candidates = []
    for candidate in info.get("formats") or []:
        if not isinstance(candidate, dict) or candidate.get("has_drm"):
            continue
        acodec = str(candidate.get("acodec") or "none").lower()
        vcodec = str(candidate.get("vcodec") or "none").lower()
        url = str(candidate.get("url") or "")
        if (
            acodec == "none"
            or vcodec != "none"
            or not url.startswith(("http://", "https://"))
        ):
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
    selector = (
        str(candidate.get("format_id"))
        if candidate and candidate.get("format_id")
        else "bestaudio/best"
    )
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
                    info,
                    candidate,
                    direct_target,
                    args.format,
                    args.timeout,
                )
                direct = require_audio_output(direct, direct_target)
                summary = audio_candidate_summary(candidate, info.get("duration"))
                report["attempts"].append(
                    process_attempt("direct_url", direct, summary)
                )
                if direct["returncode"] == 0:
                    source, strategy = direct_target, "direct_url"
                elif failure_code(direct) == "url-or-auth":
                    refreshed_info, metadata_outcome = refresh_media_info(args, output)
                    report["metadata_refreshed"] = True
                    report["attempts"].append(
                        process_attempt("metadata_refresh", metadata_outcome)
                    )
                    if refreshed_info:
                        info = refreshed_info
                        candidates = audio_format_candidates(info)
                        candidate = candidates[0] if candidates else None
                        if candidate:
                            refreshed_target = (
                                temp_dir / f"media-refreshed.{args.format}"
                            )
                            retried = ffmpeg_audio_command(
                                info,
                                candidate,
                                refreshed_target,
                                args.format,
                                args.timeout,
                            )
                            retried = require_audio_output(retried, refreshed_target)
                            summary = audio_candidate_summary(
                                candidate, info.get("duration")
                            )
                            report["attempts"].append(
                                process_attempt(
                                    "direct_url_refreshed", retried, summary
                                )
                            )
                            if retried["returncode"] == 0:
                                source, strategy = (
                                    refreshed_target,
                                    "direct_url_refreshed",
                                )

        if source is None:
            fallback = yt_dlp_audio_command(args, temp_dir, candidate)
            fallback_source: Path | None = None
            if fallback["returncode"] == 0:
                try:
                    fallback_source = find_downloaded_media(
                        temp_dir,
                        fallback["stdout"],
                        "media",
                    )
                except SystemExit as error:
                    fallback = dict(fallback)
                    fallback["returncode"] = 1
                    fallback["stderr"] = str(error)
                fallback = require_audio_output(fallback, fallback_source)
            summary = (
                audio_candidate_summary(candidate, info.get("duration"))
                if candidate and info
                else None
            )
            report["attempts"].append(process_attempt("yt_dlp", fallback, summary))
            if fallback["returncode"] == 0:
                source = fallback_source
                strategy = "yt_dlp"

        if source is None or strategy is None:
            report.update(
                {
                    "status": "failed",
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "error_code": (
                        report["attempts"][-1].get("error_code")
                        if report["attempts"]
                        else "media-unavailable"
                    ),
                }
            )
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
        report.update(
            {
                "status": "success",
                "strategy": strategy,
                "path": str(target.relative_to(output)),
                "bytes": target.stat().st_size,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        )
        write_json_atomic(result_path, report)
        transcript_status = load_json_object(output / ACQUISITION_MANIFEST).get(
            "transcript", {}
        )
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
                "summarize"
                if transcript_status.get("status") == "ready"
                else "transcribe"
            ),
        )
        print(acquisition_path)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
