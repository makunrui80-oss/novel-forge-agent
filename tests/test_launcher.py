"""启动器只复用本目录服务，空闲时加载代码更新，运行中不打断任务。"""
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('novel_launcher', Path(__file__).resolve().parents[1] / '启动.py')
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_health_does_not_confuse_other_http_services_or_folders(self):
        for data in ({'ok': True}, {'ok': True, 'app': launcher.runtime.APP_ID, 'root': '/different/project'}):
            response = io.StringIO(json.dumps(data))
            opener = Mock()
            opener.open.return_value = response
            with patch.object(launcher.urllib.request, 'build_opener', return_value=opener):
                self.assertIsNone(launcher.service_info(8765))

    def test_same_version_is_reused(self):
        with patch.object(launcher, 'find_running', return_value=8765), \
             patch.object(launcher, 'service_info', return_value={'code_version': launcher.runtime.CODE_VERSION}), \
             patch.object(launcher.subprocess, 'Popen') as popen:
            self.assertEqual(launcher.start_service(Path('python'), 8765), (None, 8765))
            popen.assert_not_called()

    def test_updated_code_does_not_interrupt_running_tasks(self):
        with patch.object(launcher, 'find_running', return_value=8765), \
             patch.object(launcher, 'service_info', return_value={'code_version': 'old', 'busy': True}), \
             patch.object(launcher, 'say'), patch.object(launcher.os, 'kill') as kill:
            self.assertEqual(launcher.start_service(Path('python'), 8765), (None, 8765))
            kill.assert_not_called()

    def test_idle_old_version_is_restarted_with_same_data_directory(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(launcher, 'LOG', Path(temp) / 'launch.log'), \
             patch.object(launcher, 'find_running', side_effect=[8765, 8765]), \
             patch.object(launcher, 'service_info', return_value={'code_version': 'old', 'busy': False, 'pid': 99999999}), \
             patch.object(launcher, 'health', return_value=False), \
             patch.object(launcher, 'say'), patch.object(launcher.os, 'kill') as kill, \
             patch.object(launcher.subprocess, 'Popen') as popen:
            _, port = launcher.start_service(Path('python'), 8765)
            self.assertEqual(port, 8765)
            kill.assert_called_once_with(99999999, launcher.signal.SIGTERM)
            self.assertEqual(popen.call_args.kwargs['cwd'], str(launcher.ROOT))
            self.assertEqual(popen.call_args.kwargs['env']['NOVEL_OPEN_BROWSER'], '0')


if __name__ == '__main__':
    unittest.main()
