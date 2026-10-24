<div align="center">

# maibot-mimotts-voice

**让 MaiBot 里的 AI 角色真的开口说话**

基于小米 MiMo-V2.5-TTS 的语音插件，把文字回复变成有情绪、有音色、有停顿的语音。

![version](https://img.shields.io/badge/version-1.9.0-blue)
![python](https://img.shields.io/badge/python-3.9%2B-3776AB)
![license](https://img.shields.io/badge/license-MIT-green)
![platform](https://img.shields.io/badge/MaiBot-%E2%89%A51.0.0-orange)

[快速开始](#快速开始) · [功能](#这个-fork-做了什么) · [配置](#配置说明) · [常见问题](#常见问题) · [与上游的关系](#与上游的关系)

</div>

---

## 这是什么

一个 MaiBot 插件：给 AI 注册一个 `send_voice_reply` 工具，它就能在聊天里用语音回你——音色可以是预置的，也可以是你自己录的 30 秒样本复刻出来的。

它解决的核心问题是**语音合成的不确定性**。MiMo 接口的延迟在 4.5 秒到 160 秒之间随机波动（实测同一句话，与文本长度无关），上游并发就会 `429`，超时后接口还会返回空音频。如果不处理，用户看到的就是「AI 回了一半突然没声」或者「消息发出去了但根本没收到」。

这个插件把这些问题都收在内部：超时降级、并发限流、失败重试、发不出去就改发文字。**对使用者来说，语音要么正常出现，要么自然退回文字，不会有第三种状态。**

## 这个 fork 做了什么

上游版本停在 v1.6.0（2026-05），这个 fork 在其基础上重做了错误处理链路，并新增本地音频后处理。

| | 上游 v1.6.0 | 本 fork v1.9.0 |
|---|---|---|
| 聊天流定位 | 拿不到时取「第一个群会话」 | 三级优先级，多群时放弃而非串群 |
| 合成超时 | 会话级硬编码 60s，超时静默失败 | 每请求独立超时，超时改发文字 |
| 重试 | 无 | 429/5xx/网络错误指数退避 |
| 并发控制 | 无（并发 2 必定 429） | 信号量 + 同流去重 |
| 发送结果判定 | 不检查返回值 | 发送失败也计入失败并降级 |
| 音高/语速/响度 | 不支持（接口忽略这些参数） | 本地 ffmpeg 后处理，10 项可调 |
| 音色取错 | preset 模式会拿 clone 的文件名 | 两条路径分离 |
| 热重载清空密钥 | 旧密钥仍留在内存计费 | 重建实例 |
| 工具可见性 | 默认 deferred，需先 `tool_search` | `visibility="visible"` 常驻 |
| 配置项 | 9 项 | 29 项，全部有默认值 |

改动明细见 [CHANGELOG.md](./CHANGELOG.md)。

## 快速开始

### 1. 放进插件目录

```bash
cd /path/to/MaiBot/plugins/
git clone https://github.com/m1o0/maibot-mimotts-voice.git
```

### 2. 装依赖

```bash
pip install aiohttp
```

> 只有配置了 `[audio]` 里的调音项才需要 **ffmpeg**。全默认时不会调用它，没装也不影响语音发送。
> 验证：`ffmpeg -version` 能输出版本号即可。

### 3. 填 API Key

首次启动会自动从 `config.example.toml` 生成 `config.toml`，编辑它：

```toml
[voice]
mimo_api_key = "你的密钥"
api_base_url = "https://token-plan-cn.xiaomimimo.com/v1"
voice_mode = "clone"
default_voice = "salt"   # voices/salt.mp3 的文件名，不含扩展名
```

然后在 `voices/` 放一段参考音频（建议 ≥30 秒、无背景噪音）。重启 MaiBot，插件会自动加载。

之后改 `config.toml` 会热重载，不用重启。

## 功能

### 语音合成

- **音色复刻**：`voices/` 里放音频即可，文件名就是音色名
- **预置音色**：9 种内置音色（中文/英文，男声/女声）
- **三种风格控制**：自然语言指令、导演模式（角色/场景/指导）、句内音频标签（`（轻笑）`/`(sighs)`）

风格由 AI 根据角色人设自己决定，不需要用户手动指定。插件只负责把 AI 的描述翻译成语音。

### 可靠性

- **超时降级**：超过 `request_timeout` 立即改发文字，不让用户干等
- **智能重试**：只对 429/5xx/网络错误重试；超时不重试（再等只会更久）
- **并发保护**：全局信号量 + 同聊天流去重，避免堆叠触发限流
- **发送结果判定**：`send.custom` 返回失败也算失败，会降级
- **标签剥离**：降级发文字时剥掉 `（轻笑）` 这类只对 TTS 有意义的标签，**只剥白名单**——
  「宝宝（明天要早起）早点睡」不会变成「宝宝早点睡」

### 本地调音（可选）

MiMo 接口**忽略** `speed`、`emotion` 等参数（实测传与不传，返回音频字节级一致）。想调声音只能在本地做：

| 项目 | 支持 |
|---|---|
| 音高 | `pitch_semitones`，半音单位，保留共振峰（不像慢放） |
| 语速 | `speed`，`0.9` 更温柔，`1.1` 更活泼 |
| 响度 | `loudness_normalize`，把每句拉到同一水平 |
| 其他 | 高通去低频、音量增益、淡入淡出、去首尾静音 |

音高对照：

| 取值 | 基频变化 | 听感 |
|------|---------|------|
| `-3` | 约 −16% | 明显成熟，接近成年女性 |
| `-2` | 约 −11% | 偏沉稳 |
| `-1.5` | 约 −8% | 温柔，仍保留少女感 |
| `-1` | 约 −6% | 微微变柔 |
| `0` | 不变 | 原样 |
| `+1` | 约 +6% | 更幼更尖 |

> 不知道调多少就从 `-1` 或 `-1.5` 开始。超过 `-3` 会从「少女」滑向「成年女性」，配猫娘一类人设容易不搭。

一个常见组合（更柔和的少女音）：

```toml
[audio]
pitch_semitones = -1.5
speed = 0.95
loudness_normalize = true
fade_ms = 40
```

ffmpeg 处理约 0.2~0.5 秒（一段 8 秒语音）。找不到 ffmpeg 时只警告一次并跳过，**语音照常发送**。

## 配置说明

### `[voice]`

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `mimo_api_key` | MiMo API Key（必填） | - |
| `api_base_url` | API 地址 | `https://token-plan-cn.xiaomimimo.com/v1` |
| `voice_mode` | 音色模式：`preset` 或 `clone` | `clone` |
| `preset_voice` | 预置音色名称（**仅 preset 模式生效**） | `mimo_default` |
| `voices_dir` | 音色复刻目录（相对插件目录） | `voices` |
| `default_voice` | clone 模式默认音色（文件名，不含扩展名） | - |
| `clone_voice` | clone 模式兜底音色 | - |
| `audio_format` | `wav` / `mp3` / `pcm` / `pcm16`。`mp3` 体积约为 `wav` 的 1/6 | `mp3` |

### `[request]`

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `request_timeout` | 单次合成总超时（秒），超时改发文字 | `45.0` |
| `connect_timeout` | 建立连接超时（秒） | `10.0` |
| `max_retries` | 429/5xx/网络错误的重试次数（不含首次） | `2` |
| `retry_backoff` | 退避基数（秒），按 `base * 2^(n-1)` 增长 | `1.5` |
| `max_concurrency` | 全局并发上限 | `1` |

> **超时不会重试。** 上游抖动时再等一轮只会让用户等更久，直接降级发文字能更快把内容送到。

> `max_concurrency` 默认 `1` 是实测结论：并发 2 必定 `429 Too many requests`，串行则 5/5 成功。

### `[fallback]`

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `fallback_to_text` | 语音失败/超时时改发文字 | `true` |
| `empty_text_placeholder` | 正文被标签清空后的兜底文案（留空则不发） | `（想说点什么，但声音没发出来）` |
| `strip_audio_tags` | 降级时剥掉只对 TTS 有意义的标签 | `true` |
| `skip_when_busy` | 同流已有语音在合成时，本次直接发文字 | `true` |

### `[audio]`

完整字段见上方[本地调音](#本地调音可选)，或 [config.example.toml](./config.example.toml) 的逐项注释。

### API 地址

| 用户类型 | 地址 |
|---------|------|
| Plan 用户（包月/套餐） | `https://token-plan-cn.xiaomimimo.com/v1` |
| 按量计费用户 | `https://api.xiaomimimo.com/v1` |

## 音色模式

### 预置音色（preset）

| 音色名 | 语言 | 性别 |
|--------|------|------|
| `mimo_default` | 中文 | 女性（冰糖） |
| `冰糖` | 中文 | 女性 |
| `茉莉` | 中文 | 女性 |
| `苏打` | 中文 | 男性 |
| `白桦` | 中文 | 男性 |
| `Mia` | 英文 | 女性 |
| `Chloe` | 英文 | 女性 |
| `Milo` | 英文 | 男性 |
| `Dean` | 英文 | 男性 |

### 音色复刻（clone）

```
plugins/maibot-mimotts-voice/voices/
└── salt.wav    # 音色名: salt
```

> 匹配顺序：`voice_name` 参数 → `default_voice` → `clone_voice`。
> 都找不到时记一条 error 并列出可用音色，**不会静默换音色**。

## 工具说明

插件给 AI 注册 `send_voice_reply`：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `reply_text` | string | ✅ | 要合成的文本 |
| `msg_id` | string | ✅ | 当前消息 ID |
| `style_instruction` | string | ❌ | 风格指令 |

### 聊天流定位

1. 宿主注入的 `stream_id` / `chat_id` / `session_id`（绝大多数情况）
2. 用 `msg_id` 反查消息归属会话
3. **仅当全局只有一个群会话时**才兜底

第 3 步的限制是有意的：早期版本无条件取「第一个群会话」，多群环境下会把私聊语音发进群里。现在宁可放弃发送并降级文字，也不串流。

### 风格控制

**1. 自然语言指令**（AI 填 `style_instruction`）

```
一位温柔的少女，声音清甜软糯，语速偏慢，用安慰的语气，带点关切
```

**2. 导演模式**（角色 / 场景 / 指导）

```
角色：一位温柔的大姐姐，性格体贴温暖，声音甜美有亲和力。
场景：安慰失恋的朋友。
指导：语调柔和温暖，气息松弛，偶尔带叹息，语速偏慢，尾音上扬带笑意。
```

**3. 音频标签**（AI 写在 `reply_text` 里）

```
（紧张）呼……冷静。（叹气）算了。（轻笑）好吧好吧。
(sighs) I don't know. (laughs) That's funny!
```

> `style_instruction` 管整体风格，标签管句内细节，两者不冲突。
> AI 不填时使用一段中性默认风格（只描述音色与生活化的在场感，不指定情绪），避免与用户单独配置的人格提示词冲突。

## 常见问题

**Q：日志说合成成功，但用户没收到语音？**

A：v1.7.0 起发送结果也计入成功判定，发送失败会自动降级文字。
若仍无内容，检查 `[fallback] fallback_to_text` 是否为 `false`。

**Q：语音要等很久？**

A：MiMo 延迟波动很大（实测 4.5s ~ 160s+，与文本长度无关）。超过 `request_timeout` 会放弃并改发文字。
经常触发的话，调高 `request_timeout`，或把 `audio_format` 改成 `mp3` 减小传输体积。

> 若把 `[audio] speed` 调到 1 以下，语音会变长，建议把 `request_timeout` 一并调大（如 `60.0`）。

**Q：频繁 429？**

A：上游对并发有限制。保持 `max_concurrency = 1` 与 `skip_when_busy = true` 可显著减少。

**Q：开了 `[audio]` 后语音发不出来 / ffmpeg 报错？**

A：不会导致发不出去。处理失败时只打一条 warning，**未处理的原始音频照常发出**。
找不到 ffmpeg 时保持 `[audio]` 全默认（`pitch_semitones = 0` 且 `speed = 1.0`），此时完全不调用 ffmpeg。

**Q：改了 `config.toml` 要重启吗？**

A：不用。配置热重载会重建 TTS 服务，密钥/音色/超时/调音参数即时生效。

## 与上游的关系

这是 [Emilia-awa/maibot-mimotts-voice](https://github.com/Emilia-awa/maibot-mimotts-voice) 的 fork，
基于上游 `8ccfa7e`（v1.6.0）。

上游的语音发送修复见 [PR #3](https://github.com/Emilia-awa/maibot-mimotts-voice/pull/3)（作者 [@Doulor](https://github.com/Doulor)），
本 fork 采用同样的 `send.custom("voice", raw_base64)` 思路，并在此之上补完了错误处理链路。

原作者与许可证信息保留在 [LICENSE](./LICENSE) 与 `_manifest.json` 的 `author` 字段中。

**同步上游更新：**

```bash
git fetch upstream
git merge upstream/main
```

## 开发

```bash
# 语法检查
python -m py_compile plugin.py tts_service.py audio_processor.py

# 把 config.example.toml 解析一遍，确认没写错
python -c "import tomllib,pathlib;tomllib.loads(pathlib.Path('config.example.toml').read_text(encoding='utf-8'))"
```

工程约定：

- 所有新增配置项必须有默认值，**不破坏老配置**（缺段自动补）
- 所有对外部服务（TTS API、ffmpeg）的调用失败都必须降级，不向上抛
- 修改发送链时，同时验证 `wav` 与 `mp3` 两种格式（SnowLuma 需转 silk）

## 注意事项

1. **API Key**：在 [小米 MiMo 平台](https://platform.xiaomimimo.com) 申请
2. **音色复刻**：录音 ≥30 秒、无背景噪音效果最好
3. **Base64 上限**：音色参考音频 Base64 后不能超过 10MB
4. **费用**：MiMo-V2.5-TTS 系列当前限时免费

## 许可证

[MIT](./LICENSE) © Emilia-awa
