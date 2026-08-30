# HTML 文档数据格式

`summary.html.json` 使用 version 1。渲染器对所有文字转义，并只接受明确的内容块类型。

```json
{
  "version": 1,
  "lang": "zh-CN",
  "title": "视频标题",
  "subtitle": "一句话核心结论",
  "source_url": "https://www.youtube.com/watch?v=example",
  "platform": "YouTube",
  "author": "频道或作者",
  "duration": "36:01",
  "transcript_source": "英语人工字幕",
  "overview": ["概览第一段。", "概览第二段。"],
  "sections": [
    {
      "id": "trust-bottleneck",
      "start": "00:00",
      "end": "03:20",
      "start_seconds": 0,
      "title": "信任问题限制了规模化",
      "blocks": [
        {"type": "p", "text": "完整段落。"},
        {"type": "ul", "items": ["并列要点一", "并列要点二"]},
        {"type": "callout", "label": "辅助理解", "text": "压缩后的解释。"},
        {"type": "image", "src": "frames/example-720p.png", "alt": "画面描述", "caption": "原片画面 · 02:10：具体可见信息"}
      ]
    }
  ],
  "takeaways": ["可迁移结论。"],
  "evidence": ["字幕来源与局限。"],
  "footer": "根据原视频整理。"
}
```

## 内容块

- `p`：`text` 完整段落。
- `ul` / `ol`：`items` 字符串数组，用于并列项和步骤。
- `callout`：`label` 与 `text`，用于辅助理解、边界或关键提示。
- `image`：相对于最终 HTML 的 `src`，以及 `alt` 和 `caption`。
- `table`：`headers` 字符串数组与 `rows` 二维数组。
- `code`：`language` 与 `text`。
- `svg`：相对于 JSON 文件的 `src` 与 `caption`。渲染器读取 SVG 并内联；SVG 保持静态，且不含脚本、事件处理器或外部资源。

所有正文均使用纯文本字段。强调通过标题、列表、callout 和表格等语义结构表达。`id` 使用小写字母、数字、连字符或下划线，并在文档内唯一。YouTube 和 Bilibili 的 `start_seconds` 会生成返回原片的时间链接。
