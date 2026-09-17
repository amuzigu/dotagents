# X 采集

X 总结同时处理帖文语境与视频内容，并记录来源层：

- `A`：帖文正文、作者、发布时间、引用帖和媒体描述。
- `B`：人工字幕或 ASR transcript。
- `C`：实际查看的关键帧。
- `D`：X Search、Grok 或远程视频理解结果。

分段总结中的口播主张由 B 支撑，视觉主张由 C 支撑。A 用于发布语境和作者定位。D 用于发现线索与交叉复核，并在引用时标明来源。数字、职位、雇主、时间和数量记录来源层；来源冲突时分别陈述各自信息。

## 采集步骤

1. 获取帖文正文、作者、发布时间、引用帖和线程上下文。可以使用浏览器，或让已安装的 Grok Build CLI 通过 `web_search` 检索公开内容：

   ```bash
   grok -p '读取这个 X URL，返回帖文原文、线程上下文、作者信息、媒体描述和来源链接：<x-url>' \
     --tools 'web_search' --output-format json
   ```

   Grok Build 环境启用 `web_fetch` 时，可以将其加入工具列表读取结果页面。
2. 运行 `acquire_video.py captions` 检查字幕轨道。字幕缺失时运行 `audio` 子命令下载音频。
3. 音频就绪后选择本地 ASR。环境具有 `XAI_API_KEY` 且用户批准付费调用时，可以使用 xAI Speech-to-Text：

   ```bash
   python3 <skill-dir>/scripts/acquire_video.py transcribe-xai \
     --audio '<work-dir>/media.m4a' --output '<work-dir>' \
     --confirm-paid-api
   ```

4. 环境提供 xAI `x_search` 时，可以启用视频理解复核帖文语境和画面。时间轴以字幕或 STT 时间戳为准。

SuperGrok 或 Grok Build 登录提供搜索与推理能力。xAI Speech-to-Text API 使用 `XAI_API_KEY` 独立鉴权；运行前确认变量存在并取得用户对 API 用量的许可。
