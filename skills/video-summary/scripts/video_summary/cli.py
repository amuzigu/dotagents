from __future__ import annotations

import argparse
from pathlib import Path

from .asr import (
    detect_local_asr,
    normalize_asr,
    transcribe_xai,
)
from .audio import acquire_audio
from .captions import acquire_captions
from .core import DEFAULT_FALLBACK_LANGUAGES
from .frames import (
    acquire_frames,
    record_frame_observation,
)
from .transcript import (
    timestamp_ms_argument,
    transcript_slice,
)


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

    audio = subparsers.add_parser(
        "audio", help="Download an audio track for transcription"
    )
    audio.add_argument("url")
    audio.add_argument("--output", type=Path, required=True)
    audio.add_argument("--format", default="m4a")
    audio.add_argument(
        "--timeout",
        type=float,
        default=900,
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
        "--height",
        type=int,
        default=720,
        help="Default maximum height; individual manifest requests may override it",
    )
    frames.add_argument(
        "--image-format",
        choices=("jpg", "png"),
        default="jpg",
        help="Default frame format; individual manifest requests may override it",
    )
    frames.add_argument(
        "--timeout",
        type=float,
        default=60,
        help="Per-process timeout in seconds for metadata, seek, and extraction",
    )
    frames.add_argument(
        "--max-full-download-mib",
        type=float,
        default=200,
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
        "--readability",
        choices=("readable", "partial", "unreadable"),
        required=True,
    )
    observe.add_argument("--visible-fact", action="append", default=[])
    observe.add_argument(
        "--supports-claim",
        choices=("yes", "partial", "no", "uncertain"),
        required=True,
    )
    observe.add_argument("--note")
    observe.set_defaults(func=record_frame_observation)

    xai = subparsers.add_parser(
        "transcribe-xai", help="Transcribe audio with xAI Speech-to-Text"
    )
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
