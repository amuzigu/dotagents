---
name: video-summary
description: Summarize a YouTube, Bilibili, or X video URL into a timestamped account grounded in platform text, captions or speech transcription, plus frames when visuals materially affect understanding. Use for lectures, interviews, reviews, demonstrations, threads with video, and visual explainers; return Markdown by default or standalone HTML when requested.
---

# Video Summary

把 YouTube、Bilibili 或 X 视频转化为一份可独立阅读、可快速定位原片的中文总结。结论以平台正文、字幕或语音转写、视频元数据和实际查看过的画面为依据；将推断明确标为推断。

## 工作流程

1. 确认视频 URL、用户期望的输出语言、关注点、详细程度和输出格式。用户未指定时，沿用用户当前语言，提供中等篇幅的综合总结，并采用 Markdown。
2. 识别平台并读取 [references/platform-acquisition.md](references/platform-acquisition.md)，按平台取得正文、元数据和带时间信息的 transcript。首先让脚本识别原生语言、区分人工与自动字幕并选择最佳轨道：

   ```bash
   python3 <skill-dir>/scripts/acquire_video.py captions '<video-url>' --output '<work-dir>'
   ```

   将 `<skill-dir>` 解析为本 `SKILL.md` 所在目录，将 `<work-dir>` 解析为当前任务的临时工作目录。

   阅读 `selected.transcript.md`、`caption-selection.json` 和 `media.info.json`。平台正文与线程上下文用于解释视频语境。总结阶段再把原生语言 transcript 转换为用户语言。
3. `caption-selection.json` 建议 ASR、内嵌字幕缺失或质量明显影响理解时，下载音频并读取 [references/local-asr-cli.md](references/local-asr-cli.md)。先探测健康的本地 CLI 和已有模型缓存，再选择转写工具并归一化输出。将高置信原生语言作为 ASR 提示；其余情况使用自动语言检测。安装工具、下载模型、使用付费 API、登录态或浏览器 Cookie 前取得用户明确许可。在总结中说明 transcript 来源与局限。
4. 通读完整字幕，先建立主题边界，再写总结。边界跟随论点、演示步骤、话题转换或章节变化，并以字幕时间为基础校准区间。将片段区分为核心、支撑和过渡：核心论点、关键方法、证据链与重要演示获得主要篇幅；支撑材料保留必要证据；过渡内容简洁连接上下文。每个重要结论都能追溯到对应区间。
5. 完整读取字幕并形成内容大纲后，执行视觉充分性判断。初始计划只写“评估字幕是否留下视觉信息缺口”；在评估列出具体缺口和对应时间范围后，再把下载视频与抽帧展开为执行步骤。字幕已经完整支撑视频主线、论证和例子时，直接进入写作。关键结论依赖界面操作、实物演示、图表数值、代码、公式、地图、前后对比或无解说画面时，读取 [references/visual-evidence.md](references/visual-evidence.md)，针对缺口抽取并实际查看关键帧，再把画面证据并入对应段落。X Search 视频理解等远程结果可作为复核证据，并标明来源。
6. 交付前核对：覆盖视频主线、时间区间连续且合理、人物观点归属清晰、数字和专有名词与素材一致、每张截图都对应明确的视觉信息缺口、视觉描述来自已查看画面、推断带有标记。

## 写作与输出

默认直接交付 Markdown。用户指定 HTML 时，读取 [references/html-output.md](references/html-output.md)，生成可独立打开的 `.html` 文件，并在交付前渲染检查。

开头用 2–4 句给出视频主题、核心结论和适合谁看。随后按内容密度组织时间段：

```markdown
## 分段总结

### 00:00–03:20 开场与问题定义
用完整段落说明这一段讲了什么、为什么重要，以及关键证据或例子。

### 03:20–08:45 核心方法
继续总结……
```

篇幅跟随信息价值。重点段落完整说明作者提出了什么、依据或推理是什么、为什么重要，以及适用条件、影响或例子。内容存在抽象概念、压缩推理、隐含背景或画面信息时，可在对应段落后加入明确标注的 `辅助理解`，用必要背景、类比、步骤重建或影响分析降低理解门槛。例如：

```markdown
> **辅助理解：** 这里可以把作者的方法理解为……这一解释基于 05:10–06:40 的论证和画面中的流程图。
```

正文先准确呈现视频内容；`辅助理解` 随后提供总结者的解释，并将推断、通用背景和视频原始主张清楚归属。每个辅助理解都对应一个具体理解难点，保留与原片证据的连接。

按需增加以下内容：

- `关键结论`：提炼可复用的观点、步骤或判断。
- `Take-away`：视频包含可迁移的方法、决策原则、实践建议或认知更新时，提炼读者可以带走并应用的内容，同时说明适用条件。`关键结论` 反映视频作者的主张；`Take-away` 面向读者形成可行动的理解。
- `关键画面`：嵌入真正帮助理解的截图，标明时间戳，并解释画面解决了哪个文字盲区。
- `结构图`：关系、流程或层级较复杂时使用 Mermaid；空间布局、精确标注或需要独立图像文件时使用 SVG。
- `局限与不确定性`：注明自动字幕误差、缺失段落、听辨不确定项和基于画面的合理推断。

采用直接、自然、具体的陈述。每段围绕一个中心意思展开，保留必要上下文和因果关系。使用观点归属词区分视频作者、受访者与总结者。让截图和图表承担真实的信息增量，并在正文中解释其意义。

Mermaid 与 SVG 表达总结者对内容的结构化理解。将重绘图标为“结构重绘”，将视频截图标为“原片画面”。

## 完成标准

交付物应让未观看视频的读者理解主线、重点内容的完整论证、关键依据与影响理解的视觉信息，也能借助时间区间迅速返回原片核查。字幕充分的视频以文字完成；存在视觉缺口的视频使用最少且足够的目标截图。辅助理解具有清楚归属，Take-away 具有实际信息增量和适用边界。素材不足以支持完整总结时，清楚列出已覆盖范围和仍缺失的部分。
