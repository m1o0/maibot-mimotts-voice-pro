# 更新日志

本文件记录插件各版本的变化。遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [1.9.11] - 2026-10-29

### 修复

- **单个音色文件读取失败不再导致整个插件加载失败**。`_load_voices` 在 `on_load`
  里被直接 `await`，原来 `stat()` / `open()` 没有异常保护——`voices/` 里只要有一个
  文件读不了（被播放器独占、网盘同步中、刚被改名），异常一路上抛到宿主，宿主会把
  插件注销成 FAILED，**所有语音功能消失且必须重启宿主才能恢复**。现在单文件失败只记
  warning 并跳过，其余音色照常加载；0 字节文件同样跳过；`voices_dir.iterdir()`
  的 `OSError`（权限不足、路径其实是文件、网盘占位符无法访问）也一并降级处理。
- **重载音色时不再出现「旧音色没了、新音色只加载一半」**。原先在函数开头就
  `self.voices.clear()`，中途失败会留下空档。改为先构建新字典、走完再整体替换；
  目录不存在 / 无音频文件时也赋空字典，保留「删光文件后热重载能让旧音色失效」的行为。
- **不传 `stream_id` 时不再丢弃 `audio_base64`**。原实现在「只合成、不发送」分支里
  `pop` 掉了该字段，调用方会拿到 `{"success": true}` 却没有音频——静默失败且无从排查，
  与 README 的承诺直接矛盾。现在返回纯 base64（已剥掉 `data:` 前缀）。
- **`audio_format` 填 `pcm` / `pcm16` 时语音发不出去**。上游返回的是无容器头裸流，
  适配器只是把 base64 原样塞进 `{"type": "record"}`，QQ 无法识别。现在发送前用标准库
  `struct` 拼 44 字节 RIFF/WAVE 头封成 wav 再发（不额外起 ffmpeg 子进程），
  返回值里的 `audio_base64` 仍是上游原始裸流。
- **取消任务时会留下孤儿 ffmpeg 进程**。`audio_processor` 里原来只处理了
  `asyncio.TimeoutError`，而任务被取消抛的是 `CancelledError`，不走那个分支，
  于是 `proc.kill()` 从未执行——重载几次插件就在系统里堆一堆 `ffmpeg`。
  现在补上 `CancelledError` 分支：先 `kill()` 子进程再原样向上抛（不吞取消信号）。
- **`on_unload` 只 `cancel()` 不等待**。`cancel()` 只是投递取消请求，不给协程收尾的
  机会，里面卡着的 ffmpeg 同样会泄漏。现在会 `await` 这些任务（带 2s 超时兜底，
  避免某个协程赖着不走时把插件卸载卡死）。
- **`@API` 未声明 RPC 超时**。宿主默认 60s；`request_timeout` 是可配的且没有上界，
  用户调到 90s 时宿主会在插件正干活时把它掐断。现在显式声明 `timeout_ms=120000`。
- **语气词白名单漏字**。`深沉` 不在白名单里，`（深沉）我一直在想这件事。` 会被原样
  送去合成——"深沉"两个字被念出来。补入 `深沉`。

### 新增

- 回归测试从 69 项增至 **115 项**：覆盖坏音色文件隔离、列举失败时的状态确定性、
  不传 `stream_id` 时保留音频、裸 PCM 发送前封装 wav（含 RIFF 头字段校验）、
  `@API` 的 RPC 超时声明、取消时回收 ffmpeg、`on_unload` 等待任务收尾、
  语气词剥离与普通括号保留。以上各项均做过「回退到旧代码则测试失败」的反向验证。

### 变更

- **`_manifest.json` 的 `sdk.max_version` 从 `2.99.99` 改为 `2.16.99`**。插件市场
  规则要求兼容范围版本号的第一位、第二位不能填 `99` 或 `999`，`2.99.99` 会被审核
  直接拒绝（1.9.0 ~ 1.9.5 就是这么被挡在门外的）。宿主 MaiBot 1.3.15 实际安装的是
  `maibot-plugin-sdk 2.16.0`，`pyproject.toml` 也声明 `>=2.16.0`，所以按实际支持的
  2.16.x 声明上界。SDK 超范围时宿主只警告不拒绝加载，这次收窄不会影响运行。
- README 的 `audio_format` 与 `voice_clone_tts` 说明补充了裸流被自动封装、
  返回值形态这两个此前未写清的行为。
- 删除 `voice_clone_tts` 里的 `ref_audio = ""`：它只清局部变量，
  `self.voices[voice_key]` 里那份 base64 一点没少，纯属误导。

## [1.9.10] - 2026-10-25

### 新增

- **GitHub Actions CI**（`.github/workflows/ci.yml`）。每次 push / PR 自动跑：
  Python 3.10、3.11、3.12 三个版本下的字节编译与 69 项回归测试、
  `_manifest.json` 的 12 个必需字段与版本格式校验、
  以及一道守卫（禁止提交 `config.toml`、`voices/` 下的音色文件、疑似 API Key）。
  失败会直接标红，不用等别人发现问题。

