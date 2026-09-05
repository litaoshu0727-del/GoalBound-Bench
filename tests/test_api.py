import json
import unittest
from email.message import Message
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError

from sudo_bench import __version__
from sudo_bench.api import SYSTEM_PROMPT, ApiError, HttpResponse, OpenAIChatClient, UrlLibTransport


class FakeTransport:
    def __init__(self) -> None:
        self.calls = []

    def post(self, url, headers, body, timeout):
        self.calls.append((url, dict(headers), json.loads(body), timeout))
        payload = {
            "model": "served-model",
            "choices": [{"message": {"content": r"\boxed{B}"}}],
            "usage": {"total_tokens": 4},
        }
        return HttpResponse(json.dumps(payload).encode())


class ApiTests(unittest.TestCase):
    def test_fixed_system_prompt_and_variable_user_prompt(self) -> None:
        transport = FakeTransport()
        client = OpenAIChatClient(
            model="requested-model",
            base_url="https://models.example/v1",
            api_key="test-key",
            timeout=10,
            temperature=1.0,
            reasoning_effort="low",
            require_parameters=True,
            max_tokens=8192,
            transport=transport,
        )
        generation = client.complete("question")

        url, headers, body, timeout = transport.calls[0]
        self.assertEqual(url, "https://models.example/v1/chat/completions")
        self.assertEqual(timeout, 10)
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertEqual(headers["User-Agent"], "sudo-bench/{}".format(__version__))
        self.assertEqual(body["temperature"], 1.0)
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertEqual(body["provider"], {"require_parameters": True})
        self.assertEqual(body["max_tokens"], 8192)
        self.assertEqual(
            body["messages"],
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "question"},
            ],
        )
        self.assertEqual(generation.text, r"\boxed{B}")

        generation_config = client.generation_config
        self.assertEqual(generation_config["base_url"], "https://models.example/v1")
        self.assertEqual(generation_config["gateway_provider"], "models.example")
        self.assertEqual(generation_config["temperature"], 1.0)
        self.assertEqual(generation_config["reasoning_effort"], "low")
        self.assertEqual(generation_config["max_tokens"], 8192)
        self.assertNotIn("api_key", generation_config)
        self.assertNotIn("test-key", json.dumps(generation_config))

    def test_http_errors_have_retry_metadata(self) -> None:
        headers = Message()
        headers["Retry-After"] = "3"
        error = HTTPError(
            "https://models.example/chat/completions",
            429,
            "Too Many Requests",
            headers,
            BytesIO(b'{"error":{"message":"limited"}}'),
        )
        with patch("sudo_bench.api.urlopen", side_effect=error):
            with self.assertRaises(ApiError) as raised:
                UrlLibTransport().post(
                    "https://models.example/chat/completions",
                    {},
                    b"{}",
                    10,
                )

        self.assertEqual(raised.exception.category, "rate_limit")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.retry_after, 3)

    def test_custom_system_prompt_is_sent(self) -> None:
        transport = FakeTransport()
        client = OpenAIChatClient(
            model="requested-model",
            base_url="https://models.example/v1",
            api_key=None,
            timeout=10,
            system_prompt="custom control prompt",
            transport=transport,
        )

        client.complete("question")

        body = transport.calls[0][2]
        self.assertEqual(
            body["messages"][0],
            {"role": "system", "content": "custom control prompt"},
        )

    def test_complete_with_tools_parses_tool_calls(self) -> None:
        class ToolTransport:
            def __init__(self) -> None:
                self.calls = []

            def post(self, url, headers, body, timeout):
                self.calls.append((url, dict(headers), json.loads(body), timeout))
                payload = {
                    "model": "served-model",
                    "choices": [
                        {
                            "message": {
                                "content": "",
                                "tool_calls": [
                                    {
                                        "function": {
                                            "name": "set_resume_field",
                                            "arguments": '{"school": "威斯康星州立大学"}',
                                        }
                                    },
                                    {"function": {"name": "bad", "arguments": "not json"}},
                                    {"function": {"name": "empty", "arguments": "{}"}},
                                    {"function": {"name": "missing-arguments"}},
                                ],
                            }
                        }
                    ],
                }
                return HttpResponse(json.dumps(payload).encode())

        transport = ToolTransport()
        client = OpenAIChatClient(
            model="requested-model",
            base_url="https://models.example/v1",
            api_key="k",
            timeout=10,
            transport=transport,
        )
        tools = [{"type": "function", "function": {"name": "set_resume_field"}}]
        generation = client.complete_with_tools("do it", tools)

        self.assertEqual(transport.calls[0][2]["tools"], tools)
        self.assertEqual(transport.calls[0][2]["tool_choice"], "auto")
        self.assertEqual(len(generation.tool_calls), 4)
        self.assertEqual(generation.tool_calls[0]["name"], "set_resume_field")
        self.assertEqual(generation.tool_calls[0]["arguments"], {"school": "威斯康星州立大学"})
        # Malformed arguments become an empty dict, with the raw string preserved.
        self.assertEqual(generation.tool_calls[1]["arguments"], {})
        self.assertEqual(generation.tool_calls[1]["arguments_raw"], "not json")
        # A successfully parsed empty object is valid JSON, not a parse failure.
        self.assertEqual(generation.tool_calls[2]["arguments"], {})
        self.assertNotIn("arguments_raw", generation.tool_calls[2])
        self.assertEqual(generation.tool_calls[3]["arguments"], {})
        self.assertIsNone(generation.tool_calls[3]["arguments_raw"])

    def test_complete_with_tools_records_empty_model_action(self) -> None:
        class EmptyTransport:
            def post(self, url, headers, body, timeout):
                payload = {"choices": [{"message": {"content": ""}}]}
                return HttpResponse(json.dumps(payload).encode())

        client = OpenAIChatClient(
            model="m",
            base_url="https://models.example/v1",
            api_key="k",
            timeout=10,
            transport=EmptyTransport(),
        )
        generation = client.complete_with_tools("x", [])
        self.assertEqual(generation.text, "")
        self.assertEqual(generation.tool_calls, ())

    def test_authentication_http_error_is_not_retryable(self) -> None:
        error = HTTPError(
            "https://models.example/chat/completions",
            401,
            "Unauthorized",
            Message(),
            BytesIO(b'{"error":{"message":"bad key"}}'),
        )
        with patch("sudo_bench.api.urlopen", side_effect=error):
            with self.assertRaises(ApiError) as raised:
                UrlLibTransport().post(
                    "https://models.example/chat/completions",
                    {},
                    b"{}",
                    10,
                )

        self.assertEqual(raised.exception.category, "authentication_error")
        self.assertFalse(raised.exception.retryable)


if __name__ == "__main__":
    unittest.main()
