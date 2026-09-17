import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import render_summary


class RenderSummaryTests(unittest.TestCase):
    def test_renders_semantic_document_with_fixed_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            source_dir = Path(directory)
            template = (
                Path(__file__).parents[1] / "assets" / "html-template" / "page.html"
            ).read_text(encoding="utf-8")
            data = {
                "version": 1,
                "lang": "zh-CN",
                "title": "Agents < Reliability",
                "subtitle": "A compact summary",
                "source_url": "https://www.youtube.com/watch?v=abc",
                "platform": "YouTube",
                "author": "Example",
                "duration": "10:00",
                "transcript_source": "English captions",
                "overview": ["Main point"],
                "sections": [
                    {
                        "id": "verification-loop",
                        "start": "00:10",
                        "end": "02:00",
                        "start_seconds": 10,
                        "title": "Verification closes the loop",
                        "blocks": [
                            {"type": "p", "text": "Use <checks> consistently."},
                            {"type": "ul", "items": ["Plan", "Verify"]},
                            {
                                "type": "callout",
                                "label": "辅助理解",
                                "text": "Treat evaluation as feedback.",
                            },
                            {
                                "type": "table",
                                "headers": ["Stage", "Result"],
                                "rows": [["Plan", "Scope"]],
                            },
                            {
                                "type": "image",
                                "src": "frames/loop.png",
                                "alt": "Loop diagram",
                                "caption": "Original frame · 00:10",
                            },
                        ],
                    }
                ],
                "takeaways": ["Verification enables scale."],
                "evidence": ["Caption-backed."],
            }
            rendered = render_summary.render_document(data, template, source_dir)
            self.assertIn("<!doctype html>", rendered)
            self.assertIn("Agents &lt; Reliability", rendered)
            self.assertIn("Use &lt;checks&gt; consistently.", rendered)
            self.assertIn("?v=abc&amp;t=10s", rendered)
            self.assertIn('class="toc"', rendered)
            self.assertIn("frames/loop.png", rendered)
            self.assertIn("<table>", rendered)
            self.assertNotIn("{{CONTENT}}", rendered)

    def test_rejects_escaping_asset_path(self):
        with self.assertRaises(SystemExit):
            render_summary.safe_url("../secret.png", relative=True)


if __name__ == "__main__":
    unittest.main()
