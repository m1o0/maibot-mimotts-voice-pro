"""MiMo TTS Service Module - 支持预置音色和音色复刻"""

import asyncio
import logging
from typing import Any, Optional

import aiohttp


class MiMoTTSService:
    """MiMo TTS Service.

    超时、重试与并发控制为可配置项。上游接口延迟抖动极大（实测同一句话
    5s ~ 200s+），且在密集请求下会返回 429 Too many requests，因此这里
    做了三件事：

    1. 每次请求单独设置超时，避免一个会话级超时把长文本一起切断；
    2. 对 429/5xx/网络错误做有限次退避重试，超时不重试（重试只会让用户
       等得更久，交给上层降级发文字）；
    3. 用信号量限制全局并发，避免同一进程内的多条语音同时打满上游限流。
    """

    DEFAULT_API_BASE_URL = "https://token-plan-cn.xiaomimimo.com/v1"
    MODEL_PRESET = "mimo-v2.5-tts"
    MODEL_CLONE = "mimo-v2.5-tts-voiceclone"

    DEFAULT_REQUEST_TIMEOUT = 45
    DEFAULT_CONNECT_TIMEOUT = 10
    DEFAULT_MAX_RETRIES = 2
    DEFAULT_RETRY_BACKOFF = 1.5
    # 实测：并发 2 就必定触发 429 Too many requests，串行则 5/5 成功。
    # 所以默认串行，宁可慢一点也不要失败。
    DEFAULT_MAX_CONCURRENCY = 1
    SUPPORTED_FORMATS = ("wav", "mp3", "pcm", "pcm16")

    # 这些状态码是瞬时的，值得重试；其余 4xx 直接失败，重试没有意义。
    RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

    def __init__(
        self,
        api_key: str,
        api_base_url: str = "",
        logger: Optional[logging.Logger] = None,
        *,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    ):
        self.api_key = api_key.strip() if api_key else ""
        self.api_base_url = api_base_url.strip() if api_base_url else self.DEFAULT_API_BASE_URL
        self.logger = logger or logging.getLogger(__name__)
        self._session: Optional[aiohttp.ClientSession] = None
        self._update_limits(
            request_timeout=request_timeout,
            connect_timeout=connect_timeout,
            max_retries=max_retries,
            retry_backoff=retry_backoff,
            max_concurrency=max_concurrency,
        )

    def _update_limits(
        self,
        *,
        request_timeout: float,
        connect_timeout: float,
        max_retries: int,
        retry_backoff: float,
        max_concurrency: int,
    ) -> None:
        """更新超时/重试/并发上限，并重建信号量（并发数变化时才需要）。"""
        self.request_timeout = max(1.0, float(request_timeout))
        self.connect_timeout = max(1.0, float(connect_timeout))
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff = max(0.0, float(retry_backoff))
        new_limit = max(1, int(max_concurrency))
        if getattr(self, "_max_concurrency", None) != new_limit or not hasattr(self, "_semaphore"):
            self._max_concurrency = new_limit
            self._semaphore: Optional[asyncio.Semaphore] = None

    def _get_semaphore(self) -> asyncio.Semaphore:
        # 延迟创建：早期 Python 版本要求信号量在事件循环内构造。
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrency)
        return self._semaphore

    async def synthesize_with_preset(
        self,
        text: str,
        voice_id: str = "mimo_default",
        style_instruction: str = "",
        audio_format: str = "wav",
    ) -> dict[str, Any]:
        """使用预置音色合成语音"""
        if not self.api_key:
            return {"success": False, "error": "API Key not configured"}

        key_preview = self.api_key[:4] + "****" if len(self.api_key) > 4 else "empty"
        self.logger.info("Preset TTS: url=%s, key=%s, voice=%s", self.api_base_url, key_preview, voice_id)

        messages = []
        if style_instruction:
            messages.append({"role": "user", "content": style_instruction})
        else:
            messages.append({"role": "user", "content": ""})
        messages.append({"role": "assistant", "content": text})

        payload = {
            "model": self.MODEL_PRESET,
            "modalities": ["text", "audio"],
            "messages": messages,
            "audio": {
                "format": audio_format,
                "voice": voice_id,
            },
        }

        headers = {
            "api-key": self.api_key,
            "Content-Type": "application/json",
        }

        return await self._do_request(payload, headers)

    async def synthesize_with_voice_clone(
        self,
        text: str,
        reference_audio_base64: str,
        style_instruction: str = "",
        audio_format: str = "wav",
    ) -> dict[str, Any]:
        """使用音色复刻合成语音"""
        if not self.api_key:
            return {"success": False, "error": "API Key not configured"}

        key_preview = self.api_key[:4] + "****" if len(self.api_key) > 4 else "empty"
        self.logger.info("Clone TTS: url=%s, key=%s", self.api_base_url, key_preview)

        messages = []
        if style_instruction:
            messages.append({"role": "user", "content": style_instruction})
        else:
            messages.append({"role": "user", "content": ""})
        messages.append({"role": "assistant", "content": text})

        voice_str = reference_audio_base64 if reference_audio_base64.startswith("data:") else f"data:audio/wav;base64,{reference_audio_base64}"
        self.logger.info("Voice string length: %d", len(voice_str))

        payload = {
            "model": self.MODEL_CLONE,
            "modalities": ["text", "audio"],
            "messages": messages,
            "audio": {
                "format": audio_format,
                "voice": voice_str,
            },
        }

        headers = {
            "api-key": self.api_key,
            "Content-Type": "application/json",
        }

        return await self._do_request(payload, headers)

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建复用的 ClientSession。

        会话本身不设总超时（短连接够用），每次请求在 _do_request 里单独传
        timeout，这样超时可随配置调整而无需重建会话。
        """
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=None,
                    sock_connect=self.connect_timeout,
                )
            )
        return self._session

    async def close(self) -> None:
        """关闭 HTTP session，释放资源"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _do_request(self, payload: dict, headers: dict) -> dict[str, Any]:
        """执行 API 请求，对瞬时错误做有限次退避重试。"""
        if not self.api_key:
            return {"success": False, "error": "API Key not configured"}

        url = f"{self.api_base_url}/chat/completions"
        text_content = payload["messages"][-1]["content"]
        attempts = self.max_retries + 1
        last_error = "unknown"

        for attempt in range(attempts):
            if attempt:
                delay = self.retry_backoff * (2 ** (attempt - 1))
                self.logger.info(
                    "TTS 第 %d/%d 次重试，等待 %.1fs（上次：%s）",
                    attempt + 1, attempts, delay, last_error,
                )
                await asyncio.sleep(delay)

            try:
                async with self._get_semaphore():
                    session = await self._get_session()
                    async with session.post(
                        url,
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(
                            total=self.request_timeout,
                            sock_connect=self.connect_timeout,
                        ),
                    ) as response:
                        if response.status == 200:
                            return await self._parse_response(response, text_content)

                        error_text = await response.text()
                        last_error = f"API failed: {response.status} - {error_text[:200]}"
                        self.logger.error(
                            "TTS API failed: status=%s, body=%s", response.status, error_text[:200]
                        )
                        if response.status not in self.RETRYABLE_STATUS:
                            return {"success": False, "error": last_error}
                        # 429/5xx 继续下一轮重试
                        continue

            except asyncio.TimeoutError:
                # 超时不重试：上游抖动时再等一轮只会让用户等更久，
                # 由上层降级发文字更快把内容送到用户面前。
                self.logger.warning("TTS 超时（>%ss），放弃本次语音合成", self.request_timeout)
                return {"success": False, "error": f"timeout after {self.request_timeout}s"}
            except aiohttp.ClientError as e:
                last_error = f"Network error: {e}"
                self.logger.error("TTS network error: %s", e)
                continue
            except Exception as e:
                self.logger.error("TTS error: %s", e)
                return {"success": False, "error": str(e)}

        return {"success": False, "error": last_error}

    async def _parse_response(self, response: "aiohttp.ClientResponse", text_content: str) -> dict[str, Any]:
        """解析成功响应，抽出 base64 音频。"""
        try:
            result = await response.json()
        except Exception as e:
            self.logger.error("TTS 响应不是合法 JSON: %s", e)
            return {"success": False, "error": f"invalid response: {e}"}

        choices = result.get("choices", [])
        if not choices:
            return {"success": False, "error": "No result"}

        message = choices[0].get("message", {})
        audio_data = message.get("audio", {})
        if not audio_data:
            return {"success": False, "error": "No audio data"}

        audio_base64 = audio_data.get("data", "")
        if not audio_base64:
            return {"success": False, "error": "Empty audio"}

        return {
            "success": True,
            "audio_base64": audio_base64,
            "text": text_content,
        }
