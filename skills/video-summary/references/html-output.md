# HTML 输出

用户指定 HTML 时，先读取 [HTML 文档数据格式](html-document-schema.md)，将总结内容写成 `summary.html.json`，再运行：

```bash
python3 <skill-dir>/scripts/render_summary.py \
  --input '<work-dir>/summary.html.json' \
  --output '<deliver-dir>/index.html'
```

渲染器负责固定版式：白色高对比页面、约 1200px 桌面容器、清晰的字号层级、少量小圆角、相对路径媒体资源，以及左侧脱离正文布局的折叠时间线。时间线通过 hover、键盘聚焦或点击展开，展开时正文位置和换行保持稳定。

写作阶段集中处理内容取舍、章节逻辑、时间区间和证据。生成成功后直接交付 HTML。浏览器验收、截图和视觉微调由用户明确要求触发。
