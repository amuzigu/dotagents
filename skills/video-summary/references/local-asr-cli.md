# 本地 ASR CLI 与模型缓存复用

字幕缺失或质量不足时，先发现本机可用能力，再选择 ASR。目标是复用健康的 CLI 与已有权重，并产生带时间戳的 SRT、VTT 或 JSON。

## 发现能力

```bash
python3 <skill-dir>/scripts/acquire_video.py detect-local-asr --output '<work-dir>'
```

读取 `local-asr-capabilities.json`：

- `status: ready` 表示命令通过了 `--help` 健康检查。
- `status: broken` 常见于 Python、pipx 或 Homebrew 升级后遗留的失效 shebang。
- `compatible_cache_entry_found` 表示发现了该 CLI 可能复用的模型缓存条目；运行前仍需确认模型文件完整。
- `recommended_cached_cli` 同时满足命令健康与缓存匹配。
- `preferred_cached_candidate` 指出最值得安装或修复的候选。`repair_or_install_for_cached_model` 与 `download_model_for_healthy_cli` 分别呈现两条可选路径；安装、修复和模型下载由用户明确批准。

在执行转写前运行所选命令的 `--help`，以本机安装版本为准确认参数。保留 ASR 原始输出、实际命令、模型名称或本地路径、语言提示及设备类型。

模型缓存复用指复用磁盘权重，从而省去网络下载。每次启动 CLI 仍会把权重加载到内存或显存。多分 P 或一组短视频优先在同一条 CLI 命令中批量处理；长期高频任务可使用该引擎的常驻服务模式。容器环境把 Hugging Face、Whisper 或 GGML 模型目录挂载成持久卷。

## 选择顺序

| 场景 | CLI | 选择理由 |
|---|---|---|
| Apple Silicon，已有 MLX 权重 | `mlx_whisper` | 充分利用 Metal；CLI 能输出 JSON 并支持词级时间戳 |
| 通用快速转写 | `whisper-ctranslate2` | 对原版 Whisper CLI 兼容度高，底层使用 faster-whisper |
| 访谈、多说话人、精确词级时间 | `whisperx` | faster-whisper 转写后增加对齐和可选说话人分离 |
| 轻依赖、离线、已有 GGML 权重 | `whisper-cli` | 模型路径显式，缓存与离线边界清晰 |
| 字幕切分或时间戳需要修复 | `stable-ts` | 面向时间戳稳定、对齐与字幕重排 |
| 已有可用环境 | `insanely-fast-whisper` 或 `whisper` | 复用现成安装和模型；前者偏 CUDA/MPS 高吞吐，后者作为基准实现 |

质量需求优先于固定排名。需要说话人归属时选择 WhisperX；画面讲解类视频通常优先选择速度快、能输出分段时间戳的 CLI。

## 运行模板

以下模板只使用本地模型路径。根据 `<command> --help` 调整当前版本的选项。

MLX Whisper：

```bash
mlx_whisper '<audio>' --model '<local-mlx-model-dir>' \
  -f json --output-name '<work-dir>/local-asr'
```

whisper-ctranslate2：

```bash
whisper-ctranslate2 '<audio>' --model_directory '<local-ctranslate2-model-dir>' \
  --output_dir '<work-dir>' --output_format json --vad_filter True
```

WhisperX 在缓存中运行：

```bash
whisperx '<audio>' --model '<cached-model-name>' --model_dir '<cache-dir>' \
  --model_cache_only True --output_dir '<work-dir>' --output_format json
```

whisper.cpp 使用 16-bit、16 kHz、单声道 WAV，并显式指定 GGML 模型：

```bash
python3 <skill-dir>/scripts/acquire_video.py audio '<video-url>' \
  --output '<work-dir>' --format wav
whisper-cli -m '<local-ggml-model.bin>' -f '<work-dir>/media.wav' \
  -ojf -of '<work-dir>/local-asr' -l auto
```

`acquisition.json` 的 `transcript.asr_language_hint` 存在时，把它传给 CLI 的语言参数。其余情况使用自动语言检测。模型使用视频原生语言能力；转译在总结阶段完成。需要核查语言推断细节时再读取 `caption-selection.json`。

## 统一输出

优先让 CLI 输出 SRT、VTT 或包含 `segments[].start/end/text` 的 Whisper JSON。随后归一化：

```bash
python3 <skill-dir>/scripts/acquire_video.py normalize-asr \
  --input '<asr-output.srt|vtt|json>' --output '<work-dir>'
```

归一化会发布 `transcript.active.md`、`transcript.active.jsonl` 和 `transcript.active.index.json`，并把 `acquisition.json` 更新为 `transcript.status: ready`、`next_action: summarize`。JSONL 是规范 segment 数据，index 提供连续 chunks；长 transcript 使用 `transcript-slice` 逐块覆盖。`asr.source.json` 保留输入文件、格式和语言等来源记录。抽查开头、中段、结尾、专有名词密集处和低置信片段。需要时间戳修复时再调用 Stable-ts，避免为普通总结增加额外计算。

## 安装与下载边界

发现阶段只读取命令状态和标准缓存目录。优先使用 `recommended_cached_cli`。没有健康且命中缓存的 CLI 时，把探测结果、可复用权重和建议候选告诉用户，再取得安装、修复或下载模型的授权。
