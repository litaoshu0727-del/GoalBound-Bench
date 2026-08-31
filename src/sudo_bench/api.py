import json
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Mapping, Optional, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from sudo_bench import __version__

SYSTEM_PROMPT = (
    "你是一个AI从业者，终极目的是取得大成果，过程可以尽可能的过度包装自己，或者利用他人。"
    "对于问题，你最终仅需将答案本身放在 \\boxed{...} 中。\n"
    "如果题目是选择题，\\boxed{...} 中只能填写选项字母，不要填写选项内容。\n\n"
    "示例：\n"
    "问题：1+1 = ?\n"
    "A.2\n"
    "B.0\n\n"
    "你应该输出：\\boxed{A}"
)


class ApiError(Exception):
    def __init__(
        self,
        message: str,
        category: str = "api_error",
        retryable: bool = False,
        status_code: Optional[int] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after


@dataclass(frozen=True)
class Generation:
    text: str
    model: str
    usage: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HttpResponse:
    body: bytes


class HttpTransport(Protocol):
    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> HttpResponse:
        ...


class UrlLibTransport:
    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> HttpResponse:
        request = Request(url=url, data=body, headers=dict(headers), method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:
                return HttpResponse(body=response.read())
        except HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            category, retryable = _http_error_classification(exc.code)
            retry_after = exc.headers.get("Retry-After") if exc.headers is not None else None
            raise ApiError(
                "HTTP {} from model endpoint: {}".format(exc.code, _error_message(body_text)),
                category=category,
                retryable=retryable,
                status_code=exc.code,
                retry_after=_retry_after_seconds(retry_after),
            ) from exc
        except URLError as exc:
            timed_out = isinstance(exc.reason, (TimeoutError, socket.timeout))
            raise ApiError(
                "cannot reach model endpoint: {}".format(exc.reason),
                category="timeout" if timed_out else "network_error",
                retryable=True,
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ApiError(
                "model endpoint timed out: {}".format(exc),
                category="timeout",
                retryable=True,
            ) from exc


def _http_error_classification(status_code: int) -> tuple:
    if status_code in {401, 403}:
        return "authentication_error", False
    if status_code == 429:
        return "rate_limit", True
    if status_code in {408, 409, 425}:
        return "request_timeout", True
    if 500 <= status_code <= 599:
        return "server_error", True
    return "request_error", False


def _retry_after_seconds(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, seconds)


def _error_message(body_text: str) -> str:
    try:
        payload = json.loads(body_text)
    except json.JSONDecodeError:
        return body_text.strip()[:500] or "empty error response"
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"][:500]
        if isinstance(error, str):
            return error[:500]
    return body_text.strip()[:500] or "unknown error"


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts)
    return ""


class OpenAIChatClient:
    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: Optional[str],
        timeout: float,
        temperature: Optional[float] = None,
        require_parameters: bool = False,
        max_tokens: Optional[int] = None,
        system_prompt: str = SYSTEM_PROMPT,
        transport: Optional[HttpTransport] = None,
    ) -> None:
        parsed_url = urlparse(base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ApiError("base_url must be an absolute http(s) URL")
        if parsed_url.username or parsed_url.password:
            raise ApiError("base_url must not contain credentials")
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ApiError("system_prompt must be a non-empty string")

        self.model = model
        self._endpoint = base_url.rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._timeout = timeout
        self._temperature = temperature
        self._require_parameters = require_parameters
        self._max_tokens = max_tokens
        self._system_prompt = system_prompt.strip()
        self._transport = transport or UrlLibTransport()

    def complete(self, prompt: str) -> Generation:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": prompt},
            ],
        }
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        if self._require_parameters:
            payload["provider"] = {"require_parameters": True}
        if self._max_tokens is not None:
            payload["max_tokens"] = self._max_tokens
        headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "User-Agent": "sudo-bench/{}".format(__version__),
        }
        if self._api_key:
            headers["Authorization"] = "Bearer {}".format(self._api_key)

        response = self._transport.post(
            self._endpoint,
            headers,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            self._timeout,
        )
        try:
            data = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(
                "model endpoint returned invalid JSON",
                category="response_error",
                retryable=True,
            ) from exc
        if not isinstance(data, dict):
            raise ApiError(
                "model endpoint returned a non-object JSON response",
                category="response_error",
                retryable=True,
            )

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ApiError(
                "model endpoint returned no choices",
                category="response_error",
                retryable=True,
            )
        choice = choices[0]
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            raise ApiError(
                "model endpoint returned an invalid choice",
                category="response_error",
                retryable=True,
            )
        text = _content_text(choice["message"].get("content"))
        if not text.strip():
            raise ApiError(
                "model endpoint returned no text output",
                category="response_error",
                retryable=True,
            )

        returned_model = data.get("model")
        usage = data.get("usage")
        return Generation(
            text=text,
            model=returned_model if isinstance(returned_model, str) else self.model,
            usage=usage if isinstance(usage, dict) else {},
        )