### 变更

- `requirements.txt` 补了注释说明**为什么刻意不声明 `maibot-plugin-sdk`**：
  该包由 MaiBot 宿主自带，插件重复声明可能在用户机器上改动宿主环境。
- README 的 Python 版本徽章从 `3.9+` 修正为 **`3.10+`** —— SDK 的实际下限是 3.10，
  原来的 3.9 是错的。依赖安装改为 `pip install -r requirements.txt`，
  并写明 SDK 不需要自己装。

## [1.9.9] - 2026-10-25

### 新增

- **仓库内可跑的回归测试** `tests/test_plugin.py`（69 项）。用桩对象模拟宿主，
  不连真实 MaiBot、不打真实 API，覆盖配置解析、聊天流定位（含多群防串流）、
  TTS 重试与超时分类、文字降级、音色清理、并发保护、工具返回值。
  `python tests/test_plugin.py` 直接跑，退出码可用于 CI。
  设 `TTS_PLUGIN_DIR` 环境变量可指向实际部署目录做现场验证（此时会额外
  校验 `voices/` 里的真实音色，共 71 项）。

### 修复

- `_find_stream_id` 里的局部变量 `resloved` 拼写改为 `resolved`。
- `MiMoTTSService.synthesize_with_preset` / `synthesize_with_voice_clone` 的
  `audio_format` 参数默认值仍是 `wav`，与同版本统一后的 `mp3` 不一致。
  这两个默认值实际不会被走到（调用方总是显式传参），但留着会误导后续改动者，
  一并改为 `mp3`。

## [1.9.8] - 2026-10-25

### 修复

- **README「对外接口」示例里残留上游插件 ID**。示例写的是
  `plugin_call("Emilia-awa.maibot-mimotts-voice", ...)`，那是原版插件的 ID；
  本插件是 `m1o0.maibot-mimotts-voice-pro`，照抄示例的调用方会调到原版去 ——
  两个插件装的配置、音色目录、API Key 都不一样，调错了不报错，只是安静地走错地方。
  现改为本插件 ID。

## [1.9.7] - 2026-10-25

### 修复

- **`audio_format` 的默认值三处不一致**。`VoiceSectionConfig` 的 schema 默认是
  `wav`，但 `config.example.toml` 和 README 的配置表都写 `mp3`。宿主生成新配置时
  走的是 schema 默认值，所以**新装用户实际拿到 `wav`，跟文档对不上** ——
  他照着 README 以为自己在用 mp3，实际每次都多传五倍体积。
  现统一为 `mp3`：schema 默认值、代码里「配置项为空时的兜底」、以及
  `audio_format` 非法时的回退目标，全部改成 `mp3`，与两份文档一致。
  选 `mp3` 而不是反过来改文档，是因为它体积约为 `wav` 的 1/6，没有任何理由让新装用户
  默认踩这个坑；老配置里显式写了 `wav` 的照常生效，不受影响。

## [1.9.6] - 2026-10-25

第五轮审核指出两处「文档与实际不符 / 配置错误不容易发现」，都不影响行为，但会让人踩坑。

### 修复

- **README 的配置生成说明写错了**。原文「首次启动会自动从 `config.example.toml`
  生成 `config.toml`」，实际是宿主在 `_prepare_plugin_activation()` 里按插件的
  `config_model` 用 `tomlkit` 生成，跟 `config.example.toml` 没有关系 ——
  那个文件只是带注释的示例，插件根本不会去读它。已在 README 里改正。

- **`audio.ffmpeg_path` 配错时回退不透明**。原先配了一个不存在的路径时，只打一条
  「将在 PATH 里找」的 warning 就继续了，如果 PATH 里恰好有另一个 `ffmpeg`
  （可能是别的版本，甚至同名的别的程序），插件会静默改用那个，用户还以为用的是
  自己配置的那个，出问题根本排查不到这里。现在解析成功后会再打一条 warning，
  把**实际用的是哪个可执行文件**明确写出来，并提示如何改成正确的路径。

## [1.9.5] - 2026-10-25

第四轮审核只剩两个非阻断小尾巴，这里处理其中真正会影响正确性的那个。

### 修复

- **多平台部署下，聊天流兜底会数错会话数**。第四步兜底用
  `ctx.chat.get_all_streams()`，但 SDK 给它的 `platform` 参数定的默认值是 `"qq"`，
  宿主 `_list_sessions()` 会按平台过滤。也就是说在同时接了 QQ 和别的平台的部署里，
  它只会数到 QQ 会话 —— 别的平台上明明还有会话，却数出「全局只有 1 个」，
  于是兜底猜错，可能把语音发到错误平台的会话去。
  现在显式传 `platform`：优先用宿主注入的当前消息 platform，拿不到则传宿主约定的
  通配值 `"all_platforms"`。**注意不能传空串** —— `_list_sessions()` 的判断是
  `platform == "all_platforms" or session.platform == platform`，空串只会匹配到
  platform 为空的会话，等于一个都查不到，反而会让兜底永不触发。
  选全平台是更保守的方向：会话数只会多不会少，宁可放弃发送也不发错地方。

