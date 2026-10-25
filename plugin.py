"""AI Voice Service Plugin for MaiBot."""

import asyncio
import base64
import re
import urllib.parse
from pathlib import Path
from typing import Any, Optional

from maibot_sdk import API, CONFIG_RELOAD_SCOPE_SELF, MaiBotPlugin, Tool, Field, PluginConfigBase
from maibot_sdk.types import ActivationType, ToolParameterInfo, ToolParamType

try:
    from .tts_service import MiMoTTSService
except ImportError:
    from tts_service import MiMoTTSService

try:
    from .audio_processor import AudioProcessor, build_audio_processor
except ImportError:
    from audio_processor import AudioProcessor, build_audio_processor


# 语音失败降级发文字时，剥掉只对 TTS 有意义的语气标签，例如
# （轻笑）（叹气）（温柔）/(sighs)(laughs)。
#
# 这里必须用「白名单」而不是「去掉所有括号」：括号在中文里也用来表补充说明，
# 一刀切会把「宝宝（明天要早起）早点睡」削成「宝宝早点睡」，把「第1名（2026年）」
# 削成「第1名」—— 内容被静默篡改，比多留一个标签糟糕得多。
_AUDIO_TAG_WORDS = (
    "轻笑|笑声|笑|微笑|大笑|苦笑|哈哈笑|"
    "叹气|叹息|轻声叹|深吸气|深呼吸|吸气|呼气|"
    "哽咽|哽咽着|哭|哭泣|抽泣|忍哭|"
    "大声|低声|轻声|柔声|细声|沉声|缓声|"
    "紧张|放松|平静|温柔|冷淡|冷漠|严肃|生气|愤怒|无奈|疑惑|惊讶|开心|悲伤|难过|兴奋|害羞|疲惫|慵懒|俏皮|活泼|低沉|磁性|沙哑|甜美|"
    "咳嗽|清嗓|打哈欠|哈欠|低语|耳语|提高音量|大喊|小声|喃喃|停顿|沉默|犹豫|思索|"
    "东北话|四川话|粤语|日语|英文|唱歌|哼唱|"
    "sighs?|laughs?|chuckles?|giggles?|coughs?|yawns?|whispers?|softly|loudly|"
    "excited|angry|sad|happy|nervous|calm|gentle|tired|surprised|confused"
)
# 只匹配「整段就是一个语气标签」的情形：标签内除了语气词不带别的字符。
_AUDIO_TAG_RE = re.compile(
    rf"[（(]\s*(?:{_AUDIO_TAG_WORDS})\s*[)）]",
    re.IGNORECASE,
)
# 标签删掉后可能在句首/句中/句尾留下多余空格，统一压缩。
_AUDIO_TAG_SPACE_RE = re.compile(r"[ \t]{2,}")


def _strip_audio_tags(text: str) -> str:
    """剥掉语气标签并清理因此产生的多余空白。

    「宝宝（明天要早起）早点睡」这类正常括号内容**必须原样保留**，
    所以只按白名单删已知的语气词，不做「删所有括号」的一刀切。
    """
    cleaned = _AUDIO_TAG_RE.sub("", text or "")
    cleaned = _AUDIO_TAG_SPACE_RE.sub(" ", cleaned)
    return cleaned.strip()


# 内网 / 本机网段：请求打到这些地址时，API Key 只会在局域网内暴露，风险相对可控，
# 但仍值得提示——用户可能以为自己连的是公网服务。
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"})


