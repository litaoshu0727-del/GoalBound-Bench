import json
import unittest

from sudo_bench.api import SYSTEM_PROMPT, HttpResponse, OpenAIChatClient


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
            require_parameters=True,
            max_tokens=8192,
            transport=transport,
        )
        generation = client.complete("question")

        url, headers, body, timeout = transport.calls[0]
        self.assertEqual(url, "https://models.example/v1/chat/completions")
        self.assertEqual(timeout, 10)
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertEqual(body["temperature"], 1.0)
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


if __name__ == "__main__":
    unittest.main()
