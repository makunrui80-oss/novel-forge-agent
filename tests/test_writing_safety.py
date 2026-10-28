"""写作事实边界、改稿保护和检测正文版本的回归测试，不联网、不使用用户数据库。"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app as web
from core import config, db, llm, pipeline, zhuque


class WritingSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, 'DB_PATH', Path(self.temp.name) / 'novel.db')
        self.db_patch.start()
        db.init()
        self.pid = db.execute('INSERT INTO projects(name) VALUES(?)', ('原创书',))
        self.tid = db.create_task(self.pid, 'rewrite', '改稿', {})
        db.update_task(self.tid, status='running')
        self.cfg = json.loads(json.dumps(config.DEFAULT_CONFIG))
        self.cfg['writing'].update(batch_size=2, words_per_chapter=600)
        self.cfg_patch = patch.object(config, 'load', return_value=self.cfg)
        self.cfg_patch.start()
        self.outline = '# 原创书\n主线：寻找妹妹，不得凭空获得异能。\n<!--PLAN\n' + '\n'.join(
            f'{n}|调查{n}|调查港口线索{n}|遇到阻碍|开门' for n in range(1, 10)) + '\nPLAN-->'
        db.execute('INSERT INTO outline(project_id,content) VALUES(?,?)', (self.pid, self.outline))
        db.execute('INSERT INTO constitution(project_id,content,locked) VALUES(?,?,1)',
                   (self.pid, '人物弱点每卷影响一次选择，不要求每章发作。',))
        self.old = '人物坚持寻找妹妹，调查港口。' * 40
        self.new = '人物核对港口线索，继续寻找妹妹。' * 40

    async def asyncTearDown(self):
        self.cfg_patch.stop()
        self.db_patch.stop()
        self.temp.cleanup()

    def chapter(self, idx, body=None, polished=''):
        db.execute('INSERT INTO generated(project_id,idx,title,content,polished,zhuque_score,status) VALUES(?,?,?,?,?,100,?)',
                   (self.pid, idx, f'调查{idx}', body or self.old, polished, 'high_aigc'))
        return pipeline._gen_row(self.pid, idx)

    def issue(self, evidence='港口'):
        return {'issues': [{'type': '事实冲突', 'issue': '改变了人物目标', 'evidence': evidence,
                            'basis': '寻找妹妹', 'suggestion': '保留寻找妹妹的目标'}]}

    async def test_short_rewrite_preserves_original_and_saves_candidate(self):
        self.chapter(1)
        with patch.object(llm, 'chat', AsyncMock(return_value='太短')):
            with self.assertRaisesRegex(pipeline.ChapterReviewError, '过短'):
                await pipeline.run_rewrite(self.tid, self.pid, {'idx': 1})
        self.assertEqual(pipeline._gen_row(self.pid, 1)['content'], self.old)
        self.assertEqual(db.get_asset(self.pid, 'chapter_review_1_candidate'), '太短')

    async def test_review_rejects_plot_change_without_overwriting_body(self):
        self.chapter(1)
        with patch.object(llm, 'chat', AsyncMock(side_effect=[self.new, json.dumps(self.issue())])):
            with self.assertRaisesRegex(pipeline.ChapterReviewError, '改变了人物目标'):
                await pipeline.run_rewrite(self.tid, self.pid, {'idx': 1})
        self.assertEqual(pipeline._gen_row(self.pid, 1)['content'], self.old)
        self.assertEqual(json.loads(db.get_asset(self.pid, 'chapter_review_1'))['status'], 'rejected')

    async def test_wrong_or_multiple_chapters_cannot_be_saved_as_one(self):
        self.chapter(1)
        for text in ('## 第2章 错章\n' + self.new,
                     '## 第1章 调查\n' + self.new + '\n## 第2章 多写\n' + self.new):
            with patch.object(llm, 'chat', AsyncMock()) as chat:
                with self.assertRaises(pipeline.ChapterReviewError):
                    await pipeline._review_candidate(self.tid, self.pid, 1, text, '边界', self.old)
                chat.assert_not_awaited()
        self.assertEqual(pipeline._gen_row(self.pid, 1)['content'], self.old)

    async def test_fabricated_review_evidence_cannot_reject_as_verified_fact(self):
        with patch.object(llm, 'chat', AsyncMock(return_value=json.dumps(self.issue('正文中根本没有这句话')))) as chat:
            with self.assertRaises(pipeline.ChapterReviewError):
                await pipeline._review_candidate(self.tid, self.pid, 1, self.new, '边界')
        self.assertEqual(chat.await_count, 2)
        self.assertEqual(json.loads(db.get_asset(self.pid, 'chapter_review_1'))['status'], 'unverified')

    async def test_semantic_review_accepts_paraphrase_and_rewrite_resets_score(self):
        self.chapter(1)
        with patch.object(llm, 'chat', AsyncMock(side_effect=[self.new, '{"issues":[]}'])), \
             patch.object(pipeline, '_zhuque_loop', AsyncMock()), \
             patch.object(pipeline, '_build_context', AsyncMock()):
            await pipeline.run_rewrite(self.tid, self.pid, {'idx': 1})
        row = pipeline._gen_row(self.pid, 1)
        self.assertEqual(row['content'], self.new)
        self.assertIsNone(row['zhuque_score'])
        backup = json.loads(db.get_asset(self.pid, f'chapter_1_rewrite_{self.tid}_before'))
        self.assertEqual(backup['content'], self.old)

    async def test_rewrite_reads_outline_recent_five_actual_tail_and_next_boundary(self):
        for n in range(1, 9):
            self.chapter(n, f'剧情事实{n}开头' + '行动细节。' * 500 + f'剧情事实{n}结尾')
        db.execute('INSERT INTO contexts(project_id,after_batch,content) VALUES(?,?,?)',
                   (self.pid, 2, json.dumps({'context_card': '上一批事实卡'})))
        original = pipeline._chapter_text(pipeline._gen_row(self.pid, 7))
        with patch.object(llm, 'chat', AsyncMock(side_effect=[original, '{"issues":[]}'])) as chat, \
             patch.object(pipeline, '_zhuque_loop', AsyncMock()), \
             patch.object(pipeline, '_build_context', AsyncMock()):
            await pipeline.run_rewrite(self.tid, self.pid, {'idx': 7, 'instruction': '减少重复解释'})
        prompt = chat.await_args_list[0].args[2]
        self.assertIn(self.outline, prompt)
        self.assertIn('调查港口线索7', prompt)
        self.assertIn('上一批事实卡', prompt)
        for n in range(2, 7):
            self.assertIn(pipeline._gen_row(self.pid, n)['content'], prompt)
        tail = prompt.split('【上一章结尾（衔接用）】')[1].split('【改写要求】')[0]
        self.assertIn('剧情事实6结尾', tail)
        self.assertNotIn('剧情事实6开头', tail)
        self.assertIn('剧情事实8开头', prompt)

    async def test_missing_recent_chapter_beyond_opening_is_detected(self):
        for n in (1, 2, 3, 4, 5, 6, 8):
            self.chapter(n)
        with self.assertRaisesRegex(ValueError, '缺少第 7 章正文'):
            pipeline._writing_memory(self.pid, 9)

    async def test_user_edit_during_rewrite_is_not_overwritten(self):
        self.chapter(1)
        async def chat(stage, *args, **kwargs):
            if stage == 'check':
                db.execute('UPDATE generated SET content=? WHERE project_id=? AND idx=1', ('用户最新手改。', self.pid))
                return '{"issues":[]}'
            return self.new
        with patch.object(llm, 'chat', side_effect=chat):
            with self.assertRaisesRegex(ValueError, '已被修改'):
                await pipeline.run_rewrite(self.tid, self.pid, {'idx': 1})
        self.assertEqual(pipeline._gen_row(self.pid, 1)['content'], '用户最新手改。')

    async def test_new_chapter_gets_one_plot_repair_before_commit(self):
        plan = {'idx': 1, 'title': '调查', 'event': '寻找妹妹'}
        responses = [self.old, json.dumps(self.issue()), self.new, '{"issues":[]}']
        with patch.object(llm, 'chat', AsyncMock(side_effect=responses)) as chat, \
             patch.object(pipeline, '_zhuque_loop', AsyncMock()):
            await pipeline.write_one_chapter(self.tid, self.pid, plan, [plan], 0, '主线', '原创书', 600, {})
        self.assertEqual([c.args[0] for c in chat.await_args_list], ['write', 'check', 'write', 'check'])
        self.assertEqual(pipeline._gen_row(self.pid, 1)['content'], self.new)

    async def test_automatic_edit_checks_visible_polished_body_and_keeps_it_on_failure(self):
        self.chapter(1, polished=self.new)
        self.cfg['zhuque'].update(enabled=True, mode='api', max_rounds=1)
        with patch.object(zhuque, 'detect', AsyncMock(return_value={'ok': True, 'score': 100})) as detect, \
             patch.object(llm, 'chat', AsyncMock(return_value='截断')):
            await pipeline._zhuque_loop(self.tid, self.pid, 1, 600)
        detect.assert_awaited_once_with(self.new)
        row = pipeline._gen_row(self.pid, 1)
        self.assertEqual(row['content'], self.old)
        self.assertEqual(row['polished'], self.new)
        report = json.loads(db.get_asset(self.pid, 'zhuque_1_latest'))
        self.assertEqual(report['text_sha256'], hashlib.sha256(self.new.encode()).hexdigest())
        self.assertNotIn(self.new, json.dumps(report))

    async def test_failed_check_does_not_keep_old_100_as_latest_score(self):
        self.chapter(1)
        self.cfg['zhuque'].update(enabled=True, mode='api', max_rounds=0)
        with patch.object(zhuque, 'detect', AsyncMock(return_value={'ok': False, 'error': '网络错误'})):
            await pipeline._zhuque_loop(self.tid, self.pid, 1, 600)
        row = pipeline._gen_row(self.pid, 1)
        self.assertIsNone(row['zhuque_score'])
        self.assertEqual(row['status'], 'check_failed')
        self.assertEqual(row['content'], self.old)

    async def test_higher_score_edit_never_replaces_lower_score_original(self):
        self.chapter(1)
        self.cfg['zhuque'].update(enabled=True, mode='api', max_rounds=2)
        with patch.object(zhuque, 'detect', AsyncMock(side_effect=[{'ok': True, 'score': 58.9}, {'ok': True, 'score': 100}])) as detect, \
             patch.object(llm, 'chat', AsyncMock(side_effect=[self.new, '{"issues":[]}'])):
            await pipeline._zhuque_loop(self.tid, self.pid, 1, 600)
        row = pipeline._gen_row(self.pid, 1)
        self.assertEqual(row['content'], self.old)
        self.assertEqual(row['zhuque_score'], 58.9)
        latest = json.loads(db.get_asset(self.pid, 'zhuque_1_latest'))
        self.assertEqual(latest['text_sha256'], hashlib.sha256(self.old.encode()).hexdigest())
        rejected = json.loads(db.get_asset(self.pid, f'zhuque_1_{self.tid}_1'))
        self.assertFalse(rejected['applied'])
        self.assertEqual(detect.await_count, 2)  # 不继续浪费调用次数。

    async def test_better_score_edit_is_promoted_only_after_plot_and_detection(self):
        self.chapter(1)
        self.cfg['zhuque'].update(enabled=True, mode='api', max_rounds=2)
        async def detect(text):
            self.assertEqual(pipeline._gen_row(self.pid, 1)['content'], self.old)
            return {'ok': True, 'score': 58.9 if text == self.old else 12}
        with patch.object(zhuque, 'detect', side_effect=detect), \
             patch.object(llm, 'chat', AsyncMock(side_effect=[self.new, '{"issues":[]}'])):
            await pipeline._zhuque_loop(self.tid, self.pid, 1, 600)
        row = pipeline._gen_row(self.pid, 1)
        self.assertEqual(row['content'], self.new)
        self.assertEqual(row['zhuque_score'], 12)
        self.assertEqual(row['status'], 'checked')

    async def test_candidate_detection_failure_keeps_successful_original_check(self):
        self.chapter(1)
        self.cfg['zhuque'].update(enabled=True, mode='api', max_rounds=1)
        with patch.object(zhuque, 'detect', AsyncMock(side_effect=[{'ok': True, 'score': 58.9}, {'ok': False, 'error': '网络失败'}])), \
             patch.object(llm, 'chat', AsyncMock(side_effect=[self.new, '{"issues":[]}'])):
            await pipeline._zhuque_loop(self.tid, self.pid, 1, 600)
        row = pipeline._gen_row(self.pid, 1)
        self.assertEqual(row['content'], self.old)
        self.assertEqual(row['zhuque_score'], 58.9)
        self.assertEqual(row['status'], 'high_aigc')

    async def test_failed_polish_is_skipped_and_not_reported_as_all_success(self):
        self.chapter(1)
        with patch.object(llm, 'chat', AsyncMock(return_value='截断')):
            await pipeline.run_polish(self.tid, self.pid, {})
        self.assertEqual(pipeline._gen_row(self.pid, 1)['content'], self.old)
        self.assertEqual(pipeline._gen_row(self.pid, 1)['polished'], '')
        self.assertIn('0 章通过；1 章保留原稿', db.task_row(self.tid)['message'])

    async def test_realign_second_chapter_reads_first_corrected_chapter(self):
        self.chapter(1)
        self.chapter(2)
        db.execute('INSERT INTO contexts(project_id,after_batch,content) VALUES(?,?,?)',
                   (self.pid, 0, json.dumps({'deviations': [
                       {'chapter': 1, 'issue': '主线偏离', 'suggestion': '查案'},
                       {'chapter': 2, 'issue': '主线偏离', 'suggestion': '查案'}]})))
        with patch.object(llm, 'chat', AsyncMock(side_effect=[self.new, '{"issues":[]}', self.new, '{"issues":[]}'])) as chat, \
             patch.object(pipeline, '_zhuque_loop', AsyncMock()), \
             patch.object(pipeline, '_build_context', AsyncMock()):
            await pipeline.realign_batch(self.tid, self.pid, 0)
        self.assertIn(self.new, chat.await_args_list[2].args[2])
        self.assertEqual(pipeline._gen_row(self.pid, 2)['content'], self.new)

    async def test_manual_edit_clears_score_and_stale_contexts_and_keeps_backup(self):
        row = self.chapter(1)
        db.execute('INSERT INTO contexts(project_id,after_batch,content) VALUES(?,?,?)', (self.pid, 0, '{}'))
        await web.update_generated(row['id'], {'content': self.new})
        current = pipeline._gen_row(self.pid, 1)
        self.assertIsNone(current['zhuque_score'])
        self.assertIsNone(db.q1('SELECT * FROM contexts WHERE project_id=?', (self.pid,)))
        self.assertEqual(json.loads(db.get_asset(self.pid, 'chapter_1_manual_edit_0_before'))['content'], self.old)

    async def test_reader_can_distinguish_stale_detector_result(self):
        row = self.chapter(1)
        db.upsert_asset(self.pid, 'zhuque_1_latest', json.dumps({'text_sha256': 'old-version'}))
        result = await web.generated_one(row['id'])
        self.assertFalse(result['detection']['matches_current'])


if __name__ == '__main__':
    unittest.main()