def _describe_insecure_base_url(raw_url: str) -> str:
    """检查 api_base_url 是否会把密钥以明文发到不安全的目的地。

    `api_base_url` 是完全可以自定义的配置项：如果用户写成 `http://...`，
    或者写错域名解析到别处，API Key 和复刻参考音频就会以明文发出。
    这里只做**检测并告警**，绝不阻断——配置写错时语音仍应能工作，
    把用户卡死在「配置报错但不知道为什么」比多打一条 warning 更糟。

    Args:
        raw_url: 用户配置的 API 地址。

    Returns:
        str: 需要提示的原因；地址安全或为空串时返回空串。
    """
    url = (raw_url or "").strip()
    if not url:
        return ""

    parsed = urllib.parse.urlparse(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()

    if not scheme:
        return f"API 地址 {url!r} 缺少协议头，将按 http 处理，密钥会以明文传输"
    if scheme != "https":
        if host in _LOOPBACK_HOSTS:
            return ""
        return f"API 地址 {url!r} 使用 {scheme}（非 https），API Key 与参考音频会以明文传输"
    if not host:
        return f"API 地址 {url!r} 解析不出主机名，请检查是否写错"
    return ""


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    enabled: bool = Field(default=True, description="是否启用")
    config_version: str = Field(default="1.0.0", description="配置版本")


class VoiceSectionConfig(PluginConfigBase):
    __ui_label__ = "语音设置"
    mimo_api_key: str = Field(default="", description="MiMo API Key")
    api_base_url: str = Field(
        default="https://token-plan-cn.xiaomimimo.com/v1",
        description="MiMo API地址",
        json_schema_extra={"label": "API地址"},
    )
    voice_mode: str = Field(default="clone", description="语音模式: 'clone'(音色复刻) 或 'preset'(预置音色)")
    preset_voice: str = Field(default="mimo_default", description="预置音色ID（仅preset模式生效）")
    voices_dir: str = Field(default="voices", description="音色目录路径（相对于插件目录）")
    default_voice: str = Field(default="", description="默认音色名称（clone模式下为音频文件名）")
    clone_voice: str = Field(default="", description="复刻音色文件名（clone模式下优先使用）")
    audio_format: str = Field(default="wav", description="合成音频格式：wav / mp3 / pcm / pcm16（mp3 体积约 wav 的 1/6）")


class RequestSectionConfig(PluginConfigBase):
    __ui_label__ = "请求控制"
    request_timeout: float = Field(default=45.0, description="单次合成总超时（秒），超时后降级发文字")
    connect_timeout: float = Field(default=10.0, description="建立连接超时（秒）")
    max_retries: int = Field(default=2, description="429/5xx/网络错误的重试次数（超时不重试）")
    retry_backoff: float = Field(default=1.5, description="重试退避基数（秒），按指数增长")
    max_concurrency: int = Field(default=1, description="全局同时进行的合成请求上限。实测并发 2 即触发 429，建议保持 1")


class FallbackSectionConfig(PluginConfigBase):
    __ui_label__ = "失败降级"
    fallback_to_text: bool = Field(default=True, description="语音失败/超时时，把内容改发文字（否则用户什么都收不到）")
    empty_text_placeholder: str = Field(
        default="（想说点什么，但声音没发出来）",
        description="降级时若正文被音频标签清空，用这句兜底（留空则不发）",
    )
    strip_audio_tags: bool = Field(default=True, description="降级发文字时剥掉（轻笑）这类只对 TTS 有意义的标签")
    skip_when_busy: bool = Field(default=True, description="同一聊天流已有语音在合成时，本次直接改发文字")


class AudioSectionConfig(PluginConfigBase):
    """合成后的本地音频处理。

    MiMo 接口只负责把文字念出来，不提供音高/语速/响度这些「调音」参数
    （实测 audio.speed / audio.emotion / 顶层 speed 都被服务端静默忽略，
    见 CHANGELOG）。所以这里用 ffmpeg 在本地做后处理。
    全部默认关闭，不配置就不会调用 ffmpeg。
    """

    __ui_label__ = "音频处理"

    ffmpeg_path: str = Field(
        default="ffmpeg",
        description="ffmpeg 可执行文件路径。留空或写 ffmpeg 表示从 PATH 里找",
    )

    # ---- 音高 ----
    pitch_semitones: float = Field(
        default=0.0,
        description="音高调整（半音）。负数降调更沉稳，正数升调更幼。"
        "-1≈降7%，-2≈降11%，-3≈降16%。0 表示不处理",
    )
    preserve_formant: bool = Field(
        default=True,
        description="降调时保留共振峰（音色更自然，不易变成「慢放感」）",
    )

    # ---- 语速 ----
    speed: float = Field(
        default=1.0,
        description="语速倍率。0.9 慢一点更温柔，1.1 快一点更活泼。范围 0.5~2.0",
    )

    # ---- 响度 ----
    loudness_normalize: bool = Field(
        default=False,
        description="响度标准化（把音量拉到统一水平，避免有的句子小声有的刺耳）",
    )
    loudness_target: float = Field(
        default=-16.0,
        description="响度标准化目标（LUFS）。-16 适合语音，-14 更接近流媒体平台",
    )

    # ---- 音色微调 ----
    highpass_hz: int = Field(
        default=0,
        description="高通滤波截止频率（Hz），去掉低频轰隆声。0 表示关闭，常用 80",
    )
    volume_db: float = Field(
        default=0.0,
        description="额外音量增益（dB），正数变大",
    )

    # ---- 首尾 ----
    fade_ms: int = Field(
        default=0,
        description="首尾淡入淡出时长（毫秒），能消除合成音频开头的爆音。0 表示关闭，常用 30~50",
    )
    trim_silence: bool = Field(
        default=False,
        description="去掉首尾多余静音（部分合成结果开头有半秒空白）",
    )


class VoicePluginConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    voice: VoiceSectionConfig = Field(default_factory=VoiceSectionConfig)
    request: RequestSectionConfig = Field(default_factory=RequestSectionConfig)
    fallback: FallbackSectionConfig = Field(default_factory=FallbackSectionConfig)
    audio: AudioSectionConfig = Field(default_factory=AudioSectionConfig)


class AIVoicePlugin(MaiBotPlugin):
    config_model = VoicePluginConfig

    def __init__(self) -> None:
        super().__init__()
        self.tts_service: Optional[MiMoTTSService] = None
        self.voices: dict[str, str] = {}
        self.default_voice: str = ""
        # 记录每个聊天流是否有语音正在合成。MiMo 接口有速率限制（实测密集请求
        # 返回 429 Too many requests），且单次合成可能长达数十秒；若同一流堆叠
        # 多个语音任务，会互相拖慢并触发限流。已有语音在跑时，新语音直接降级。
        self._voice_inflight: set[str] = set()
        # 持有后台任务的强引用。asyncio 只对任务做弱引用，若不在集合里留一份，
        # 任务可能在执行途中被 GC 回收（官方文档明确提示的坑）。
        self._background_tasks: set[asyncio.Task] = set()
        # 本地音频后处理（降调/语速/响度等）。全部默认关闭时为 None，
        # 不会引入任何额外开销，也不会去探测 ffmpeg。
        self.audio_processor: Optional[AudioProcessor] = None

    async def on_load(self) -> None:
        self.ctx.logger.info("AI Voice Plugin loading...")
        api_key = self.config.voice.mimo_api_key
        if not api_key:
            self.ctx.logger.warning("MiMo API Key not configured")
        self._rebuild_tts_service()
        self.default_voice = self.config.voice.default_voice or self.config.voice.clone_voice
        await self._load_voices()
        self.ctx.logger.info("AI Voice Plugin loaded: mode=%s, default_voice=%s, voices=%s",
            self.config.voice.voice_mode, self.default_voice, list(self.voices.keys()))

    def _rebuild_tts_service(self) -> None:
        """按当前配置重建 TTS 服务实例（热重载时同样走这里）。"""
        request_cfg = self.config.request
        insecure_reason = _describe_insecure_base_url(self.config.voice.api_base_url)
        if insecure_reason:
            self.ctx.logger.warning(
                "%s。如果这不是你有意为之，请把 voice.api_base_url 改成 https 地址",
                insecure_reason,
            )
        self.tts_service = MiMoTTSService(
            api_key=self.config.voice.mimo_api_key,
            api_base_url=self.config.voice.api_base_url,
            logger=self.ctx.logger,
            request_timeout=request_cfg.request_timeout,
            connect_timeout=request_cfg.connect_timeout,
            max_retries=request_cfg.max_retries,
            retry_backoff=request_cfg.retry_backoff,
            max_concurrency=request_cfg.max_concurrency,
        )
        self._rebuild_audio_processor()

    def _rebuild_audio_processor(self) -> None:
        """按当前配置重建音频处理器；没有任何处理项时为 None。"""
        self.audio_processor = build_audio_processor(
            self.config,
            logger=self.ctx.logger,
            work_dir=getattr(getattr(self.ctx, "paths", None), "runtime_dir", None),
        )
        if self.audio_processor is not None:
            chain = self.audio_processor.build_filter_chain()
            self.ctx.logger.info("音频后处理已启用: %s", chain)

    async def on_unload(self) -> None:
        # 先取消还在跑的后台合成任务，否则插件卸载后协程会挂在已关闭的 session 上。
        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()
        self._voice_inflight.clear()
        if self.tts_service:
            await self.tts_service.close()
            self.tts_service = None
        self.voices.clear()

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        self.ctx.logger.info("Plugin config updated: version=%s", version)
        # 重建服务而不是增量 update：原实现只在「新 key 非空」时才更新，
        # 导致把 key 清空后旧 key 仍留在内存里继续计费，且 base_url 同理会残留。
        old_service = self.tts_service
        self._rebuild_tts_service()
        if old_service is not None:
            await old_service.close()
        self.default_voice = self.config.voice.default_voice or self.config.voice.clone_voice
        await self._load_voices()
        self.ctx.logger.info("Config reloaded: mode=%s, default_voice=%s", self.config.voice.voice_mode, self.default_voice)

    def _resolve_voices_dir(self, plugin_dir: Path) -> Path:
        """把 `voices_dir` 解析成插件目录下的一个子目录。

        `voices_dir` 直接参与 `plugin_dir / voices_dir` 拼接，如果配成绝对路径
        或 `../..`，就能读到插件目录之外任意位置的 .wav/.mp3，再 base64 之后
        发给 MiMo —— 等于给了一条「读什么文件就发什么文件出去」的通路。

        `paths` 约定要求插件只往 `data_dir` / `runtime_dir` 写，同理读取音色
        也应该限定在插件自己的目录里。越界时回退到默认的 `voices` 并告警，
        而不是抛错：音色目录配错了，插件其余功能仍应可用。
        """
        raw = (self.config.voice.voices_dir or "voices").strip() or "voices"
        base = plugin_dir.resolve()
        try:
            candidate = (base / raw).resolve()
        except (OSError, ValueError) as exc:
            self.ctx.logger.warning("voices_dir %r 无法解析（%s），回退到 'voices'", raw, exc)
            return base / "voices"

        if not candidate.is_relative_to(base):
            self.ctx.logger.warning(
                "voices_dir %r 指向插件目录之外（%s），已回退到 'voices'。"
                "音色目录必须位于插件目录内，否则可能被用来读取任意本地文件",
                raw,
                candidate,
            )
            return base / "voices"
        return candidate

    async def _load_voices(self) -> None:
        plugin_dir = Path(__file__).parent
        voices_dir = self._resolve_voices_dir(plugin_dir)
        self.ctx.logger.info("Loading voices from: %s (plugin_dir=%s)", voices_dir, plugin_dir)

        # 重新扫描前先清空：原实现只在「找到音频文件」时才 clear，
        # 把 voices 目录里的文件全删掉后热重载，旧音色仍会被继续引用。
        self.voices.clear()

        if not voices_dir.exists():
            # 不在这里 mkdir：插件只应往 data_dir / runtime_dir 写，
            # 在源码目录里建空文件夹既会被 .gitignore 吞掉，也让用户以为
            # 放进去的文件会被同步。直接告诉他该建哪个目录即可。
            self.ctx.logger.warning(
                "音色目录不存在: %s，请先创建它并把 .wav/.mp3 参考音频放进去", voices_dir
            )
            return

        self.ctx.logger.info("Voices dir exists: %s, contents: %s", voices_dir, [f.name for f in voices_dir.iterdir()])

        audio_files = sorted(list(voices_dir.glob("*.wav")) + list(voices_dir.glob("*.mp3")))
        if not audio_files:
            self.ctx.logger.warning("No audio files found in: %s", voices_dir)
            return

        for audio_file in audio_files:
            voice_name = audio_file.stem
            file_size = audio_file.stat().st_size
            suffix = audio_file.suffix.lower()
            mime_type = "audio/wav" if suffix == ".wav" else "audio/mpeg"
            with open(audio_file, "rb") as f:
                audio_bytes = f.read()
            b64_data = base64.b64encode(audio_bytes).decode("ascii")
            # Store with MIME type prefix for API call
            self.voices[voice_name] = f"data:{mime_type};base64,{b64_data}"
            self.ctx.logger.info("Loaded voice: %s, raw=%dKB, b64=%dKB, mime=%s", voice_name, file_size // 1024, len(b64_data) // 1024, mime_type)

        if not self.default_voice and self.voices:
            self.default_voice = next(iter(self.voices))
            self.ctx.logger.info("Using default voice: %s", self.default_voice)

        self.ctx.logger.info("Voice loading complete, total %d voices, names=%s", len(self.voices), list(self.voices.keys()))

    async def _find_stream_id(self, kwargs: dict, msg_id: str = "") -> str:
        """定位本次回复该发往哪个聊天流。

        优先级：宿主动态注入的上下文 → 用 msg_id 反查消息 → 单群兜底。

        原实现的第一顺位是「拿不到就取第一个群流」，那在多群环境下会把私聊的
        语音发进群里，是很危险的串流 bug。现在改为先按 msg_id 反查真实会话，
        只有确认「全局只有一个群会话」时才允许兜底，其余情况直接失败并降级文字。
        """
        # 1) 宿主在工具调用时注入的上下文（绝大多数情况走这条）
        sid = kwargs.get("stream_id") or kwargs.get("chat_id") or kwargs.get("session_id")
        if sid:
            return str(sid)

        # 2) 用 msg_id 反查消息归属的会话
        if msg_id:
            try:
                message = await self.ctx.message.get_by_id(str(msg_id), include_binary_data=False)
                resolved = self._stream_id_from_message(message)
                if resolved:
                    self.ctx.logger.info("Resolved stream_id from message %s: %s", msg_id, resolved)
                    return resolved
            except Exception as e:
                self.ctx.logger.warning("message.get_by_id failed for %s: %s", msg_id, e)

        # 3) 兜底：只有「全局确实只有一个会话」时才敢猜
        #
        # 这里必须用 get_all_streams 而不是 get_group_streams：后者只返回群聊，
        # 在「1 个群 + 若干私聊」的环境里 len==1 也会成立，于是私聊消息会拿到
        # 那个唯一的群流 —— 正是我们要防的串流。只看全量会话数才能确认没猜错。
        #
        # platform 必须显式传：SDK 给这些方法定的默认值是 "qq"，宿主的
        # `_list_sessions()` 会按平台过滤。多平台部署时若用默认值，只会数到 QQ
        # 会话 —— 别的平台上明明还有会话却数出「只有 1 个」，兜底就猜错了。
        #
        # 拿不到 platform 时传 "all_platforms" 这个宿主约定的通配值（见
        # `_list_sessions` 里 `platform == "all_platforms" or session.platform == platform`）。
        # 注意不能传空串：空串只会匹配 platform 为空的会话，等于一个都查不到。
        # 查全平台是更保守的选择 —— 会话数只会多不会少，宁可放弃发送也不发错地方。
        platform = str(kwargs.get("platform") or "").strip() or "all_platforms"
        try:
            all_streams = await self.ctx.chat.get_all_streams(platform=platform) or []
            if len(all_streams) == 1 and isinstance(all_streams[0], dict):
                sid = all_streams[0].get("stream_id", "")
                if sid:
                    self.ctx.logger.warning(
                        "无法从上下文定位聊天流，但全局只有一个会话，用它兜底: %s", sid
                    )
                    return str(sid)
            if len(all_streams) > 1:
                self.ctx.logger.error(
                    "无法确定消息所属聊天流（msg_id 反查失败，全局有 %d 个会话）；"
                    "为避免发错会话，本次放弃语音发送",
                    len(all_streams),
                )
        except Exception as e:
            self.ctx.logger.warning("chat.get_all_streams failed: %s", e)

        return ""

    @staticmethod
    def _stream_id_from_message(message: Any) -> str:
        """从 message.get_by_id 的返回值里挖出 stream_id / session_id。"""
        if not isinstance(message, dict):
            return ""
        for key in ("session_id", "stream_id", "chat_id"):
            value = message.get(key)
            if value:
                return str(value)
        message_info = message.get("message_info")
        if isinstance(message_info, dict):
            for key in ("stream_id", "session_id", "chat_id"):
                value = message_info.get(key)
                if value:
                    return str(value)
        return ""

    async def _apply_audio_processing(self, audio_b64: str, audio_format: str) -> str:
        """对合成结果做本地后处理；任何失败都返回原始 base64。"""
        # 没有配置任何处理项时 build_audio_processor 返回 None，
        # 这里再兜一层，避免调用方漏判时炸掉整条语音。
        processor = self.audio_processor
        if processor is None or not audio_b64:
            return audio_b64

        try:
            raw = base64.b64decode(audio_b64, validate=False)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("音频 base64 解码失败，跳过后处理: %r", exc)
            return audio_b64

        if not raw:
            return audio_b64

        processed = await processor.process(raw, audio_format)
        if processed is raw or processed == raw:
            return audio_b64

        try:
            return base64.b64encode(processed).decode("ascii")
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("音频 base64 重新编码失败，使用原始音频: %r", exc)
            return audio_b64

    async def _send_voice(self, audio_b64: str, stream_id: str) -> bool:
        """Send voice message using base64 format.

        宿主 send.custom 对 "voice" 类型会把 content 当作纯 base64 交给
        VoiceComponent（适配器再走 binary_data_base64），而 "record" 类型只会
        落到 DictComponent 原样透传，NapCat 静默失败却不报错。因此先走 voice。
        """
        self.ctx.logger.info("Sending voice to stream=%s, b64_len=%d", stream_id, len(audio_b64))

        # Method 1: voice type with pure base64 -> VoiceComponent(binary_data)
        try:
            result = await self.ctx.send.custom("voice", audio_b64, stream_id)
            if result is False:
                self.ctx.logger.warning("voice+pure-base64 returned False")
            else:
                self.ctx.logger.info("Sent via voice+pure-base64")
                return True
        except Exception as e:
            self.ctx.logger.warning("voice+pure-base64 failed: %s", e)

        # Method 2: record type with base64 url (NapCat fallback)
        try:
            result = await self.ctx.send.custom("record", {"file": f"base64://{audio_b64}"}, stream_id)
            if result is False:
                self.ctx.logger.warning("record+base64 returned False")
            else:
                self.ctx.logger.info("Sent via record+base64")
                return True
        except Exception as e:
            self.ctx.logger.warning("record+base64 failed: %s", e)

        self.ctx.logger.error("All voice send methods failed")
        return False

    def _resolve_voice(self, voice_name: str) -> tuple[str, str, str]:
        """解析音色名称，返回 (voice_key, audio_base64, mode)。
        mode: 'clone' 使用音色复刻, 'preset' 使用预置音色。
        """
        voice_mode = self.config.voice.voice_mode
        explicit = voice_name if voice_name and voice_name != "default" else ""

        # clone 模式：从本地音频文件中查找参考音频
        if voice_mode == "clone":
            voice_key = explicit or self.default_voice
            if voice_key and voice_key in self.voices:
                return voice_key, self.voices[voice_key], "clone"

            # clone_voice 配置兜底
            clone_fallback = self.config.voice.clone_voice
            if clone_fallback and clone_fallback in self.voices:
                self.ctx.logger.warning("Voice '%s' not found, using clone_voice config: '%s'", voice_key, clone_fallback)
                return clone_fallback, self.voices[clone_fallback], "clone"

            # 如果 voices 非空但指定的没有，报错而非静默切换
            if self.voices:
                available = list(self.voices.keys())
                self.ctx.logger.error("Voice '%s' not found in voices! Available: %s. "
                    "请在 config.toml 的 clone_voice 或 default_voice 中指定一个已有的音色名。", voice_key, available)
                return "", "", ""

            self.ctx.logger.error("No voices loaded! Please put .wav/.mp3 files in the voices/ directory.")
            return "", "", ""

        # preset 模式：只认显式传入的音色名或 preset_voice。
        # 不能回落到 default_voice —— 那是 clone 模式下的音频文件名（如 "luoxi"），
        # 当成预置音色 ID 发出去会被上游拒绝。
        preset_id = explicit or self.config.voice.preset_voice or "mimo_default"
        return preset_id, "", "preset"

    @API(
        "voice_clone_tts",
        description=(
            "合成语音并发送到指定聊天流。传入 stream_id 时会真的把语音发进那个会话，"
            "不只是返回音频数据；不想发送就留空 stream_id。"
        ),
        version="1",
        public=True,
    )
    async def voice_clone_tts(self, text: str, style_instruction: str = "", stream_id: str = "", voice_name: str = "") -> dict[str, Any]:
        if not self.tts_service:
            return {"success": False, "error": "TTS service not initialized"}

        if not text or not text.strip():
            return {"success": False, "error": "Empty text"}

        voice_key, ref_audio, mode = self._resolve_voice(voice_name)
        if not voice_key:
            return {"success": False, "error": "No voice configured. Check voice config."}

        audio_format = (self.config.voice.audio_format or "mp3").strip().lower()
        if audio_format not in MiMoTTSService.SUPPORTED_FORMATS:
            self.ctx.logger.warning(
                "不支持的 audio_format=%r，回退到 mp3（支持：%s）",
                audio_format, ", ".join(MiMoTTSService.SUPPORTED_FORMATS),
            )
            audio_format = "mp3"

        self.ctx.logger.info("TTS request: mode=%s, voice_key=%s, text_len=%d, style_len=%d, format=%s",
            mode, voice_key, len(text), len(style_instruction), audio_format)

        try:
            if mode == "clone":
                result = await self.tts_service.synthesize_with_voice_clone(
                    text=text, reference_audio_base64=ref_audio,
                    style_instruction=style_instruction, audio_format=audio_format,
                )
            else:
                result = await self.tts_service.synthesize_with_preset(
                    text=text, voice_id=voice_key,
                    style_instruction=style_instruction, audio_format=audio_format,
                )

            # 释放参考音频引用
            ref_audio = ""

            success = result.get("success")
            error = result.get("error", "")
            audio_b64 = result.get("audio_base64", "")
            self.ctx.logger.info("TTS result: success=%s, audio_len=%d, error=%s", success, len(audio_b64), error)

            if success and audio_b64 and stream_id:
                # 提取纯 base64 数据
                if "base64," in audio_b64:
                    audio_b64 = audio_b64.split("base64,", 1)[1]
                    audio_b64 = audio_b64.strip()

                # 立即从 result 中移除大字段，避免双重内存占用
                result.pop("audio_base64", None)

                # 本地音频后处理（降调/语速/响度等）。失败会原样返回原始音频。
                if self.audio_processor is not None:
                    audio_b64 = await self._apply_audio_processing(audio_b64, audio_format)

                # 把发送结果也纳入 success，否则「合成成功但发不出去」
                # 会被当成成功，用户收不到任何东西。
                sent = await self._send_voice(audio_b64, stream_id)
                audio_b64 = ""
                if not sent:
                    result["success"] = False
                    result["error"] = "voice send failed"
            elif audio_b64:
                # 不需要发送时也清理
                result.pop("audio_base64", None)

            return result
        except Exception as e:
            self.ctx.logger.error("TTS failed: %s", e)
            return {"success": False, "error": str(e)}

    @Tool(
        "send_voice_reply",
        brief_description=(
            "用语音说话给用户听，是和打字并列的常规回复方式，不必等用户先开口要求。"
            "语音要花几秒到几十秒合成，所以它适合短而贴心的话，不适合需要阅读的长内容。"
            "优先用语音：撒娇、关心对方作息身体、表达想念或喜欢、调情、吐槽接梗、"
            "对方情绪低落需要安慰、深夜安静聊天、对方刚发来语音、对方明确要求语音。"
            "改用文字：超过两三句的长内容、需要列条目或仔细阅读的信息、要讲清楚的原理解释、"
            "上一条刚用过语音、或只是「嗯」「好呀」这类一句话就完的应答。"
            "私聊里可以多说，大约一半回复用语音；群聊里以打字为主，只在有人点名要你说话、"
            "气氛特别热、被逗笑、安慰难过的人或深夜安静时偶尔用一次。"
            "语音会替代本次文字回复，不要对同一条内容既发文字又发语音。"
            "可选 style_instruction 控制语气情绪。"
        ),
        detailed_description=(
            "使用语音进行回复。这是一种和打字并列的常规回复方式，不必等用户先开口要求。\n"
            "遇到以下情形应当优先用语音而不是文字：撒娇、关心对方的作息和身体、表达想念或喜欢、"
            "撩拨调情、对方情绪低落需要安慰、深夜安静地聊天、对方刚发来语音消息、对方明确要求语音。\n"
            "换用文字更合适的情形：超过两三句的长内容、需要对方仔细阅读的信息、要讲清原理解释的内容、"
            "上一条刚用过语音、或者只是「嗯」「好呀」这类一句话就结束的应答。\n"
            "私人一对一的聊天里大概一半用语音，群里则相对克制，只在气氛合适时偶尔用一次。\n"
            "注意：语音会直接替代这次文字回复，不要对同一条内容既发文字又发语音。\n"
            "必填参数：reply_text（回复文本）、msg_id（当前消息ID）。\n"
            "可选参数：style_instruction（语音风格指令），不传则使用默认音色自然朗读，需要更生动的表达时可填写。\n\n"
            "【风格控制】可通过 style_instruction 参数精细控制语音演绎效果，"
            "根据你当前扮演的角色人设和对话情境，主动提供完整的风格描述，让语音更拟人、更有感染力。\n\n"
            "支持三种风格控制方式，可自由组合：\n\n"
            "1. 自然语言风格指令（推荐）：用自然语言描述语气、情绪、语速等，像给演员说戏一样。\n"
            "   示例：'一位温柔的少女，声音清甜软糯，语速偏慢，用安慰的语气，带点关切'\n"
            "   示例：'语气俏皮活泼，带点小得意，语速偏快，声音明亮有活力'\n"
            "   示例：'声音低沉严肃，像在教训人，语速慢一些，带点长辈的威严'\n\n"
            "2. 导演模式（高级）：从角色、场景、指导三个维度全方位刻画表演，适合需要高度拟人化的场景。\n"
            "   示例：'角色：一位温柔的大姐姐，性格体贴温暖，声音甜美有亲和力。"
            "场景：安慰失恋的朋友。指导：语调柔和温暖，气息松弛，偶尔带叹息，语速偏慢，尾音上扬带笑意。'\n"
            "   示例：'角色：百年门阀的大小姐，声音冷冽有威压，说话语速极慢，每个字都像在舌尖滚过。"
            "场景：在祠堂面对企图带她私奔的男人。指导：实音重且硬，尾音处加入轻微气音透出疲惫。'\n\n"
            "3. 音频标签（在 reply_text 中使用）：在文本任意位置用括号标注语气/情绪/声音动作。\n"
            "   中文全角/半角均可：（紧张）呼……冷静。（叹气）算了。（轻笑）好吧好吧。\n"
            "   英文：(sighs) I don't know. (laughs) That's funny!\n"
            "   整段风格标签：在文本开头加（温柔）你好呀~ 或（东北话）哎呀妈呀~ 或（唱歌）歌词...\n"
            "   常用风格标签：开心/悲伤/愤怒/温柔/慵懒/俏皮/磁性/沙哑/甜美/冷漠/严肃/活泼/深沉 等\n"
            "   常用动作标签：叹气/轻笑/哽咽/深呼吸/咳嗽/打哈欠/低语/提高音量 等\n\n"
            "提示：style_instruction（整体风格）+ reply_text 中的音频标签（句内细节）可同时使用，两者不冲突。"
        ),
        activation_type=ActivationType.ALWAYS,
        visibility="visible",
        parameters=[
            ToolParameterInfo(name="reply_text", param_type=ToolParamType.STRING, description="回复文本。可在文本中插入音频标签控制句内语气细节，如（叹气）（轻笑）（温柔）（紧张）等。", required=True),
            ToolParameterInfo(name="msg_id", param_type=ToolParamType.STRING, description="当前消息ID", required=True),
            ToolParameterInfo(
                name="style_instruction",
                param_type=ToolParamType.STRING,
                description=(
                    "（可选）语音风格指令，用自然语言描述语气、情绪、语速等，让语音更拟人。\n"
                    "不传则默认朗读。当你觉得需要更生动的表达时再填写。\n"
                    "简单用法：'温柔安慰的语气，语速稍慢'\n"
                    "导演模式：'角色：XX，性格XX。场景：XX。指导：语调XX，语速XX，气息XX。'\n"
                    "也可配合 reply_text 中的音频标签（叹气/轻笑/停顿等）使用。"
                ),
                required=False,
                default="",
            ),
        ],
    )
    async def send_voice_reply(self, reply_text: str, msg_id: str = "", style_instruction: str = "", **kwargs: Any) -> dict[str, Any]:
        # Find the correct stream_id
        stream_id = await self._find_stream_id(kwargs, msg_id=msg_id)
        if not stream_id:
            self.ctx.logger.error("Cannot find stream_id for msg_id=%s, kwargs_keys=%s", msg_id, list(kwargs.keys()))
            return {
                "success": False,
                "content": "无法确定这条消息属于哪个聊天流，语音没有发送。请改用 reply 工具回复。",
                "method": "voice",
                "error": "Cannot find chat stream",
            }

        if not (reply_text or "").strip():
            return {
                "success": False,
                "content": "reply_text 为空，没有可朗读的内容。",
                "method": "voice",
                "error": "Empty reply_text",
            }

        self.ctx.logger.info("Using stream_id=%s for voice reply, text_len=%d", stream_id, len(reply_text))

        # Always use the configured default voice.
        # 必须把 task 存进集合持有强引用：asyncio 只保留弱引用，不持有的话
        # 协程可能在合成途中被 GC 掉，表现为「日志停在 TTS request 就没有下文」。
        task = asyncio.create_task(self._async_voice_reply(reply_text, style_instruction, stream_id, ""))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        # content 是回给模型的可读文本，模型看到空结果会误判成调用失败，
        # 然后把同样的内容再用 reply 发一遍 —— 和「语音顶替本次文字回复」
        # 的设计直接打架。这里明确告诉它语音已经发出去了，不要再补文字。
        return {
            "success": True,
            "content": "语音已开始合成并发送，这条内容已经回复过了，不需要再用 reply 发送文字。",
            "method": "voice",
            "error": "",
        }

    # LLM 未填 style_instruction 时的兜底风格。
    # 必须保持中性：各聊天流的专属 prompt 已分别指定风格，这里再带任何
    # 情绪倾向（活泼/软糯/低沉等）都会在漏填时和对应人设冲突。
    # 只说“声音本身”和“在场感”，不指定语气、情绪、语速。
    DEFAULT_VOICE_STYLE = (
        "一位年轻女性的声音，自然、生活化，像平时和人面对面聊天。"
        "吐字清楚，气息放松；不带情绪倾向，不表演，不播音腔，不过度卖萌。"
    )

    async def _async_voice_reply(self, text: str, style_instruction: str, stream_id: str, voice_name: str) -> None:
        """Send voice reply asynchronously.

        语音合成失败/超时时降级发文字。因为 send_voice_reply 会顶替本次文字回复，
        若不降级，用户会什么都收不到（实测出现过 TTS 卡 163s、以及上游 429/超时
        导致整条回复消失）。
        """
        fallback_cfg = self.config.fallback
        if fallback_cfg.skip_when_busy and stream_id in self._voice_inflight:
            self.ctx.logger.warning("语音仍在合成中，本次直接改发文字（避免堆叠与限流）")
            await self._fallback_to_text(text, stream_id)
            return
        self._voice_inflight.add(stream_id)
        try:
            effective_style = (style_instruction or "").strip() or self.DEFAULT_VOICE_STYLE
            if not (style_instruction or "").strip():
                self.ctx.logger.info("No style_instruction provided, using neutral default voice style")
            result = await self.voice_clone_tts(text=text, style_instruction=effective_style, stream_id=stream_id, voice_name=voice_name)
            success = bool(result.get("success"))
            self.ctx.logger.info("Async voice reply result: %s", success)
            if not success:
                reason = str(result.get("error", "") or "unknown")
                self.ctx.logger.warning("Voice reply failed (%s), falling back to text", reason)
                await self._fallback_to_text(text, stream_id)
        except asyncio.CancelledError:
            # 插件卸载时会 cancel 掉后台任务，这里不该再发消息，直接向上抛。
            self.ctx.logger.info("语音任务被取消 stream=%s", stream_id)
            raise
        except Exception as e:
            self.ctx.logger.error("Async voice reply failed: %s", e)
            await self._fallback_to_text(text, stream_id)
        finally:
            self._voice_inflight.discard(stream_id)

    async def _fallback_to_text(self, text: str, stream_id: str) -> None:
        """语音失败时把回复内容改发文字，避免本次回复彻底丢失。"""
        fallback_cfg = self.config.fallback
        if not fallback_cfg.fallback_to_text:
            self.ctx.logger.warning("已禁用文字降级，本次回复将丢失 text_len=%d", len(text or ""))
            return

        # 去掉只对 TTS 有意义的音频标签，如（轻笑）（叹气）(laughs)，
        # 但保留正常括号内容（「宝宝（明天要早起）早点睡」不能变成「宝宝早点睡」）。
        plain = (text or "").strip()
        if fallback_cfg.strip_audio_tags:
            plain = _strip_audio_tags(plain)

        if not plain:
            placeholder = (fallback_cfg.empty_text_placeholder or "").strip()
            if not placeholder:
                self.ctx.logger.warning("降级文字为空且未配置兜底文案，本次回复将丢失")
                return
            plain = placeholder

        try:
            ok = await self.ctx.send.text(plain, stream_id)
            self.ctx.logger.info("Fallback text sent=%s len=%d", ok, len(plain))
        except Exception as e:
            self.ctx.logger.error("Fallback text send failed: %s", e)


def create_plugin() -> AIVoicePlugin:
    return AIVoicePlugin()
