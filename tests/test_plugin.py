"""插件功能验证：用桩对象模拟宿主，不连真实 MaiBot。

覆盖：
1. 配置模型解析（新配置段）
2. _find_stream_id 四种场景（防串群）
3. _stream_id_from_message 字段提取
4. _fallback_to_text 各种降级分支
5. TTS 重试 / 超时 / 非重试状态码
6. _load_voices 清空旧音色
6b. _load_voices 坏文件隔离（单个音色读不了不能拖垮整个插件）
6c. _load_voices 失败时状态确定性
"""
import asyncio
import base64
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

# 默认测仓库里的代码（插件在 tests/ 的上一级）；设 TTS_PLUGIN_DIR 可指向运行目录
PLUGIN_DIR = Path(os.environ.get("TTS_PLUGIN_DIR") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(PLUGIN_DIR))

import plugin as P
import tts_service as T
import audio_processor as A


def _set_config(p, cfg):
    """插件 config 是只读 property，直接写私有字段。"""
    p._plugin_config_instance = cfg

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail and not cond else ""))


class FakeLogger:
    def __init__(self):
        self.lines = []

    def _rec(self, lvl):
        def fn(msg, *a):
            try:
                self.lines.append(f"[{lvl}] " + (msg % a if a else str(msg)))
            except Exception:
                self.lines.append(f"[{lvl}] {msg} {a}")
        return fn

    def __getattr__(self, item):
        return self._rec(item)

    def text(self):
        return "\n".join(self.lines)


# ---------------- 1. 配置模型 ----------------
def test_config():
    print("\n=== 1. 配置模型 ===")
    cfg = P.VoicePluginConfig()
    check("默认 request_timeout", cfg.request.request_timeout == 45.0)
    check("默认 max_retries", cfg.request.max_retries == 2)
    check("默认 max_concurrency", cfg.request.max_concurrency == 1)
    check("默认 fallback_to_text", cfg.fallback.fallback_to_text is True)
    check("默认 audio_format", cfg.voice.audio_format == "mp3")

    cfg2 = P.VoicePluginConfig(
        request={"request_timeout": 30, "max_retries": 0, "max_concurrency": 1},
        fallback={"fallback_to_text": False, "empty_text_placeholder": ""},
        voice={"voice_mode": "preset", "audio_format": "mp3"},
    )
    check("覆盖 request_timeout", cfg2.request.request_timeout == 30)
    check("覆盖 max_retries", cfg2.request.max_retries == 0)
    check("覆盖 fallback_to_text", cfg2.fallback.fallback_to_text is False)
    check("覆盖 audio_format", cfg2.voice.audio_format == "mp3")


# ---------------- 2. _find_stream_id ----------------
def make_plugin():
    p = object.__new__(P.AIVoicePlugin)
    p.tts_service = None
    p.voices = {}
    p.default_voice = "luoxi"
    p._voice_inflight = set()
    p._background_tasks = set()
    p._ctx = types.SimpleNamespace(logger=FakeLogger())
    return p


class FakeMessage:
    def __init__(self, payload):
        self.payload = payload

    async def get_by_id(self, message_id, **kw):
        return self.payload


class FakeChat:
    def __init__(self, streams):
        self.streams = streams
        self.calls = 0

    async def get_all_streams(self, platform="qq"):
        self.calls += 1
        return self.streams

    async def get_group_streams(self, platform="qq"):
        self.calls += 1
        return [s for s in self.streams if s.get("is_group_session")]


