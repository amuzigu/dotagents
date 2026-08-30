from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from .core import cookie_arguments


def find_downloaded_media(output: Path, command_output: str, stem: str) -> Path:
    for line in reversed(command_output.splitlines()):
        candidate = Path(line.strip())
        if candidate.is_file():
            return candidate
    excluded = {".json", ".json3", ".part", ".ytdl", ".jpg", ".webp", ".md"}
    candidates = [
        path
        for path in output.glob(f"{stem}.*")
        if path.is_file() and path.suffix.lower() not in excluded
    ]
    if len(candidates) == 1:
        return candidates[0]
    raise SystemExit(f"Unable to identify downloaded {stem} file.")


def serialized_http_headers(headers: dict[str, str]) -> str:
    return "".join(f"{key}: {value}\r\n" for key, value in headers.items())


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
    if any(
        marker in diagnostic
        for marker in (
            "401",
            "403",
            "412",
            "forbidden",
            "unauthorized",
            "expired",
            "precondition failed",
        )
    ):
        return "url-or-auth"
    if any(
        marker in diagnostic
        for marker in ("connection", "timed out", "network is unreachable")
    ):
        return "network"
    return "process-failed"


def refresh_media_info(
    args: argparse.Namespace, output: Path
) -> tuple[dict | None, dict]:
    command = [
        "yt-dlp",
        "--no-playlist",
        "--skip-download",
        "--dump-single-json",
        *cookie_arguments(args),
        args.url,
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
        json.dumps(info, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
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


def merged_http_headers(info: dict, candidate: dict) -> dict[str, str]:
    headers: dict[str, str] = {}
    for source in (info.get("http_headers"), candidate.get("http_headers")):
        if isinstance(source, dict):
            headers.update({str(key): str(value) for key, value in source.items()})
    return headers


def process_attempt(
    strategy: str, outcome: dict, candidate: dict | None = None
) -> dict:
    attempt = {
        "strategy": strategy,
        "status": "success" if outcome["returncode"] == 0 else "failed",
        "elapsed_ms": outcome["elapsed_ms"],
    }
    if candidate:
        attempt.update(candidate)
    if outcome["returncode"] != 0:
        attempt.update(
            {
                "error_code": failure_code(outcome),
                "diagnostic": redact_diagnostic(outcome.get("stderr", "")),
            }
        )
    return attempt
