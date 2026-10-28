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

# 裸 PCM 采样参数（上游实测 s16le / 24kHz / 单声道）。
# 发送给 QQ 前要先封装成 wav，否则 record 段没有容器头、客户端无法识别。
RAW_PCM_SAMPLE_RATE = 24000
RAW_PCM_CHANNELS = 1
RAW_PCM_SAMPLE_WIDTH = 2  # s16le = 2 字节/样本
RAW_PCM_FORMATS: frozenset[str] = frozenset({"pcm", "pcm16"})


def wrap_raw_pcm_to_wav(
    pcm: bytes,
    sample_rate: int = RAW_PCM_SAMPLE_RATE,
    channels: int = RAW_PCM_CHANNELS,
    sample_width: int = RAW_PCM_SAMPLE_WIDTH,
) -> bytes:
    """给裸 PCM 套一个标准 44 字节 RIFF/WAVE 头。

    不依赖 ffmpeg：PCM 本来就是 wav 的负载，只是缺头；用标准库 struct
    直接拼，避免为了「加个 44 字节的头」再起一个子进程（发送路径上多一次
    ffmpeg 调用就多一份超时风险）。

    采样参数必须与上游一致（s16le/24kHz/单声道），否则播放速度会不对。
    """
    import struct

    if not pcm:
        return b""

    block_align = channels * sample_width
    byte_rate = sample_rate * block_align
    data_size = len(pcm)
    # RIFF 头里的 chunk size 是 32 位有符号，超 4GB 会溢出（实际不可能，
    # 但保险起见按上限截断，宁可少报也不产出非法文件）。
    riff_size = min(36 + data_size, 0xFFFFFFFF)

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        riff_size,
        b"WAVE",
        b"fmt ",
        16,           # fmt chunk 长度
        1,            # PCM
        channels,
        sample_rate,
        byte_rate,
        block_align,
        sample_width * 8,
        b"data",
        data_size,
    )
    return header + pcm


# 半音 -> 频率倍率。rubberband 的 pitch 参数直接吃这个倍率。
_SEMITONE_RATIO = 2.0 ** (1.0 / 12.0)


def _kill_and_reap(proc: "asyncio.subprocess.Process") -> None:
    """杀掉子进程并回收，避免留下孤儿 ffmpeg。

    只在协程被取消这类「来不及正常 await」的路径上用，所以**不能** await
    （await 会立刻被同一个 CancelledError 再打断）。这里 fire-and-forget 地
    等它退出，退出后 asyncio 自己会回收 pid，不会变成僵尸。
    """
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        # 已经退出了，没什么可杀的
        return
    except Exception:  # noqa: BLE001 - 收尾路径不该再抛新异常盖住 CancelledError
        return
    # 后台把 wait() 跑完，让 asyncio 回收进程
    asyncio.ensure_future(_wait_quietly(proc))


async def _wait_quietly(proc: "asyncio.subprocess.Process") -> None:
    """等待子进程结束，吞掉所有异常（纯收尾用）。"""
    try:
        await proc.wait()
    except Exception:  # noqa: BLE001
        pass



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

    def __init__(
        self,
        options: AudioOptions,
        logger: Optional[logging.Logger] = None,
        work_dir: Optional[Path] = None,
    ) -> None:
        """初始化处理器。

        Args:
            options: 调音参数。
            logger: 日志器。
            work_dir: ffmpeg 中转文件目录。传 `ctx.paths.runtime_dir` 可让中间
                音频落在宿主为本插件分配的运行时目录里，按插件生命周期管理；
                不传则退回系统临时目录（仍会即时清理）。
        """
        self.options = options
        self.logger = logger or logging.getLogger(__name__)
        self.work_dir = work_dir
        # 只在首次真正需要处理时才去解析 ffmpeg 路径并做一次可用性检查，
        # 避免不加处理的用户白白承担一次 shutil.which。
        self._ffmpeg: Optional[str] = None
        self._resolved = False
        self._warned_unavailable = False

    # ------------------------------------------------------------------ 临时目录

    def _make_tmpdir(self) -> str:
        """建一个本次处理专用的临时目录。

        runtime_dir 不可写时（权限、磁盘、宿主版本差异）不能因此放弃调音，
        退回系统临时目录即可——那正是改动前的行为。
        """
        if self.work_dir is not None:
            try:
                self.work_dir.mkdir(parents=True, exist_ok=True)
                return tempfile.mkdtemp(prefix="mimotts_audio_", dir=str(self.work_dir))
            except OSError as exc:
                self.logger.debug("runtime_dir 不可用（%r），退回系统临时目录", exc)
        return tempfile.mkdtemp(prefix="mimotts_audio_")

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
        configured_missing = False
        if looks_like_path:
            expanded = Path(configured).expanduser()
            if expanded.is_file():
                candidates.append(str(expanded))
            else:
                configured_missing = True
                self.logger.warning("配置的 ffmpeg 路径不存在: %s，将在 PATH 里找", configured)

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
        if self._ffmpeg and configured_missing:
            # 用户明确配了一个路径却不存在，回退成功后必须点明实际用的是哪个：
            # 否则他会以为自己配置的那个 ffmpeg 生效了（可能是另一个版本、
            # 甚至另一个程序的同名可执行文件），出问题时排查不到这里。
            self.logger.warning(
                "audio.ffmpeg_path 配置的 %r 不存在，实际改用 PATH 里的: %s"
                "（如需固定版本请把该配置改成正确的路径）",
                configured, self._ffmpeg,
            )
        elif not self._ffmpeg and not self._warned_unavailable:
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
            tmpdir = self._make_tmpdir()
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
            except asyncio.CancelledError:
                # 上层（on_unload / 调用方取消）把任务掐了。这里必须自己收尾：
                # asyncio 不会因为 task 被 cancel 就去杀子进程，不 kill 的话
                # ffmpeg 会变成孤儿进程一直挂在系统里。处理完再把取消向上抛，
                # 别把 CancelledError 吞掉（吞掉会让调用方以为正常完成）。
                _kill_and_reap(proc)
                raise

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


def build_audio_processor(
    config: object,
    logger: Optional[logging.Logger] = None,
    work_dir: Optional[Path] = None,
) -> Optional[AudioProcessor]:
    """从插件配置构建处理器；全部默认值时返回 None（不产生任何开销）。

    Args:
        config: 插件配置（读取其中的 `audio` 段）。
        logger: 日志器。
        work_dir: ffmpeg 中转文件目录，通常传 `ctx.paths.runtime_dir`。
    """
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
    return AudioProcessor(options, logger=logger, work_dir=work_dir)
