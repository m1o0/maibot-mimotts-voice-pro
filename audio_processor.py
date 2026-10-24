"""本地音频后处理（依赖 ffmpeg）。

MiMo 接口只负责把文字念出来，不提供音高、语速、响度等「调音」参数——
实测 `audio.speed`、`audio.emotion` 以及顶层 `speed` 都被服务端静默忽略，
返回的音频与不传时完全一致（详见 CHANGELOG）。所以这些调整只能在客户端做。

设计原则：
- **全部默认关闭**。没配任何一项时 `build_filter_chain()` 返回空串，
  调用方直接跳过，不会 spawn 进程。
- **失败不致命**。ffmpeg 不可用、滤镜报错、超时，一律原样返回未处理的音频，
  只记一条 warning。调音只是锦上添花，不该让整条语音发不出去。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# 各格式对应的 ffmpeg 编码参数。mp3/wav 用固定码率保证体积可控；
# pcm / pcm16 是裸流（无文件头），只能指定采样格式。
# 实测上游 pcm 与 pcm16 都是 s16le / 24kHz / 单声道，故参数一致。
_PCM_OUTPUT_ARGS: list[str] = ["-f", "s16le", "-c:a", "pcm_s16le", "-ar", "24000", "-ac", "1"]
_ENCODERS: dict[str, list[str]] = {
    "mp3": ["-c:a", "libmp3lame", "-b:a", "128k"],
    "wav": ["-c:a", "pcm_s16le"],
    "pcm": list(_PCM_OUTPUT_ARGS),
    "pcm16": list(_PCM_OUTPUT_ARGS),
}

# 裸 PCM 没有文件头，输入侧也必须显式声明格式，否则 ffmpeg 无法探测。
_PCM_INPUT_ARGS: list[str] = ["-f", "s16le", "-ar", "24000", "-ac", "1"]
_DECODER_ARGS: dict[str, list[str]] = {
    "pcm": list(_PCM_INPUT_ARGS),
    "pcm16": list(_PCM_INPUT_ARGS),
}

# 半音 -> 频率倍率。rubberband 的 pitch 参数直接吃这个倍率。
_SEMITONE_RATIO = 2.0 ** (1.0 / 12.0)


@dataclass(frozen=True)
class AudioOptions:
    """从配置提取出的、与音频处理相关的选项快照。"""

    ffmpeg_path: str = "ffmpeg"
    pitch_semitones: float = 0.0
    preserve_formant: bool = True
    speed: float = 1.0
    loudness_normalize: bool = False
    loudness_target: float = -16.0
    highpass_hz: int = 0
    volume_db: float = 0.0
    fade_ms: int = 0
    trim_silence: bool = False

    @property
    def is_noop(self) -> bool:
        """所有可能改变音频的项都是默认值时返回 True。"""
        return not (
            abs(self.pitch_semitones) > 1e-6
            or abs(self.speed - 1.0) > 1e-6
            or self.loudness_normalize
            or self.highpass_hz > 0
            or abs(self.volume_db) > 1e-6
            or self.fade_ms > 0
            or self.trim_silence
        )


class AudioProcessor:
    """把 ffmpeg 滤镜链包装成一个可 await 的调用。"""

    def __init__(self, options: AudioOptions, logger: Optional[logging.Logger] = None) -> None:
        self.options = options
        self.logger = logger or logging.getLogger(__name__)
        # 只在首次真正需要处理时才去解析 ffmpeg 路径并做一次可用性检查，
        # 避免不加处理的用户白白承担一次 shutil.which。
        self._ffmpeg: Optional[str] = None
        self._resolved = False
        self._warned_unavailable = False

    # ------------------------------------------------------------------ ffmpeg 定位

    def _resolve_ffmpeg(self) -> Optional[str]:
        """解析 ffmpeg 可执行文件；找不到返回 None（只会警告一次）。"""
        if self._resolved:
            return self._ffmpeg

        self._resolved = True
        configured = (self.options.ffmpeg_path or "ffmpeg").strip()
        candidates: list[str] = []

        # 配置项既可以是命令名（走 PATH），也可以是绝对/相对路径。
        # 只有带路径分隔符或盘符时才当文件检查，避免把裸命令名误判成工作目录里的同名文件。
        looks_like_path = any(sep in configured for sep in (os.sep, "/", "\\")) or Path(configured).is_absolute()
        if looks_like_path:
            expanded = Path(configured).expanduser()
            if expanded.is_file():
                candidates.append(str(expanded))
            else:
                self.logger.warning("配置的 ffmpeg 路径不存在: %s，将回退到 PATH 查找", configured)

        found = shutil.which(configured)
        if found:
            candidates.append(found)

        if not candidates:
            # 兜底：有些人会写成 "ffmpeg.exe" 或干脆留空
            for alt in ("ffmpeg", "ffmpeg.exe"):
                if alt == configured:
                    continue
                alt_found = shutil.which(alt)
                if alt_found:
                    candidates.append(alt_found)
                    break

        self._ffmpeg = candidates[0] if candidates else None
        if not self._ffmpeg and not self._warned_unavailable:
            self._warned_unavailable = True
            self.logger.warning(
                "已配置音频处理但找不到 ffmpeg（当前路径: %r），本次将跳过音频处理。"
                "请安装 ffmpeg 或把 audio.ffmpeg_path 指向可执行文件。",
                configured,
            )
        elif self._ffmpeg:
            self.logger.debug("音频处理使用 ffmpeg: %s", self._ffmpeg)
        return self._ffmpeg

    # ------------------------------------------------------------------ 滤镜链

    def build_filter_chain(self) -> str:
        """按配置拼出 ffmpeg -af 的滤镜链，空串表示无需处理。"""
        if self.options.is_noop:
            return ""

        o = self.options
        filters: list[str] = []

        # 高通放最前面：先去掉低频噪声，后面的处理不会被轰隆声带偏
        if o.highpass_hz > 0:
            hz = max(20, min(int(o.highpass_hz), 20000))
            filters.append(f"highpass=f={hz}")

        # 音高：rubberband 同时改音高与共振峰，比 asetrate+atempo 自然。
        # asetrate 会把共振峰一起搬移，听感像"慢放"。
        if abs(o.pitch_semitones) > 1e-6:
            ratio = _SEMITONE_RATIO ** float(o.pitch_semitones)
            ratio = max(0.25, min(ratio, 4.0))
            if o.preserve_formant:
                filters.append(f"rubberband=pitch={ratio:.6f}:formant=preserved")
            else:
                filters.append(f"rubberband=pitch={ratio:.6f}")
            # 注意：rubberband 默认保持时长不变，所以音高与语速是独立的，
            # 不需要再配 atempo 补偿。

        # 语速：单个 atempo 只接受 0.5~2.0，超出范围要串联多个。
        if abs(o.speed - 1.0) > 1e-6:
            speed = max(0.1, min(float(o.speed), 10.0))
            while speed > 2.0:
                filters.append("atempo=2.0")
                speed /= 2.0
            while speed < 0.5:
                filters.append("atempo=0.5")
                speed /= 0.5
            filters.append(f"atempo={speed:.6f}")

        # 响度标准化放在音量调整之前，避免后面的增益把 loudnorm 的结论推翻
        if o.loudness_normalize:
            target = max(-70.0, min(float(o.loudness_target), -5.0))
            filters.append(f"loudnorm=I={target}:TP=-1.5:LRA=11")

        if abs(o.volume_db) > 1e-6:
            db = max(-40.0, min(float(o.volume_db), 20.0))
            filters.append(f"volume={db:.2f}dB")

        # 去首尾静音：用 silenceremove 掐头去尾。
        # 尾部要反向处理，所以整体是 掐头→反向→掐头→反向。
        if o.trim_silence:
            filters.extend(
                [
                    "silenceremove=start_periods=1:start_duration=0:start_threshold=-50dB",
                    "areverse",
                    "silenceremove=start_periods=1:start_duration=0:start_threshold=-50dB",
                    "areverse",
                ]
            )

        # 淡入淡出压最后，保证作用在最终波形上
        if o.fade_ms > 0:
            d = max(1, min(int(o.fade_ms), 2000)) / 1000.0
            filters.append(f"afade=t=in:st=0:d={d:.3f}")
            # 淡出需要知道总时长，用 areverse 技巧实现尾淡出
            filters.extend([f"areverse", f"afade=t=in:st=0:d={d:.3f}", "areverse"])

        return ",".join(filters)

    # ------------------------------------------------------------------ 执行

    async def process(self, audio: bytes, audio_format: str = "mp3") -> bytes:
        """对合成结果做后处理；任何失败都返回原始音频。"""
        chain = self.build_filter_chain()
        if not chain or not audio:
            return audio

        ffmpeg = self._resolve_ffmpeg()
        if not ffmpeg:
            return audio

        fmt = (audio_format or "mp3").lower()
        encoder = _ENCODERS.get(fmt)
        if encoder is None:
            self.logger.warning("未知音频格式 %r，跳过音频处理", audio_format)
            return audio

        suffix = ".pcm" if fmt in ("pcm", "pcm16") else f".{fmt}"
        tmpdir = None
        try:
            tmpdir = tempfile.mkdtemp(prefix="mimotts_audio_")
            src = Path(tmpdir) / f"in{suffix}"
            dst = Path(tmpdir) / f"out{suffix}"
            src.write_bytes(audio)

            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
            # 裸 PCM 没有文件头，必须先告诉 ffmpeg 输入是什么格式
            cmd.extend(_DECODER_ARGS.get(fmt, []))
            cmd.extend(["-i", str(src), "-af", chain])
            cmd.extend(encoder)
            cmd.append(str(dst))

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                # 10 秒足够处理一分钟以内的语音；卡住说明 ffmpeg 出了问题
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                self.logger.warning("ffmpeg 处理超时（>10s），本次跳过音频处理")
                return audio

            if proc.returncode != 0:
                msg = (stderr or b"").decode("utf-8", "ignore").strip()[:300]
                self.logger.warning("ffmpeg 处理失败（exit=%s），本次跳过音频处理: %s", proc.returncode, msg)
                return audio

            if not dst.is_file() or dst.stat().st_size == 0:
                self.logger.warning("ffmpeg 未产出有效音频，本次跳过音频处理")
                return audio

            result = dst.read_bytes()
            self.logger.info(
                "音频处理完成: %s -> %s 字节（滤镜: %s）",
                len(audio),
                len(result),
                chain[:120],
            )
            return result

        except FileNotFoundError:
            self.logger.warning("调用 ffmpeg 失败（文件不存在），本次跳过音频处理")
            return audio
        except Exception as exc:  # noqa: BLE001 - 调音失败绝不该影响发语音
            self.logger.warning("音频处理异常，本次跳过: %r", exc)
            return audio
        finally:
            if tmpdir:
                shutil.rmtree(tmpdir, ignore_errors=True)


def build_audio_processor(config: object, logger: Optional[logging.Logger] = None) -> Optional[AudioProcessor]:
    """从插件配置构建处理器；全部默认值时返回 None（不产生任何开销）。"""
    section = getattr(config, "audio", None)
    if section is None:
        return None

    options = AudioOptions(
        ffmpeg_path=getattr(section, "ffmpeg_path", "ffmpeg") or "ffmpeg",
        pitch_semitones=float(getattr(section, "pitch_semitones", 0.0) or 0.0),
        preserve_formant=bool(getattr(section, "preserve_formant", True)),
        speed=float(getattr(section, "speed", 1.0) or 1.0),
        loudness_normalize=bool(getattr(section, "loudness_normalize", False)),
        loudness_target=float(getattr(section, "loudness_target", -16.0) or -16.0),
        highpass_hz=int(getattr(section, "highpass_hz", 0) or 0),
        volume_db=float(getattr(section, "volume_db", 0.0) or 0.0),
        fade_ms=int(getattr(section, "fade_ms", 0) or 0),
        trim_silence=bool(getattr(section, "trim_silence", False)),
    )
    if options.is_noop:
        return None
    return AudioProcessor(options, logger=logger)
