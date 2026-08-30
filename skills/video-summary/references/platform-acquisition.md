# 平台素材与 transcript 获取

根据 URL 只读取对应平台分支。基础素材层包括平台正文与元数据、带时间信息的 transcript；内容地图发现视觉机会后，再增加关键画面用于验证、解释、示例或比较。

## 统一采集接口

字幕、音频和转写子命令都更新 `<work-dir>/acquisition.json`。Agent 每次采集后先读取该文件，并通过 `next_action` 决定下一步。字幕、xAI STT 和本地 ASR 共用同一组 active transcript 文件，后续总结无需识别上游来源。

核心字段：

```json
{
  "version": 1,
  "url": "https://…",
  "platform": "youtube|bilibili|x|unknown",
  "media": {
    "id": "…",
    "title": "…",
    "duration_ms": 1234500,
    "original_language": "en"
  },
  "transcript": {
    "status": "ready|selecting|needs_asr|failed",
    "source": "manual-caption|automatic-caption|local-asr|xai-stt",
    "language": "en",
    "raw_cue_count": 720,
    "segment_count": 500,
    "paths": {
      "markdown": "transcript.active.md",
      "jsonl": "transcript.active.jsonl",
      "index": "transcript.active.index.json",
      "raw_jsonl": "transcript.active.raw.jsonl"
    },
    "compaction": {
      "segments_removed": 220,
      "segment_reduction_ratio": 0.31,
      "character_reduction_ratio": 0.31,
      "jsonl_character_reduction_ratio": 0.42
    }
  },
  "audio": {
    "status": "ready|failed",
    "path": "media.m4a",
    "strategy": "direct_url|direct_url_refreshed|yt_dlp"
  },
  "paths": {
    "media_info": "media.info.json"
  },
  "next_action": "select_transcript|download_audio|transcribe|summarize|resolve_audio_access"
}
```

稳定入口文件：

- `acquisition.json`：当前采集状态、媒体摘要、active transcript、音频结果和下一步动作。
- `transcript.active.md`：适合连续阅读的压缩 transcript。
- `transcript.active.jsonl`：规范 segment 数据，每行包含 segment ID、毫秒时间和必要的原始 cue 范围。
- `transcript.active.index.json`：原始 cue 数、压缩后 segment 数、字符压缩率、时间范围、连续 chunk 索引和平台章节。
- `transcript.active.raw.jsonl`：规范化前的逐 cue 文本，仅用于核查发生合并的具体证据。
- `media.info.json`：完整平台元数据，供诊断和高风险事实复核。
- `audio/result.json`：音频采集尝试、策略、耗时、失败类型和输出文件。

`captions` 找不到可用字幕时会正常写入 `transcript.status: needs_asr` 和 `next_action: download_audio`。Agent 继续执行音频与 ASR 链路。

## Cue 压缩

字幕和 ASR 归一化时自动执行保守压缩：

- 合并时间相邻或重叠的完全重复文本。
- 合并短时间内以前缀或后缀累积扩展的滚动字幕。
- 只有时间实际重叠、词项重叠比例至少 40%，且英文重叠至少 2 个词并达到 8 个字符或中文重叠至少 4 个字时，才拼接滑动窗口。
- 连续残句在句末标点、说话人标记或音效 cue 处停止合并。
- 单个合并结果最长 20 秒、最多 400 字符；距离较远的重复文本保持原样。

发生合并的 segment 带有 `source_cues.start` 和 `source_cues.end`。原始 cue 保存在 `transcript.active.raw.jsonl`，压缩统计保存在 index 和 `acquisition.json`。常规工作流只读取 active transcript。某条重点证据出现语义跳跃、引文异常或时间范围疑点时，按来源范围核查：

```bash
python3 <skill-dir>/scripts/acquire_video.py transcript-slice \
  --input '<work-dir>/transcript.active.raw.jsonl' \
  --cue-start 31 --cue-end 33
```

压缩后的 `segment_count` 用于完整覆盖检查；`raw_cue_count` 只描述平台字幕或 ASR 的原始切分粒度。连续 chunk 同时受 200 segments 和约 12k 字符约束，先达到的上限形成边界，避免语义合并后单个读取块膨胀。

## 字幕选择

`acquire_video.py captions` 先读取完整元数据，再对字幕轨道评分，只下载最高分轨道。原生语言优先于输出语言；翻译发生在最终总结阶段。

评分构成：

- 人工字幕基础分 100；自动字幕基础分 50。
- 与原生语言匹配加 60；平台标记为 Original 加 20。
- 英语、简体中文、繁体中文作为默认回退语言，按顺序获得少量回退加分。
- 翻译轨道减 40。

