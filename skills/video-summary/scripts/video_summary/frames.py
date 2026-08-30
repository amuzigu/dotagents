from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from .core import (
    cookie_arguments,
    parse_clock,
    require_program,
    write_json_atomic,
)
from .transport import (
    failure_code,
    find_downloaded_media,
    format_size_estimate,
    merged_http_headers,
    process_attempt,
    protocol_name,
    redact_diagnostic,
    refresh_media_info,
    run_bounded,
    timestamp_label,
)


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
        candidate
        for candidate in candidates
        if isinstance(candidate.get("height"), (int, float))
        and candidate["height"] <= height
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
            raise SystemExit(
                f"Frame request {request_id} needs claim_id and expected_observation."
            )
        requests.append(
            {
                "id": request_id,
                "claim_id": claim_id,
                "timestamp_ms": round(timestamp_ms),
                "height": height,
                "image_format": image_format,
                "crop": str(raw.get("crop") or "").strip() or None,
                "expected_observation": expected,
                "purpose": purpose,
                "user_requested": bool(raw.get("user_requested")),
            }
        )
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
            rendered_headers = "".join(
                f"{key}: {value}\r\n" for key, value in headers.items()
            )
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
    outcome = run_bounded(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(path),
        ],
        10,
    )
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
    result.update(
        {
            "status": "success",
            "output": str(target),
            "width": width,
            "height": height,
            "bytes": target.stat().st_size,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "expected_observation": request["expected_observation"],
        }
    )


def frame_target(frames_dir: Path, request: dict) -> Path:
    return frames_dir / (
        f"{request['id']}-{timestamp_label(request['timestamp_ms'] / 1000)}-"
        f"{request['height']}p.{request['image_format']}"
    )


def load_frame_registry(path: Path) -> dict:
    if not path.is_file():
        return {"version": 2, "results": [], "runs": []}
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 2, "results": [], "runs": []}
    if not isinstance(registry, dict) or registry.get("version") != 2:
        return {"version": 2, "results": [], "runs": []}
    if not isinstance(registry.get("results"), list):
        registry["results"] = []
    if not isinstance(registry.get("runs"), list):
        registry["runs"] = []
    return registry


def reusable_frame(request: dict, registry: dict) -> dict | None:
    for result in registry.get("results", []):
        if (
            result.get("id") == request["id"]
            and result.get("status") == "success"
            and result.get("timestamp_ms") == request["timestamp_ms"]
            and result.get("requested_height") == request["height"]
            and result.get("image_format") == request["image_format"]
        ):
            output = Path(str(result.get("output") or ""))
            if output.is_file() and output.stat().st_size > 0:
                reused = dict(result)
                reused.update(
                    {
                        "claim_id": request["claim_id"],
                        "purpose": request["purpose"],
                        "expected_observation": request["expected_observation"],
                        "attempts": [
                            {
                                "strategy": "cache",
                                "status": "success",
                                "elapsed_ms": 0,
                            }
                        ],
                        "reused": True,
                    }
                )
                return reused
    return None


