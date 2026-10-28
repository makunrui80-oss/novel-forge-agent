import asyncio
import unittest
from unittest.mock import patch

from core import llm


class _Response:
    status_code = 200

    def __init__(self, body, status=200):
        self.body = body
        self.text = ""
        self.status_code = status

    def json(self):
        return self.body


class _Client:
    responses = []
    requests = []
    timeouts = []

    def __init__(self, *args, **kwargs):
        self.timeouts.append(kwargs.get('timeout', 'not-set'))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, headers, json):
        self.requests.append(json)
        response = self.responses.pop(0)
        return response if isinstance(response, _Response) else _Response(response)


class LLMResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_none_timeout_disables_httpx_request_deadline(self):
        _Client.responses = [{"choices": [{"message": {"content": "慢速返回的答案"}}]}]
        _Client.requests = []
        _Client.timeouts = []
        provider = {"type": "openai", "base_url": "http://localhost/v1", "api_key": "x"}
        with patch.object(llm.httpx, "AsyncClient", _Client), patch.object(llm, "_record_usage"):
            answer = await llm.chat("analyze", "系统", "用户", provider=provider,
                                    model="mock", timeout=None)
        self.assertEqual(answer, "慢速返回的答案")
        self.assertEqual(_Client.timeouts, [None])

    def test_extracts_common_openai_and_anthropic_shapes(self):
        self.assertEqual(llm._extract_response_text(
            {"choices": [{"message": {"content": "answer"}}]}, "openai"), "answer")
        self.assertEqual(llm._extract_response_text(
            {"choices": [{"message": {"content": [{"type": "text", "text": "a"},
                                                        {"type": "text", "text": "b"}]}}]}, "openai"), "ab")
        self.assertEqual(llm._extract_response_text(
            {"content": [{"type": "text", "text": "answer"}]}, "anthropic"), "answer")
        self.assertEqual(llm._extract_response_text(
            {"output_text": "answer"}, "openai"), "answer")
        sse = 'data: {"choices":[{"delta":{"content":"a"}}]}\n' \
              'data: {"choices":[{"delta":{"content":"b"}}]}\n' \
              'data: [DONE]\n'
        self.assertEqual(llm._extract_response_text(llm._parse_sse(sse, "openai"), "openai"), "ab")

    async def test_empty_reply_is_retried_with_clearer_prompt_and_more_tokens(self):
        _Client.responses = [
            {"choices": [{"message": {"content": None}, "finish_reason": "length"}],
             "usage": {"prompt_tokens": 1, "completion_tokens": 2}},
            {"choices": [{"message": {"content": "valid answer"}, "finish_reason": "stop"}],
             "usage": {"prompt_tokens": 1, "completion_tokens": 2}},
        ]
        _Client.requests = []
        provider = {"id": "test", "type": "openai", "base_url": "http://localhost/v1", "api_key": "x"}
        with patch.object(llm.httpx, "AsyncClient", _Client), \
             patch.object(llm, "_record_usage"), \
             patch.object(asyncio, "sleep", return_value=None):
            answer = await llm.chat("test", "system", "user", provider=provider,
                                    model="mock", max_tokens=100, retries=2)
        self.assertEqual(answer, "valid answer")
        self.assertEqual(len(_Client.requests), 2)
        self.assertEqual(_Client.requests[1]["max_tokens"], 2100)
        self.assertIn("没有返回可用的最终答案", _Client.requests[1]["messages"][-1]["content"])

    async def test_reasoning_exhaustion_expands_budget_and_thinking_option_can_fall_back(self):
        _Client.responses = [
            {"choices": [{"message": {"content": "", "reasoning_content": "private reasoning"}, "finish_reason": "length"}]},
            _Response({}, status=400),  # 某些中转不支持 thinking 选项。
            {"choices": [{"message": {"content": "最终答案"}, "finish_reason": "stop"}]},
        ]
        _Client.requests = []
        provider = {"type": "openai", "base_url": "http://localhost/v1", "api_key": "x"}
        with patch.object(llm.httpx, 'AsyncClient', _Client), patch.object(llm, '_record_usage'), \
             patch.object(asyncio, 'sleep', return_value=None):
            result = await llm.chat('analyze', 's', 'u', provider=provider, model='deepseek-test', max_tokens=4200)
        self.assertEqual(result, '最终答案')
        self.assertEqual(_Client.requests[1]['max_tokens'], 8400)
        self.assertEqual(_Client.requests[1]['thinking'], {'type': 'disabled'})
        self.assertNotIn('thinking', _Client.requests[2])

    async def test_truncated_nonempty_outline_is_retried_with_larger_budget(self):
        _Client.responses = [
            {"choices": [{"message": {"content": "不完整大纲"}, "finish_reason": "length"}]},
            {"choices": [{"message": {"content": "完整大纲"}, "finish_reason": "stop"}]},
        ]
        _Client.requests = []
        provider = {"type": "openai", "base_url": "http://localhost/v1", "api_key": "x"}
        with patch.object(llm.httpx, 'AsyncClient', _Client), patch.object(llm, '_record_usage'), \
             patch.object(asyncio, 'sleep', return_value=None):
            result = await llm.chat('outline', 's', 'u', provider=provider, model='mock', max_tokens=8000)
        self.assertEqual(result, '完整大纲')
        self.assertEqual(_Client.requests[1]['max_tokens'], 16000)

    def test_sse_preserves_length_finish_reason(self):
        result = llm._parse_sse('data: {"choices":[{"delta":{"content":"partial"},"finish_reason":"length"}]}', 'openai')
        self.assertEqual(result['choices'][0]['finish_reason'], 'length')


if __name__ == "__main__":
    unittest.main()