async def test_find_stream():
    print("\n=== 2. _find_stream_id（防串群）===")
    p = make_plugin()

    # 2.1 kwargs 直接给出
    sid = await p._find_stream_id({"stream_id": "SID_FROM_KW"}, msg_id="m1")
    check("kwargs.stream_id 优先", sid == "SID_FROM_KW", sid)

    # 2.2 chat_id 兜底
    sid = await p._find_stream_id({"chat_id": "SID_FROM_CHAT"}, msg_id="m1")
    check("kwargs.chat_id 次之", sid == "SID_FROM_CHAT", sid)

    # 2.3 session_id 兜底
    sid = await p._find_stream_id({"session_id": "SID_FROM_SESS"}, msg_id="m1")
    check("kwargs.session_id 第三", sid == "SID_FROM_SESS", sid)

    # 2.4 靠 msg_id 反查
    p.ctx.message = FakeMessage({"session_id": "SID_FROM_MSG"})
    p.ctx.chat = FakeChat([{"stream_id": "GROUP_A"}, {"stream_id": "GROUP_B"}])
    sid = await p._find_stream_id({}, msg_id="m42")
    check("msg_id 反查成功", sid == "SID_FROM_MSG", sid)
    check("反查命中时不调用群列表兜底", p.ctx.chat.calls == 0, f"calls={p.ctx.chat.calls}")

    # 2.5 反查失败 + 多个会话 → 拒绝兜底（关键：不串群/不串私聊）
    p.ctx.message = FakeMessage(None)
    p.ctx.chat = FakeChat([{"stream_id": "GROUP_A"}, {"stream_id": "GROUP_B"}])
    sid = await p._find_stream_id({}, msg_id="")
    check("多会话时不乱发（返回空）", sid == "", sid)
    check("多会话时报 error 日志", "放弃语音发送" in p.ctx.logger.text(), p.ctx.logger.text()[-200:])

    # 2.6 反查失败 + 全局只有一个会话 → 允许兜底
    p.ctx.chat = FakeChat([{"stream_id": "ONLY_GROUP", "is_group_session": True}])
    sid = await p._find_stream_id({}, msg_id="")
    check("全局单会话时兜底可用", sid == "ONLY_GROUP", sid)

    # 2.6b 1 个群 + 1 个私聊：旧实现用 get_group_streams 会数出 1 个群
    #      而错误地把私聊消息发到那个群里，这里必须拒绝
    p.ctx.message = FakeMessage(None)
    p.ctx.chat = FakeChat([
        {"stream_id": "GROUP_A", "is_group_session": True},
        {"stream_id": "PRIVATE_B", "is_group_session": False},
    ])
    sid = await p._find_stream_id({}, msg_id="")
    check("1群+1私聊时不串流（返回空）", sid == "", sid)

    # 2.7 反查抛异常 → 不崩
    class Boom:
        async def get_by_id(self, *a, **k):
            raise RuntimeError("boom")
    p.ctx.message = Boom()
    p.ctx.chat = FakeChat([{"stream_id": "ONLY_GROUP", "is_group_session": True}])
    sid = await p._find_stream_id({}, msg_id="m9")
    check("反查异常不崩溃", sid == "ONLY_GROUP", sid)

    # 2.8 完全没有消息源
    p.ctx.chat = FakeChat([])
    sid = await p._find_stream_id({}, msg_id="")
    check("无任何信息时返回空", sid == "", sid)


# ---------------- 3. _stream_id_from_message ----------------
def test_stream_extract():
    print("\n=== 3. _stream_id_from_message ===")
    f = P.AIVoicePlugin._stream_id_from_message
    check("顶层 session_id", f({"session_id": "A"}) == "A")
    check("顶层 stream_id", f({"stream_id": "B"}) == "B")
    check("顶层 chat_id", f({"chat_id": "C"}) == "C")
    check("message_info 内", f({"message_info": {"stream_id": "D"}}) == "D")
    check("非 dict 返回空", f(None) == "" and f("x") == "")
    check("空 dict 返回空", f({}) == "")
    check("优先 session_id", f({"session_id": "S", "stream_id": "T"}) == "S")


# ---------------- 4. _fallback_to_text ----------------
class FakeSend:
    def __init__(self, ok=True, raise_exc=False):
        self.sent = []
        self.ok = ok
        self.raise_exc = raise_exc

    async def text(self, text, stream_id, **kw):
        if self.raise_exc:
            raise RuntimeError("send boom")
        self.sent.append((text, stream_id))
        return self.ok


async def test_fallback():
    print("\n=== 4. _fallback_to_text ===")
    p = make_plugin()
    _set_config(p, P.VoicePluginConfig())

    # 4.1 剥标签
    p.ctx.send = FakeSend()
    await p._fallback_to_text("宝宝~ 我在呢（轻笑）", "s1")
    check("剥掉音频标签", p.ctx.send.sent and p.ctx.send.sent[0][0] == "宝宝~ 我在呢", str(p.ctx.send.sent))

    # 4.2 英文标签
    p.ctx.send = FakeSend()
    await p._fallback_to_text("(sighs) I don't know.", "s1")
    check("剥掉英文标签", p.ctx.send.sent[0][0] == "I don't know.", str(p.ctx.send.sent))

    # 4.3 全是标签 → 用兜底文案
    p.ctx.send = FakeSend()
    await p._fallback_to_text("（轻笑）（叹气）", "s1")
    check("空正文用兜底文案", p.ctx.send.sent and p.ctx.send.sent[0][0] == "（想说点什么，但声音没发出来）", str(p.ctx.send.sent))

    # 4.4 兜底文案也留空 → 不发
    _set_config(p, P.VoicePluginConfig(fallback={"empty_text_placeholder": ""}))
    p.ctx.send = FakeSend()
    await p._fallback_to_text("（轻笑）", "s1")
    check("兜底文案留空则不发", p.ctx.send.sent == [], str(p.ctx.send.sent))

    # 4.5 关闭降级 → 完全不发
    _set_config(p, P.VoicePluginConfig(fallback={"fallback_to_text": False}))
    p.ctx.send = FakeSend()
    await p._fallback_to_text("正文", "s1")
    check("关闭降级后不发", p.ctx.send.sent == [], str(p.ctx.send.sent))

    # 4.6 关闭剥标签 → 原样发
    _set_config(p, P.VoicePluginConfig(fallback={"strip_audio_tags": False}))
    p.ctx.send = FakeSend()
    await p._fallback_to_text("宝宝（轻笑）", "s1")
    check("关闭剥标签则原样发", p.ctx.send.sent[0][0] == "宝宝（轻笑）", str(p.ctx.send.sent))

    # 4.7 发送异常不崩
    _set_config(p, P.VoicePluginConfig())
    p.ctx.send = FakeSend(raise_exc=True)
    try:
        await p._fallback_to_text("正文", "s1")
        check("发送异常被吞掉不崩溃", True)
    except Exception as e:
        check("发送异常被吞掉不崩溃", False, repr(e))


