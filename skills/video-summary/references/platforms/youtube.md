# YouTube 采集

1. 运行 `acquire_video.py captions` 获取平台元数据、官方字幕和自动字幕候选。
2. 使用 `acquisition.json` 发布的原生人工字幕或原生自动字幕。
3. `next_action: download_audio` 时运行：

   ```bash
   python3 <skill-dir>/scripts/acquire_video.py audio '<video-url>' --output '<work-dir>'
   ```

4. 音频就绪后读取 [本地 ASR](../local-asr-cli.md)，完成转写和归一化。

视频章节保存在 `transcript.active.index.json`。平台标题、频道、发布时间和时长用于说明来源语境；视频陈述由 transcript 或实际查看的画面支撑。
