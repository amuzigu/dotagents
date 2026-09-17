# Bilibili 采集

1. 运行 `acquire_video.py captions` 获取字幕、章节和元数据。Bilibili CC 字幕可能需要登录。
2. 登录字幕确有必要时，取得用户授权，再增加 `--cookies-from-browser <browser>`。浏览器参数使用本机实际浏览器名称。
3. `next_action: download_audio` 时运行：

   ```bash
   python3 <skill-dir>/scripts/acquire_video.py audio '<video-url>' --output '<work-dir>'
   ```

   音频采集依次尝试缓存元数据中的音频流、刷新一次元数据、`yt-dlp` 回退。结果和失败分类位于 `audio/result.json`。
4. 音频就绪后读取 [本地 ASR](../local-asr-cli.md)，完成转写和归一化。
5. 多分 P、合集或番剧按实际条目建立独立时间轴；总结明确当前 URL 覆盖的分集或分 P。

弹幕可以提示观众关注点。视频陈述由 transcript、平台正文或实际查看的画面支撑。
