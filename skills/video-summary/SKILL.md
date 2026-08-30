---
name: video-summary
description: Summarize a YouTube, Bilibili, or X video into a selective, timestamped Markdown or standalone HTML document grounded in captions or ASR, with frames reserved for core conclusions that require readable visual evidence. Use for lectures, interviews, reviews, demonstrations, and visual explainers where readers need the main argument, adequate depth, and minimal peripheral detail.
---

# Video Summary

把 YouTube、Bilibili 或 X 视频转化为一份可独立阅读、可快速定位原片的中文总结。以读者注意力为稀缺资源：完整展开主旨、机制、关键证据与边界；压缩必要背景；省略缺少理解增量的插曲、寒暄和过程记录。

结论以平台正文、字幕或语音转写、视频元数据和实际查看过的画面为依据。总结者补充的解释与建议使用清晰标签。

## 工作流程

1. 确认视频 URL、输出语言、关注点、详略模式和格式。沿用用户当前语言；默认使用 `balanced` 模式和 Markdown。
2. 识别平台并读取 [references/platform-acquisition.md](references/platform-acquisition.md)，取得平台语境、元数据和带时间信息的 transcript：

   ```bash
   python3 <skill-dir>/scripts/acquire_video.py captions '<video-url>' --output '<work-dir>'
   ```

   将 `<skill-dir>` 解析为本 `SKILL.md` 所在目录，将 `<work-dir>` 解析为当前任务的临时工作目录。首先读取唯一入口 `acquisition.json`。当 `transcript.status` 为 `ready` 时，按 `transcript.paths` 读取 `transcript.active.index.json`、`transcript.active.jsonl` 或适合连续阅读的 `transcript.active.md`。需要完整平台元数据或采集诊断时，再沿 `paths.media_info` 和各结果文件读取细节。总结阶段把原生语言 transcript 转换成用户语言。
3. 按 `acquisition.json` 的 `next_action` 推进。值为 `download_audio` 或 `transcribe` 时，下载音频并读取 [references/local-asr-cli.md](references/local-asr-cli.md)。复用健康的本地 CLI 与完整模型缓存。安装工具、下载模型、使用付费 API、登录态或浏览器 Cookie 前取得用户明确许可。ASR 归一化后重新读取 `acquisition.json`，确认 transcript 已切换为 `ready`。
4. 通读完整 transcript，写出一句“视频主问题”和 3–7 条候选核心结论。长 transcript 根据 `transcript.active.index.json` 的连续 chunks 调用 `transcript-slice` 读取 `transcript.active.jsonl`，逐块覆盖全部 segment；每个 segment 恰好进入一次阅读范围。随后读取 [references/content-selection.md](references/content-selection.md)，按其注意力预算、内容分级和删除测试形成内容地图。完成条件：全部 transcript segment 已覆盖；每条核心结论都有时间范围和证据；支撑信息有明确用途；外围内容已从默认大纲移除。
5. 依据内容地图划分章节。章节边界跟随论点、机制、演示步骤或主题转换。重点章节展开“结论 → 机制或推理 → 证据或例子 → 重要性与边界”；简单内容保持简短。每个重要结论都能回溯到对应时间区间。
6. 完整读取 transcript 并形成内容地图后，执行严格视觉门槛。初始计划只保留“评估核心结论是否强依赖视觉证据”这一条件式步骤。默认总结只有在核心性、不可替代性、信息增量和预期可读性同时成立时，才读取 [references/visual-evidence.md](references/visual-evidence.md)，创建 `frame-requests.json` 并调用远程按点抽帧。其余内容直接使用 transcript 与文字解释。用户明确要求关键画面时，按指定范围执行，并继续检查来源与清晰度。
7. 按 [写作与输出](#写作与输出) 生成总结。用户指定 HTML 时，读取 [references/html-output.md](references/html-output.md)，生成可独立打开的 `.html` 文件并直接交付。浏览器验收、截图和额外校验仅在用户明确要求时执行。
8. 执行终稿审计：主线与重点完整、外围信息克制、时间区间合理、观点归属清楚、数字和专有名词经交叉核对、总结者扩展带标签、每张截图通过四项视觉门槛并具有查看记录、结论与 Take-away 各自提供信息增量。

## 详略模式

- `concise`：2–4 句概览、少量核心章节、可选 Take-away 和简短证据说明。适合快速阅读。
- `balanced`：完整覆盖主线，展开所有核心章节，适量保留支撑信息，提供时间戳、必要的辅助理解与可选 Take-away。作为默认模式。
- `detailed`：在 `balanced` 基础上增加重要次级论点、更多代表性案例、技术背景和关键问答；外围内容仍需通过删除测试。

模式控制篇幅与证据密度，核心论点在三种模式下都保留完整逻辑。

## 写作与输出

默认直接交付 Markdown。开头用 2–4 句说明视频主题、核心结论和适合谁看。正文使用带结论的小标题和时间区间：

```markdown
## 分段总结

### 00:00–03:20 开场与问题定义
用完整段落说明这一段讲了什么、为什么重要，以及关键证据或例子。

### 03:20–08:45 核心方法
继续总结……

……
```

段落承载论证与因果；列表承载步骤、规则、并列要点和比较。小标题优先表达该段结论，便于扫读。主持人的普通提问直接转写为答案主题；只有提问本身揭示关键反例、约束或争议时，才保留问题含义。

内容出现抽象概念、压缩推理或必要背景时，可在对应段落后加入：

```markdown
> **辅助理解：** 这里可以把作者的方法理解为……这一解释基于 05:10–06:40 的论证。
```

`辅助理解` 用于解释概念、重建隐含推理或连接背景知识。总结者进一步提出的指标、实施步骤、评估方案或回滚策略放入 `基于视频的落地建议`，注明它们属于总结者延伸，并说明依据和适用条件。

按需增加：

- `Take-away`：视频包含可迁移的方法、决策原则或实践价值时，提炼少量可行动结论。每条都可追溯到正文，并补充适用边界。
- `关键画面`：核心结论强依赖视觉证据时，嵌入通过四项视觉门槛的清晰截图，并标明时间戳与信息增量。
- `结构重绘`：简单图示可以使用 ASCII；关系、流程或层级复杂时使用 Mermaid；空间布局、精确标注或独立图像文件使用 SVG。
- `证据与局限`：说明 transcript 来源、自动字幕误差、覆盖缺口、听辨不确定项和画面局限。

采用自然、具体、克制的陈述。每段围绕一个中心意思，保留推动理解所需的上下文和因果。观点归属词区分视频作者、受访者、画面观察与总结者。截图标为“原片画面”，图解标为“结构重绘”。

## 完成标准

未观看视频的读者能够理解主线、重点内容的完整论证、关键证据和适用边界，并能通过时间区间返回原片核查。文档的每一节都消耗读者注意力来换取清楚的理解增量。文字作为默认表达；核心结论通过四项视觉门槛后，使用最少且足够的清晰画面。每张交付截图在观察记录中具有实际可见事实。辅助理解与落地建议具有明确归属，Take-away 简洁、可追溯且可应用。