# ---------------- 5. TTS 重试 / 超时 ----------------
async def test_tts():
    print("\n=== 5. TTS 重试 / 超时 ===")
    svc = T.MiMoTTSService("key", logger=FakeLogger(), max_retries=2, retry_backoff=0)
    check("max_retries 生效", svc.max_retries == 2)
    check("request_timeout 生效", svc.request_timeout == 45.0)
    check("SUPPORTED_FORMATS 含 pcm16", "pcm16" in svc.SUPPORTED_FORMATS)

    svc._update_limits(request_timeout=12, connect_timeout=10, max_retries=0, retry_backoff=1.5, max_concurrency=1)
    check("apply_settings 生效", svc.request_timeout == 12 and svc.max_retries == 0 and svc._max_concurrency == 1)

    # 空 key 直接失败，不发请求
    svc2 = T.MiMoTTSService("", logger=FakeLogger())
    r = await svc2._do_request({"messages": [{"content": "x"}]}, {})
    check("无 key 时快速失败", r["success"] is False and "API Key" in r["error"], str(r))

    # 模拟 _get_session 返回会抛超时的 session
    class FakeResp:
        def __init__(self, status, body="", payload=None):
            self.status = status
            self._body = body
            self._payload = payload or {}

        async def text(self):
            return self._body

        async def json(self):
            return self._payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def __init__(self, responses, exc=None):
            self.responses = responses
            self.exc = exc
            self.n = 0

        def post(self, *a, **kw):
            if self.exc:
                raise self.exc
            r = self.responses[min(self.n, len(self.responses) - 1)]
            self.n += 1
            return r

    async def run_with(session, retries=2):
        s = T.MiMoTTSService("k", logger=FakeLogger(), max_retries=retries, retry_backoff=0)
        s._session = session
        async def _get():
            return session
        s._get_session = _get
        payload = {"messages": [{"role": "assistant", "content": "hi"}]}
        return await s._do_request(payload, {})

    # 5.1 429 → 重试后成功
    s = FakeSession([FakeResp(429, "too many"), FakeResp(200, payload={"choices": [{"message": {"audio": {"data": "AAA"}}}]})])
    r = await run_with(s, retries=2)
    check("429 后重试成功", r["success"] is True and r.get("audio_base64") == "AAA", str(r))
    check("429 触发 2 次请求", s.n == 2, f"n={s.n}")

    # 5.2 400 不可重试 → 立即失败
    s = FakeSession([FakeResp(400, "bad request")])
    r = await run_with(s, retries=2)
    check("400 不重试", r["success"] is False and s.n == 1, f"n={s.n} {r}")

    # 5.3 全部 500 → 重试耗尽后失败
    s = FakeSession([FakeResp(503, "unavail")])
    r = await run_with(s, retries=1)
    check("503 重试耗尽后失败", r["success"] is False, str(r))
    check("503 共 2 次请求（1+retry1）", s.n == 2, f"n={s.n}")

    # 5.4 超时不重试 → 只发 1 次
    class TimeoutSession(FakeSession):
        def post(self, *a, **kw):
            self.n += 1
            raise asyncio.TimeoutError()

    s = TimeoutSession([])
    r = await run_with(s, retries=3)
    check("超时不重试（只 1 次）", s.n == 1, f"n={s.n}")
    check("超时错误信息含超时秒数", "timeout after" in r.get("error", ""), str(r))

    # 5.5 成功但无 audio
    s = FakeSession([FakeResp(200, payload={"choices": [{"message": {}}]})])
    r = await run_with(s, retries=0)
    check("无 audio 时失败", r["success"] is False and "No audio" in r["error"], str(r))

    # 5.6 非法 JSON
    class BadJson(FakeResp):
        async def json(self):
            raise ValueError("not json")
    s = FakeSession([BadJson(200)])
    r = await run_with(s, retries=0)
    check("非法 JSON 被捕获", r["success"] is False and "invalid response" in r["error"], str(r))


