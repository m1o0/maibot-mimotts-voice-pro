# MiMo TTS Voice Plugin for MaiBot

基于小米 MiMo-V2.5-TTS 的 AI 语音服务插件，为 MaiBot 提供智能语音回复功能。支持**预置音色**和**音色复刻**两种模式。

## 功能特性(好用的话请点个star⭐~)

- 🎤 **预置音色**：内置 9 种精品音色（中文/英文，男声/女声），开箱即用
- 🎭 **音色复刻**：通过音频样本精准复刻任意音色，放入 `voices/` 目录即可
- 🤖 **AI 工具链暴露**：`send_voice_reply` 工具暴露在 AI 工具链中（`activation_type=ALWAYS`，`visibility=visible`），AI 可自主决定何时使用语音回复
- 🎨 **AI 智能风格控制**：风格指令由 AI 根据角色人设和对话情境自行决定，无需用户手动配置
- 🎬 **导演模式**：支持从角色、场景、指导三个维度全方位刻画语音表演，AI 自动判断何时需要深度演绎
- ⚡ **异步发送**：语音合成和发送异步执行，不阻塞 AI 主循环
- 🛡️ **失败降级**：合成失败/超时时自动改发文字，不会让用户干等或收不到回复
- 🔁 **自动重试**：429、5xx、网络抖动按指数退避重试；超时快速放弃（不重试）
- 🚦 **并发保护**：全局并发上限 + 同流去重，避免触发上游限流
- 🎚️ **音色可调**：音高、语速、响度、淡入淡出、去静音等本地 ffmpeg 后处理（可选，默认关闭）
- 🔧 **自动配置初始化**：首次启动时自动从 `config.example.toml` 生成 `config.toml`，不覆盖用户已有配置
- 🔄 **热重载**：修改 `config.toml` 后自动重建服务，密钥/音色/超时/调音参数即时生效

## 安装

### 1. 复制插件到 MaiBot

```bash
cd /path/to/MaiBot/plugins/
git clone https://github.com/Emilia-awa/maibot-mimotts-voice.git
```

### 2. 安装依赖

```bash
pip install aiohttp
```

> 可选：只有配置了 `[audio]` 里的调音项才需要 **ffmpeg**（音高/语速/响度等本地后处理）。
> 保持全默认时不会调用它，没装也不影响语音发送。
> 验证：`ffmpeg -version` 能输出版本号即可。

### 3. 配置

编辑 `config.toml`（可从 `config.example.toml` 复制）：

```toml
[plugin]
enabled = true
config_version = "1.0.0"

[voice]
mimo_api_key = "your_api_key_here"
api_base_url = "https://token-plan-cn.xiaomimimo.com/v1"
voice_mode = "clone"          # 或 "preset"
preset_voice = "mimo_default"  # 仅 preset 模式生效
voices_dir = "voices"
default_voice = "salt"         # clone 模式默认音色，填 voices/ 里的文件名（不含扩展名）
clone_voice = "salt"
audio_format = "mp3"

[request]
request_timeout = 45.0
connect_timeout = 10.0
max_retries = 2
retry_backoff = 1.5
max_concurrency = 1

[fallback]
fallback_to_text = true
empty_text_placeholder = "（想说点什么，但声音没发出来）"
strip_audio_tags = true
skip_when_busy = true
```

### 4. 重启 MaiBot

插件会自动加载。之后修改 `config.toml` 会触发热重载，无需重启。

## 配置说明

### `[voice]`

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `mimo_api_key` | MiMo API Key（必填） | - |
| `api_base_url` | API 地址 | `https://token-plan-cn.xiaomimimo.com/v1` |
| `voice_mode` | 音色模式：`preset` 或 `clone` | `clone` |
| `preset_voice` | 预置音色名称（**仅 preset 模式生效**） | `mimo_default` |
| `voices_dir` | 音色复刻目录（clone 模式，相对于插件目录） | `voices` |
| `default_voice` | clone 模式默认音色，填 `voices/` 下的文件名（不含扩展名） | - |
| `clone_voice` | clone 模式兜底音色；`default_voice` 找不到时使用 | - |
| `audio_format` | 输出格式：`wav` / `mp3` / `pcm` / `pcm16`（`mp3` 体积约为 `wav` 的 1/6，上传更快；`pcm`/`pcm16` 是同样参数的裸流，一般不需要用） | `mp3` |

