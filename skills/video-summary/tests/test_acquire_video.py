import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

SCRIPT_DIR = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from video_summary import core, frames, transcript


class TranscriptCompactionTests(unittest.TestCase):
    def compact(self, rows):
        return transcript.compact_transcript_rows(rows)

    def test_merges_overlapping_exact_cues(self):
        raw, segments, stats = self.compact(
            [
                (0, 2_000, "Build reliable agents"),
                (1_500, 3_500, "Build reliable agents."),
            ]
        )
        self.assertEqual(len(raw), 2)
        self.assertEqual(
            [segment["text"] for segment in segments], ["Build reliable agents."]
        )
        self.assertEqual(segments[0]["source_cues"], {"start": 0, "end": 1})
        self.assertEqual(stats["merge_counts"]["exact"], 1)

    def test_merges_cumulative_rolling_cues(self):
        _, segments, stats = self.compact(
            [
                (0, 2_000, "We need to build"),
                (2_000, 4_000, "We need to build reliable agents"),
            ]
        )
        self.assertEqual(
            [segment["text"] for segment in segments],
            [
                "We need to build reliable agents",
            ],
        )
        self.assertEqual(stats["merge_counts"]["rolling-containment"], 1)

    def test_merges_high_confidence_overlapping_windows(self):
        _, segments, stats = self.compact(
            [
                (0, 2_500, "We need reliable agents"),
                (2_000, 4_000, "reliable agents require evaluations"),
            ]
        )
        self.assertEqual(
            [segment["text"] for segment in segments],
            [
                "We need reliable agents require evaluations",
            ],
        )
        self.assertEqual(stats["merge_counts"]["rolling-overlap"], 1)

    def test_groups_adjacent_fragments_without_deleting_repeated_subject(self):
        _, segments, stats = self.compact(
            [
                (0, 2_000, "We need reliable agents"),
                (2_000, 4_000, "Reliable agents require evaluations"),
            ]
        )
        self.assertEqual(
            [segment["text"] for segment in segments],
            [
                "We need reliable agents Reliable agents require evaluations",
            ],
        )
        self.assertEqual(stats["merge_counts"]["sentence-continuation"], 1)

    def test_terminal_punctuation_keeps_sentence_boundary(self):
        _, segments, stats = self.compact(
            [
                (0, 2_000, "We need reliable agents."),
                (2_000, 4_000, "Evaluations make them dependable."),
            ]
        )
        self.assertEqual(len(segments), 2)
        self.assertEqual(stats["segments_removed"], 0)

    def test_speaker_marker_keeps_turn_boundary(self):
        _, segments, _ = self.compact(
            [
                (0, 2_000, ">> First speaker explains the problem"),
                (2_000, 4_000, ">> Second speaker gives an example"),
            ]
        )
        self.assertEqual(len(segments), 2)

    def test_sentence_grouping_respects_duration_cap(self):
        _, segments, _ = self.compact(
            [
                (0, 11_000, "A long fragment without punctuation"),
                (11_000, 22_000, "continues beyond the duration budget"),
            ]
        )
        self.assertEqual(len(segments), 2)

    def test_merges_overlapping_chinese_windows(self):
        _, segments, stats = self.compact(
            [
                (0, 2_500, "我们需要构建可靠的智能体"),
                (2_000, 4_000, "可靠的智能体需要持续评估"),
            ]
        )
        self.assertEqual(
            [segment["text"] for segment in segments],
            [
                "我们需要构建可靠的智能体需要持续评估",
            ],
        )
        self.assertEqual(stats["merge_counts"]["rolling-overlap"], 1)

    def test_preserves_distant_repeated_cues(self):
        _, segments, stats = self.compact(
            [
                (0, 1_000, "Build reliable agents"),
                (5_000, 6_000, "Build reliable agents"),
            ]
        )
        self.assertEqual(len(segments), 2)
        self.assertEqual(stats["segments_removed"], 0)

    def test_writes_raw_provenance_and_compaction_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            transcript_path = transcript.write_transcript(
                output / "captions.srt",
                [
                    (0, 2_000, "Build reliable agents"),
                    (1_500, 3_500, "Build reliable agents"),
                ],
            )
            jsonl, index_path = core.transcript_bundle_paths(transcript_path)
            raw_path = core.transcript_raw_path(transcript_path)
            index = json.loads(index_path.read_text())
            self.assertEqual(len(raw_path.read_text().splitlines()), 2)
            self.assertEqual(len(jsonl.read_text().splitlines()), 1)
            self.assertEqual(index["raw_cue_count"], 2)
            self.assertEqual(index["segment_count"], 1)
            self.assertEqual(index["character_count"], len("Build reliable agents"))
            self.assertGreater(index["compaction"]["character_reduction_ratio"], 0)
            self.assertGreater(
                index["compaction"]["jsonl_character_reduction_ratio"], 0
            )

    def test_chunk_index_respects_character_budget(self):
        segments = [
            {
                "segment": index,
                "start_ms": index * 1_000,
                "end_ms": (index + 1) * 1_000,
                "text": "x" * 4_100,
            }
            for index in range(4)
        ]
        chunks = transcript.transcript_chunks(segments)
        self.assertEqual(
            [(chunk["segment_start"], chunk["segment_end"]) for chunk in chunks],
            [
                (0, 1),
                (2, 3),
            ],
        )
        self.assertTrue(
            all(
                chunk["character_count"] <= core.TRANSCRIPT_CHUNK_CHARACTERS
                for chunk in chunks
            )
        )

    def test_active_transcript_copies_raw_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            transcript_path = transcript.write_transcript(
                output / "captions.srt",
                [
                    (0, 2_000, "We need to build"),
                    (2_000, 4_000, "We need to build reliable agents"),
                ],
            )
            _, manifest = transcript.publish_active_transcript(
                output,
                transcript_path,
                source="automatic-caption",
                language="en",
                original_language="en",
                media_info={"duration": 4},
            )
            self.assertTrue((output / "transcript.active.raw.jsonl").is_file())
            self.assertEqual(manifest["raw_cue_count"], 2)
            self.assertEqual(manifest["segment_count"], 1)
            self.assertEqual(
                manifest["character_count"], len("We need to build reliable agents")
            )
            self.assertEqual(
                manifest["paths"]["raw_jsonl"], "transcript.active.raw.jsonl"
            )
            self.assertEqual(manifest["compaction"]["segments_removed"], 1)

    def test_transcript_slice_reads_raw_cue_ranges(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            transcript_path = transcript.write_transcript(
                output / "captions.srt",
                [
                    (0, 2_000, "Build reliable agents"),
                    (1_500, 3_500, "Build reliable agents"),
                ],
            )
            target = output / "raw-slice.md"
            transcript.transcript_slice(
                type(
                    "Args",
                    (),
                    {
                        "input": core.transcript_raw_path(transcript_path),
                        "start": None,
                        "end": None,
                        "segment_start": None,
                        "segment_end": None,
                        "cue_start": 0,
                        "cue_end": 1,
                        "output": target,
                    },
                )()
            )
            rendered = target.read_text()
            self.assertIn("Cues: `0–1`", rendered)
            self.assertEqual(rendered.count("Build reliable agents"), 2)


class FrameRequestTests(unittest.TestCase):
    def write_manifest(self, directory, value):
        path = Path(directory) / "frame-requests.json"
        path.write_text(json.dumps(value))
        return path

    def test_accepts_visual_value_without_strict_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_manifest(
                directory,
                {
                    "version": 2,
                    "requests": [
                        {
                            "id": "ui-example",
                            "claim_id": "support-02",
                            "timestamp_ms": 12_000,
                            "purpose": "example",
                            "expected_observation": "展示设置面板中的选项排列",
                        }
                    ],
                },
            )
            requests = frames.load_frame_requests(
                path,
                Namespace(height=720, image_format="jpg"),
            )
            self.assertEqual(requests[0]["purpose"], "example")
            self.assertNotIn("gate", requests[0])

    def test_requires_visual_purpose(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_manifest(
                directory,
                {
                    "version": 2,
                    "requests": [
                        {
                            "id": "ui-example",
                            "claim_id": "support-02",
                            "timestamp_ms": 12_000,
                            "expected_observation": "展示设置面板中的选项排列",
                        }
                    ],
                },
            )
            with self.assertRaises(SystemExit):
                frames.load_frame_requests(
                    path,
                    Namespace(height=720, image_format="jpg"),
                )

    def test_rejects_version_one_gate_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_manifest(
                directory,
                {
                    "version": 1,
                    "requests": [
                        {
                            "id": "legacy",
                            "claim_id": "claim-01",
                            "timestamp_ms": 1_000,
                            "gate": {
                                "core": True,
                                "irreplaceable": True,
                                "information_gain": True,
                                "readable_expected": True,
                            },
                            "expected_observation": "旧门槛",
                        }
                    ],
                },
            )
            with self.assertRaises(SystemExit):
                frames.load_frame_requests(
                    path,
                    Namespace(height=720, image_format="jpg"),
                )


if __name__ == "__main__":
    unittest.main()
