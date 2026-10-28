"""真实启动本地服务并检查页面与队列；使用隔离目录，不调用任何外部模型。"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


class LocalStartupTests(unittest.TestCase):
    def test_server_serves_health_and_homepage_with_isolated_data(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            env = {**os.environ, 'NOVEL_OPEN_BROWSER': '0', 'NOVEL_DATA_DIR': str(data),
                   'NOVEL_DB_PATH': str(data / 'novel.db')}
            with (data / 'service.log').open('w', encoding='utf-8') as log:
                proc = subprocess.Popen([sys.executable, '-u', str(root / 'app.py')],
                                        cwd=str(root), env=env, stdout=log, stderr=log)
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                try:
                    ready = None
                    for _ in range(80):
                        if proc.poll() is not None:
                            break
                        for port in range(8765, 8795):
                            try:
                                with opener.open(f'http://127.0.0.1:{port}/api/health', timeout=0.2) as response:
                                    state = json.load(response)
                                if state.get('pid') == proc.pid:
                                    ready = port, state
                                    break
                            except (OSError, ValueError, urllib.error.URLError):
                                continue
                        if ready:
                            break
                        time.sleep(0.25)
                    self.assertIsNotNone(ready, (data / 'service.log').read_text(encoding='utf-8'))
                    port, state = ready
                    self.assertEqual(state['app'], 'novel-forge-agent')
                    self.assertEqual(Path(state['root']), root)
                    self.assertTrue(state['worker_active'])
                    with opener.open(f'http://127.0.0.1:{port}/', timeout=2) as response:
                        self.assertEqual(response.status, 200)
                        self.assertIn('小说', response.read().decode('utf-8'))
                    self.assertTrue((data / 'novel.db').exists())
                finally:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