# ---------------- 6. _load_voices 清空旧音色 ----------------
async def test_load_voices():
    print("\n=== 6. _load_voices 清理 ===")
    p = make_plugin()
    _set_config(p, P.VoicePluginConfig(voice={"voices_dir": "voices"}))
    p.voices = {"stale_voice": "data:audio/wav;base64,OLD"}
    await p._load_voices()
    check("重载后不含上传前残留的旧音色", "stale_voice" not in p.voices, str(list(p.voices.keys())))
    # 仓库里不带 voices/（被 .gitignore 排除，音色由用户自己放），
    # 所以这条只在运行目录下跑测试时才断真实文件，仓库里只断言「不崩且清掉了旧音色」。
    if (PLUGIN_DIR / "voices" / "luoxi.mp3").is_file():
        check("加载到真实音色 luoxi", "luoxi" in p.voices, str(list(p.voices.keys())))
        if "luoxi" in p.voices:
            check("音色带 data URI 前缀", p.voices["luoxi"].startswith("data:audio/"), p.voices["luoxi"][:40])
    else:
        print("  ⏭  跳过 luoxi 真实音色断言（当前目录没有 voices/luoxi.mp3）")

    # 指向不存在的目录 → 清空且不崩
    _set_config(p, P.VoicePluginConfig(voice={"voices_dir": "__no_such_dir__"}))
    await p._load_voices()
    check("目录不存在时清空音色", p.voices == {}, str(p.voices))
    # 清理掉测试建的目录
    d = PLUGIN_DIR / "__no_such_dir__"
    if d.exists():
        try:
            d.rmdir()
        except OSError:
            pass