### `[request]`

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `request_timeout` | 单次合成总超时（秒）。超时后降级发文字 | `45.0` |
| `connect_timeout` | 建立连接超时（秒） | `10.0` |
| `max_retries` | 429/5xx/网络错误的**重试**次数（不含首次） | `2` |
| `retry_backoff` | 退避基数（秒），按 `base * 2^(n-1)` 增长 | `1.5` |
| `max_concurrency` | 全局同时进行的合成请求上限 | `1` |

> 说明：**超时不会重试**。上游抖动时再等一轮只会让用户等更久，
> 直接降级发文字能更快把内容送到用户面前。

> `max_concurrency` 默认为 `1` 是实测结论：并发 2 时必定出现
> `429 Too many requests`，串行调用则 5/5 成功。若你的套餐额度更高可以适当上调。

### `[fallback]`

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `fallback_to_text` | 语音失败/超时时把内容改发文字 | `true` |
| `empty_text_placeholder` | 降级时正文被标签清空后用的兜底文案（留空则不发） | `（想说点什么，但声音没发出来）` |
| `strip_audio_tags` | 降级发文字时剥掉 `（轻笑）(sighs)` 这类只对 TTS 有意义的标签 | `true` |
| `skip_when_busy` | 同一聊天流已有语音在合成时，本次直接改发文字 | `true` |

### `[audio]` — 自定义音色观感

MiMo 只负责把文字念出来，**不提供音高、语速、响度这些调音参数**
（实测 `audio.speed`、`audio.emotion` 和顶层 `speed` 都被服务端静默忽略，
返回音频与不传时字节级一致）。所以想调声音只能在本地做。

