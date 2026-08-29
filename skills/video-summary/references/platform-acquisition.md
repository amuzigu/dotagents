# 平台素材与 transcript 获取

根据 URL 只读取对应平台分支。基础素材层包括平台正文与元数据、带时间信息的 transcript；核心结论通过严格视觉门槛后，再增加关键画面作为定向证据。

## 统一字幕选择

`acquire_video.py captions` 先读取完整元数据，再对字幕轨道评分，只下载最高分轨道。原生语言优先于输出语言；翻译发生在最终总结阶段。

评分构成：

- 人工字幕基础分 100；自动字幕基础分 50。
- 与原生语言匹配加 60；平台标记为 Original 加 20。
- 英语、简体中文、繁体中文作为默认回退语言，按顺序获得少量回退加分。
- 翻译轨道减 40。

原生语言依次来自命令行覆盖、平台元数据、Original 字幕标记、唯一人工字幕语言。前三种属于高置信，可自动作为 ASR 语言提示；唯一人工字幕语言属于中等置信，ASR 使用自动检测。

输出文件：

- `selected.transcript.md`：最终选择的时间轴文本。
- `selected.transcript.jsonl`：规范 transcript 数据，每行一个带 segment ID 和毫秒时间的片段。
- `selected.transcript.index.json`：segment 总数、时间范围、连续 chunk 索引和平台章节。
- `caption-selection.json`：原生语言判断、所有候选轨道、评分、选择结果和 ASR 建议。
- `media.info.json`：完整平台元数据。

长 transcript 按索引中的 chunk 连续读取：

```bash
python3 <skill-dir>/scripts/acquire_video.py transcript-slice \
  --input '<work-dir>/selected.transcript.jsonl' \
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
3. 视频没有 CC 字幕时，使用 `audio` 子命令下载音频，再调用可用 ASR。弹幕只用于发现观众关注点，不作为视频陈述的事实依据。
4. 多分 P、合集或番剧按实际条目建立独立时间轴；最终总结明确当前 URL 覆盖的分集或分 P。

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
