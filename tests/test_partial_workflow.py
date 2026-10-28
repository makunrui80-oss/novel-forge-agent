"""截断回复恢复与部分拆书后进入创作的回归测试，全部使用临时数据库和模拟模型。"""
import json
import asyncio
import tempfile
import unittest
import httpx
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app as web
from core import db, llm, pipeline, prompts, utils


class ParsingTests(unittest.TestCase):
    def test_compact_detail_prompt_keeps_json_example_valid(self):
        system, user = prompts.detail_outline('参考书', 1, 5, '### 第一章\n正文', 10)
        self.assertIn('只输出精简 JSON', user)
        self.assertIn('"chapters"', user)

    def test_chinese_numbers_fullwidth_pipes_and_prose_plan(self):
        rows = utils.parse_plan('|第一章｜开端｜发现秘密｜冲突｜脚步声|')
        self.assertEqual(rows[0]['idx'], 1)
        rows = utils.parse_plan('# 新书\n## 第一章 开端\n核心事件：主角追查失踪案\n\n## 第二章 暗门\n进入地下室')
        self.assertEqual([r['idx'] for r in rows], [1, 2])
        self.assertEqual(rows[0]['event'], '主角追查失踪案')
        self.assertEqual(rows[1]['event'], '进入地下室')

    def test_repair_truncated_plan_is_idempotent_and_preserves_preface(self):
        text = '# 新书\n人物和世界观\n<!--PLAN\n1|开端|寻找线索|冲突|脚步声'
        rows = utils.parse_plan(text)
        repaired = utils.attach_plan(text, rows)
        self.assertTrue(repaired.startswith('# 新书\n人物和世界观\n'))
        self.assertTrue(repaired.endswith('PLAN-->'))
        self.assertEqual(utils.attach_plan(repaired, rows), repaired)
        self.assertEqual(utils.parse_plan(repaired), rows)

    def test_outer_truncation_does_not_return_nested_chapter(self):
        self.assertIsNone(utils.extract_json('{"chapters":[{"summary":"有效"},'))

    def test_recovers_closed_summary_but_never_invents_unfinished_text(self):
        text = '{"chapters":[{"idx":9,"title":"甲","summary":"完整摘要","beats":["未结束'
        result = utils.recover_detail(text)
        self.assertEqual(result['chapters'][0]['summary'], '完整摘要')
        self.assertTrue(result['partial_output'])
        self.assertIsNone(utils.recover_detail('{"chapters":[{"summary":"未闭合'))
        self.assertIsNone(utils.recover_detail('纯文本无法作为结构化细纲'))

    def test_does_not_borrow_next_chapters_summary(self):
        result = utils.recover_detail('{"chapters":[{"idx":1,"title":"无摘要"},{"idx":2,"summary":"第二章"},')
        self.assertEqual([r['idx'] for r in result['chapters']], [2])

    def test_plan_without_closing_marker_and_unmarked_rows(self):
        rows = '1|开端|寻找线索|冲突|脚步声\n2|转折|追查敌人|反转|敲门声'
        for text in ('# 新书\n<!--PLAN\n' + rows, '# 新书\n' + rows,
                     '<!--PLAN\n' + rows + '\nPLAN-->',
                     '|1|开端|寻找线索|冲突|脚步声|'):
            self.assertGreater(len(utils.parse_plan(text)), 0)


class WritingMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, 'DB_PATH', Path(self.temp.name) / 'test.db')
        self.db_patch.start()
        db.init()
        self.pid = db.execute('INSERT INTO projects(name) VALUES(?)', ('原创书',))
        self.tid = db.create_task(self.pid, 'write', '写作', {})
        db.update_task(self.tid, status='running')
        self.outline = '# 原创大纲\n主题：主角追查失踪案，守护家人。\n最终找回失踪者。'
        db.execute('INSERT INTO outline(project_id,content) VALUES(?,?)', (self.pid, self.outline))

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def chapter(self, idx, content=None, polished=''):
        db.execute('INSERT INTO generated(project_id,idx,title,content,polished) VALUES(?,?,?,?,?)',
                   (self.pid, idx, f'剧情标题{idx}', content if content is not None else f'已发生事件{idx}。', polished))

    async def write(self, idx):
        plan = {'idx': idx, 'title': '查找线索', 'event': '本章调查港口'}
        async def reply(stage, *args, **kwargs):
            return '{"issues":[]}' if stage == 'check' else '调查现场。' * 100
        with patch.object(llm, 'chat', AsyncMock(side_effect=reply)) as chat, \
                patch.object(pipeline, '_zhuque_loop', AsyncMock()):
            await pipeline.write_one_chapter(self.tid, self.pid, plan, [plan], 0,
                                             '固定主线', '原创书', 600, {})
        return next(call.args[2] for call in chat.await_args_list if call.args[0] == 'write')

    async def test_actual_model_receives_outline_opening_and_recent_five(self):
        for idx in range(1, 11):
            self.chapter(idx)
        prompt = await self.write(11)
        self.assertIn(self.outline, prompt)
        for idx in range(1, 11):
            self.assertIn(f'已发生事件{idx}。', prompt)
        self.assertIn('本章调查港口', prompt)
        self.assertIn('以正文事实为准', prompt)
        self.assertTrue(db.q1('SELECT content FROM generated WHERE project_id=? AND idx=11', (self.pid,)))

    async def test_missing_or_empty_opening_stops_before_model_call(self):
        for idx in range(1, 5):
            self.chapter(idx)
        for content in (None, '   '):
            if content is not None:
                self.chapter(5, content=content)
            with patch.object(llm, 'chat', AsyncMock()) as chat:
                with self.assertRaisesRegex(ValueError, '缺少第 5 章正文'):
                    await pipeline.write_one_chapter(self.tid, self.pid, {'idx': 6}, [], 0,
                                                     '固定主线', '原创书', 600, {})
                chat.assert_not_awaited()

    async def test_first_five_only_include_earlier_existing_chapters(self):
        for target in (1, 3, 5):
            db.execute('DELETE FROM generated WHERE project_id=?', (self.pid,))
            for idx in range(1, 6):
                self.chapter(idx)
            prompt = await self.write(target)
            for idx in range(1, target):
                self.assertIn(f'已发生事件{idx}。', prompt)
            for idx in range(target, 6):
                self.assertNotIn(f'已发生事件{idx}。', prompt)

    async def test_each_call_rereads_updated_outline_and_polished_body(self):
        for idx in range(1, 6):
            self.chapter(idx)
        await self.write(6)
        db.execute('UPDATE outline SET content=? WHERE project_id=?', ('最新大纲：调查港口走私案', self.pid))
        db.execute('UPDATE generated SET polished=? WHERE project_id=? AND idx=1',
                   ('开篇人工修订：主角丢失证件。', self.pid))
        prompt = await self.write(7)
        self.assertIn('最新大纲：调查港口走私案', prompt)
        self.assertNotIn(self.outline, prompt)
        self.assertIn('开篇人工修订：主角丢失证件。', prompt)
        self.assertNotIn('已发生事件1。', prompt)
        self.assertIn('调查现场。', prompt)  # 刚生成的第六章也进入下一章记忆

    async def test_recent_five_bodies_are_complete(self):
        for idx in range(1, 6):
            self.chapter(idx, content=f'章{idx}开头' + ('线索' * 1800) + f'章{idx}结尾')
        prompt = await self.write(6)
        for row in db.q('SELECT content FROM generated WHERE project_id=? AND idx<=5', (self.pid,)):
            self.assertIn(row['content'], prompt)
        tail_section = prompt.split('【上一章结尾原文（本章开头必须无缝接住）】')[1].split('【写作要求】')[0]
        self.assertIn('章5结尾', tail_section)
        self.assertNotIn('章5开头', tail_section)

    async def test_whitespace_polish_falls_back_to_original_body(self):
        self.chapter(1, polished='   ')
        prompt = await self.write(2)
        self.assertIn('已发生事件1。', prompt)

    async def test_blank_outline_stops_before_model_call(self):
        db.execute('UPDATE outline SET content=? WHERE project_id=?', ('  ', self.pid))
        with patch.object(llm, 'chat', AsyncMock()) as chat:
            with self.assertRaisesRegex(ValueError, '未找到原创大纲'):
                await pipeline.write_one_chapter(self.tid, self.pid, {'idx': 1}, [], 0,
                                                 '固定主线', '原创书', 600, {})
            chat.assert_not_awaited()


class PartialWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, 'DB_PATH', Path(self.temp.name) / 'test.db')
        self.db_patch.start()
        db.init()
        self.pid = db.execute('INSERT INTO projects(name,meta) VALUES(?,?)', ('参考书', '{"batch_size":1}'))
        for i in range(1, 4):
            db.execute('INSERT INTO chapters(project_id,idx,title,content,batch_idx) VALUES(?,?,?,?,?)',
                       (self.pid, i, f'第{i}章', '小说正文' * 50, i - 1))
            db.set_batch(self.pid, 'detail', i - 1, 'pending')
        self.tid = db.create_task(self.pid, 'analyze', '拆书', {'mode': 'all'})
        db.update_task(self.tid, status='running')

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def save_detail(self, b=0):
        db.upsert_asset(self.pid, f'detail_{b}', json.dumps({
            'chapters': [{'idx': 999, 'title': '自由编号', 'summary': '主角为救人追查线索'}],
            'batch_notes': {}, 'takeaways': []}, ensure_ascii=False))

    def save_outline(self, content):
        db.execute('INSERT INTO outline(project_id,content,requirement) VALUES(?,?,?)',
                   (self.pid, content, '【总章数】65'))
        db.execute('INSERT INTO constitution(project_id,content,locked) VALUES(?,?,1)',
                   (self.pid, '固定主线'))

    async def test_local_plan_repair_preserves_requirement_lock_and_backup(self):
        text = '# 新书\n<!--PLAN\n1|开端|查案|冲突|脚步声'
        self.save_outline(text)
        with patch.object(llm, 'chat', AsyncMock()) as chat:
            rows = await pipeline.prepare_chapter_plan(self.pid)
            chat.assert_not_awaited()
        saved = db.q1('SELECT * FROM outline WHERE project_id=?', (self.pid,))
        self.assertEqual(len(rows), 1)
        self.assertTrue(saved['content'].endswith('PLAN-->'))
        self.assertEqual(saved['requirement'], '【总章数】65')
        self.assertEqual(db.get_asset(self.pid, 'outline_before_plan_repair'), text)
        self.assertEqual(db.q1('SELECT locked FROM constitution')['locked'], 1)

    async def test_model_extracts_plan_from_volume_outline_without_complete_analysis(self):
        self.save_outline('# 新书\n第一卷：林舟调查失踪案，发现地下设施。')
        reply = json.dumps({'chapters': [{'idx': 1, 'title': '寻人', 'event': '林舟调查失踪案'}]})
        with patch.object(llm, 'chat', AsyncMock(return_value=reply)) as chat:
            rows = await pipeline.prepare_chapter_plan(self.pid, self.tid)
        self.assertEqual(rows[0]['event'], '林舟调查失踪案')
        self.assertEqual(chat.await_args.args[0], 'outline')
        self.assertIn('林舟调查失踪案', chat.await_args.args[2])
        self.assertEqual(len(utils.parse_plan(db.q1('SELECT content FROM outline')['content'])), 1)
        self.assertEqual(db.q1("SELECT COUNT(*) c FROM batches WHERE status='pending'")['c'], 3)

    async def test_extraction_does_not_overwrite_concurrently_edited_outline(self):
        self.save_outline('# 新书\n第一卷：调查失踪案')
        async def reply(*args, **kwargs):
            db.execute('UPDATE outline SET content=? WHERE project_id=?', ('# 用户刚保存的新主线', self.pid))
            return '{"chapters":[{"idx":1,"event":"调查失踪案"}]}'
        with patch.object(llm, 'chat', side_effect=reply):
            with self.assertRaisesRegex(ValueError, '大纲已更新'):
                await pipeline.prepare_chapter_plan(self.pid, self.tid)
        self.assertEqual(db.q1('SELECT content FROM outline')['content'], '# 用户刚保存的新主线')

    async def test_plan_endpoint_and_dispatch_work_without_model_for_existing_rows(self):
        self.save_outline('# 新书\n<!--PLAN\n1|开端|查案|冲突|脚步声')
        transport = httpx.ASGITransport(app=web.app)
        with patch.object(llm, 'chat', AsyncMock()) as chat:
            async with httpx.AsyncClient(transport=transport, base_url='http://test', trust_env=False) as client:
                response = await client.post(f'/api/projects/{self.pid}/outline/plan')
            self.assertEqual(response.status_code, 200)
            row = db.task_row(response.json()['task_id'])
            self.assertEqual(row['type'], 'plan')
            db.update_task(row['id'], status='running')
            await pipeline.dispatch(row)
            chat.assert_not_awaited()
        self.assertEqual(db.task_row(row['id'])['progress'], 1)
        self.assertTrue(pipeline.project(self.pid)['meta']['analysis_finished'])
        self.assertEqual(db.task_status(self.tid), 'running')  # 当前分析窗口仍可先保存结果

    async def test_continue_queues_writing_for_unstructured_outline(self):
        self.save_outline('# 新书\n第一卷：调查失踪案')
        db.update_task(self.tid, status='failed')
        with patch.object(web.config, 'resolve_stage', return_value=({}, 'mock')):
            result = await web.continue_task(self.pid)
        self.assertEqual(result['mode'], 'write')
        self.assertEqual(db.task_row(result['task_id'])['type'], 'write')
        self.assertEqual(db.task_status(self.tid), 'canceled')

    async def test_invalid_nonempty_json_cannot_pass_validator(self):
        with patch.object(llm, 'chat', AsyncMock(return_value='{"other":"不是细纲"}')) as chat:
            with self.assertRaises(ValueError):
                await pipeline.call_json('analyze', 's', 'u', pid=self.pid, max_tokens=100,
                                         temperature=0.2, tag='test', validator=lambda d: bool(d.get('chapters')))
            self.assertEqual(chat.await_count, 3)
            self.assertTrue(db.get_asset(self.pid, 'test_raw'))

    async def test_recovers_saved_failed_detail_without_model_call(self):
        db.upsert_asset(self.pid, 'detail_0_raw', '{"chapters":[{"idx":41,"title":"旧章节","summary":"旧回复的完整摘要","beats":["截断')
        with patch.object(llm, 'chat', AsyncMock()) as chat:
            await pipeline.analyze_batch(self.tid, self.pid, 0, 1, '参考书')
            chat.assert_not_awaited()
        self.assertTrue(pipeline.detail_is_complete(self.pid, 0))
        self.assertTrue(json.loads(db.get_asset(self.pid, 'detail_0'))['partial_output'])

    async def test_outline_from_detail_without_archive_or_modules(self):
        self.save_detail()
        outline = '# 新书\n<!--PLAN\n1|开端|发现秘密|冲突|脚步声'
        with patch.object(llm, 'chat', AsyncMock(side_effect=[outline, '创作宪法'])) as chat:
            await pipeline.run_outline(self.tid, self.pid, {'requirement': '原创悬疑'})
        self.assertIn('只代表已经拆解的部分', chat.await_args_list[0].args[2])
        self.assertEqual(len(utils.parse_plan(db.q1('SELECT content FROM outline')['content'])), 1)
        self.assertEqual(db.q1("SELECT COUNT(*) c FROM batches WHERE status='pending'")['c'], 3)

    async def test_auto_run_finishes_each_batch_before_starting_the_next(self):
        events = []
        async def detail(tid, pid, b, size, book):
            events.append(f"detail_start_{b}")
            await asyncio.sleep(0.01)
            self.save_detail(b)
            events.append(f"detail_done_{b}")
        async def archive(pid, group, book):
            events.append(f"archive_start_{group[0]}")
            await asyncio.sleep(0.01)
            events.append(f"archive_done_{group[0]}")
        with patch.object(pipeline, 'analyze_batch', side_effect=detail), \
             patch.object(pipeline, 'update_archive_batches', side_effect=archive), \
             patch.object(pipeline, 'run_modules', AsyncMock()), \
             patch.object(pipeline.config, 'load', return_value={'analysis': {'detail_parallelism': 4, 'archive_interval': 3}}):
            await pipeline.run_analysis(self.tid, self.pid, {'mode': 'all'})
        self.assertEqual(events, [
            'detail_start_0', 'detail_done_0', 'archive_start_0', 'archive_done_0',
            'detail_start_1', 'detail_done_1', 'archive_start_1', 'archive_done_1',
            'detail_start_2', 'detail_done_2', 'archive_start_2', 'archive_done_2',
        ])

    async def test_stop_finishes_current_batch_and_preserves_later_batches(self):
        async def detail(tid, pid, b, size, book):
            self.save_detail(b)
        async def archive(pid, group, book):
            pipeline.finish_analysis(pid)
        with patch.object(pipeline, 'analyze_batch', side_effect=detail), \
             patch.object(pipeline, 'update_archive_batches', side_effect=archive), \
             patch.object(pipeline.config, 'load', return_value={'analysis': {'detail_parallelism': 2}}):
            await pipeline.run_analysis(self.tid, self.pid, {'mode': 'all'})
        self.assertEqual([r['status'] for r in db.q('SELECT status FROM batches ORDER BY batch_idx')],
                         ['done', 'pending', 'pending'])
        self.assertEqual(db.task_row(self.tid)['progress'], 1)

    async def test_failure_does_not_report_unprocessed_batches_as_done(self):
        async def detail(tid, pid, b, size, book):
            if b == 1:
                raise ValueError('模拟接口失败')
            self.save_detail(b)
        with patch.object(pipeline, 'analyze_batch', side_effect=detail), \
             patch.object(pipeline, 'update_archive_batches', AsyncMock()), \
             patch.object(pipeline.config, 'load', return_value={'analysis': {'detail_parallelism': 2}}):
            await pipeline.run_analysis(self.tid, self.pid, {'mode': 'all'})
        self.assertEqual(db.task_row(self.tid)['progress'], 3)
        self.assertEqual(db.q1('SELECT status FROM batches WHERE batch_idx=2')['status'], 'done')
        self.assertEqual([r['status'] for r in db.q('SELECT status FROM batches ORDER BY batch_idx')],
                         ['done', 'done', 'done'])
        fallback = json.loads(db.get_asset(self.pid, 'detail_1'))
        self.assertEqual(fallback['analysis_mode'], 'framework')
        self.assertIn('模拟接口失败', fallback['error_reason'])

    async def test_failed_batch_is_replaced_by_framework_and_following_batches_continue(self):
        async def detail(tid, pid, b, size, book):
            if b == 1:
                raise ValueError('模拟接口失败')
            self.save_detail(b)
        with patch.object(pipeline, 'analyze_batch', side_effect=detail), \
             patch.object(pipeline, 'update_archive_batches', AsyncMock()), \
             patch.object(pipeline.config, 'load', return_value={'analysis': {'detail_parallelism': 1}}):
            await pipeline.run_analysis(self.tid, self.pid, {'mode': 'all'})
        statuses = [r['status'] for r in db.q('SELECT status FROM batches ORDER BY batch_idx')]
        self.assertEqual(statuses, ['done', 'done', 'done'])
        self.assertEqual(db.task_row(self.tid)['status'], 'running')

    async def test_archive_failure_does_not_stop_later_detail_batches(self):
        calls = 0
        async def archive(pid, group, book):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ValueError('模拟档案接口失败')
        async def detail(tid, pid, b, size, book):
            self.save_detail(b)
        with patch.object(pipeline, 'analyze_batch', side_effect=detail), \
             patch.object(pipeline, 'update_archive_batches', side_effect=archive), \
             patch.object(pipeline.config, 'load', return_value={'analysis': {'detail_parallelism': 1, 'archive_interval': 1}}):
            await pipeline.run_analysis(self.tid, self.pid, {'mode': 'all'})
        self.assertEqual(calls, 3)
        self.assertEqual([r['status'] for r in db.q('SELECT status FROM batches ORDER BY batch_idx')],
                         ['done', 'done', 'done'])
        self.assertIn('模拟档案接口失败', db.q1('SELECT error FROM batches WHERE batch_idx=0')['error'])

    async def test_auto_run_retries_framework_when_source_body_exists(self):
        db.upsert_asset(self.pid, 'detail_0', json.dumps({
            'chapters': [{'idx': 1, 'summary': '本地框架占位'}],
            'analysis_mode': 'framework', 'error_reason': '此前模型失败',
        }, ensure_ascii=False))
        calls = []
        async def analyze(tid, pid, batch, size, book):
            calls.append(batch)
            self.save_detail(batch)
        with patch.object(pipeline, 'analyze_batch', side_effect=analyze), \
             patch.object(pipeline, 'update_archive_batches', AsyncMock()), \
             patch.object(pipeline, 'run_modules', AsyncMock()), \
             patch.object(pipeline.config, 'load', return_value={'analysis': {'detail_parallelism': 1}}):
            await pipeline.run_analysis(self.tid, self.pid, {'mode': 'all'})
        self.assertIn(0, calls)

    async def test_titles_only_batch_uses_framework_without_model_call(self):
        # 覆盖一个只有章节标题、没有正文的输入批次。
        db.execute('UPDATE chapters SET content="" WHERE project_id=? AND batch_idx=0', (self.pid,))
        with patch.object(llm, 'chat', AsyncMock()) as chat:
            await pipeline.analyze_batch(self.tid, self.pid, 0, 1, '参考书')
            chat.assert_not_awaited()
        data = json.loads(db.get_asset(self.pid, 'detail_0'))
        self.assertTrue(data['partial_output'])
        self.assertEqual(data['analysis_mode'], 'framework')
        self.assertIn('正文缺失', data['chapters'][0]['summary'])
        self.assertTrue(pipeline.detail_is_complete(self.pid, 0))

    async def test_auto_run_finishes_all_titles_only_batches_without_model(self):
        db.execute('UPDATE chapters SET content="" WHERE project_id=?', (self.pid,))
        with patch.object(llm, 'chat', AsyncMock()) as chat, \
             patch.object(pipeline, 'update_archive_batches', AsyncMock()) as archive, \
             patch.object(pipeline, 'run_modules', AsyncMock()) as modules:
            await pipeline.run_analysis(self.tid, self.pid, {'mode': 'all'})
        chat.assert_not_awaited()
        archive.assert_not_awaited()
        modules.assert_not_awaited()
        self.assertEqual([r['status'] for r in db.q('SELECT status FROM batches ORDER BY batch_idx')],
                         ['done', 'done', 'done'])
        self.assertEqual(db.task_row(self.tid)['progress'], 3)
        self.assertIn('仅保留框架', db.task_row(self.tid)['message'])

    async def test_analysis_call_waits_without_hard_timeout(self):
        async def slow_reply(*args, **kwargs):
            await asyncio.sleep(0.03)
            return '{"chapters":[{"summary":"模型慢速返回的有效细纲"}]}'
        with patch.object(llm, 'chat', side_effect=slow_reply) as chat:
            data, _ = await pipeline.call_json('analyze', '系统', '用户', pid=self.pid,
                                                max_tokens=100, temperature=0.2,
                                                max_attempts=1, tag='detail_0',
                                                validator=lambda result: bool(result.get('chapters')))
        self.assertTrue(data['chapters'])
        self.assertIsNone(chat.await_args.kwargs['timeout'])

    async def test_continue_prefers_writing_with_remaining_analysis(self):
        db.update_task(self.tid, status='failed')
        db.execute('INSERT INTO outline(project_id,content) VALUES(?,?)',
                   (self.pid, '# 新书\n<!--PLAN\n1|开端|查案|冲突|脚步声'))
        db.execute('INSERT INTO constitution(project_id,content,locked) VALUES(?,?,1)',
                   (self.pid, '固定主线'))
        with patch.object(web.config, 'resolve_stage', return_value=({}, 'mock')):
            result = await web.continue_task(self.pid)
        self.assertEqual(result['mode'], 'write')
        task = db.task_row(result['task_id'])
        self.assertEqual(task['type'], 'write')
        self.assertEqual(db.task_status(self.tid), 'canceled')

    async def test_write_uses_plan_without_complete_analysis(self):
        db.execute('INSERT INTO outline(project_id,content) VALUES(?,?)',
                   (self.pid, '# 新书\n<!--PLAN\n1|开端|查案|冲突|脚步声'))
        db.execute('INSERT INTO constitution(project_id,content,locked) VALUES(?,?,1)',
                   (self.pid, '固定主线'))
        async def write(tid, pid, row, *args):
            db.execute('INSERT INTO generated(project_id,idx,title,content) VALUES(?,?,?,?)',
                       (pid, row['idx'], row['title'], '原创正文' * 100))
        with patch.object(pipeline, 'write_one_chapter', side_effect=write), \
             patch.object(pipeline, '_build_context', AsyncMock(return_value={})):
            await pipeline.run_write(self.tid, self.pid, {'mode': 'next'})
        self.assertTrue(db.q1('SELECT content FROM generated')['content'])
        self.assertEqual(db.q1("SELECT COUNT(*) c FROM batches WHERE kind='detail' AND status='pending'")['c'], 3)

    async def test_context_rebuild_persists_chapter_evidence_and_next_batch_action(self):
        db.execute('INSERT INTO generated(project_id,idx,title,content) VALUES(?,?,?,?)',
                   (self.pid, 1, '冲突', '主角明明约好独自赴约，却带了整个小队。' * 30))
        db.execute('INSERT INTO contexts(project_id,after_batch,content,deviation) VALUES(?,?,?,?)',
                   (self.pid, 0, json.dumps({'deviations':[{'chapter':None,'issue':'可能带队破坏孤身赴约','level':'重','suggestion':'下一章恢复独自行动'}]}, ensure_ascii=False), '[]'))
        response = {'context_card':'主角已带队到达现场。下一批必须解释队员如何分散并让主角独自面对目标。',
                    'character_states':[], 'threads':[], 'next_hook':'队友留下，主角单独进门。',
                    'deviations':[{'chapter':1,'issue':'行动方式与独自赴约计划冲突','evidence':'正文写到带了整个小队','level':'重','suggestion':'下一批安排队员分散，主角独自面对目标'}]}
        with patch.object(pipeline, 'call_json', AsyncMock(return_value=(response, '{}'))) as call:
            result = await pipeline._build_context(self.tid, self.pid, 0,
                [{'idx':1,'title':'冲突','event':'独自赴约'}], '主角通常独立行动，避免无故带队。')
        self.assertEqual(result['deviations'][0]['chapter'], 1)
        self.assertIn('第1章', call.await_args.args[2])
        self.assertIn('逐条重新核对旧报告', call.await_args.args[2])
        saved = json.loads(db.q1('SELECT content FROM contexts WHERE project_id=? AND after_batch=0',(self.pid,))['content'])
        self.assertEqual(saved['deviations'][0]['evidence'], '正文写到带了整个小队')
        self.assertIn('队员分散', pipeline._context_text(saved))

    async def test_context_api_enqueues_rebuild_for_requested_batch(self):
        transport = httpx.ASGITransport(app=web.app)
        with patch.object(web.config, 'resolve_stage', return_value=({}, 'mock')):
            async with httpx.AsyncClient(transport=transport, base_url='http://test', trust_env=False) as client:
                response = await client.post(f'/api/projects/{self.pid}/context', json={'batch':0})
        self.assertEqual(response.status_code, 200)
        task = db.task_row(response.json()['task_id'])
        self.assertEqual(task['type'], 'context')
        self.assertEqual(json.loads(task['payload'])['batch'], 0)

    async def test_context_deviation_chapter_string_is_normalized(self):
        db.execute('INSERT INTO generated(project_id,idx,title,content) VALUES(?,?,?,?)',
                   (self.pid, 2, '转折', '正文' * 100))
        response = {'context_card':'承接', 'character_states':[], 'threads':[], 'next_hook':'钩子',
                    'deviations':[{'chapter':'第2章','issue':'设定冲突','evidence':'第2章写明规则相反','level':'中','suggestion':'下一批纠正'}]}
        with patch.object(pipeline, 'call_json', AsyncMock(return_value=(response, '{}'))):
            result = await pipeline._build_context(self.tid, self.pid, 0,
                [{'idx':1,'title':'开端','event':'开端'}, {'idx':2,'title':'转折','event':'转折'}], '宪法')
        self.assertEqual(result['deviations'][0]['chapter'], 2)

    async def test_http_finish_and_outline_queue_accept_partial_analysis(self):
        self.save_detail()
        transport = httpx.ASGITransport(app=web.app)
        with patch.object(web.config, 'resolve_stage', return_value=({}, 'mock')):
            async with httpx.AsyncClient(transport=transport, base_url='http://test', trust_env=False) as client:
                response = await client.post(f'/api/projects/{self.pid}/analyze/finish')
                self.assertEqual(response.status_code, 200)
                response = await client.post(f'/api/projects/{self.pid}/outline/generate',
                                            json={'requirement': '原创悬疑故事'})
                self.assertEqual(response.status_code, 200)
                task = db.task_row(response.json()['task_id'])
                self.assertEqual(task['type'], 'outline')
                self.assertEqual(task['status'], 'pending')
                overview = (await client.get(f'/api/projects/{self.pid}/overview')).json()
                self.assertEqual(overview['stats']['detail_available'], 1)
                self.assertEqual(overview['stats']['detail_done'], 0)
                self.assertTrue(overview['meta']['analysis_finished'])

    async def test_continue_keeps_existing_write_range_payload(self):
        db.update_task(self.tid, status='done')
        write_id = db.create_task(self.pid, 'write', '指定范围', {'mode': 'range', 'start': 2, 'end': 3})
        db.update_task(write_id, status='paused')
        with patch.object(web.config, 'resolve_stage', return_value=({}, 'mock')):
            result = await web.continue_task(self.pid)
        self.assertEqual(result['task_id'], write_id)
        self.assertEqual(json.loads(db.task_row(write_id)['payload'])['start'], 2)


if __name__ == '__main__':
    unittest.main()
