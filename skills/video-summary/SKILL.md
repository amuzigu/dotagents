---
name: video-summary
description: Summarize YouTube, Bilibili, or X videos into selective, timestamped Markdown or standalone HTML using captions or ASR, adding targeted frames when visuals improve understanding. Use for video summaries, lectures, interviews, reviews, demos, and visual explainers.
---

# Video Summary

把在线视频转化为可独立阅读、可返回原片核查的中文总结。以读者注意力为预算：展开主旨、机制、关键证据与边界，压缩必要背景，省略缺少理解增量的过程信息。

结论以平台语境、带时间信息的 transcript、视频元数据和实际查看过的画面为依据。总结者补充的解释与建议使用清晰标签。

## 工作流程

1. 从请求确定视频 URL、输出语言、关注点、详略模式和格式。沿用用户当前语言；默认使用 `balanced` 和 Markdown。关注点会实质改变总结范围且无法可靠推断时，再向用户确认。
2. 识别平台。读取 [采集核心协议](references/acquisition-core.md)，并只读取匹配 URL 的平台文件：[YouTube](references/platforms/youtube.md)、[Bilibili](references/platforms/bilibili.md) 或 [X](references/platforms/x.md)。运行字幕采集：

   ```bash
   python3 <skill-dir>/scripts/acquire_video.py captions '<video-url>' --output '<work-dir>'
   ```

   首先读取 `<work-dir>/acquisition.json`，按 `next_action` 推进。`transcript.status: ready` 时进入内容处理；`download_audio` 或 `transcribe` 时读取 [本地 ASR](references/local-asr-cli.md) 并完成归一化。安装工具、下载模型、付费 API、登录态和浏览器 Cookie 均在用户明确许可后使用。
3. 在读取 transcript 正文前，读取 [内容筛选与长字幕分流](references/content-selection.md)。依据 `transcript.active.index.json` 选择直接阅读或分块内容地图路径，覆盖全部 active segments，并生成 `<work-dir>/content-map.json`。重点证据存在语义、引文或时间疑点时，读取 [transcript 格式与核查](references/transcript-format.md)，按 `source_cues` 返回 raw transcript 复核。
4. 依据内容地图划分章节。章节边界跟随论点、机制、演示步骤或主题转换。重点章节展开“结论 → 机制或推理 → 证据或例子 → 重要性与边界”；简单内容保持简短。每个重要结论都能回溯到时间区间。
5. 内容地图形成后评估视觉机会。初始计划使用“根据内容地图决定关键画面”的条件式步骤。图表、架构图、代码、UI、实物演示、动作顺序和前后对比具有验证、解释、示例或比较价值时，读取 [视觉证据](references/visual-evidence.md)，创建 `frame-requests.json` 并按点抽帧。用户明确要求关键画面时，按指定范围执行。
6. 按 [写作与输出](#写作与输出) 生成总结。HTML 请求读取 [HTML 输出](references/html-output.md)，生成可独立打开的文件并直接交付。浏览器验收、截图和额外校验由用户明确要求触发。
7. 使用 [内容筛选与长字幕分流](references/content-selection.md) 的终稿审计完成内容验收；视觉和 HTML 分支分别使用对应 reference 的完成条件。

将 `<skill-dir>` 解析为本 `SKILL.md` 所在目录，将 `<work-dir>` 解析为当前任务的临时工作目录。

## 详略模式

- `concise`：2–4 句概览、少量核心章节、可选 Take-away 和简短证据说明。
- `balanced`：完整覆盖主线，展开核心章节，适量保留支撑信息，提供时间戳、必要的辅助理解与可选 Take-away。默认使用此模式。
- `detailed`：增加重要次级论点、代表性案例、技术背景和关键问答；所有内容继续通过删除测试。

模式控制篇幅与证据密度，三种模式都保留核心论点的完整逻辑。

## 写作与输出

默认直接交付 Markdown。开头用 2–4 句说明视频主题、核心结论和适合谁看。正文使用带结论的小标题和时间区间：

```markdown
## 分段总结

### 00:00–03:20 信任问题限制了规模化
用完整段落说明结论、机制、证据、重要性与边界。

### 03:20–08:45 验证闭环让并行工作可控
继续总结……
```

段落承载论证与因果；列表承载步骤、规则、并列要点和比较。小标题直接表达该段结论。抽象概念、压缩推理或必要背景可以加入：

```markdown
> **辅助理解：** 这里可以把作者的方法理解为……这一解释基于 05:10–06:40 的论证。
```

总结者提出的指标、实施步骤、评估方案和回滚策略放入 `基于视频的落地建议`，注明证据连接和适用条件。

按需增加：

- `Take-away`：提炼少量可迁移、可行动且能回溯正文的结论。
- `关键画面`：嵌入清晰截图，标明时间戳与具体可见信息。
- `结构重绘`：简单关系使用 ASCII；复杂流程或层级使用 Mermaid；空间布局与精确标注使用 SVG。
- `证据与局限`：说明 transcript 来源、覆盖缺口、听辨不确定项和画面局限。

采用自然、具体、克制的陈述。观点归属词区分视频作者、受访者、画面观察与总结者。截图标为“原片画面”，图解标为“结构重绘”。

## 完成标准

未观看视频的读者能够理解主线、重点论证、关键证据和适用边界，并能通过时间区间返回原片核查。文档的每一节都用读者注意力换取清楚的理解增量。