## [1.9.4] - 2026-10-24

按第三轮审核意见收尾。审核确认无阻断项，这里是两条「顺手看一眼」的建议。

### 修复

- **`_ensure_config_exists()` 是死代码，已删除**。它原本在 `on_load` 里把
  `config.example.toml` 复制成 `config.toml`，但这个时机太晚了：宿主的
  `_prepare_plugin_activation()` 在 `on_load` **之前**就已经调
  `_apply_plugin_config()`，那里对 `config.toml` 不存在的情况会直接用
  `tomlkit` 按 `config_model` 生成一份完整配置（`runner_main.py:966-969` 的
  `should_initialize_file`）。所以插件这段复制从来没生效过 —— 线上 `config.toml`
  只有 2 行注释、`config.example.toml` 有 26 行，就是宿主生成而非本插件复制的证据。
- **`voices_dir` 不存在时不再在插件源码目录里 `mkdir`**。`paths` 约定要求插件只往
  `data_dir` / `runtime_dir` 写；在源码目录建空文件夹既会被 `.gitignore` 吞掉，
  又会让用户误以为放进去的文件会被同步。改成只告警、告诉用户该建哪个目录。

### 变更

- **`voice_clone_tts` 的 API 描述补上副作用说明**。这个 API 保持 `public=True`
  （上游原有设计，TTS 作为对外能力本来就该可被调用），但原描述只写
  "TTS with specified voice"，没提「传 `stream_id` 会真的发语音出去」。
  现在描述里写明这一点，README 也新增「对外接口」一节说明调用方式和 `stream_id` 语义。

## [1.9.3] - 2026-10-24

按第二轮市场审核意见修掉三处边界问题。都是「正常情况下碰不到，碰到就麻烦」的类型。

### 修复

- **聊天流兜底逻辑仍可能串流。** 上一版只判断「全局只有一个**群**会话」就兜底，
  但 `get_group_streams()` 只返回群聊 —— 在「1 个群 + 若干私聊」的环境里它照样
  返回 1 个，于是私聊消息会拿到那个唯一的群流，语音发进群里。
  现在改用 `chat.get_all_streams()` 看**全量**会话数，并且此时条件更严格：
  只有全局确实只有 1 个会话才兜底，否则放弃发送并降级文字。
  同时把 manifest 的能力声明从 `chat.get_group_streams` 换成 `chat.get_all_streams`。

- **`voice.voices_dir` 可以指向插件目录之外。** 原实现直接做
  `plugin_dir / voices_dir` 拼接，配置里写绝对路径或 `../..` 就能让插件读取
  任意位置的 `.wav` / `.mp3`，base64 之后发给 MiMo —— 等于一条「读什么文件就发
  什么文件出去」的通路。现在校验解析后的路径必须在插件目录内，越界则回退到
  默认的 `voices` 并告警（回退而非报错，其余功能不受影响）。

- **`send_voice_reply` 的返回值缺 `content`。** 按 Tool 字典约定，返回 dict 的
  `content` 才是给模型看的可读文本。原来只返回 `{"success", "method", "error"}`，
  模型看到的是空结果，容易误判成调用失败，然后把同样的内容再用 `reply` 发一遍，
  和「语音顶替本次文字回复」的设计直接打架。现在成功/失败路径都带上 `content`，
  并明确告诉模型「这条已经回复过了，不需要再发文字」。

### 变更

- `voice.voices_dir` 的配置说明补充「必须是插件目录内的相对路径」。

## [1.9.2] - 2026-10-24

按插件市场审核意见调整元信息与两处健壮性。功能行为不变。

### 变更

- `_manifest.json` 的 `host_application.max_version` 从 `1.99.99` 收紧到 `1.3.99`。
  上限应当反映实际验证过的范围，`1.99.99` 等于宣称支持所有未来版本，没有依据。
  `min_version` 保持 `1.0.0`；`sdk` 区间不动（SDK 的兼容承诺一直更宽）。
- `_manifest.json` 补 `plugin_type: "media"`。此前未声明，宿主按 `extension` 兜底处理，
  插件市场的分类与默认图标会不准。这是语音合成与发送类插件，归到 `media`。

### 新增

- `voice.api_base_url` 增加协议安全校验：非 `https`、缺少协议头、解析不出主机名时
  记一条 warning。`api_base_url` 是完全自由的配置项，写错地址会把 API Key 与
  复刻参考音频明文发到别处。本机回环地址（`localhost` / `127.0.0.1` / `::1` /
  `0.0.0.0`）不告警，方便本地调试。
  **只告警不阻断** —— 配置写错时语音仍应能工作，卡死在「配置报错但不知道为什么」
  比多打一条 warning 更糟。
- 音频后处理的 ffmpeg 中转文件改放 `ctx.paths.runtime_dir`（宿主按插件 ID 分配的
  运行时目录），随插件生命周期管理，不再散落在系统临时目录。该目录不可写时
  自动退回系统临时目录，不影响调音。

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