# ---------------- 6b. 坏文件隔离 ----------------
async def test_load_voices_bad_file():
    """回归：voices/ 里有一个读不了的文件时，插件不能整个挂掉。

    _load_voices 在 on_load 里被直接 await，异常上抛会让宿主把插件注销成
    FAILED（runner_main.py:1662-1666），所有语音能力消失且要重启宿主。而
    「音频被播放器独占、网盘同步中、文件刚被改名」都是正常运维场景。
    """
    print("\n=== 6b. _load_voices 坏文件隔离 ===")
    p = make_plugin()

    tmp = Path(tempfile.mkdtemp(prefix="mimotts_voices_", dir=PLUGIN_DIR))
    try:
        (tmp / "good.wav").write_bytes(b"RIFF" + b"\x00" * 64)
        (tmp / "empty.mp3").write_bytes(b"")
        (tmp / "locked.wav").write_bytes(b"RIFF" + b"\x00" * 32)

        # 用「stat 就抛 PermissionError」模拟被独占/无权限的文件：
        # 真实环境里构造不出稳定的占用时序。
        real_stat = Path.stat

        def fake_stat(self, *a, **kw):
            if self.name == "locked.wav":
                raise PermissionError(13, "另一个程序正在使用此文件，进程无法访问")
            return real_stat(self, *a, **kw)

        _set_config(p, P.VoicePluginConfig(voice={"voices_dir": str(tmp)}))
        crashed = None
        try:
            Path.stat = fake_stat
            await p._load_voices()
        except Exception as exc:  # noqa: BLE001 - 任何异常都算「拖垮插件」
            crashed = exc
        finally:
            Path.stat = real_stat

        check("坏文件不抛异常（不拖垮 on_load）", crashed is None, repr(crashed))
        check("正常音色仍被加载", "good" in p.voices, str(list(p.voices.keys())))
        check("空文件被跳过", "empty" not in p.voices, str(list(p.voices.keys())))
        check("坏文件被跳过", "locked" not in p.voices, str(list(p.voices.keys())))
        check(
            "加载结果带 data URI 前缀",
            p.voices.get("good", "").startswith("data:audio/wav;base64,"),
            p.voices.get("good", "")[:40],
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------- 6c. 列举失败时状态确定性 ----------------
async def test_load_voices_keeps_old_on_partial_failure():
    """回归：目录列举失败时不抛异常，且状态确定（不会半新半旧）。"""
    print("\n=== 6c. _load_voices 状态确定性 ===")
    p = make_plugin()
    p.voices = {"old": "data:audio/mpeg;base64,OLD"}

    tmp = Path(tempfile.mkdtemp(prefix="mimotts_voices_"))
    try:
        (tmp / "new.wav").write_bytes(b"RIFF" + b"\x00" * 16)
        real_iterdir = Path.iterdir

        def fake_iterdir(self, *a, **kw):
            if str(self) == str(tmp):
                raise PermissionError(13, "拒绝访问")
            return real_iterdir(self, *a, **kw)

        _set_config(p, P.VoicePluginConfig(voice={"voices_dir": str(tmp)}))
        crashed = None
        try:
            Path.iterdir = fake_iterdir
            await p._load_voices()
        except Exception as exc:  # noqa: BLE001
            crashed = exc
        finally:
            Path.iterdir = real_iterdir

        check("目录列举失败不抛异常", crashed is None, repr(crashed))
        check(
            "状态确定（全空或全旧，无半成品）",
            p.voices == {} or p.voices == {"old": "data:audio/mpeg;base64,OLD"},
            str(list(p.voices.keys())),
        )
        check("不存在无 data URI 的坏条目", all(v.startswith("data:audio/") for v in p.voices.values()), str(list(p.voices.keys())))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------- 7. _resolve_voice ----------------
def test_resolve_voice():
    print("\n=== 7. _resolve_voice ===")
    p = make_plugin()
    p.voices = {"luoxi": "data:audio/mpeg;base64,X", "other": "data:audio/mpeg;base64,Y"}
    p.default_voice = "luoxi"
    _set_config(p, P.VoicePluginConfig(voice={"voice_mode": "clone", "clone_voice": "other"}))

    key, audio, mode = p._resolve_voice("")
    check("空名用 default_voice", key == "luoxi" and mode == "clone", f"{key},{mode}")

    key, audio, mode = p._resolve_voice("default")
    check("'default' 用 default_voice", key == "luoxi", key)

    key, audio, mode = p._resolve_voice("other")
    check("指定存在的音色", key == "other", key)

    key, audio, mode = p._resolve_voice("ghost")
    check("不存在则回退 clone_voice", key == "other", key)

    p.voices = {}
    key, audio, mode = p._resolve_voice("anything")
    check("无音色时返回空", key == "" and mode == "", f"{key},{mode}")

    p.voices = {"luoxi": "x"}
    _set_config(p, P.VoicePluginConfig(voice={"voice_mode": "preset", "preset_voice": "冰糖"}))
    key, audio, mode = p._resolve_voice("")
    check("preset 模式返回预置名", key == "冰糖" and mode == "preset", f"{key},{mode}")


# ---------------- 8. audio_format 校验 ----------------
async def test_audio_format():
    print("\n=== 8. audio_format 校验 ===")
    p = make_plugin()
    _set_config(p, P.VoicePluginConfig(voice={"voice_mode": "clone", "audio_format": "opus"}))
    p.voices = {"luoxi": "data:audio/mpeg;base64,X"}
    p.default_voice = "luoxi"

    captured = {}

    class FakeTTS:
        async def synthesize_with_voice_clone(self, **kw):
            captured.update(kw)
            return {"success": False, "error": "stub"}

        async def synthesize_with_preset(self, **kw):
            captured.update(kw)
            return {"success": False, "error": "stub"}

    p.tts_service = FakeTTS()
    await p.voice_clone_tts("你好", "", "s1", "")
    check("非法格式回退 mp3", captured.get("audio_format") == "mp3", str(captured.get("audio_format")))
    check("非法格式有 warning", "不支持的 audio_format" in p.ctx.logger.text(), p.ctx.logger.text()[-200:])

    _set_config(p, P.VoicePluginConfig(voice={"voice_mode": "clone", "audio_format": "mp3"}))
    captured.clear()
    await p.voice_clone_tts("你好", "", "s1", "")
    check("合法格式 mp3 透传", captured.get("audio_format") == "mp3", str(captured.get("audio_format")))


# ---------------- 8b. 不传 stream_id 时必须保留 audio_base64 ----------------
class FakeSendCustom:
    """记录 custom 发送内容，用于断言真正发出去的音频。"""

    def __init__(self, ok=True):
        self.customs = []
        self.ok = ok

    async def custom(self, seg_type, content, stream_id, **kw):
        self.customs.append((seg_type, content, stream_id))
        return self.ok

    async def text(self, text, stream_id, **kw):
        return True


async def test_audio_base64_kept_without_stream():
    """回归：不传 stream_id 时不能把 audio_base64 删掉。

    README:275 与 API 描述都承诺「不传则只合成不发送，result["audio_base64"]
    里是原始音频」。原实现在这个分支里 pop 掉了该字段，调用方会拿到
    {"success": True} 却没有音频——静默失败。
    """
    print("\n=== 8b. 不传 stream_id 保留音频 ===")
    p = make_plugin()
    _set_config(p, P.VoicePluginConfig(voice={"voice_mode": "clone", "audio_format": "mp3"}))
    p.voices = {"luoxi": "data:audio/mpeg;base64,REF"}
    p.default_voice = "luoxi"
    p.audio_processor = None
    p.ctx.send = FakeSendCustom()

    class FakeTTS:
        async def synthesize_with_voice_clone(self, **kw):
            return {"success": True, "audio_base64": "QUJD"}

        async def synthesize_with_preset(self, **kw):
            return {"success": True, "audio_base64": "QUJD"}

    p.tts_service = FakeTTS()
    r = await p.voice_clone_tts("你好", "", "", "")
    check("返回值 success=True", r.get("success") is True, str(r))
    check("未传 stream_id 时 audio_base64 仍在", bool(r.get("audio_base64")), str(list(r.keys())))
    check("audio_base64 是纯 base64（无 data URI 前缀）", r.get("audio_base64") == "QUJD", repr(r.get("audio_base64")))
    check("未传 stream_id 时确实没发送", p.ctx.send.customs == [], str(p.ctx.send.customs))

    # 带 data URI 前缀的返回值也要剥干净
    class FakeTTS2:
        async def synthesize_with_voice_clone(self, **kw):
            return {"success": True, "audio_base64": "data:audio/mpeg;base64,QUJD"}

        async def synthesize_with_preset(self, **kw):
            return {"success": True, "audio_base64": "data:audio/mpeg;base64,QUJD"}

    p.tts_service = FakeTTS2()
    r2 = await p.voice_clone_tts("你好", "", "", "")
    check("data URI 前缀被剥离", r2.get("audio_base64") == "QUJD", repr(r2.get("audio_base64")))


# ---------------- 8c. 裸 PCM 发送前封装成 wav ----------------
async def test_raw_pcm_wrapped_before_send():
    """回归：audio_format=pcm/pcm16 时，发出去的必须是带 RIFF 头的 wav。

    上游 pcm 是无头裸流，适配器只是把 base64 原样塞进 {"type":"record"}，
    QQ 无法识别——要么发不出去要么是噪音。
    """
    print("\n=== 8c. 裸 PCM 发送前封 wav ===")
    raw = b"\x00\x01" * 64  # 128 字节假 PCM

    # 先单测封装函数本身
    wrapped = A.wrap_raw_pcm_to_wav(raw)
    check("封装后以 RIFF 开头", wrapped[:4] == b"RIFF", repr(wrapped[:4]))
    check("封装后 WAVE 标识正确", wrapped[8:12] == b"WAVE", repr(wrapped[8:12]))
    check("封装后长度 = 原始 + 44 字节头", len(wrapped) == len(raw) + 44, f"{len(wrapped)} vs {len(raw)}")
    check("采样率写的是 24000", int.from_bytes(wrapped[24:28], "little") == 24000, str(int.from_bytes(wrapped[24:28], "little")))
    check("声道数写的是 1", int.from_bytes(wrapped[22:24], "little") == 1, str(int.from_bytes(wrapped[22:24], "little")))
    check("空输入返回空", A.wrap_raw_pcm_to_wav(b"") == b"", "")

    # 再走完整链路
    p = make_plugin()
    _set_config(p, P.VoicePluginConfig(voice={"voice_mode": "clone", "audio_format": "pcm"}))
    p.voices = {"luoxi": "data:audio/mpeg;base64,REF"}
    p.default_voice = "luoxi"
    p.audio_processor = None
    p.ctx.send = FakeSendCustom()

    class FakeTTS:
        async def synthesize_with_voice_clone(self, **kw):
            return {"success": True, "audio_base64": base64.b64encode(raw).decode()}

        async def synthesize_with_preset(self, **kw):
            return {"success": True, "audio_base64": base64.b64encode(raw).decode()}

    p.tts_service = FakeTTS()
    r = await p.voice_clone_tts("你好", "", "s1", "")
    check("pcm 模式发送成功", r.get("success") is True, str(r))
    check("确实调用了一次发送", len(p.ctx.send.customs) == 1, str(p.ctx.send.customs))

    sent_b64 = p.ctx.send.customs[0][1]
    sent_raw = base64.b64decode(sent_b64)
    check("发出去的音频带 RIFF 头", sent_raw[:4] == b"RIFF", repr(sent_raw[:4]))
    check("发出去的长度 = 原始 + 44", len(sent_raw) == len(raw) + 44, str(len(sent_raw)))

    # mp3 不该被封装
    p.ctx.send = FakeSendCustom()
    _set_config(p, P.VoicePluginConfig(voice={"voice_mode": "clone", "audio_format": "mp3"}))
    await p.voice_clone_tts("你好", "", "s1", "")
    mp3_sent = base64.b64decode(p.ctx.send.customs[0][1])
    check("mp3 不被封装（保持原样）", mp3_sent == raw, repr(mp3_sent[:8]))



# ---------------- 8d. API 声明了足够长的 RPC 超时 ----------------
async def test_api_rpc_timeout_declared():
    """回归：@API 必须显式声明 timeout_ms，且要盖得住最坏耗时。

    宿主默认 RPC 超时 60s；request_timeout 是可配的且没有上界，用户调到 90s
    时宿主会在插件正干活时把它掐断。插件必须自己声明一个够长的值。
    """
    print("\n=== 8d. API RPC 超时声明 ===")
    info = getattr(P.AIVoicePlugin.voice_clone_tts, "__maibot_component_info__", None)
    check("voice_clone_tts 有组件元信息", info is not None, "找不到 __maibot_component_info__")

    meta = getattr(info, "metadata", {}) or {}
    timeout_ms = meta.get("timeout_ms")
    check("声明了 timeout_ms", timeout_ms is not None, str(meta))
    if timeout_ms is not None:
        check("timeout_ms 是正整数", isinstance(timeout_ms, int) and timeout_ms > 0, repr(timeout_ms))
        # 宿主默认值 60000 是下限，必须比它长
        check("timeout_ms 大于宿主默认 60s", timeout_ms > 60000, f"{timeout_ms} <= 60000")

    # 语义校验：要盖得住「合成超时 + ffmpeg 后处理超时」
    cfg = P.VoicePluginConfig()
    worst = cfg.request.request_timeout + 10  # ffmpeg wait_for timeout=10
    check(
        f"默认配置最坏 {worst:.0f}s 不超 RPC 上限",
        timeout_ms is not None and timeout_ms / 1000.0 >= worst,
        f"{timeout_ms}ms < {worst}s",
    )


# ---------------- 8e. 取消时不留孤儿 ffmpeg ----------------
class _FakeProc:
    """假的子进程：communicate() 一直挂着，直到被 kill()。

    用它替掉真 ffmpeg，能在任何平台、不需要装 ffmpeg 的前提下验证
    「process() 被取消时到底有没有 kill 子进程」——本测试的全部意义就在这。
    """

    def __init__(self):
        self.killed = False
        self.returncode = None
        self._stop = asyncio.Event()

    async def communicate(self):
        await self._stop.wait()
        return (b"", b"")

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._stop.set()

    async def wait(self):
        await self._stop.wait()
        return self.returncode


async def test_cancel_kills_ffmpeg():
    """回归：任务被 cancel 时，ffmpeg 子进程必须被杀掉。

    原来只处理了 asyncio.TimeoutError。task.cancel() 抛出的是 CancelledError，
    不走那个分支，于是 proc.kill() 从未被调用——ffmpeg 变成孤儿进程留在系统里，
    热重载几次就堆一堆。
    """
    print("\n=== 8e. 取消时回收 ffmpeg 子进程 ===")
    app = A.AudioProcessor(options=A.AudioOptions(speed=1.5), logger=FakeLogger())

    spawned: list[_FakeProc] = []

    async def fake_spawn(*args, **kwargs):
        proc = _FakeProc()
        spawned.append(proc)
        return proc

    orig_spawn = A.asyncio.create_subprocess_exec
    orig_resolve = A.AudioProcessor._resolve_ffmpeg
    A.asyncio.create_subprocess_exec = fake_spawn
    A.AudioProcessor._resolve_ffmpeg = lambda self: "ffmpeg"

    try:
        task = asyncio.ensure_future(app.process(b"fake audio", "mp3"))
        await asyncio.sleep(0.05)        # 让子进程起来并卡在 communicate
        check("子进程已启动", len(spawned) == 1, f"spawned={len(spawned)}")
        if not spawned:
            return
        proc = spawned[0]
        check("取消前子进程还活着", proc.returncode is None and not proc.killed, "")

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            check("取消向上传播（没被吞掉）", True)
        else:
            check("取消向上传播（没被吞掉）", False, "CancelledError 被吞了")

        check("取消后子进程被 kill", proc.killed, "proc.kill() 从未被调用 = 孤儿进程")
    finally:
        A.asyncio.create_subprocess_exec = orig_spawn
        A.AudioProcessor._resolve_ffmpeg = orig_resolve

    # 已退出的进程再收尾一次不能炸
    done = _FakeProc()
    done.returncode = 0
    try:
        A._kill_and_reap(done)
        check("对已退出进程重复收尾不抛异常", not done.killed)
    except Exception as exc:  # noqa: BLE001
        check("对已退出进程重复收尾不抛异常", False, repr(exc))


# ---------------- 8f. on_unload 会等后台任务结束 ----------------
async def test_unload_awaits_cancel():
    """回归：on_unload 必须 await 被取消的任务，不能只 cancel() 就走。

    cancel() 只是投递请求，不给协程收尾的机会——里面卡着的 ffmpeg 就会泄漏。
    """
    print("\n=== 8f. on_unload 等待任务收尾 ===")
    p = make_plugin()
    _set_config(p, P.VoicePluginConfig())

    finished = {"n": 0}

    async def stubborn():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            # 模拟「取消后还要做收尾」的协程
            await asyncio.sleep(0.05)
            finished["n"] += 1
            raise

    t = asyncio.ensure_future(stubborn())
    p._background_tasks = {t}
    await asyncio.sleep(0.01)

    await p.on_unload()

    check("后台任务确实被取消", t.cancelled() or t.done(), str(t))
    check("取消后的收尾代码跑完了", finished["n"] == 1, str(finished))
    check("_background_tasks 已清空", not p._background_tasks, str(p._background_tasks))


# ---------------- 8g. 语气标签白名单 ----------------
def test_audio_tag_words():
    """回归：常见语气词都要能从待合成文本里剥掉，否则会被念出来。"""
    print("\n=== 8g. 语气标签剥离 ===")
    # 「深沉」曾经漏在白名单外，会被原样念出来
    for tag in ["深沉", "低沉", "无奈", "磁性", "温柔", "叹气", "轻笑"]:
        src = f"（{tag}）我一直在想这件事。"
        out = P._strip_audio_tags(src)
        check(f"剥离「{tag}」", out == "我一直在想这件事。", repr(out))

    # 正常括号内容必须保留（这是白名单设计的前提）
    keep = "宝宝（明天要早起）早点睡"
    check("普通括号内容原样保留", P._strip_audio_tags(keep) == keep, repr(P._strip_audio_tags(keep)))
    keep2 = "第1名（2026年）"
    check("年份括号原样保留", P._strip_audio_tags(keep2) == keep2, repr(P._strip_audio_tags(keep2)))


# ---------------- 9. 并发保护 ----------------
async def test_inflight():
    print("\n=== 9. 并发保护 ===")
    p = make_plugin()
    _set_config(p, P.VoicePluginConfig())
    p.ctx.send = FakeSend()
    called = {"tts": 0}

    async def slow_tts(**kw):
        called["tts"] += 1
        await asyncio.sleep(0.2)
        return {"success": True}

    p.voice_clone_tts = slow_tts
    t1 = asyncio.create_task(p._async_voice_reply("第一句", "", "same_stream", ""))
    await asyncio.sleep(0.05)
    await p._async_voice_reply("第二句", "", "same_stream", "")
    check("忙碌时第二句走文字降级", len(p.ctx.send.sent) == 1 and p.ctx.send.sent[0][0] == "第二句", str(p.ctx.send.sent))
    await t1
    check("忙碌时只调 1 次 TTS", called["tts"] == 1, str(called))

    # 关闭 skip_when_busy → 允许并发
    _set_config(p, P.VoicePluginConfig(fallback={"skip_when_busy": False}))
    called["tts"] = 0
    p.ctx.send = FakeSend()
    t1 = asyncio.create_task(p._async_voice_reply("A", "", "s2", ""))
    await asyncio.sleep(0.05)
    t2 = asyncio.create_task(p._async_voice_reply("B", "", "s2", ""))
    await asyncio.gather(t1, t2)
    check("关闭 skip_when_busy 后允许并发", called["tts"] == 2, str(called))

    # 结束后 inflight 被清理
    check("inflight 已清理", "s2" not in p._voice_inflight, str(p._voice_inflight))


# ---------------- 10. 工具返回值 ----------------

async def test_tool_result():
    print("\n=== 10. send_voice_reply 返回值（模型可读的 content）===")
    # 模型读的是返回 dict 的 content 字段。返回空或只有结构化字段时，
    # 模型会以为调用失败，然后拿同样内容再 reply 一遍，和「语音顶替
    # 本次文字回复」的设计打架。

    p = make_plugin()
    p._voice_inflight = set()

    async def fake_find(kwargs, msg_id=""):
        return "SID_OK"

    async def fake_async(*a, **k):
        return None

    p._find_stream_id = fake_find
    p._async_voice_reply = fake_async

    r = await p.send_voice_reply("你好呀", msg_id="m1")
    check("成功 success=True", r.get("success") is True, str(r))
    check("成功返回 content", bool(r.get("content")), str(r))
    check("content 提示不要重复发", "不需要再用 reply" in r.get("content", ""), str(r))

    # 找不到聊天流时也要有可读的 content
    async def fake_find_empty(kwargs, msg_id=""):
        return ""

    p._find_stream_id = fake_find_empty
    r = await p.send_voice_reply("你好呀", msg_id="m1")
    check("失败 success=False", r.get("success") is False, str(r))
    check("失败返回 content", bool(r.get("content")), str(r))

    # reply_text 为空
    p._find_stream_id = fake_find
    r = await p.send_voice_reply("   ", msg_id="m1")
    check("空文本有 content", bool(r.get("content")), str(r))


async def main():
    test_config()
    await test_find_stream()
    test_stream_extract()
    await test_fallback()
    await test_tts()
    await test_load_voices()
    await test_load_voices_bad_file()
    await test_load_voices_keeps_old_on_partial_failure()
    test_resolve_voice()
    await test_audio_format()
    await test_audio_base64_kept_without_stream()
    await test_raw_pcm_wrapped_before_send()
    await test_api_rpc_timeout_declared()
    await test_cancel_kills_ffmpeg()
    await test_unload_awaits_cancel()
    test_audio_tag_words()
    await test_inflight()
    await test_tool_result()

    print("\n" + "=" * 50)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  -", f)
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))


