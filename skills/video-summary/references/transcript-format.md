# Transcript 格式与核查

active transcript 是字幕或 ASR 的统一下游接口。归一化过程保守合并重复 cue、滚动字幕和连续残句，保留说话人、句末、音效和时间边界。

## 文件职责

- `transcript.active.md`：常规短视频与中等长度视频的连续阅读入口。
- `transcript.active.jsonl`：每行一个规范 segment，包含 `segment`、`start_ms`、`end_ms`、格式化时间、文本和 `source_cues`。
- `transcript.active.index.json`：包含 `segment_count`、`character_count`、连续 `chunks`、覆盖时间和平台章节。
- `transcript.active.raw.jsonl`：每行一个原始 cue，保留平台或 ASR 的原始切分粒度。

`segment_count` 用于完整覆盖检查；`raw_cue_count` 描述原始字幕粒度。合并 segment 的 `source_cues.start/end` 指向 raw transcript。

## 读取 segment 范围

```bash
python3 <skill-dir>/scripts/acquire_video.py transcript-slice \
  --input '<work-dir>/transcript.active.jsonl' \
  --segment-start 0 --segment-end 199
```

也可以使用 `--start` 和 `--end` 按时间读取。长 transcript 按 index 中的 chunks 连续覆盖。

## 返回原始 cue 核查

重点证据出现语义跳跃、引文异常或时间范围疑点时，根据 `source_cues` 读取原始范围：

```bash
python3 <skill-dir>/scripts/acquire_video.py transcript-slice \
  --input '<work-dir>/transcript.active.raw.jsonl' \
  --cue-start 31 --cue-end 33
```

常规写作使用 active transcript；raw transcript 服务于局部复核。数字、职位、雇主、时间、数量和直接引语优先执行复核。

完成条件：读取范围与 index 对齐；重点事实保留 segment 或 raw cue 来源；字幕压缩产生的语义疑点已经回到原始 cue 解决或标记。
