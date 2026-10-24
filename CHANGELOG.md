# 更新日志

本文件记录插件各版本的变化。遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [1.9.1] - 2026-10-24

仅元信息调整，功能代码与 1.9.0 完全一致。

### 变更

- `_manifest.json` 的 `author` 改为本 fork 的维护者 `m1o0`。插件市场会把
  `author.name` 显示在插件卡片上，填原作者会让人以为这个 fork 也是原作者发布的。
- `LICENSE` 增补一行 `Copyright (c) 2026 m1o0`，原作者署名保留不变（MIT 要求）。
- README 的版本徽章与对照表同步到 1.9.1。

## [1.9.0] - 2026-10-24

本次为提 PR 前的终审，修掉 3 个会在真实使用中静默出错的问题。

### 修复

- **修复文字降级会误删正常括号内容的问题**。1.7.0 引入的 `_AUDIO_TAG_RE` 是
  「任意括号内 1~12 个字符」，于是 `宝宝（明天要早起）早点睡` 被降级成
  `宝宝早点睡`、`第1名（2026年）` 被截成 `第1名`——静默篡改用户内容，比多留
  一个标签严重得多。现在改为白名单匹配（约 80 个语气/动作词，含中英），
  不认识的括号一律保留。
- **修复 `audio_format = "pcm"` 时音频处理被静默跳过的问题**。上游支持
  `wav/mp3/pcm/pcm16` 四种格式，但 `_ENCODERS` 只覆盖了三种，配 `pcm` 时
  `process()` 只打一条 warning 就原样返回未处理音频。实测上游 `pcm` 与
  `pcm16` 输出格式完全相同（裸 s16le / 24kHz / 单声道），已补齐编码器与解码器。
- **移除 `tts_service.py` 中三个已失效的方法**（`apply_settings`、
  `update_api_key`、`update_api_base_url`）。1.7.0 改成「配置变更时重建实例」
  之后它们已无调用方，留着只会误导后续维护者以为「增量更新」仍然可用。

### 变更

- `config.example.toml` 的 `audio_format` 默认值改为 `mp3`（体积约为 `wav` 的 1/6）。
- README 补充 `[audio]` 与 `request_timeout` 的关系说明：放慢语速会让语音变长，
  建议同时调大超时。

## [1.8.0] - 2026-10-24

### 新增

- **`[audio]` 本地音频后处理**（新增 `audio_processor.py`）。MiMo 接口不接受任何
  调音参数——实测 `audio.speed`、`audio.emotion` 以及顶层 `speed` 都被服务端
  静默忽略，返回音频与不传时行为一致。所以音高、语速、响度这些只能在客户端用
  ffmpeg 做：
  - `pitch_semitones` + `preserve_formant`：音高调整，用 rubberband 保留共振峰。
    比 `asetrate + atempo` 自然得多——后者会把共振峰一起搬移，听感像慢放。
  - `speed`：语速，超出 ffmpeg 单级范围时自动串联多级 `atempo`。
  - `loudness_normalize` + `loudness_target`：响度标准化，避免有的句子小声有的刺耳。
  - `highpass_hz` / `volume_db`：高通去低频轰隆、额外增益。
  - `fade_ms` / `trim_silence`：首尾淡入淡出（消除开头爆音）、去掉多余静音。
  - 全部默认关闭，不配置就不会 spawn 进程，也不会去探测 ffmpeg。
    找不到 ffmpeg 或滤镜报错时只警告一次并**原样返回未处理音频**，
    不会让整条语音因此发不出去。

## [1.7.0] - 2026-10-24

本次版本集中修复了「语音发不出去」「发错聊天流」「热重载不生效」三类问题，
并把超时、重试、并发、降级等硬编码参数改为可配置。

### 修复

- **修复语音可能被发到错误聊天流的问题**。`send_voice_reply` 拿不到宿主注入的
  `stream_id` 时，原实现会直接取「第一个群会话」兜底，在多群环境下会把私聊的
  语音发进群里。现在改为先用 `msg_id` 反查消息归属的会话；只有在确认全局仅存在
  一个群会话时才允许兜底，否则放弃发送并降级为文字。`msg_id` 参数此前虽为必填，
  实际从未被使用。
