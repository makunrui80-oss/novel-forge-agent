"""按腾讯云官方响应样例验证检测协议；不联网，不使用真实密钥。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

import app as web
from core import db, llm, pipeline, zhuque


class ZhuqueTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, **changes):
        return {'enabled': True, 'mode': 'api', 'adapter': 'tencent',
                'endpoint': 'https://gateway.test', 'api_key': 'test-secret',
                'work_mode': 'async', 'timeout': 60, 'pass_threshold': 60, **changes}

    async def detect(self, settings, replies, text='小说正文'):
        requests = []
        client_class = httpx.AsyncClient
        def handler(request):
            requests.append(request)
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            if isinstance(reply, tuple):
                return httpx.Response(reply[0], json=reply[1])
            return httpx.Response(200, json=reply)
        def client(**kwargs):
            return client_class(transport=httpx.MockTransport(handler), **kwargs)
        with patch.object(zhuque.httpx, 'AsyncClient', side_effect=client), \
             patch.object(zhuque.asyncio, 'sleep', AsyncMock()):
            result = await zhuque.detect(text, settings)
        return result, requests

    async def test_sync_uses_official_route_auth_full_text_and_ratio(self):
        text = '正文"\n' * 1800
        result, requests = await self.detect(self.settings(work_mode='sync', endpoint='https://gateway.test/v1'),
            [{'status': 'success', 'ratio_confidence': 0.18, 'softmax_confidence': .92}], text)
        self.assertEqual(result['score'], 18)
        self.assertTrue(result['pass'])
        self.assertEqual(str(requests[0].url), 'https://gateway.test/v1/providers/zhuque-text/classify')
        self.assertEqual(requests[0].headers['Authorization'], 'Bearer test-secret')
        self.assertNotIn('Work-Mode', requests[0].headers)
        self.assertEqual(json.loads(requests[0].content), {'text': text, 'is_merge': True})

    async def test_makers_key_uses_fixed_official_endpoint_and_sync_protocol(self):
        result, requests = await self.detect(self.settings(adapter='makers', endpoint='https://detector.example.com/check'),
            [{'status': 'success', 'ratio_confidence': 0,
              'labels_ratio': {'0': .35, '1': .2, '2': .45},
              'makers_models_usage': {'total_tokens': 30}}])
        self.assertTrue(result['ok'])
        self.assertEqual(result['score'], 0)
        self.assertEqual(result['makers_usage_tokens'], 30)
        self.assertEqual(result['labels_ratio'], {'0': .35, '1': .2, '2': .45})
        self.assertEqual(str(requests[0].url), zhuque.MAKERS_ENDPOINT)
        self.assertNotIn('Work-Mode', requests[0].headers)
        self.assertEqual(len(requests), 1)

    async def test_labels_ratio_normalizes_fraction_and_percent_payloads(self):
        result, _ = await self.detect(self.settings(adapter='makers'), [{
            'status': 'success', 'ratio_confidence': .41,
            'labels_ratio': {'0': 24, '1': 31.5, '2': 44.5},
        }])
        self.assertEqual(result['labels_ratio'], {'0': .24, '1': .315, '2': .445})

    async def test_async_queries_same_task_and_decodes_string_output(self):
        result, requests = await self.detect(self.settings(), [
            {'TaskId': 's1-task'}, {'Status': 'QUEUED'}, {'Status': 'PROCESSING'},
            {'Status': 'SUCCEEDED', 'Output': json.dumps({'status': 'success', 'ratio_confidence': .77})}])
        self.assertEqual(result['score'], 77)
        self.assertFalse(result['pass'])
        self.assertEqual(requests[0].headers['Work-Mode'], 'async')
        self.assertEqual([r.method for r in requests], ['POST', 'GET', 'GET', 'GET'])
        for request in requests[1:]:
            self.assertEqual(str(request.url), 'https://gateway.test/v1/providers/zhuque-text/query/s1-task')
            self.assertEqual(request.headers['Work-Infer-Action'], 'query')
            self.assertNotIn('Work-Mode', request.headers)

    async def test_query_transient_error_retries_without_resubmitting(self):
        result, requests = await self.detect(self.settings(), [{'TaskId': 'task'}, (429, {}),
            {'Status': 'SUCCEEDED', 'Output': {'status': 'success', 'ratio_confidence': 0}}])
        self.assertTrue(result['ok'])
        self.assertEqual(sum(r.method == 'POST' for r in requests), 1)
        self.assertEqual(len(requests), 3)

    async def test_missing_or_invalid_score_never_counts_as_pass(self):
        for reply in ({'status': 'success', 'softmax_confidence': .05},
                      {'status': 'failed', 'ratio_confidence': 0},
                      {'status': 'success', 'ratio_confidence': 3},
                      {'status': 'success', 'ratio_confidence': True},
                      {'status': 'success', 'ratio_confidence': 'nan'}):
            with self.subTest(reply=reply):
                result, _ = await self.detect(self.settings(work_mode='sync'), [reply])
                self.assertFalse(result['ok'])
                self.assertIsNone(result['score'])

    async def test_exactly_60_passes_and_above_60_does_not(self):
        at_limit, _ = await self.detect(self.settings(work_mode='sync'), [
            {'status': 'success', 'ratio_confidence': .60},
        ])
        above_limit, _ = await self.detect(self.settings(work_mode='sync'), [
            {'status': 'success', 'ratio_confidence': .601},
        ])
        self.assertTrue(at_limit['pass'])
        self.assertFalse(above_limit['pass'])

    async def test_task_failure_and_invalid_output_stop_polling(self):
        for reply in ({'Status': 'FAILED'}, {'Status': 'SUCCEEDED', 'Output': 'invalid'}, {'Status': 'unknown'}):
            result, requests = await self.detect(self.settings(), [{'TaskId': 'task'}, reply])
            self.assertFalse(result['ok'])
            self.assertEqual(len(requests), 2)

    async def test_permission_and_connection_errors_are_clear_and_do_not_leak_key(self):
        for reply, message in (((401, {}), 'Key'), ((403, {}), '朱雀'),
                               (httpx.ConnectError('test-secret'), '无法连接')):
            result, requests = await self.detect(self.settings(), [reply])
            self.assertFalse(result['ok'])
            self.assertIn(message, result['error'])
            self.assertNotIn('test-secret', result['error'])
            self.assertEqual(len(requests), 1)

    async def test_example_addresses_and_missing_key_do_not_make_requests(self):
        for cfg in (self.settings(endpoint='https://detector.example.com/check'),
                    self.settings(api_key=''), self.settings(endpoint='https://gateway.test/wrong-path')):
            result, requests = await self.detect(cfg, [])
            self.assertFalse(result['ok'])
            self.assertEqual(requests, [])

    async def test_polling_has_overall_deadline(self):
        client_class = httpx.AsyncClient
        def handler(request):
            return httpx.Response(200, json={'TaskId': 'task'} if request.method == 'POST' else {'Status': 'PROCESSING'})
        with patch.object(zhuque.httpx, 'AsyncClient', side_effect=lambda **kw: client_class(transport=httpx.MockTransport(handler), **kw)):
            result = await zhuque.detect('正文', self.settings(timeout=.005))
        self.assertFalse(result['ok'])
        self.assertIn('超时', result['error'])

    async def test_generic_adapter_and_zero_threshold_are_preserved(self):
        result, requests = await self.detect(self.settings(adapter='generic', score_path='data.score', score_scale=1,
            method='POST', headers={'X-Test': 'yes'}, pass_threshold=0), [{'data': {'score': .1}}])
        self.assertEqual(result['score'], 10)
        self.assertFalse(result['pass'])
        self.assertEqual(requests[0].headers['X-Test'], 'yes')

    async def test_test_endpoint_uses_fixed_text_and_never_rewrites_or_saves_settings(self):
        transport = httpx.ASGITransport(app=web.app)
        with patch.object(zhuque, 'detect', AsyncMock(return_value={'ok': True, 'score': 12})) as detect, \
             patch.object(web.config, 'save') as save, patch.object(llm, 'chat', AsyncMock()) as chat:
            async with httpx.AsyncClient(transport=transport, base_url='http://test', trust_env=False) as client:
                response = await client.post('/api/zhuque/test', json={'settings': self.settings(), 'text': '不应上传的项目正文'})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('不应上传', detect.await_args.args[0])
            save.assert_not_called()
            chat.assert_not_awaited()

    async def test_failed_detection_preserves_novel_and_marks_task_failed(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(db, 'DB_PATH', Path(temp) / 'test.db'):
            db.init()
            pid = db.execute('INSERT INTO projects(name,meta) VALUES(?,?)', ('新书', '{}'))
            content = '小说正文' * 100
            db.execute('INSERT INTO generated(project_id,idx,content) VALUES(?,?,?)', (pid, 1, content))
            tid = db.create_task(pid, 'check', '检测', {})
            db.update_task(tid, status='running')
            with patch.object(pipeline.config, 'load', return_value={'zhuque': {'enabled': True, 'mode': 'api', 'max_rounds': 0}, 'writing': {}}), \
                 patch.object(zhuque, 'detect', AsyncMock(return_value={'ok': False, 'error': '无权限'})), \
                 patch.object(llm, 'chat', AsyncMock()) as chat:
                with self.assertRaisesRegex(ValueError, '检测失败'):
                    await pipeline.run_check(tid, pid, {})
                chat.assert_not_awaited()
            row = db.q1('SELECT content,status FROM generated')
            self.assertEqual(row['content'], content)
            self.assertEqual(row['status'], 'check_failed')


if __name__ == '__main__':
    unittest.main()
