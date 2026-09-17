from __future__ import annotations

import argparse
import html
import json
import re
from pathlib import Path
from urllib.parse import urlparse


def text(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def require_string(data: dict, key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"summary document needs a non-empty {key}.")
    return value.strip()


def safe_url(value: object, *, relative: bool = False) -> str:
    raw = str(value or "").strip()
    parsed = urlparse(raw)
    if relative:
        if parsed.scheme or raw.startswith(("/", "\\")) or ".." in Path(raw).parts:
            raise SystemExit(f"Asset path must be relative to the HTML file: {raw}")
        return text(raw)
    if parsed.scheme not in {"http", "https"}:
        raise SystemExit(f"URL must use http or https: {raw}")
    return text(raw)


def timestamp_url(source_url: str, platform: str, seconds: object) -> str | None:
    if not isinstance(seconds, int) or seconds < 0:
        return None
    if platform.lower() not in {"youtube", "bilibili"}:
        return None
    separator = "&" if "?" in source_url else "?"
    return f"{source_url}{separator}t={seconds}s"


def render_block(block: dict, source_dir: Path) -> str:
    kind = block.get("type")
    if kind == "p":
        return f"<p>{text(block.get('text'))}</p>"
    if kind in {"ul", "ol"}:
        items = block.get("items")
        if not isinstance(items, list):
            raise SystemExit(f"{kind} block needs items.")
        return (
            f"<{kind}>"
            + "".join(f"<li>{text(item)}</li>" for item in items)
            + f"</{kind}>"
        )
    if kind == "callout":
        return f'<aside class="callout"><strong>{text(block.get("label") or "辅助理解")}</strong>{text(block.get("text"))}</aside>'
    if kind == "image":
        src = safe_url(block.get("src"), relative=True)
        alt = text(block.get("alt"))
        caption = text(block.get("caption"))
        return f'<figure class="media"><img src="{src}" alt="{alt}" loading="lazy"><figcaption>{caption}</figcaption></figure>'
    if kind == "table":
        headers = block.get("headers")
        rows = block.get("rows")
        if not isinstance(headers, list) or not isinstance(rows, list):
            raise SystemExit("table block needs headers and rows.")
        head = "".join(f"<th>{text(item)}</th>" for item in headers)
        body = "".join(
            "<tr>" + "".join(f"<td>{text(cell)}</td>" for cell in row) + "</tr>"
            for row in rows
            if isinstance(row, list)
        )
        return f'<div class="media"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'
    if kind == "code":
        language = re.sub(r"[^a-zA-Z0-9_+-]", "", str(block.get("language") or ""))
        return f'<pre><code class="language-{language}">{text(block.get("text"))}</code></pre>'
    if kind == "svg":
        relative = str(block.get("src") or "")
        safe_url(relative, relative=True)
        source = (source_dir / relative).resolve()
        if source_dir.resolve() not in source.parents or not source.is_file():
            raise SystemExit(f"SVG asset is unavailable: {relative}")
        svg = source.read_text(encoding="utf-8")
        lowered = svg.lower()
        if not re.search(r"<svg(?:\s|>)", lowered) or any(
            token in lowered
            for token in ("<script", "javascript:", "onload=", "onclick=")
        ):
            raise SystemExit(f"SVG asset contains unsupported markup: {relative}")
        return f'<figure class="media">{svg}<figcaption>{text(block.get("caption"))}</figcaption></figure>'
    raise SystemExit(f"Unsupported content block type: {kind}")


def render_document(data: dict, template: str, source_dir: Path) -> str:
    if data.get("version") != 1:
        raise SystemExit("summary document must use version 1.")
    title = require_string(data, "title")
    source_url = require_string(data, "source_url")
    safe_source = safe_url(source_url)
    platform = require_string(data, "platform")
    subtitle = text(data.get("subtitle"))
    metadata = []
    for label, key in (
        ("平台", "platform"),
        ("作者", "author"),
        ("时长", "duration"),
        ("Transcript", "transcript_source"),
    ):
        value = data.get(key)
        if value:
            metadata.append(f"<span>{label}：<strong>{text(value)}</strong></span>")
    header = f"<h1>{text(title)}</h1>"
    if subtitle:
        header += f'<p class="subtitle">{subtitle}</p>'
    header += f'<div class="meta"><span>原始视频：<a href="{safe_source}">{safe_source}</a></span>{"".join(metadata)}</div>'

    overview = data.get("overview", [])
    if not isinstance(overview, list):
        raise SystemExit("overview must be a list of paragraphs.")
    content = (
        '<section class="overview" id="overview"><h2>概览</h2>'
        + "".join(f"<p>{text(item)}</p>" for item in overview)
        + "</section>"
    )
    toc_items = ['<li><a href="#overview">概览</a></li>']
    sections = data.get("sections")
    if not isinstance(sections, list) or not sections:
        raise SystemExit("summary document needs at least one section.")
    seen = {"overview"}
    for index, section in enumerate(sections, 1):
        if not isinstance(section, dict):
            raise SystemExit("Each section must be an object.")
        section_id = str(section.get("id") or f"section-{index}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", section_id) or section_id in seen:
            raise SystemExit(f"Invalid or duplicate section id: {section_id}")
        seen.add(section_id)
        section_title = require_string(section, "title")
        start = require_string(section, "start")
        end = require_string(section, "end")
        time_label = f"{start}–{end}"
        time_url = timestamp_url(source_url, platform, section.get("start_seconds"))
        time_html = (
            f'<a class="time" href="{safe_url(time_url)}">{text(time_label)}</a>'
            if time_url
            else f'<span class="time">{text(time_label)}</span>'
        )
        blocks = section.get("blocks", [])
        if not isinstance(blocks, list):
            raise SystemExit(f"Section {section_id} blocks must be a list.")
        rendered_blocks = "".join(
            render_block(block, source_dir)
            for block in blocks
            if isinstance(block, dict)
        )
        content += f'<section class="chapter" id="{text(section_id)}"><div class="chapter-head">{time_html}<h2>{text(section_title)}</h2></div>{rendered_blocks}</section>'
        toc_items.append(
            f'<li><a href="#{text(section_id)}">{text(time_label)} {text(section_title)}</a></li>'
        )

    for key, heading, css_class in (
        ("takeaways", "Take-away", "takeaways"),
        ("evidence", "证据与局限", "evidence"),
    ):
        items = data.get(key, [])
        if items:
            if not isinstance(items, list):
                raise SystemExit(f"{key} must be a list.")
            content += (
                f'<section class="{css_class}" id="{key}"><h2>{heading}</h2><ul>'
                + "".join(f"<li>{text(item)}</li>" for item in items)
                + "</ul></section>"
            )
            toc_items.append(f'<li><a href="#{key}">{heading}</a></li>')

    toc = (
        '<nav class="toc" data-pinned="false" aria-label="文章目录"><button class="toc-trigger" type="button" aria-expanded="false">目录</button><div class="toc-panel"><strong>时间线</strong><ol>'
        + "".join(toc_items)
        + "</ol></div></nav>"
    )
    replacements = {
        "{{LANG}}": text(data.get("lang") or "zh-CN"),
        "{{PAGE_TITLE}}": text(title),
        "{{TOC}}": toc,
        "{{HEADER}}": header,
        "{{CONTENT}}": content,
        "{{FOOTER}}": text(
            data.get("footer") or "根据原视频 transcript 与视觉证据整理。"
        ),
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render a structured video summary as standalone HTML"
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.input.resolve()
    data = json.loads(source.read_text(encoding="utf-8"))
    template_path = Path(__file__).parents[1] / "assets" / "html-template" / "page.html"
    rendered = render_document(
        data, template_path.read_text(encoding="utf-8"), source.parent
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(args.output.resolve())


if __name__ == "__main__":
    main()