def merge_frame_run(registry: dict, report: dict, run_path: Path) -> dict:
    existing = {
        item.get("id"): item
        for item in registry.get("results", [])
        if isinstance(item, dict) and item.get("id")
    }
    for result in report.get("results", []):
        frame_id = result.get("id")
        if not frame_id:
            continue
        previous = existing.get(frame_id)
        if (
            result.get("status") == "success"
            or previous is None
            or previous.get("status") != "success"
        ):
            existing[frame_id] = result
    runs = list(registry.get("runs", []))
    runs.append(
        {
            "run_id": report["run_id"],
            "result": str(run_path),
            "status": report.get("status"),
            "elapsed_ms": report.get("elapsed_ms"),
        }
    )
    return {
        "version": 2,
        "video_url": report.get("video_url") or registry.get("video_url"),
        "results": list(existing.values()),
        "runs": runs,
        "latest_run": str(run_path),
        "observation_file": report.get("observation_file"),
    }


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
        temp = (
            temp_dir / f"{request['id']}-{uuid.uuid4().hex}.{request['image_format']}"
        )
        outcome = ffmpeg_frame_command(
            str(candidate["url"]),
            request["timestamp_ms"] / 1000,
            temp,
            request,
            timeout,
            merged_http_headers(info, candidate),
        )
        summary = candidate_summary(candidate, info.get("duration"))
        result["attempts"].append(process_attempt("remote_seek", outcome, summary))
        if outcome["returncode"] == 0:
            target = frame_target(frames_dir, request)
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
        "yt-dlp",
        "--no-playlist",
        "--force-overwrites",
        "--format",
        f"bestvideo*[height<={request['height']}]/best[height<={request['height']}]",
        "--download-sections",
        f"*{section_start:.3f}-{section_end:.3f}",
        "--output",
        str(temp_dir / f"{stem}.%(ext)s"),
        "--print",
        "after_move:filepath",
        *cookie_arguments(args),
        args.url,
    ]
    download = run_bounded(command, max(args.timeout * 2, 90))
    result["attempts"].append(process_attempt("partial_section_download", download))
    if download["returncode"] != 0:
        return False
    try:
        section = find_downloaded_media(temp_dir, download["stdout"], stem)
    except SystemExit as error:
        result["attempts"].append(
            {
                "strategy": "partial_section_extract",
                "status": "failed",
                "elapsed_ms": 0,
                "error_code": "missing-section",
                "diagnostic": str(error),
            }
        )
        return False
    temp = temp_dir / f"{request['id']}-partial.{request['image_format']}"
    extract = ffmpeg_frame_command(
        str(section),
        seconds - section_start,
        temp,
        request,
        args.timeout,
    )
    result["attempts"].append(process_attempt("partial_section_extract", extract))
    if extract["returncode"] != 0:
        return False
    target = frame_target(frames_dir, request)
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
        "yt-dlp",
        "--no-playlist",
        "--force-overwrites",
        "--format",
        str(
            candidate.get("format_id")
            or (f"bestvideo*[height<={max_height}]/best[height<={max_height}]")
        ),
        "--max-filesize",
        str(budget),
        "--output",
        str(temp_dir / f"{stem}.%(ext)s"),
        "--print",
        "after_move:filepath",
        *cookie_arguments(args),
        args.url,
    ]
    download = run_bounded(command, max(args.timeout * 4, 180))
    report.update(
        {
            "elapsed_ms": download["elapsed_ms"],
            "status": "downloaded" if download["returncode"] == 0 else "failed",
        }
    )
    if download["returncode"] != 0:
        report.update(
            {
                "error_code": failure_code(download),
                "diagnostic": redact_diagnostic(download.get("stderr", "")),
            }
        )
        return report
    try:
        source = find_downloaded_media(temp_dir, download["stdout"], stem)
    except SystemExit as error:
        report.update(
            {"status": "failed", "reason": "missing-download", "diagnostic": str(error)}
        )
        return report
    report["actual_bytes"] = source.stat().st_size
    for request, result in pending:
        temp = temp_dir / f"{request['id']}-full.{request['image_format']}"
        extract = ffmpeg_frame_command(
            str(source),
            request["timestamp_ms"] / 1000,
            temp,
            request,
            args.timeout,
        )
        result["attempts"].append(
            process_attempt("full_download_extract", extract, summary)
        )
        if extract["returncode"] == 0:
            target = frame_target(frames_dir, request)
            finalize_frame(temp, target, request, result)
    return report


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
    registry = load_frame_registry(result_path)
    run_id = f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    runs_dir = frames_dir / "runs" / run_id
    runs_dir.mkdir(parents=True, exist_ok=False)
    run_path = runs_dir / "result.json"
    started = time.monotonic()
    report = {
        "version": 1,
        "run_id": run_id,
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
            cached = reusable_frame(request, registry)
            report["results"].append(
                cached
                or {
                    "id": request["id"],
                    "claim_id": request["claim_id"],
                    "purpose": request["purpose"],
                    "timestamp_ms": request["timestamp_ms"],
                    "requested_height": request["height"],
                    "image_format": request["image_format"],
                    "status": "pending",
                    "attempts": [],
                }
            )
        unresolved = [
            (request, result)
            for request, result in zip(requests, report["results"])
            if result.get("status") != "success"
        ]
        for _, result in zip(requests, report["results"]):
            if result.get("reused"):
                print(result["output"])

        info_path = output / "media.info.json"
        info = None
        if unresolved and info_path.is_file():
            try:
                loaded = json.loads(info_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and loaded.get("formats"):
                    info = loaded
                    report["metadata"].append({"source": "cached", "status": "success"})
            except json.JSONDecodeError:
                pass
        if unresolved and info is None:
            info, outcome = refresh_media_info(args, output)
            report["metadata"].append(process_attempt("metadata_fetch", outcome))
        if unresolved and info is None:
            raise RuntimeError("Unable to acquire media metadata for frame extraction.")

        pending: list[tuple[dict, dict]] = []
        refreshed = False
        for request, result in unresolved:
            success, expired = try_remote_frame(
                request,
                result,
                info,
                temp_dir,
                frames_dir,
                args.timeout,
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
                        request,
                        result,
                        info,
                        temp_dir,
                        frames_dir,
                        args.timeout,
                    )
            if success:
                print(result["output"])
                continue
            if try_partial_section(args, request, result, temp_dir, frames_dir):
                print(result["output"])
                continue
            pending.append((request, result))

        if pending:
            report["full_download"] = full_download_fallback(
                args,
                pending,
                info,
                temp_dir,
                frames_dir,
            )
        else:
            report["full_download"] = {"attempted": False, "status": "unneeded"}
        for request, result in pending:
            if result["status"] == "success":
                print(result["output"])
                continue
            result.update(
                {
                    "status": "failed",
                    "failure_reason": report["full_download"].get("reason")
                    or report["full_download"].get("error_code")
                    or "all-frame-strategies-failed",
                }
            )
        success_count = sum(
            result["status"] == "success" for result in report["results"]
        )
        report["status"] = (
            "success"
            if success_count == len(report["results"])
            else "partial"
            if success_count
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
        write_json_atomic(run_path, report)
        write_json_atomic(result_path, merge_frame_run(registry, report, run_path))
        shutil.rmtree(temp_dir, ignore_errors=True)
    if not observations_path.exists():
        write_json_atomic(observations_path, {"version": 1, "observations": []})
    print(result_path)
    if fatal or report["status"] != "success":
        raise SystemExit(
            fatal
            or "Some requested frames could not be extracted; inspect result.json."
        )


def load_successful_frames(result_path: Path) -> dict[str, dict]:
    if not result_path.is_file():
        raise SystemExit(f"Frame result is unavailable: {result_path}")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    return {
        item["id"]: item
        for item in result.get("results", [])
        if isinstance(item, dict) and item.get("id") and item.get("status") == "success"
    }


def validate_observation(raw: dict, frames: dict[str, dict]) -> dict:
    frame_id = str(raw.get("frame_id") or "")
    frame = frames.get(frame_id)
    if frame is None:
        raise SystemExit(f"Successful frame id is unavailable: {frame_id}")
    readability = raw.get("readability")
    if readability not in {"readable", "partial", "unreadable"}:
        raise SystemExit(f"Invalid readability for frame {frame_id}: {readability}")
    supports_claim = raw.get("supports_claim")
    if supports_claim not in {"yes", "partial", "no", "uncertain"}:
        raise SystemExit(
            f"Invalid supports_claim for frame {frame_id}: {supports_claim}"
        )
    visible_facts = raw.get("visible_facts", [])
    if not isinstance(visible_facts, list) or not all(
        isinstance(item, str) and item.strip() for item in visible_facts
    ):
        raise SystemExit(f"visible_facts must be a string list for frame {frame_id}")
    return {
        "frame_id": frame_id,
        "claim_id": frame.get("claim_id"),
        "purpose": frame.get("purpose"),
        "output": frame.get("output"),
        "readability": readability,
        "visible_facts": [item.strip() for item in visible_facts],
        "supports_claim": supports_claim,
        "note": raw.get("note"),
    }


def write_frame_observations(result_path: Path, raw_items: list[dict]) -> Path:
    frames = load_successful_frames(result_path)
    entries = [validate_observation(raw, frames) for raw in raw_items]
    observations_path = result_path.parent / "observations.json"
    if observations_path.is_file():
        observations = json.loads(observations_path.read_text(encoding="utf-8"))
    else:
        observations = {"version": 1, "observations": []}
    updated_ids = {entry["frame_id"] for entry in entries}
    items = [
        item
        for item in observations.get("observations", [])
        if item.get("frame_id") not in updated_ids
    ]
    items.extend(entries)
    observations["observations"] = items
    write_json_atomic(observations_path, observations)
    return observations_path


def record_frame_observation(args: argparse.Namespace) -> None:
    result_path = args.result.resolve()
    observations_path = write_frame_observations(
        result_path,
        [
            {
                "frame_id": args.frame_id,
                "readability": args.readability,
                "visible_facts": args.visible_fact,
                "supports_claim": args.supports_claim,
                "note": args.note,
            }
        ],
    )
    print(observations_path)


def record_frame_observations(args: argparse.Namespace) -> None:
    input_path = args.input.resolve()
    if not input_path.is_file():
        raise SystemExit(f"Observation draft is unavailable: {input_path}")
    draft = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(draft, dict) or draft.get("version") != 1:
        raise SystemExit("Observation draft must use version 1.")
    raw_items = draft.get("observations")
    if not isinstance(raw_items, list) or not raw_items:
        raise SystemExit("Observation draft must contain observations.")
    if not all(isinstance(item, dict) for item in raw_items):
        raise SystemExit("Each observation draft entry must be an object.")
    frame_ids = [str(item.get("frame_id") or "") for item in raw_items]
    if len(frame_ids) != len(set(frame_ids)):
        raise SystemExit("Observation draft contains duplicate frame_id values.")
    observations_path = write_frame_observations(args.result.resolve(), raw_items)
    print(observations_path)