开启这一节需要系统里有 [ffmpeg](https://ffmpeg.org/download.html)。
**全部保持默认值时不会调用 ffmpeg**，也没有任何额外开销。

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `ffmpeg_path` | ffmpeg 命令名（走 PATH）或绝对路径 | `ffmpeg` |
| `pitch_semitones` | **音高**，半音单位。负数降调更沉稳，正数升调更幼 | `0.0` |
| `preserve_formant` | 降调时保留共振峰（音色更自然，不然像慢放） | `true` |
| `speed` | **语速**倍率。`0.9` 更温柔，`1.1` 更活泼 | `1.0` |
| `loudness_normalize` | **响度标准化**，把每句音量拉到同一水平 | `false` |
| `loudness_target` | 响度目标（LUFS），`-16` 适合语音，`-14` 接近流媒体 | `-16.0` |
| `highpass_hz` | 高通滤波，去掉低频轰隆声。`0` 关闭，常用 `80` | `0` |
| `volume_db` | 额外音量增益（dB） | `0.0` |
| `fade_ms` | **首尾淡入淡出**（毫秒），消除开头爆音 | `0` |
| `trim_silence` | 去掉首尾多余静音 | `false` |

音高对照（`pitch_semitones` → 基频变化）：

| 取值 | 基频变化 | 听感 |
|------|---------|------|
| `-3` | 约 −16% | 明显成熟，接近成年女性 |
| `-2` | 约 −11% | 偏沉稳 |
| `-1.5` | 约 −8% | 温柔，仍保留少女感 |
| `-1` | 约 −6% | 微微变柔 |
| `0` | 不变 | 原样 |
| `+1` | 约 +6% | 更幼更尖 |

> 不知道该调多少时，从 `-1` 或 `-1.5` 开始。
> 超过 `-3` 会从"少女"滑向"成年女性"，和猫娘一类的角色人设容易不搭。

一个常见的组合（更柔和的少女音）：

```toml
[audio]
pitch_semitones = -1.5
speed = 0.95
loudness_normalize = true
fade_ms = 40
trim_silence = true
```

> ffmpeg 处理耗时约 0.2~0.5 秒（一段 8 秒语音）。
> 找不到 ffmpeg 时只会警告一次并跳过处理，**语音照常发送**，不会因此失败。

### API 地址

| 用户类型 | 地址 |
|---------|------|
| Plan 用户（包月/套餐） | `https://token-plan-cn.xiaomimimo.com/v1` |
| 按量计费用户 | `https://api.xiaomimimo.com/v1` |

## 音色模式

### 预置音色（preset）

无需额外配置，直接在 `preset_voice` 中填入音色名称：

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

1. 将 `voice_mode` 改为 `clone`
2. 将参考音频文件放入 `voices/` 目录（支持 WAV 和 MP3）
3. 文件名即为音色名称，建议录音时长 30 秒以上
4. 在 `default_voice` 填入该文件名（不含扩展名）

```
plugins/maibot-mimotts-voice/voices/
└── salt.wav    # 音色名称: salt
```

> 音色匹配顺序：`send_voice_reply` 传入的 `voice_name` → `default_voice` → `clone_voice`。
> 都找不到时会记录一条 error 日志并列出可用音色，不会静默换音色。

## 工具说明

插件提供 `send_voice_reply` 工具给 AI，AI 会根据对话上下文自行判断是否调用。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `reply_text` | string | ✅ | 要合成语音的回复文本 |
| `msg_id` | string | ✅ | 当前消息 ID，用于自动查找聊天流 |
| `style_instruction` | string | ❌ | 语音风格指令 |

### 聊天流定位

`send_voice_reply` 按以下顺序确定语音发往哪个聊天流：

1. 宿主在工具调用时注入的 `stream_id` / `chat_id` / `session_id`（绝大多数情况）
2. 用 `msg_id` 反查消息归属的会话
3. **仅在全局只有一个群会话时**，才用该会话兜底

第 3 步的严格限制是有意为之：早期版本无条件取「第一个群会话」，
在多群环境下会把私聊的语音发进群里。现在宁可放弃发送并降级发文字，也不串流。

### 风格控制方式

风格控制由 AI 根据当前扮演的角色人设和对话情境**自行决定**，无需用户手动指定。AI 会自动判断何时需要风格指令，并选择合适的表达方式。

支持三种风格控制方式，AI 可自由组合使用：

**1. 自然语言风格指令**（AI 通过 `style_instruction` 参数传入）

AI 用自然语言描述语气、情绪、语速等，像给演员说戏一样：

- `一位温柔的少女，声音清甜软糯，语速偏慢，用安慰的语气`
- `语气俏皮活泼，带点小得意，语速偏快`
- `声音低沉严肃，像在教训人，语速慢一些`

**2. 导演模式**（AI 根据场景需要自动启用）

AI 从角色、场景、指导三个维度全方位刻画表演：

```
角色：一位温柔的大姐姐，性格体贴温暖，声音甜美有亲和力。
场景：安慰失恋的朋友。
指导：语调柔和温暖，气息松弛，偶尔带叹息，语速偏慢，尾音上扬带笑意。
```

**3. 音频标签**（AI 在 `reply_text` 中自动插入）

AI 在文本任意位置用括号标注语气/情绪/声音动作：

- 中文：`（紧张）呼……冷静。（叹气）算了。（轻笑）好吧好吧。`
- 英文：`(sighs) I don't know. (laughs) That's funny!`
- 整段风格：`（温柔）你好呀~` `（东北话）哎呀妈呀~` `（唱歌）歌词...`
- 常用标签：开心/悲伤/愤怒/温柔/慵懒/俏皮/磁性/沙哑/甜美/冷漠/叹气/轻笑/哽咽/深呼吸 等

> `style_instruction`（整体风格）+ `reply_text` 中的音频标签（句内细节）可同时使用，两者不冲突。
>
> 语音合成失败降级发文字时，插件会剥掉这些标签（`[fallback] strip_audio_tags`）。
> 剥离只针对白名单里的语气/动作词，普通括号内容一律保留——
> 「宝宝（明天要早起）早点睡」不会变成「宝宝早点睡」。

> 若 AI 未填写 `style_instruction`，插件会使用一段中性的默认风格（只描述音色与生活化的在场感，
> 不指定语气情绪），避免与用户为该聊天流单独配置的人格提示词冲突。

## 常见问题

**Q：日志显示合成成功，但用户没收到语音？**

A：v1.7.0 起会把「发送结果」也计入成功判定；发送失败会自动降级发文字。
若仍无内容，检查 `[fallback] fallback_to_text` 是否为 `false`。

**Q：语音要等很久才出现？**

A：MiMo 接口延迟波动很大（实测同一句话 4.5s ~ 160s+，与文本长度无关）。
v1.7.0 起超过 `request_timeout` 会放弃合成并立即改发文字。
如果经常触发，可以适当调高 `request_timeout`，或把 `audio_format` 改为 `mp3` 减小传输体积。

> 若 `[audio]` 里把 `speed` 调到 1 以下（放慢语速），语音本身会变长，
> 此时建议把 `request_timeout` 一并调大（例如 `60.0`），避免长句被超时切断。

**Q：频繁出现 429？**

A：上游对并发有速率限制。调低 `max_concurrency`（如设为 `1`），
并保持 `skip_when_busy = true` 可显著减少。

**Q：换了 API Key 但要重启才生效？**

A：v1.7.0 起不需要。配置变更会重建服务实例并关闭旧连接池。

**Q：语音音调偏高/偏低，或者语速偏快，能调吗？**

A：能，用 `[audio]` 段。MiMo 接口本身不接受音高、语速参数，所以这部分是插件在本地用
ffmpeg 做的，需要系统里有 ffmpeg。
- 调音高：`pitch_semitones = -1.5` 就是降 1.5 个半音，不知道调多少先试 `-1`。
- 调语速：`speed = 0.95` 会整体放慢 5%，**不改变音高**；`1.05` 则稍快。
详见上面的 `[audio]` 说明。

**Q：开启 `[audio]` 后语音发不出来 / 日志里有 ffmpeg 报错？**

A：不会导致发不出去。处理失败时插件会打一条 warning 并**把未处理的原始音频照常发出去**。
如果日志提示找不到 ffmpeg，确认 `ffmpeg -version` 能跑；不想装就让 `[audio]` 段保持全默认
（`pitch_semitones = 0` 且 `speed = 1.0`），此时 `build_audio_processor()` 返回 `None`，
插件完全不会调用 ffmpeg。

## 目录结构

```
maibot-mimotts-voice/
├── plugin.py             # 插件主入口
├── tts_service.py        # MiMo TTS 服务封装
├── audio_processor.py    # 本地音频后处理（音高/语速/响度，可选）
├── config.toml           # 插件配置（gitignored）
├── config.example.toml   # 配置示例
├── _manifest.json        # 插件清单
├── README.md             # 本文档
├── CHANGELOG.md          # 更新日志
├── LICENSE               # MIT 许可证
├── .gitignore            # Git 忽略规则
├── requirements.txt      # Python 依赖
└── voices/               # 音色复刻文件目录（gitignored）
```

## 更新日志

见 [CHANGELOG.md](./CHANGELOG.md)。

## 注意事项

1. **API Key**：需要在 [小米 MiMo 平台](https://platform.xiaomimimo.com) 申请
2. **音色复刻**：建议录音时长不低于 30 秒，确保无背景噪音
3. **Base64 大小**：音色参考音频的 Base64 编码不能超过 10MB
4. **费用**：MiMo-V2.5-TTS 系列当前限时免费

## 许可证

MIT License
