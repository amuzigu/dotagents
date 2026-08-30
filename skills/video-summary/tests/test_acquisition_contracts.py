import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from video_summary import audio, cli, core, frames


def outcome(returncode=0, stdout="", stderr=""):
    return {
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "elapsed_ms": 1,
        "timed_out": False,
    }


class AcquisitionContractTests(unittest.TestCase):
    def test_update_acquisition_preserves_transcript_and_advances_action(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            core.update_acquisition(
                output,
                requested_url="https://example.test/video",
                transcript={"status": "needs_asr"},
                next_action="download_audio",
            )
            core.update_acquisition(
                output,
                audio={"status": "ready", "path": "media.m4a"},
                next_action="transcribe",
            )
            manifest = json.loads((output / "acquisition.json").read_text())
            self.assertEqual(manifest["transcript"]["status"], "needs_asr")
            self.assertEqual(manifest["audio"]["status"], "ready")
            self.assertEqual(manifest["next_action"], "transcribe")

    def test_cli_routes_every_public_subcommand(self):
        parser = cli.build_parser()
        cases = {
            "captions": ["captions", "https://example.test/v", "--output", "/tmp/v"],
            "audio": ["audio", "https://example.test/v", "--output", "/tmp/v"],
            "frames": [
                "frames",
                "https://example.test/v",
                "--output",
                "/tmp/v",
                "--manifest",
                "/tmp/frames.json",
            ],
            "record-frame-observation": [
                "record-frame-observation",
                "--result",
                "/tmp/result.json",
                "--frame-id",
                "frame-1",
                "--readability",
                "readable",
                "--supports-claim",
                "yes",
            ],
            "transcribe-xai": [
                "transcribe-xai",
                "--audio",
                "/tmp/media.m4a",
                "--output",
                "/tmp/v",
            ],
            "detect-local-asr": ["detect-local-asr", "--output", "/tmp/v"],
            "normalize-asr": [
                "normalize-asr",
                "--input",
                "/tmp/asr.json",
                "--output",
                "/tmp/v",
            ],
            "transcript-slice": [
                "transcript-slice",
                "--input",
                "/tmp/transcript.jsonl",
                "--segment-start",
                "0",
            ],
        }
        for command, arguments in cases.items():
            with self.subTest(command=command):
                parsed = parser.parse_args(arguments)
                self.assertEqual(parsed.command, command)
                self.assertTrue(callable(parsed.func))


class MediaFallbackContractTests(unittest.TestCase):
    def test_bilibili_audio_refreshes_then_uses_ytdlp_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            info = {
                "extractor_key": "BiliBili",
                "webpage_url": "https://www.bilibili.com/video/BV1test",
                "id": "BV1test",
                "title": "Example",
                "duration": 60,
                "formats": [
                    {
                        "format_id": "30280",
                        "url": "https://example.test/audio.m4s",
                        "protocol": "https",
                        "acodec": "aac",
                        "vcodec": "none",
                        "abr": 128,
                    }
                ],
            }
            (output / "media.info.json").write_text(json.dumps(info))
            core.update_acquisition(
                output,
                requested_url=info["webpage_url"],
                transcript={"status": "needs_asr"},
                next_action="download_audio",
            )

            def ytdlp_success(_args, temp_dir, _candidate):
                target = temp_dir / "media.m4a"
                target.write_bytes(b"audio")
                return outcome(stdout=str(target))

            args = Namespace(
                url=info["webpage_url"],
                output=output,
                format="m4a",
                timeout=10,
                cookies_from_browser=None,
            )
            with (
                patch.object(audio, "require_program"),
                patch.object(
                    audio,
                    "ffmpeg_audio_command",
                    return_value=outcome(returncode=1, stderr="HTTP Error 403"),
                ) as direct,
                patch.object(
                    audio,
                    "refresh_media_info",
                    return_value=(info, outcome(stdout=json.dumps(info))),
                ) as refresh,
                patch.object(
                    audio, "yt_dlp_audio_command", side_effect=ytdlp_success
                ) as fallback,
            ):
                audio.acquire_audio(args)

            manifest = json.loads((output / "acquisition.json").read_text())
            report = json.loads((output / "audio" / "result.json").read_text())
            self.assertEqual(direct.call_count, 2)
            self.assertEqual(refresh.call_count, 1)
            self.assertEqual(fallback.call_count, 1)
            self.assertEqual(report["strategy"], "yt_dlp")
            self.assertEqual(manifest["audio"]["status"], "ready")
            self.assertEqual(manifest["next_action"], "transcribe")

    def test_full_video_fallback_respects_download_budget(self):
        request = {
            "id": "diagram",
            "height": 720,
            "image_format": "png",
            "timestamp_ms": 10_000,
        }
        result = {"attempts": []}
        info = {
            "duration": 60,
            "formats": [
                {
                    "format_id": "720p",
                    "url": "https://example.test/video.mp4",
                    "protocol": "https",
                    "vcodec": "h264",
                    "height": 720,
                    "filesize": 2 * 1024 * 1024,
                }
            ],
        }
        args = Namespace(
            max_full_download_mib=1,
            timeout=10,
            cookies_from_browser=None,
            url="https://example.test/video",
        )
        report = frames.full_download_fallback(
            args,
            [(request, result)],
            info,
            Path("/tmp"),
            Path("/tmp"),
        )
        self.assertFalse(report["attempted"])
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["reason"], "over-budget")


if __name__ == "__main__":
    unittest.main()
