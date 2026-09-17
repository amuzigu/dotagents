from __future__ import annotations

import argparse
import http.client
import json
import mimetypes
import os
import platform
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from .core import (
    ACQUISITION_MANIFEST,
    LOCAL_ASR_CLIS,
    load_json_object,
    normalize_caption_text,
    normalize_language,
    update_acquisition,
    write_json_atomic,
)
from .transcript import (
    group_words,
    publish_active_transcript,
    rows_from_asr_json,
    rows_from_json3,
    rows_from_timed_text,
    write_transcript,
)


def multipart_parts(
    boundary: str, fields: list[tuple[str, str]], audio: Path
) -> tuple[bytes, bytes]:
    chunks: list[bytes] = []
    for name, value in fields:
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )
    filename = audio.name.replace('"', "_")
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    chunks.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{filename}"\r\nContent-Type: {media_type}\r\n\r\n'.encode()
    )
    return b"".join(chunks), f"\r\n--{boundary}--\r\n".encode()


def call_xai_stt(audio: Path, fields: list[tuple[str, str]], api_key: str) -> dict:
    boundary = f"codex-{uuid.uuid4().hex}"
    prefix, suffix = multipart_parts(boundary, fields, audio)
    connection = http.client.HTTPSConnection("api.x.ai", timeout=600)
    connection.putrequest("POST", "/v1/stt")
    connection.putheader("Authorization", f"Bearer {api_key}")
    connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
    connection.putheader(
        "Content-Length", str(len(prefix) + audio.stat().st_size + len(suffix))
    )
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


def infer_asr_language(output: Path) -> tuple[str | None, str]:
    acquisition = load_json_object(output / ACQUISITION_MANIFEST)
    transcript = acquisition.get("transcript")
    if isinstance(transcript, dict) and (
        language := normalize_language(transcript.get("asr_language_hint"))
    ):
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
        raise SystemExit(
            "Pass --confirm-paid-api after the user approves xAI API usage."
        )
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
        language, language_source = (
            normalize_language(args.language),
            "command-line override",
        )
    else:
        language, language_source = infer_asr_language(output)
    if language:
        fields.extend([("language", language), ("format", "true")])
    if args.diarize:
        fields.append(("diarize", "true"))
    fields.extend(("keyterm", keyterm) for keyterm in args.keyterm)
    result = call_xai_stt(audio, fields, api_key)
    raw_path = output / "xai-stt.json"
    raw_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    request_path = output / "xai-stt-request.json"
    request_path.write_text(
        json.dumps(
            {
                "requested_language": language,
                "language_source": language_source,
                "diarize": args.diarize,
                "keyterms": args.keyterm,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
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
    result.update(
        {
            "status": "ready" if probe.returncode == 0 or help_rendered else "broken",
            "returncode": probe.returncode,
            "diagnostic": diagnostic,
        }
    )
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
            entry.name
            for entry in root.iterdir()
            if entry.is_dir()
            and entry.name.startswith("models--")
            and "whisper" in entry.name.lower()
        )
    patterns = ("*.pt",) if kind == "whisper" else ("ggml-*.bin", "ggml-*.gguf")
    return sorted(
        {
            entry.name
            for pattern in patterns
            for entry in root.glob(pattern)
            if entry.is_file()
        }
    )


def cli_has_compatible_cache(command: str, cache_report: list[dict]) -> bool:
    models_by_kind = {
        kind: [
            model
            for cache in cache_report
            if cache["kind"] == kind
            for model in cache["models"]
        ]
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
        cli["compatible_cache_entry_found"] = cli_has_compatible_cache(
            cli["command"], caches
        )

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
            cli
            for command in preferred_order
            for cli in ready
            if cli["command"] == command and cli["compatible_cache_entry_found"]
        ),
        None,
    )
    cached_candidate = next(
        (
            cli
            for command in preferred_order
            for cli in clis
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
        "preferred_cached_candidate": cached_candidate["command"]
        if cached_candidate
        else None,
        "healthy_cli_count": len(ready),
        "repair_or_install_for_cached_model": recommendation is None
        and cached_candidate is not None,
        "download_model_for_healthy_cli": recommendation is None and bool(ready),
    }
    report_path = output / "local-asr-capabilities.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
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
    write_json_atomic(
        manifest_path,
        {
            "source": str(source),
            "format": suffix.removeprefix("."),
            "segments": len(rows),
            "language": language,
            "language_source": language_source,
        },
    )
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