- **修复后台语音任务可能被垃圾回收的问题**。`asyncio.create_task()` 的返回值
  未被持有，asyncio 只对任务做弱引用，任务可能在合成途中被 GC 回收，表现为
  日志停在 `TTS request` 之后没有下文。现在把任务存入集合持有强引用。
- **修复 preset 模式下音色 ID 取错的问题**。`default_voice` 是 clone 模式的参考
  音频文件名（如 `luoxi`），preset 模式此前会把它当作预置音色 ID 发给上游而被拒。
  现在 preset 模式只认显式传入的音色名或 `preset_voice`。
- **修复热重载后 API Key 清空不生效的问题**。原实现仅在「新 key 非空」时才更新，
  把 key 清空后旧 key 仍留在内存中继续计费；`base_url` 同样会残留。现在配置变更
  时重建服务实例并关闭旧连接池。
- **修复音色目录清空后仍保留旧音色的问题**。`_load_voices()` 此前只在找到音频
  文件时才清空缓存，把 `voices/` 里的文件全部删除后再热重载，旧音色仍会被继续
  引用。现在每次重载都先清空。
- **修复插件卸载时后台任务不取消的问题**。`on_unload()` 现在会取消仍在进行的
  合成任务，避免协程挂在已关闭的 HTTP 会话上。

### 新增

- **429 / 5xx / 网络错误自动重试**，按指数退避，重试次数可配。超时**不重试**——
  上游抖动时再等一轮只会让用户等更久，直接降级发文字更快。
- **全局并发限制**（`request.max_concurrency`）。MiMo 接口在密集请求下会返回
  `429 Too many requests`，限制同时进行的合成数可以避免触发限流。
- **同一聊天流的语音去重**（`fallback.skip_when_busy`）。某条语音还在合成时，
  新语音直接改发文字，既不会堆叠等待也不会互相拖慢。
- **`audio_format` 配置项**，支持 `wav` / `mp3` / `pcm16`。接口不支持 `opus`、
  `aac`；`mp3` 体积约为 `wav` 的 1/6（实测 22KB vs 141KB）。非法值自动回退 `wav` 并告警。
- **失败降级开关**（`fallback.*`）。语音失败或超时时把内容改发文字，并可选择
  剥掉 `（轻笑）(sighs)` 这类只对 TTS 有意义的标签，正文被清空时用兜底文案。
- **`[request]` / `[fallback]` 配置段**，所有超时与重试参数不再硬编码。
- `.gitignore`、`CHANGELOG.md`、`voices/.gitkeep`。

### 变更

- 移除 `tts_service` 中在 async 函数里调用 `gc.collect()` 的行为，它会阻塞事件循环；
  改为依赖局部变量释放与 CPython 的引用计数。
- HTTP 会话不再设置会话级总超时，改为在每次请求上单独传 `timeout`，这样调整
  `request_timeout` 无需重建会话。
- `_manifest.json` 的能力声明与实际调用对齐：移除宿主并不存在的 `send.record`
  和从未使用的 `chat.get_all_streams`，补充 `chat.get_group_streams`。
- `fallback.empty_text_placeholder` 的兜底文案改为可配置。

## [1.6.0]

- 🔧 **工具链暴露**：`send_voice_reply` 工具暴露在 AI 工具链中，AI 可自主决定何时使用语音回复
- 🎬 **AI 智能风格控制**：风格指令由 AI 根据角色人设和对话情境自行决定，支持自然语言指令、导演模式、音频标签三种方式
- 🎨 **style_instruction 可选化**：AI 自行判断何时需要风格指令，无需每次填写
- 📋 **配置自动初始化**：首次启动自动从 `config.example.toml` 生成 `config.toml`

## [1.0.0]

- 初始版本：预置音色、音色复刻、基础语音回复功能