原生语言依次来自命令行覆盖、平台元数据、Original 字幕标记、唯一人工字幕语言。前三种属于高置信，可自动作为 ASR 语言提示；唯一人工字幕语言属于中等置信，ASR 使用自动检测。

字幕选择详情保存在 `caption-selection.json`，并嵌入 `acquisition.json` 的 transcript details。常规工作流读取 active transcript；需要检查轨道评分和语言判断时再打开详情文件。

长 transcript 按索引中的 chunk 连续读取：

```bash
python3 <skill-dir>/scripts/acquire_video.py transcript-slice \
  --input '<work-dir>/transcript.active.jsonl' \
  --segment-start 0 --segment-end 199
```

后续 chunk 从前一个 `segment_end + 1` 开始，直到覆盖 `segment_count`。

## YouTube

1. 使用 `acquire_video.py captions` 获取官方字幕、自动字幕和元数据。
2. 使用脚本选出的原生人工字幕或原生自动字幕。
3. 字幕覆盖不足时使用 `audio` 子命令下载音频，再调用可用 ASR。

## Bilibili

1. 使用 `acquire_video.py captions` 获取字幕、章节和元数据。Bilibili CC 字幕可能要求登录。
2. 登录字幕确有必要时，先取得用户授权，再为命令增加 `--cookies-from-browser <browser>`。浏览器参数使用本机实际浏览器名称。
3. 视频没有 CC 字幕时，使用 `audio` 子命令下载音频，再调用可用 ASR。音频采集按固定顺序执行：读取缓存的媒体元数据并直连音频流；遇到 URL 过期、403、412 或鉴权失败时刷新一次元数据并重试；直连仍未产出有效文件时调用 `yt-dlp` 回退。弹幕只用于发现观众关注点，不作为视频陈述的事实依据。
4. 多分 P、合集或番剧按实际条目建立独立时间轴；最终总结明确当前 URL 覆盖的分集或分 P。

音频命令结束后读取 `acquisition.json`。成功时 `audio.status` 为 `ready`，`audio.strategy` 说明实际路径；`next_action` 通常为 `transcribe`。诊断细节位于 `audio/result.json`，其中记录每次尝试的输入类型、格式、返回码、耗时和失败分类。临时文件在成功、失败和中断路径中统一清理。

## X

1. 获取帖文正文、作者、发布时间、引用帖和线程上下文。可使用浏览器，或让已安装的 Grok Build CLI 通过 `web_search` 检索公开内容：

   ```bash
   grok -p '读取这个 X URL，返回帖文原文、线程上下文、作者信息、媒体描述和来源链接：<x-url>' \
     --tools 'web_search' --output-format json
   ```

   Grok Build 环境已启用 `web_fetch` 时，可把它加入工具列表以读取搜索结果页面。

2. 使用 `acquire_video.py captions` 检查媒体流中是否存在字幕轨道。字幕轨道缺失时使用 `audio` 子命令下载音频并转写。
3. 环境具有 xAI API Key 且用户批准付费调用时，优先使用 xAI Speech-to-Text；它返回词级时间戳，并自动读取高置信原生语言提示：

   ```bash
   python3 <skill-dir>/scripts/acquire_video.py transcribe-xai \
     --audio '<work-dir>/media.m4a' --output '<work-dir>' \
     --confirm-paid-api
   ```

4. 环境提供 xAI `x_search` 时，可启用视频理解来复核帖文语境和视频画面。`x_search(enable_video_understanding=True)` 适合获得语义理解；时间轴仍以字幕或 STT 返回的时间戳为准。
5. SuperGrok 或 Grok Build 登录提供搜索与推理能力。xAI Speech-to-Text API 使用 `XAI_API_KEY` 独立鉴权；运行前检查变量是否存在并确认用户接受 API 用量。

## 本地或其他 ASR

下载音频：

```bash
python3 <skill-dir>/scripts/acquire_video.py audio '<video-url>' --output '<work-dir>'
```

读取 [local-asr-cli.md](local-asr-cli.md)，探测已安装 CLI、验证命令健康状态并复用已有模型缓存。选择能输出分段或词级时间戳的工具，保留原始转写结果，再通过 `normalize-asr` 转换成统一时间轴。专有名词和数字使用平台正文、章节名及画面文字复核。

## 质量与授权

- 抽查开头、中段、结尾以及数字密集片段，确认时间轴与音频对应。
- 使用 `[听辨不确定]` 标记低置信内容，并保留可供核查的时间区间。
- 第三方 transcript 缺少时间戳时，以章节或抽查播放位置重建近似区间，并标为近似时间。
- 登录态、会员内容、私密视频、浏览器 Cookie 和付费 API 均在明确授权后访问。

完成条件：平台语境已记录；主线具有可追溯的 transcript；素材缺口和访问条件得到明确说明。
