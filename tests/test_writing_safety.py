"""写前记忆、直接保存、改稿保护和检测正文版本的回归测试，不联网、不使用用户数据库。"""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app as web
from core import config, db, llm, pipeline, prompts, zhuque


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

    async def test_short_rewrite_preserves_original_and_saves_candidate(self):
        self.chapter(1)
        with patch.object(llm, 'chat', AsyncMock(return_value='太短')):
            with self.assertRaisesRegex(pipeline.ChapterOutputError, '过短'):
                await pipeline.run_rewrite(self.tid, self.pid, {'idx': 1})
        self.assertEqual(pipeline._gen_row(self.pid, 1)['content'], self.old)
        self.assertEqual(db.get_asset(self.pid, 'chapter_output_1_candidate'), '太短')

    async def test_rewrite_saves_and_runs_one_automatic_zhuque_check(self):
        self.chapter(1)
        self.cfg['zhuque'].update(enabled=True, mode='api')
        with patch.object(llm, 'chat', AsyncMock(return_value=self.new)) as chat, \
             patch.object(zhuque, 'detect', AsyncMock(return_value={
                 'ok': True, 'score': 19, 'balance': 6,
                 'labels_ratio': {'0': .62, '1': .19, '2': .19},
             })) as detect, \
             patch.object(pipeline, '_build_context', AsyncMock()):
            await pipeline.run_rewrite(self.tid, self.pid, {'idx': 1})
        self.assertEqual([c.args[0] for c in chat.await_args_list], ['polish'])
        detect.assert_awaited_once_with(self.new)
        self.assertEqual(pipeline._gen_row(self.pid, 1)['content'], self.new)
        self.assertEqual(pipeline._gen_row(self.pid, 1)['status'], 'checked')
        report = json.loads(db.get_asset(self.pid, 'zhuque_1_latest'))
        self.assertEqual(report['balance'], 6)
        self.assertEqual(report['labels_ratio']['0'], .62)

    async def test_score_above_60_blocks_next_chapter(self):
        self.cfg['zhuque'].update(enabled=True, mode='api')
        plan = [
            {'idx': 1, 'title': '调查一', 'event': '查案', 'payoff': '', 'hook': ''},
            {'idx': 2, 'title': '调查二', 'event': '追踪', 'payoff': '', 'hook': ''},
        ]
        async def save_chapter(tid, pid, row, *args):
            db.execute('INSERT INTO generated(project_id,idx,title,content) VALUES(?,?,?,?)',
                       (pid, row['idx'], row['title'], self.new))
        with patch.object(pipeline, 'prepare_chapter_plan', AsyncMock(return_value=plan)), \
             patch.object(pipeline, 'write_one_chapter', side_effect=save_chapter) as write, \
             patch.object(pipeline, '_build_context', AsyncMock(return_value={})), \
             patch.object(zhuque, 'detect', AsyncMock(return_value={'ok': True, 'score': 61, 'balance': 5})) as detect:
            await pipeline.run_write(self.tid, self.pid, {'mode': 'next'})
            self.assertEqual(write.await_count, 1)
            detect.assert_awaited_once_with(self.new)
            self.assertEqual(pipeline._gen_row(self.pid, 1)['status'], 'high_aigc')
            with self.assertRaisesRegex(ValueError, '不高于 60'):
                await pipeline.run_write(self.tid, self.pid, {'mode': 'next'})
            self.assertEqual(write.await_count, 1)
            self.assertEqual(detect.await_count, 1)

    async def test_score_exactly_60_allows_next_chapter(self):
        self.cfg['zhuque'].update(enabled=True, mode='api')
        plan = [
            {'idx': 1, 'title': '调查一', 'event': '查案', 'payoff': '', 'hook': ''},
            {'idx': 2, 'title': '调查二', 'event': '追踪', 'payoff': '', 'hook': ''},
        ]
        async def save_chapter(tid, pid, row, *args):
            db.execute('INSERT INTO generated(project_id,idx,title,content) VALUES(?,?,?,?)',
                       (pid, row['idx'], row['title'], self.new))
        with patch.object(pipeline, 'prepare_chapter_plan', AsyncMock(return_value=plan)), \
             patch.object(pipeline, 'write_one_chapter', side_effect=save_chapter) as write, \
             patch.object(pipeline, '_build_context', AsyncMock(return_value={})), \
             patch.object(zhuque, 'detect', AsyncMock(side_effect=[
                 {'ok': True, 'score': 60, 'balance': 5},
                 {'ok': True, 'score': 0, 'balance': 4},
             ])) as detect:
            await pipeline.run_write(self.tid, self.pid, {'mode': 'next'})
            self.assertEqual(pipeline._gen_row(self.pid, 1)['status'], 'medium_aigc')
            await pipeline.run_write(self.tid, self.pid, {'mode': 'next'})
            self.assertEqual(pipeline._gen_row(self.pid, 2)['content'], self.new)
            self.assertEqual(write.await_count, 2)
            self.assertEqual(detect.await_count, 2)

    async def test_natural_edit_reads_previous_plot_and_rechecks_without_improvement(self):
        previous = '上一章收尾：妹妹留在港口，主角刚走出修理铺。' * 25
        self.chapter(1, previous)
        self.chapter(2)
        self.cfg['zhuque'].update(enabled=True, mode='api')
        with patch.object(llm, 'chat', AsyncMock(return_value=self.new)) as chat, \
             patch.object(zhuque, 'detect', AsyncMock(return_value={'ok': True, 'score': 25.3})) as detect, \
             patch.object(pipeline, '_build_context', AsyncMock()):
            await pipeline.run_rewrite(self.tid, self.pid, {'idx': 2, 'style': 'natural'})
        prompt = chat.await_args_list[0].args[2]
        self.assertIn(previous, prompt)
        self.assertIn('调查港口线索2', prompt)
        self.assertIn(prompts.NATURAL_EDIT_INSTRUCTION, prompt)
        self.assertEqual(detect.await_count, 1)
        self.assertEqual(chat.await_count, 1)
        row = pipeline._gen_row(self.pid, 2)
        self.assertEqual(row['content'], self.new)
        self.assertEqual(row['zhuque_score'], 25.3)
        self.assertEqual(row['status'], 'checked')
        backup = json.loads(db.get_asset(self.pid, f'chapter_2_natural_edit_{self.tid}_before'))
        self.assertEqual(backup['content'], self.old)
        self.assertIn('人工率 74.7%', db.task_row(self.tid)['message'])

    async def test_natural_edit_missing_previous_chapter_does_not_call_model(self):
        self.chapter(2)
        with patch.object(llm, 'chat', AsyncMock()) as chat:
            with self.assertRaisesRegex(ValueError, '缺少第 1 章正文'):
                await pipeline.run_rewrite(self.tid, self.pid, {'idx': 2, 'style': 'natural'})
        chat.assert_not_awaited()
        self.assertEqual(pipeline._gen_row(self.pid, 2)['content'], self.old)

    async def test_natural_edit_promotes_better_rechecked_candidate(self):
        self.chapter(1, '上一章收尾：主角离开港口。' * 45)
        self.chapter(2)
        self.cfg['zhuque'].update(enabled=True, mode='api', max_rounds=1)
        improved = '主角从港口转进仓库，手里仍攥着那张纸条。' * 40
        with patch.object(llm, 'chat', AsyncMock(return_value=self.new)) as chat, \
             patch.object(zhuque, 'detect', AsyncMock(return_value={
                 'ok': True, 'score': 25.3, 'labels_ratio': {'0': .3, '1': .4, '2': .3},
             })) as detect, patch.object(pipeline, '_build_context', AsyncMock()):
            await pipeline.run_rewrite(self.tid, self.pid, {'idx': 2, 'style': 'natural'})
        self.assertEqual(chat.await_count, 1)
        self.assertEqual(detect.await_count, 1)
        row = pipeline._gen_row(self.pid, 2)
        self.assertEqual(row['content'], self.new)
        self.assertEqual(row['zhuque_score'], 25.3)
        self.assertEqual(row['status'], 'checked')
        report = json.loads(db.get_asset(self.pid, 'zhuque_2_latest'))
        self.assertEqual(report['labels_ratio']['0'], .3)

    def test_zhuque_three_band_status_preserves_risk_colors(self):
        self.assertEqual(pipeline._zhuque_status(49.9), 'checked')
        self.assertEqual(pipeline._zhuque_status(50), 'medium_aigc')
        self.assertEqual(pipeline._zhuque_status(60), 'medium_aigc')
        self.assertEqual(pipeline._zhuque_status(60.1), 'high_aigc')

    def test_natural_edit_prompt_mixes_dialogue_and_narration_without_length_limits(self):
        self.assertIn('逐行台词的感觉改成完整的小说场景', prompts.NATURAL_EDIT_INSTRUCTION)
        self.assertIn('普通接话若没有戏剧分量', prompts.NATURAL_EDIT_INSTRUCTION)
        self.assertIn('重新组织场景和段落，不沿用原稿的换行', prompts.NATURAL_EDIT_INSTRUCTION)
        self.assertIn('段落不设长度', prompts.NATURAL_EDIT_INSTRUCTION)
        self.assertIn('删掉重复确认和空转问答', prompts.NATURAL_EDIT_INSTRUCTION)
        _, first = prompts.de_aigc(self.old, 75, 20, round_no=0, memory='前文事实')
        _, second = prompts.de_aigc(self.old, 75, 20, round_no=1, memory='前文事实')
        self.assertIn('普通的一问一答若没有戏剧停顿', first)
        self.assertIn('前文事实', first)
        self.assertIn('不要逐项打勾', second)

    async def test_insufficient_credit_falls_back_to_web_manual_score(self):
        row = self.chapter(1)
        self.cfg['zhuque'].update(enabled=True, mode='api')
        with patch.object(zhuque, 'detect', AsyncMock(return_value={
            'ok': False, 'score': None, 'balance': 0, 'manual_fallback': True,
            'error': '朱雀积分余额不足',
        })), patch.object(config, 'save') as save:
            await pipeline._auto_zhuque_check(self.tid, self.pid, 1)
        self.assertEqual(pipeline._gen_row(self.pid, 1)['status'], 'await_check')
        report = json.loads(db.get_asset(self.pid, 'zhuque_1_latest'))
        self.assertTrue(report['manual_fallback'])
        self.assertEqual(report['balance'], 0)
        self.assertEqual(save.call_args.args[0]['zhuque']['mode'], 'manual')
        result = await web.record_score(row['id'], {'score': 19.9, 'text': self.old})
        self.assertTrue(result['pass'])
        self.assertEqual(pipeline._gen_row(self.pid, 1)['status'], 'checked')

    async def test_successful_last_credit_passes_this_chapter_then_switches_mode(self):
        self.chapter(1)
        self.cfg['zhuque'].update(enabled=True, mode='api')
        with patch.object(zhuque, 'detect', AsyncMock(return_value={
            'ok': True, 'score': 19, 'balance': 0,
        })), patch.object(config, 'save') as save:
            await pipeline._auto_zhuque_check(self.tid, self.pid, 1)
        row = pipeline._gen_row(self.pid, 1)
        self.assertEqual(row['status'], 'checked')
        self.assertEqual(row['zhuque_score'], 19)
        report = json.loads(db.get_asset(self.pid, 'zhuque_1_latest'))
        self.assertTrue(report['manual_mode_next'])
        self.assertEqual(save.call_args.args[0]['zhuque']['mode'], 'manual')

    async def test_manual_score_rejects_unsaved_text_and_exactly_60_passes(self):
        row = self.chapter(1)
        with self.assertRaisesRegex(ValueError, '先保存章节'):
            await web.record_score(row['id'], {'score': 19, 'text': '编辑后但未保存的正文'})
        result = await web.record_score(row['id'], {'score': 60, 'text': self.old})
        self.assertTrue(result['pass'])
        self.assertEqual(result['human_rate'], 40.0)
        self.assertEqual(pipeline._gen_row(self.pid, 1)['status'], 'medium_aigc')

    async def test_manual_score_45_is_green_and_reports_human_rate(self):
        row = self.chapter(1)
        result = await web.record_score(row['id'], {'score': 45, 'text': self.old})
        self.assertTrue(result['pass'])
        self.assertEqual(result['human_rate'], 55.0)
        self.assertIn('人工率 55.0%', result['message'])
        self.assertEqual(pipeline._gen_row(self.pid, 1)['status'], 'checked')

    async def test_wrong_or_multiple_chapters_cannot_be_saved_as_one(self):
        self.chapter(1)
        for text in ('## 第2章 错章\n' + self.new,
                     '## 第1章 调查\n' + self.new + '\n## 第2章 多写\n' + self.new):
            with patch.object(llm, 'chat', AsyncMock()) as chat:
                with self.assertRaises(pipeline.ChapterOutputError):
                    await pipeline._prepare_candidate(self.tid, self.pid, 1, text, self.old)
                chat.assert_not_awaited()
        self.assertEqual(pipeline._gen_row(self.pid, 1)['content'], self.old)

    async def test_old_plot_report_cannot_block_usable_output(self):
        db.upsert_asset(self.pid, 'chapter_review_1', json.dumps({'status': 'rejected'}))
        with patch.object(llm, 'chat', AsyncMock()) as chat:
            body = await pipeline._prepare_candidate(self.tid, self.pid, 1, self.new)
        chat.assert_not_awaited()
        self.assertEqual(body, self.new)

    async def test_rewrite_resets_score_and_backs_up_original(self):
        self.chapter(1)
        with patch.object(llm, 'chat', AsyncMock(return_value=self.new)), \
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
        with patch.object(llm, 'chat', AsyncMock(return_value=original)) as chat, \
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
            db.execute('UPDATE generated SET content=? WHERE project_id=? AND idx=1', ('用户最新手改。', self.pid))
            return self.new
        with patch.object(llm, 'chat', side_effect=chat):
            with self.assertRaisesRegex(ValueError, '已被修改'):
                await pipeline.run_rewrite(self.tid, self.pid, {'idx': 1})
        self.assertEqual(pipeline._gen_row(self.pid, 1)['content'], '用户最新手改。')

    async def test_new_chapter_saves_after_one_generation_without_plot_gate(self):
        plan = {'idx': 1, 'title': '调查', 'event': '寻找妹妹'}
        self.cfg['zhuque'].update(enabled=True, mode='api')
        with patch.object(llm, 'chat', AsyncMock(return_value=self.new)) as chat, \
             patch.object(zhuque, 'detect', AsyncMock()) as detect:
            await pipeline.write_one_chapter(self.tid, self.pid, plan, [plan], 0, '主线', '原创书', 600, {})
        self.assertEqual([c.args[0] for c in chat.await_args_list], ['write'])
        detect.assert_not_awaited()
        self.assertEqual(pipeline._gen_row(self.pid, 1)['content'], self.new)

    def test_writing_prompt_uses_flexible_quality_rules_not_fixed_dialogue_quota(self):
        system, user = prompts.write_chapter(
            '宪法', '新书', {'idx': 1, 'title': '开端', 'event': '调查', 'payoff': '', 'hook': ''},
            '', '', '', '', 2600, outline=self.outline,
            opening_anchor='开篇事实', continuity_anchor='前五章事实',
        )
        self.assertIn('段落长短和对白多少都由这一场戏决定', user)
        self.assertIn('前五章事实', user)
        self.assertNotIn('对白占比不低于 35%', user)
        self.assertNotIn('开头三句内进入场景', user)
        self.assertNotIn('每章必须留钩子', user)
        self.assertIn('不设模板', user)
        self.assertIn('普通接话不必句句另起一段', user)
        self.assertIn('少用报告腔、空泛总结和无必要的专业词', user)

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
             patch.object(llm, 'chat', AsyncMock(return_value=self.new)):
            await pipeline._zhuque_loop(self.tid, self.pid, 1, 600)
        row = pipeline._gen_row(self.pid, 1)
        self.assertEqual(row['content'], self.old)
        self.assertEqual(row['zhuque_score'], 58.9)
        latest = json.loads(db.get_asset(self.pid, 'zhuque_1_latest'))
        self.assertEqual(latest['text_sha256'], hashlib.sha256(self.old.encode()).hexdigest())
        self.assertEqual(detect.await_count, 1)  # 每次手动检测只调用一次。

    async def test_better_score_edit_is_promoted_only_after_detection(self):
        self.chapter(1)
        self.cfg['zhuque'].update(enabled=True, mode='api', max_rounds=2)
        async def detect(text):
            self.assertEqual(pipeline._gen_row(self.pid, 1)['content'], self.old)
            return {'ok': True, 'score': 58.9}
        with patch.object(zhuque, 'detect', side_effect=detect), \
             patch.object(llm, 'chat', AsyncMock(return_value=self.new)):
            await pipeline._zhuque_loop(self.tid, self.pid, 1, 600)
        row = pipeline._gen_row(self.pid, 1)
        self.assertEqual(row['content'], self.old)
        self.assertEqual(row['zhuque_score'], 58.9)
        self.assertEqual(row['status'], 'medium_aigc')

    async def test_candidate_detection_failure_keeps_successful_original_check(self):
        self.chapter(1)
        self.cfg['zhuque'].update(enabled=True, mode='api', max_rounds=1)
        with patch.object(zhuque, 'detect', AsyncMock(side_effect=[{'ok': True, 'score': 58.9}, {'ok': False, 'error': '网络失败'}])), \
             patch.object(llm, 'chat', AsyncMock(return_value=self.new)):
            await pipeline._zhuque_loop(self.tid, self.pid, 1, 600)
        row = pipeline._gen_row(self.pid, 1)
        self.assertEqual(row['content'], self.old)
        self.assertEqual(row['zhuque_score'], 58.9)
        self.assertEqual(row['status'], 'medium_aigc')

    async def test_polish_saves_without_plot_gate_before_context_failure(self):
        self.chapter(1)
        with patch.object(llm, 'chat', AsyncMock(return_value=self.new)) as chat, \
             patch.object(pipeline, '_build_context', AsyncMock(side_effect=ValueError('承接卡暂不可用'))):
            with self.assertRaisesRegex(ValueError, '承接卡暂不可用'):
                await pipeline.run_polish(self.tid, self.pid, {})
        self.assertEqual([c.args[0] for c in chat.await_args_list], ['polish'])
        self.assertEqual(pipeline._gen_row(self.pid, 1)['polished'], self.new)

    async def test_failed_polish_is_skipped_and_not_reported_as_all_success(self):
        self.chapter(1)
        with patch.object(llm, 'chat', AsyncMock(return_value='截断')):
            await pipeline.run_polish(self.tid, self.pid, {})
        self.assertEqual(pipeline._gen_row(self.pid, 1)['content'], self.old)
        self.assertEqual(pipeline._gen_row(self.pid, 1)['polished'], '')
        self.assertIn('0 章已更新；1 章保留原稿', db.task_row(self.tid)['message'])

    async def test_realign_second_chapter_reads_first_corrected_chapter(self):
        self.chapter(1)
        self.chapter(2)
        db.execute('INSERT INTO contexts(project_id,after_batch,content) VALUES(?,?,?)',
                   (self.pid, 0, json.dumps({'deviations': [
                       {'chapter': 1, 'issue': '主线偏离', 'suggestion': '查案'},
                       {'chapter': 2, 'issue': '主线偏离', 'suggestion': '查案'}]})))
        with patch.object(llm, 'chat', AsyncMock(side_effect=[self.new, self.new])) as chat, \
             patch.object(pipeline, '_zhuque_loop', AsyncMock()), \
             patch.object(pipeline, '_build_context', AsyncMock()):
            await pipeline.realign_batch(self.tid, self.pid, 0)
        self.assertEqual([c.args[0] for c in chat.await_args_list], ['polish', 'polish'])
        self.assertIn(self.new, chat.await_args_list[1].args[2])
        self.assertEqual(pipeline._gen_row(self.pid, 2)['content'], self.new)

    async def test_manual_edit_clears_score_and_stale_contexts_and_keeps_backup(self):
        row = self.chapter(1, polished='基于旧正文的优化稿')
        db.execute('INSERT INTO contexts(project_id,after_batch,content) VALUES(?,?,?)', (self.pid, 0, '{}'))
        await web.update_generated(row['id'], {'content': self.new})
        current = pipeline._gen_row(self.pid, 1)
        self.assertIsNone(current['zhuque_score'])
        self.assertEqual(current['polished'], '')
        self.assertIsNone(db.q1('SELECT * FROM contexts WHERE project_id=?', (self.pid,)))
        self.assertEqual(json.loads(db.get_asset(self.pid, 'chapter_1_manual_edit_0_before'))['content'], self.old)

    async def test_reader_can_distinguish_stale_detector_result(self):
        row = self.chapter(1)
        db.upsert_asset(self.pid, 'zhuque_1_latest', json.dumps({'text_sha256': 'old-version'}))
        result = await web.generated_one(row['id'])
        self.assertFalse(result['detection']['matches_current'])
        self.assertNotIn('plot_review', result)


    async def test_flagged_ranges_merges_adjacent_ai_segments(self):
        segments = [
            {'label': 0, 'position': [0, 100]},
            {'label': 2, 'position': [100, 200]},
            {'label': 2, 'position': [330, 150]},
            {'label': 0, 'position': [500, 100]},
        ]
        self.assertEqual(pipeline._flagged_ranges(segments, 600), [(100, 480)])
        self.assertEqual(pipeline._flagged_ranges([], 600), [])

    def test_clean_repair_text_rejects_headings_and_keeps_dialogue(self):
        self.assertEqual(pipeline._clean_repair_text('第2章 标题', 100), '')
        self.assertEqual(pipeline._clean_repair_text('“你走。”', 100), '“你走。”')
        self.assertEqual(pipeline._clean_repair_text('改写后：\n他走了。', 100), '他走了。')
        self.assertEqual(pipeline._clean_repair_text('长' * 120, 100), '')

    async def test_targeted_repair_only_rewrites_flagged_segment_and_keeps_facts(self):
        self.chapter(1)
        row = pipeline._gen_row(self.pid, 1)
        text = row['content']
        start, end = 100, 260
        replacement = '删掉方案，改成人物当场发火。' * 6
        first = {'ok': True, 'score': 50, 'diagnostics': {'segments': [
            {'label': 0, 'position': [0, start]},
            {'label': 2, 'position': [start, end - start]},
        ]}}
        second = {'ok': True, 'score': 10, 'diagnostics': {'segments': [
            {'label': 0, 'position': [0, len(text) - (end - start) + len(replacement)]},
        ]}}
        with patch.object(llm, 'chat', AsyncMock(return_value=replacement)) as chat, \
             patch.object(zhuque, 'detect', AsyncMock(return_value=second)):
            new_text, new_score, _, repaired = await pipeline._repair_flagged_segments(
                self.tid, self.pid, 1, text, 50, first, 20, 1)
        self.assertTrue(repaired)
        self.assertEqual(new_score, 10)
        self.assertEqual(new_text, text[:start] + replacement + text[end:])
        self.assertIn(text[start:end], chat.await_args.args[2])

    async def test_auto_check_repairs_above_target_and_saves_best_version(self):
        self.chapter(1)
        self.cfg['zhuque'].update(enabled=True, mode='api', auto_detect=True,
                                  repair_enabled=True, repair_target=20, repair_rounds=1)
        text = pipeline._gen_row(self.pid, 1)['content']
        replacement = '删掉方案。' * 30
        first = {'ok': True, 'score': 55, 'diagnostics': {'segments': [
            {'label': 2, 'position': [0, 200]},
            {'label': 0, 'position': [200, len(text) - 200]},
        ]}}
        second = {'ok': True, 'score': 12, 'diagnostics': {'segments': [
            {'label': 0, 'position': [0, len(text) - 200 + len(replacement)]},
        ]}}
        with patch.object(zhuque, 'detect', AsyncMock(side_effect=[first, second])) as detect, \
             patch.object(llm, 'chat', AsyncMock(return_value=replacement)):
            await pipeline._auto_zhuque_check(self.tid, self.pid, 1)
        row = pipeline._gen_row(self.pid, 1)
        self.assertEqual(row['zhuque_score'], 12)
        self.assertEqual(row['status'], 'checked')
        self.assertTrue(row['content'].startswith(replacement))
        self.assertEqual(detect.await_count, 2)
        latest = json.loads(db.get_asset(self.pid, 'zhuque_1_latest'))
        self.assertEqual(latest['score'], 12)
        self.assertTrue(latest['applied'])
        self.assertIsNotNone(db.get_asset(self.pid, f'chapter_1_auto_repair_{self.tid}_before'))

    async def test_auto_check_skips_repair_when_score_already_below_target(self):
        self.chapter(1)
        self.cfg['zhuque'].update(enabled=True, mode='api', auto_detect=True,
                                  repair_enabled=True, repair_target=20, repair_rounds=2)
        result = {'ok': True, 'score': 10, 'diagnostics': {'segments': []}}
        with patch.object(zhuque, 'detect', AsyncMock(return_value=result)) as detect, \
             patch.object(llm, 'chat', AsyncMock()) as chat:
            await pipeline._auto_zhuque_check(self.tid, self.pid, 1)
        detect.assert_awaited_once()
        chat.assert_not_awaited()
        self.assertEqual(pipeline._gen_row(self.pid, 1)['status'], 'checked')


    async def test_failed_detection_keeps_previous_score(self):
        self.chapter(1)
        db.execute("UPDATE generated SET zhuque_score=45, status='medium_aigc' WHERE project_id=? AND idx=1",
                   (self.pid,))
        self.cfg['zhuque'].update(enabled=True, mode='api', auto_detect=True)
        db.upsert_asset(self.pid, 'zhuque_1_latest', json.dumps({'score': 45, 'ok': True}))
        failure = {'ok': False, 'score': None, 'error': '检测接口 HTTP 402：朱雀积分余额不足'}
        with patch.object(zhuque, 'detect', AsyncMock(return_value=failure)):
            await pipeline._auto_zhuque_check(self.tid, self.pid, 1)
        row = pipeline._gen_row(self.pid, 1)
        self.assertEqual(row['zhuque_score'], 45)
        self.assertEqual(row['status'], 'medium_aigc')
        latest = json.loads(db.get_asset(self.pid, 'zhuque_1_latest'))
        self.assertEqual(latest['score'], 45)
        warnings = pipeline.project(self.pid)['meta'].get('warnings') or []
        self.assertTrue(any('保留上次分数' in item.get('text', '') for item in warnings))

    async def test_manual_mode_keeps_existing_score_instead_of_await_check(self):
        self.chapter(1)
        db.execute("UPDATE generated SET zhuque_score=45, status='checked' WHERE project_id=? AND idx=1",
                   (self.pid,))
        self.cfg['zhuque'].update(enabled=True, mode='manual')
        await pipeline._auto_zhuque_check(self.tid, self.pid, 1)
        row = pipeline._gen_row(self.pid, 1)
        self.assertEqual(row['zhuque_score'], 45)
        self.assertEqual(row['status'], 'checked')
        latest = json.loads(db.get_asset(self.pid, 'zhuque_1_latest'))
        self.assertEqual(latest['score'], 45)
        self.assertEqual(latest['source'], 'retained')
        self.assertEqual(latest['human_rate'], 55.0)


if __name__ == '__main__':
    unittest.main()
