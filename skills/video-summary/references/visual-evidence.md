# 视觉证据与辅助图

仅在完整读取 transcript、形成内容地图，并发现核心结论可能强依赖视觉证据后读取本文件。调用时应具备对应的核心结论、视觉依赖说明和时间范围。

## 严格视觉门槛

默认总结需要同时满足四项：

1. **核心性**：画面直接支撑视频主旨、关键机制、重要证据、适用边界或核心结论。
2. **不可替代性**：纯文字会丢失影响理解的数值、结构、状态、位置关系、动作顺序或前后变化。
3. **信息增量**：截图能向读者提供正文尚未表达、且值得消耗阅读注意力的信息。
4. **预期可读性**：根据源分辨率、画面构图和目标元素大小，关键文字、代码、图表或界面预计能在最终文档中辨认。

transcript 中的“这里”“如图”“看屏幕”等指代、无解说片段和视觉化演示属于候选信号。将信号映射到核心结论并执行四项门槛。人物访谈画面、标题页、装饰性幻灯片、普通 Logo、重复正文结论的图和外围内容默认以文字完成。用户明确要求关键画面、视觉分析或截图证据时，将请求范围记录为 `user_requested`，并保留来源、可读性和信息增量检查。

## Frame request manifest

`frames` 强制消费 `frame-requests.json`。每条请求映射到一个核心结论，并记录四项门槛与预期观察：

```json
{
  "version": 1,
  "requests": [
    {
      "id": "control-glass-flow",
      "claim_id": "claim-03",
      "timestamp_ms": 1842000,
      "gate": {
        "core": true,
        "irreplaceable": true,
        "information_gain": true,
        "readable_expected": true
      },
      "expected_observation": "确认界面中的验证步骤与执行顺序",
      "height": 720,
      "image_format": "png"
    }
  ]
}
```

默认高度为 720，默认格式为 JPEG。文字、代码、图表、UI 和细线结构可在 manifest 中直接选择 PNG。`crop` 可选，值使用 FFmpeg 的 `w:h:x:y` 表达式。显式用户请求可以设置 `"user_requested": true`。

## 远程按点抽帧

执行：

```bash
python3 <skill-dir>/scripts/acquire_video.py frames '<video-url>' \
  --output '<work-dir>' \
  --manifest '<work-dir>/frame-requests.json'
```

脚本按固定顺序处理 YouTube、Bilibili 和 X：

1. 复用 `<work-dir>/media.info.json`，选择不超过请求高度的视频格式。
2. FFmpeg 对远程 HTTP、HLS 或 DASH URL 按时间点 seek。
3. 鉴权或签名 URL 失效时刷新一次元数据并重试。
4. 远程 seek 仍失败时，通过 yt-dlp 与 FFmpeg 下载目标附近的短片段。
5. 局部片段失败后，根据选中格式的大小估算决定整片下载回退。

整片下载预算默认为 200 MiB。大小未知或超过预算时，`frames/result.json` 记录 `size-unknown` 或 `over-budget`。用户批准更大下载后，可通过 `--max-full-download-mib` 调整预算；值为 `0` 时关闭该回退。

每次调用使用独立临时目录。超时和中断会终止子进程组并清理本次调用创建的半成品。结构化结果写入 `<work-dir>/frames/result.json`，包括每个请求的策略、格式、协议、分辨率、耗时、元数据刷新、失败原因和输出文件。诊断记录会隐藏签名 URL 与鉴权头。

## 查看、记录与单点清晰度升级

逐张使用图像查看工具检查，并在查看后立即记录实际可见事实：

```bash
python3 <skill-dir>/scripts/acquire_video.py record-frame-observation \
  --result '<work-dir>/frames/result.json' \
  --frame-id 'control-glass-flow' \
  --readability readable \
  --supports-claim yes \
  --visible-fact '界面把规划、执行和验证显示为三个连续阶段'
```

观察记录写入 `<work-dir>/frames/observations.json`。后续写作优先读取该文件，图片只在需要复核原始画面时再次加载。

720p 画面中的核心文字或结构清晰时直接使用。实际查看结果为 `partial` 或 `unreadable` 时，为该时间点创建单条重试 manifest，并选择 1080p、1440p、PNG 或局部裁切。清晰度升级范围保持在当前 frame request。

## 在总结中使用

- 原片图表、界面、代码和实物状态以截图作为视觉证据，配上时间戳与一句信息增量说明。
- Mermaid 适合重绘流程、因果链、角色关系、决策树和层级结构。
- SVG 适合重绘空间布局、精确标注结构、时间轴或独立图像文件。
- 重绘内容忠实于视频信息，并用“结构重绘”标注其总结性质；截图使用“原片画面”标签。
- 每张交付截图在 `observations.json` 中具有查看记录，并直接服务于对应核心结论。

完成条件：每张保留的截图都映射到一个核心结论、通过四项视觉门槛并具有实际查看记录；最终画面数量保持最少且足够。
