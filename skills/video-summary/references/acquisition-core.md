# 采集核心协议

所有平台共用 `acquire_video.py` 和 `<work-dir>/acquisition.json`。字幕、音频和转写子命令都会更新该状态文件；每次采集后先读取它，再根据 `next_action` 选择下一步。

## 统一状态

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
    "segment_count": 500,
    "character_count": 42000,
    "paths": {
      "markdown": "transcript.active.md",
      "jsonl": "transcript.active.jsonl",
      "index": "transcript.active.index.json",
      "raw_jsonl": "transcript.active.raw.jsonl"
    }
  },
  "audio": {
    "status": "ready|failed",
    "path": "media.m4a",
    "strategy": "direct_url|direct_url_refreshed|yt_dlp"
  },
  "paths": {"media_info": "media.info.json"},
  "next_action": "select_transcript|download_audio|transcribe|summarize|resolve_audio_access"
}
```

`captions` 找到可用字幕时发布统一 active transcript，并把 `next_action` 设为 `summarize`。字幕缺失时写入 `transcript.status: needs_asr` 和 `next_action: download_audio`；继续执行音频与 ASR 链路。

## 字幕选择

字幕轨道按照以下证据排序：原生语言、人工轨道、平台 Original 标记、默认回退语言、翻译轨道。原生语言优先；翻译在最终总结阶段完成。轨道选择结果位于 `caption-selection.json`，常规总结直接使用 active transcript。

原生语言依次取自命令行覆盖、平台元数据、Original 字幕标记和唯一人工字幕语言。高置信语言可以作为 ASR 提示；中等置信信息交给 ASR 自动检测。

## 稳定输出

- `acquisition.json`：当前状态、媒体摘要、active transcript、音频结果和下一步动作。
- `transcript.active.md`：适合连续阅读的 transcript。
- `transcript.active.jsonl`：带毫秒时间和来源范围的规范 segments。
- `transcript.active.index.json`：segment 数、字符数、连续 chunks 和平台章节。
- `transcript.active.raw.jsonl`：逐 cue 来源，用于重点证据核查。
- `media.info.json`：完整平台元数据和媒体格式。
- `audio/result.json`：音频采集策略、耗时和诊断。

字段语义与核查命令见 [transcript 格式与核查](transcript-format.md)。

## 质量与授权

- 抽查开头、中段、结尾以及数字密集片段，确认时间轴与音频对应。
- 使用 `[听辨不确定]` 标记低置信内容，并保留核查时间区间。
- 第三方 transcript 缺少时间戳时，以章节或抽查播放位置重建近似区间，并标明近似时间。
- 登录态、会员内容、私密视频、浏览器 Cookie 和付费 API 均在用户明确授权后访问。

完成条件：`acquisition.json` 给出明确的 `next_action`；`ready` transcript 具有时间信息、来源记录和可读取路径；访问条件与素材缺口得到说明。
