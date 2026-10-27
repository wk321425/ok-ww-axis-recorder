"""在 ok-ww 源码目录下运行: python <本文件> <ok-ww 源码目录> <.okscript 路径>"""
import contextlib
import ctypes
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
OK_WW = os.path.abspath(sys.argv[1])
OKSCRIPT = os.path.abspath(sys.argv[2])
del sys.argv[1:3]
os.chdir(OK_WW)
sys.path.insert(0, OK_WW)

from ok import og  # noqa: E402
from ok.core.plugins import import_plugin_class, prepare_local_plugin  # noqa: E402
from ok.core.script_packager import import_script  # noqa: E402
from ok.device.interaction_methods.base import BaseInteraction  # noqa: E402
from ok.feature.Box import Box  # noqa: E402
from ok.task.TaskExecutor import TaskExecutor  # noqa: E402
from ok.task.task import ExecutorOperation, FindFeature  # noqa: E402
from src.char.BaseChar import BaseChar  # noqa: E402
from src.combat.CombatCheck import CombatCheck  # noqa: E402
from src.scene.WWScene import WWScene  # noqa: E402
from src.task.AutoCombatTask import AutoCombatTask  # noqa: E402
from src.task.BaseCombatTask import BaseCombatTask  # noqa: E402
from src.task.BaseWWTask import BaseWWTask  # noqa: E402

IMPORT_BASE = tempfile.mkdtemp(prefix='axis_rec_test_')
ok, message, FOLDER = import_script(OKSCRIPT, import_base=IMPORT_BASE)
assert ok, message
MANIFEST = prepare_local_plugin(OKSCRIPT).manifest
TASK_CLASS = import_plugin_class(FOLDER, MANIFEST['tasks'][0])
TAB_CLASS = import_plugin_class(FOLDER, MANIFEST['custom_tabs'][0])
task_module = sys.modules[TASK_CLASS.__module__]
package = TASK_CLASS.__module__.rsplit('.', 1)[0]
rec = sys.modules[package + '.recorder']
runs = sys.modules[package + '.runs']
media = sys.modules[package + '.media']
feedback = sys.modules[package + '.feedback']
screenrec = sys.modules[package + '.screenrec']
# 测试进程别真去占全局热键(ok-ww 开着时 F8 已经被装好的插件占了); 注册逻辑单独测
REAL_BIND = media.HotkeyThread._bind
media.HotkeyThread._bind = lambda self, user32, wanted: setattr(self, 'current', wanted)

PATCHED = [(FindFeature, 'find_one'), (FindFeature, 'find_feature'),
           (ExecutorOperation, 'calculate_color_percentage'), (BaseCombatTask, 'get_cd'),
           (BaseCombatTask, 'get_current_con'), (BaseWWTask, 'in_team'), (BaseChar, 'perform'),
           (CombatCheck, 'in_combat'), (CombatCheck, 'reset_to_false'), (TaskExecutor, 'next_frame')]
ORIGINALS = {(owner, name): owner.__dict__.get(name) for owner, name in PATCHED}
INTERACTION_ORIGINALS = {(cls, name): cls.__dict__[name]
                         for cls in rec.Recorder._interaction_classes(BaseInteraction)
                         for name in rec.INPUT_METHODS if name in cls.__dict__}
SEND_INPUT_ORIGINAL = ctypes.windll.user32.SendInput
OK_HANDLERS = list(logging.getLogger('ok').handlers)

SCRATCH = tempfile.mkdtemp(prefix='axis_rec_frames_')
TEAM_FILE = os.path.join(SCRATCH, 'configs', 'custom_teams', 'Alpha__Beta__Gamma', 'Alpha.py')
os.makedirs(os.path.dirname(TEAM_FILE))
with open(os.path.join(os.path.dirname(TEAM_FILE), 'team.json'), 'w', encoding='utf-8') as f:
    json.dump({'name': 'test team', 'version': '9.9.9'}, f)
CALLER_SOURCE = 'def call(fn, *args, **kwargs):\n    return fn(*args, **kwargs)\n'


def caller_in(path):
    """返回一个函数, 它的栈帧文件名是 path: 用来冒充队伍代码/框架代码去调被包的函数。"""
    scope = {}
    exec(compile(CALLER_SOURCE, path, 'exec'), scope)
    return scope['call']


TEAM_CALL = caller_in(TEAM_FILE)
TEAM_CALL_OUTER = caller_in(TEAM_FILE.replace('Alpha.py', 'Beta.py'))
COMBAT_CHECK_FILE = sys.modules['src.combat.CombatCheck'].__file__
BASE_COMBAT_FILE = sys.modules['src.task.BaseCombatTask'].__file__
PLUMB_CALL = caller_in(COMBAT_CHECK_FILE)
FW_CALL = caller_in(BASE_COMBAT_FILE)


class FakeInteraction(BaseInteraction):
    """测试里定义的后端类: 装补丁时还不存在, 要靠开局时 ensure_interaction_patched 补上。"""

    def __init__(self):
        super().__init__(None)

    def send_key(self, key, down_time=0.02):
        pass

    def click(self, x=-1, y=-1, move_back=False, name=None, down_time=0.01, move=True, key='left'):
        pass

    def mouse_down(self, x=-1, y=-1, name=None, key='left'):
        pass

    def mouse_up(self, key='left'):
        pass


def wait_for(predicate, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def read_events(path):
    with open(path, encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


class PluginCase(unittest.TestCase):
    def setUp(self):
        if og.my_app is None:
            og.my_app = Mock()  # in_team 认到队伍时会写 og.my_app.logged_in
        self.out_dir = tempfile.mkdtemp(prefix='axis_rec_runs_')
        self.scene = WWScene()
        self.interaction = FakeInteraction()
        self.executor = Mock(scene=self.scene, debug=False, trigger_tasks=[], onetime_tasks=[],
                             interaction=self.interaction, paused=False, debug_mode=False)
        self.plugin = TASK_CLASS(self.executor, None)
        self.plugin.config = dict(self.plugin.default_config)
        self.plugin._enabled = True
        self.plugin._executor = self.executor
        self.plugin._try_install(quiet=False)
        self.assertIsNotNone(self.plugin.recorder)
        self.recorder = self.plugin.recorder
        self.recorder.out_dir = self.out_dir

        self.task = AutoCombatTask(self.executor, None)
        self.task.scene = self.scene
        self.task.config = dict(self.task.default_config)
        self.task.do_check_in_combat = Mock(return_value=True)
        self.task.key_config = {'Resonance Key': 'e', 'Liberation Key': 'r', 'Echo Key': 'q'}
        self.task.chars = [BaseChar(self.task, 0, char_name='Alpha'), BaseChar(self.task, 1, char_name='Beta'),
                           BaseChar(self.task, 2, char_name='Gamma')]
        for char in self.task.chars:
            char.do_perform = Mock()
        self.task.running = True  # 执行器正在跑这个任务时的样子
        self.task._enabled = True

    def tearDown(self):
        self.plugin.on_destroy()
        shutil.rmtree(self.out_dir, ignore_errors=True)

    def start(self):
        self.task._in_combat = True
        self.assertTrue(self.task.in_combat())
        self.assertIsNotNone(self.recorder.run)
        return self.recorder.run

    def finish(self, reason='test end'):
        run = self.recorder.run
        self.task.reset_to_false(reason)
        self.assertIsNone(self.recorder.run)
        self.assertTrue(wait_for(lambda: run.path and any(e['e'] == 'end' for e in read_events(run.path))))
        return read_events(run.path)

    def hud(self, current):
        """in_team 里认 1/2/3 号位文字: 当前角色那格认不到。走真的 find_one(被包着), 只换掉底下的 find_feature。"""
        self.task.find_feature = Mock(side_effect=lambda feature_name=None, **kw: []
                                      if feature_name == f'char_{current + 1}_text' else [Box(0, 0, 5, 5, 0.9)])


class TestInstall(PluginCase):
    def test_uninstall_restores_everything(self):
        self.plugin.on_destroy()
        for key, original in ORIGINALS.items():
            self.assertIs(key[0].__dict__.get(key[1]), original, key)
        for (cls, name), original in INTERACTION_ORIGINALS.items():
            self.assertIs(cls.__dict__[name], original, (cls, name))
        self.assertIs(ctypes.windll.user32.SendInput, SEND_INPUT_ORIGINAL)
        self.assertEqual(logging.getLogger('ok').handlers, OK_HANDLERS)
        self.assertNotIn(rec.MARK_ATTR, ExecutorOperation.__dict__)
        self.assertIsNone(ExecutorOperation.__dict__.get(rec.HOLDER_ATTR))
        names = [thread.name for thread in threading.enumerate()]
        self.assertNotIn('axis_recorder_hotkey', names)
        self.assertNotIn('axis_recorder_media', names)

    def test_reinstall_does_not_stack(self):
        second = rec.Recorder(out_dir=self.out_dir)
        second.owner = self.plugin
        second.install()
        try:
            self.assertIs(getattr(FindFeature.find_one, rec.ORIGINAL_ATTR), ORIGINALS[(FindFeature, 'find_one')])
            self.assertIsNone(self.recorder._writer)
            self.assertEqual(sum(isinstance(h, rec._LogTap) for h in logging.getLogger('ok').handlers), 1)
        finally:
            second.uninstall()
            self.plugin.recorder = None

    def test_backend_defined_later_patched_at_run_start(self):
        class LateInteraction(FakeInteraction):
            def send_key(self, key, down_time=0.02):
                pass

        self.executor.interaction = LateInteraction()
        self.assertFalse(hasattr(LateInteraction.__dict__['send_key'], rec.ORIGINAL_ATTR))
        self.start()
        self.assertTrue(hasattr(LateInteraction.__dict__['send_key'], rec.ORIGINAL_ATTR))
        TEAM_CALL(self.task.send_key, 'q')
        events = self.finish()
        self.assertEqual([(e['a'], e['k']) for e in events if e['e'] == 'in'], [('key', 'q')])
        self.plugin.on_destroy()
        self.assertFalse(hasattr(LateInteraction.__dict__['send_key'], rec.ORIGINAL_ATTR))


class TestLifecycle(PluginCase):
    def test_run_records_inputs_field_perform_and_logs(self):
        logging.getLogger('ok').setLevel(logging.DEBUG)
        run = self.start()
        self.hud(0)
        TEAM_CALL(self.task.in_team)
        TEAM_CALL(self.task.send_key, 'e')
        TEAM_CALL(self.task.click, 100, 100)
        self.task.chars[0].perform()
        self.hud(1)
        TEAM_CALL(self.task.send_key, 2)
        TEAM_CALL(self.task.in_team)
        TEAM_CALL(self.task.send_key, 'r')
        rec.logger.info('hello from the axis')
        events = self.finish('target enemy failed')

        header, end = events[0], events[-1]
        self.assertEqual(header['e'], 'start')
        self.assertEqual([c['n'] for c in header['team']], ['Alpha', 'Beta', 'Gamma'])
        self.assertEqual(header['keys']['Liberation Key'], 'r')
        self.assertIn('local', header)
        self.assertNotIn('inputs', end)  # 按键次数只有手打的局写
        self.assertEqual(end['reason'], 'target enemy failed')
        inputs = [e for e in events if e['e'] == 'in']
        self.assertEqual([(e['a'], e['k']) for e in inputs], [('key', 'e'), ('click', 'left'), ('key', '2'),
                                                              ('key', 'r')])
        self.assertTrue(all(e['src'].startswith('Alpha.py:') for e in inputs))
        field = [(e['team'], e['i']) for e in events if e['e'] == 'field']
        self.assertEqual(field, [(True, 0), (True, 1)])
        perf = [(e['c'], e['p']) for e in events if e['e'] == 'perf']
        self.assertEqual(perf, [('Alpha', 'in'), ('Alpha', 'out')])
        self.assertTrue(any(e['e'] == 'log' and 'hello from the axis' in e['m'] for e in events))
        # in_team 里那三次认 HUD 的 find_one 不算轴的读屏
        self.assertFalse(any(e['e'] == 'read' for e in events))
        self.assertIsNotNone(run.path)

    def test_ends_when_in_combat_turns_false(self):
        self.start()
        self.task._in_combat = False
        self.task.out_of_combat_reason = 'sleep check'
        self.task.in_combat()
        self.assertIsNone(self.recorder.run)

    def test_other_tasks_not_recorded_by_default(self):
        other = BaseCombatTask(self.executor, None)
        other._enabled = True  # 执行器只跑开着的任务
        other.scene = self.scene
        other.do_check_in_combat = Mock(return_value=True)
        other._in_combat = True
        other.in_combat()
        self.assertIsNone(self.recorder.run)
        self.plugin.config[TASK_CLASS.ALL_TASKS_KEY] = True
        other.in_combat()
        self.assertIsNotNone(self.recorder.run)
        other.reset_to_false('done')

    def test_disabled_plugin_records_nothing(self):
        self.plugin._enabled = False
        self.task._in_combat = True
        self.task.in_combat()
        self.assertIsNone(self.recorder.run)

    def test_disabling_mid_run_ends_it(self):
        self.start()
        self.plugin._enabled = False
        self.task.in_combat()
        self.assertIsNone(self.recorder.run)

    def end_reason(self, run):
        self.assertTrue(wait_for(lambda: run.path and any(e['e'] == 'end' for e in read_events(run.path))))
        return read_events(run.path)[-1]['reason']

    def test_task_stopped_by_error_ends_run(self):
        # 战斗任务抛异常: 执行器 running=False 再 disable(), 不走 reset_to_false, 也不会再调 in_combat
        run = self.start()
        with patch.object(rec, 'TASK_STOPPED_END', 0.3):
            time.sleep(1.0)
            self.assertIs(self.recorder.run, run)  # 还在跑(一次 perform 很长)就不动它
            self.task.running = False
            self.task._enabled = False
            self.assertTrue(wait_for(lambda: self.recorder.run is None))
        self.assertIn('被停用', self.end_reason(run))
        self.task._enabled = True
        self.task.running = True
        self.start()  # 下一局照常开
        self.assertIsNot(self.recorder.run, run)
        self.finish()

    def test_ok_ww_stopped_ends_run(self):
        # ok-ww 的开始/停止只让执行器暂停: 自动战斗开着、任务也算在跑, 轴停在 sleep 里
        run = self.start()
        self.executor.debug_mode = True
        self.executor.paused = True
        time.sleep(rec.TASK_PAUSED_END + 0.8)
        self.assertIs(self.recorder.run, run)  # 调试模式下暂停不拦执行器
        self.executor.debug_mode = False
        self.assertTrue(wait_for(lambda: self.recorder.run is None, timeout=rec.TASK_PAUSED_END + 1.5))
        self.assertEqual(self.end_reason(run), rec.TASK_PAUSED_REASON)
        self.executor.paused = False
        self.assertIsNot(self.start(), run)  # 接着开始, 下次判战斗另开一局
        self.finish()

    def test_task_gone_without_reset_ends_run(self):
        run = self.start()
        with patch.object(rec, 'TASK_STOPPED_END', 0.3):
            self.task.running = False
            self.assertTrue(wait_for(lambda: self.recorder.run is None))
        self.assertIn('不跑了', self.end_reason(run))

    def test_auto_combat_turned_off_while_axis_keeps_running(self):
        # 队伍代码 try: next_frame() except Exception 吞了停止信号: 自动战斗关了, 轴照跑、任务一直算在跑
        run = self.start()
        self.task._enabled = False
        self.assertTrue(wait_for(lambda: self.recorder.run is None, timeout=rec.TASK_DISABLED_END + 1.5))
        self.assertEqual(self.end_reason(run), rec.TASK_DISABLED_REASON)
        self.task.in_combat()  # 轴还在判战斗也不开新局
        self.assertIsNone(self.recorder.run)
        self.task._enabled = True
        run = self.start()
        self.task._enabled = False
        self.task.in_combat()  # 关掉后轴一判战斗就收
        self.assertIsNone(self.recorder.run)
        self.assertEqual(self.end_reason(run), rec.TASK_DISABLED_REASON)

    def test_dead_writer_restarted_at_next_run(self):
        writer = self.recorder._writer
        self.recorder.queue.put(rec._STOP)
        writer.join(3)
        self.assertFalse(writer.is_alive())
        run = self.start()
        self.assertTrue(self.recorder._writer.is_alive())
        events = self.finish()
        self.assertEqual((events[0]['e'], events[-1]['e']), ('start', 'end'))
        self.assertIsNotNone(run.path)

    def test_axis_mark(self):
        self.start()
        TEAM_CALL(self.task.axis_mark, '光圈', True)
        events = self.finish()
        marks = [e for e in events if e['e'] == 'mark']
        self.assertEqual(len(marks), 1)
        self.assertEqual((marks[0]['name'], marks[0]['v']), ('光圈', True))
        self.assertTrue(marks[0]['src'].startswith('Alpha.py:'))

    def test_header_reads_team_dir_from_custom_char_file(self):
        module = type(sys)('fake_custom_char')
        module.__file__ = TEAM_FILE
        sys.modules['fake_custom_char'] = module
        custom = type('Alpha', (BaseChar,), {'__module__': 'fake_custom_char'})
        self.task.chars[0] = custom(self.task, 0, char_name='Alpha')
        try:
            self.start()
            header = self.finish()[0]
        finally:
            del sys.modules['fake_custom_char']
        self.assertEqual(header['team_dir'], 'Alpha__Beta__Gamma')
        self.assertEqual(header['team_ver'], '9.9.9')
        self.assertTrue(header['team'][0]['custom'])
        self.assertFalse(header['team'][1]['custom'])

    def test_prune_keeps_newest(self):
        self.plugin.config[TASK_CLASS.KEEP_KEY] = 5
        for i in range(8):
            with open(os.path.join(self.out_dir, f'20260101_00000{i}_old.jsonl'), 'w') as f:
                f.write('{}\n')
        self.start()
        run = self.recorder.run
        self.finish()
        names = sorted(os.listdir(self.out_dir))
        self.assertEqual(len(names), 5)
        self.assertIn(os.path.basename(run.path), names)


class TestReads(PluginCase):
    def wrapped(self, kind, extract, value):
        return rec.Recorder._read_wrapper(self.recorder, kind, extract)(
            lambda task, *a, **k: value() if callable(value) else value)

    def test_reads_from_team_code_grouped(self):
        find = self.wrapped('find', rec._extract_find_one, lambda: None)
        self.start()
        for _ in range(10):
            TEAM_CALL(find, self.task, 'hsin_lib2', threshold=0.8)
        con_values = iter([0.5, 0.51, 0.515, 0.9])
        con = self.wrapped('con', rec._extract_con, lambda: next(con_values))
        for _ in range(4):
            TEAM_CALL_OUTER(TEAM_CALL, con, self.task)
        events = self.finish()
        reads = [e for e in events if e['e'] == 'read']
        finds = [e for e in reads if e['k'] == 'find']
        self.assertEqual(len(finds), 1)
        self.assertEqual((finds[0]['n'], finds[0]['ok'], finds[0]['th']), (10, False, 0.8))
        cons = sorted((e for e in reads if e['k'] == 'con'), key=lambda e: e['t'])
        self.assertEqual([(c.get('n', 1), c['v']) for c in cons], [(3, 0.5), (1, 0.9)])
        self.assertEqual((cons[0]['lo'], cons[0]['hi']), (0.5, 0.515))
        self.assertTrue(cons[0]['src'].startswith('Alpha.py:'))
        self.assertTrue(cons[0]['src2'].startswith('Beta.py:'))

    def test_numpy_reads_do_not_kill_writer(self):
        # find_feature(limit=0) 给的置信度是 numpy.float32: 以前合并好的读数定时冲刷时写不出, 写盘线程直接退出,
        # 这局没有 end, 之后每局都只进队列不落盘
        values = iter([np.float32(0.5), np.float32(0.51), np.float64(0.9)])
        con = self.wrapped('con', rec._extract_con, lambda: next(values))
        feat = self.wrapped('feat', rec._extract_find_feature, [Box(1, 2, 3, 4, np.float32(0.87), 'icon')])
        self.start()
        for _ in range(3):
            TEAM_CALL(con, self.task)
        TEAM_CALL(feat, self.task, 'icon')
        TEAM_CALL(self.task.axis_mark, '数组', np.array([1, 2]))
        time.sleep(rec.GROUP_IDLE_FLUSH + 0.8)  # 等写盘线程定时冲刷
        self.assertTrue(self.recorder._writer.is_alive())
        events = self.finish()
        reads = sorted((e for e in events if e['e'] == 'read'), key=lambda e: e['t'])
        self.assertEqual([(e['k'], e.get('n', 1), e['v']) for e in reads],
                         [('con', 2, 0.5), ('con', 1, 0.9), ('feat', 1, 0.87)])
        self.assertEqual((reads[0]['lo'], reads[0]['hi']), (0.5, 0.51))
        self.start()
        self.assertEqual(self.finish()[-1]['e'], 'end')

    def test_plumbing_reads_skipped_framework_helpers_kept(self):
        color = self.wrapped('color', rec._extract_color, 0.3)
        self.start()
        TEAM_CALL(PLUMB_CALL, color, self.task, (1, 2, 3), 'box_a')
        TEAM_CALL(FW_CALL, color, self.task, (1, 2, 3), 'box_b')
        # 纯框架线程里的读屏(栈上没有角色代码)不记
        worker = threading.Thread(target=FW_CALL, args=(color, self.task, (1, 2, 3), 'box_c'))
        worker.start()
        worker.join()
        events = self.finish()
        reads = [e for e in events if e['e'] == 'read']
        self.assertEqual([e['name'] for e in reads], ['box_b'])
        self.assertTrue(reads[0]['via'].startswith('BaseCombatTask.py:'))

    def test_group_splits_after_gap(self):
        cd = self.wrapped('cd', rec._extract_cd, 3.0)
        self.start()
        with patch.object(rec, 'GROUP_GAP', -1.0):
            TEAM_CALL(cd, self.task, 'e')
            TEAM_CALL(cd, self.task, 'e')
            # 合并在写盘线程里判, 补丁要撑到它处理完
            wait_for(self.recorder.queue.empty)
            time.sleep(0.2)
        events = self.finish()
        self.assertEqual(len([e for e in events if e['e'] == 'read']), 2)

    def test_default_search_box_from_feature(self):
        feature = Mock(x=100, y=50, width=40, height=20, scaling=1)
        self.executor.feature_set = Mock(feature_dict={'icon': feature}, width=1920, height=1080,
                                         default_horizontal_variance=0.01, default_vertical_variance=0)
        find = self.wrapped('find', rec._extract_find_one, None)
        self.start()
        TEAM_CALL(find, self.task, 'icon')
        TEAM_CALL(find, self.task, 'other')
        events = self.finish()
        boxes = {e['name']: e.get('box') for e in events if e['e'] == 'read'}
        # 横向偏差 1920*0.01=19.2: 81~159
        self.assertEqual(boxes, {'icon': [81, 50, 78, 20], 'other': None})

    def test_reads_off_records_nothing(self):
        self.plugin.config[TASK_CLASS.READS_KEY] = False
        find = self.wrapped('find', rec._extract_find_one, None)
        self.start()
        TEAM_CALL(find, self.task, 'x')
        events = self.finish()
        self.assertFalse(any(e['e'] == 'read' for e in events))


class TestMedia(PluginCase):
    @staticmethod
    def screen(value, height=1440, width=2560):
        # 跟 WGC 一样: 截的是 BGRA 整块, 给出去的是前三个通道的切片
        return np.full((height, width, 4), value, np.uint8)[:, :, :3]

    def feed(self, frames):
        pending = list(frames)
        next_frame = self.recorder._next_frame_wrapper(lambda executor: pending.pop(0))
        for _ in range(len(frames)):
            next_frame(self.executor)

    def test_whole_run_frames(self):
        with patch.object(media, 'FRAME_INTERVAL', 0.0):
            run = self.start()
            self.feed([self.screen(v) for v in (10, 20, 30)])
            self.finish()
        self.assertTrue(wait_for(self.recorder.media.idle))
        index = media.MediaIndex(run.path)
        self.assertEqual(len(index.frames), 3)
        image = cv2.imdecode(np.frombuffer(index.read(index.frames[1]), np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(image.shape, (540, 960, 3))
        self.assertAlmostEqual(float(image.mean()), 20, delta=2)
        self.assertEqual((index.frame_index(0.0), index.frame_index(99)), (0, 2))

    def test_sampling_interval(self):
        run = self.start()
        self.feed([self.screen(1, 108, 192) for _ in range(5)])
        self.finish()
        self.assertTrue(wait_for(self.recorder.media.idle))
        self.assertEqual(len(media.MediaIndex(run.path).frames), 1)

    def test_ring_capped_by_memory(self):
        with patch.object(media, 'FRAME_INTERVAL', 0.0), patch.object(media, 'RING_MEMORY_CAP', 3 * 108 * 192 * 4), \
                patch.object(media, 'RING_GUARANTEE_SECONDS', 0.1):
            self.start()
            self.feed([self.screen(v, 108, 192) for v in range(6)])
            self.assertEqual([int(f[0, 0, 0]) for _, f in self.recorder.sink.recent()], [3, 4, 5])
            self.finish()

    def test_ring_frames_by_resolution(self):
        # 每秒 4 帧, 320MB 上限, 保证 4 秒
        sizes = {'1080p': 1920 * 1080 * 4, '2K': 2560 * 1440 * 4, '4K': 3840 * 2160 * 4}
        self.assertEqual({k: media.ring_frames(16, n) for k, n in sizes.items()}, {'1080p': 16, '2K': 16, '4K': 16})
        self.assertEqual({k: media.ring_frames(40, n) for k, n in sizes.items()}, {'1080p': 40, '2K': 22, '4K': 16})
        self.assertEqual(media.ring_frames(8, sizes['4K']), 8)

    def test_export_notes_memory_limit(self):
        self.plugin.config[TASK_CLASS.FRAMES_KEY] = False
        with patch.object(media, 'FRAME_INTERVAL', 0.0), patch.object(media, 'RING_MEMORY_CAP', 1), \
                patch.object(media, 'RING_GUARANTEE_SECONDS', 0.1):
            self.start()
            self.feed([self.screen(v, 108, 192) for v in range(4)])
            self.assertEqual([int(f[0, 0, 0]) for _, f in self.recorder.sink.recent()], [2, 3])
            with patch.object(self.recorder, '_notify') as notify:
                thread = self.recorder.export_recent()
                thread.join(10)
            self.finish()
        with open(os.path.join(self.recorder.last_export, 'info.json'), encoding='utf-8') as f:
            info = json.load(f)
        self.assertEqual((info['export_seconds'], info['ring_seconds']), (4.0, 0.1))
        self.assertIn('192x108', info['note'])
        self.assertIn('只留得下最近 0.1 秒', notify.call_args[0][0])

    def test_reused_buffer_is_copied(self):
        buffer = np.zeros((108, 192, 4), np.uint8)

        def grab(executor):
            buffer[:] += 1
            return buffer[:, :, :3]

        with patch.object(media, 'FRAME_INTERVAL', 0.0):
            self.start()
            next_frame = self.recorder._next_frame_wrapper(grab)
            for _ in range(3):
                next_frame(self.executor)
            values = [int(f[0, 0, 0]) for _, f in self.recorder.sink.recent()]
            self.finish()
        # 第一帧还看不出是同一块缓冲区, 从第二帧起都拷了
        self.assertEqual(values[1:], [2, 3])

    def test_export_without_whole_run_frames(self):
        self.plugin.config[TASK_CLASS.FRAMES_KEY] = False
        with patch.object(media, 'FRAME_INTERVAL', 0.0):
            run = self.start()
            self.feed([self.screen(v, 1080, 1920) for v in (40, 80)])
            thread = self.recorder.export_recent()
            self.assertIsNotNone(thread)
            thread.join(10)
            self.finish()
        folder = self.recorder.last_export
        with open(os.path.join(folder, 'info.json'), encoding='utf-8') as f:
            info = json.load(f)
        self.assertEqual(len(info['frames']), 2)
        self.assertEqual(info['size'], [1920, 1080])
        self.assertEqual(info['export_seconds'], 4.0)
        self.assertNotIn('note', info)
        png = cv2.imdecode(np.fromfile(os.path.join(folder, info['frames'][1]['file']), np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual((png.shape, int(png[0, 0, 0])), ((1080, 1920, 3), 80))
        # 打完一会儿内还能导
        thread = self.recorder.export_recent()
        self.assertIsNotNone(thread)
        thread.join(10)
        self.assertTrue(wait_for(self.recorder.media.idle))
        self.assertFalse(os.path.exists(media.media_paths(run.path)[0]))

    def test_crops_when_reading_changes(self):
        self.executor._frame = self.screen(50, 1080, 1920)
        results = [None, None, Box(100, 100, 20, 20, 0.9), Box(100, 100, 20, 20, 0.91), None,
                   Box(500, 500, 30, 30, 0.95)]
        find = rec.Recorder._read_wrapper(self.recorder, 'find', rec._extract_find_one)(
            lambda task, *a, **k: results.pop(0))
        with patch.object(media, 'CROP_MIN_GAP', 0.0):
            run = self.start()
            for _ in range(5):
                TEAM_CALL(find, self.task, 'hsin_lib2', box=Box(90, 90, 60, 40))
                time.sleep(0.002)  # 实机读屏之间至少隔几毫秒; 记录里时间只留到 0.1ms
            # 整屏找图: 搜索框太大, 退而截找到的那块
            TEAM_CALL(find, self.task, 'whole', box=Box(0, 0, 1920, 1080))
            TEAM_CALL(self.task.axis_mark, '光圈', True, Box(10, 10, 30, 30))
            events = self.finish()
        self.assertTrue(wait_for(self.recorder.media.idle))
        index = media.MediaIndex(run.path)
        crops = sorted((e for entries in index.crops.values() for e in entries), key=lambda e: e['t'])
        finds = [e for e in crops if e['name'] == 'hsin_lib2']
        self.assertEqual([(e['name'], e.get('ok')) for e in finds],
                         [('hsin_lib2', False), ('hsin_lib2', True), ('hsin_lib2', False)])
        self.assertEqual(finds[0]['box'], [90, 90, 60, 40])
        self.assertEqual([e['box'] for e in crops if e['name'] == 'whole'], [[492, 492, 46, 46]])
        image = cv2.imdecode(np.frombuffer(index.read(finds[0]), np.uint8), cv2.IMREAD_UNCHANGED)
        self.assertEqual(image.shape[:2], (40, 60))
        reads = sorted((e for e in events if e['e'] == 'read' and e['name'] == 'hsin_lib2'), key=lambda e: e['t'])
        entry, exact = index.crop_for(reads[1])
        self.assertTrue(exact)
        self.assertTrue(entry['ok'])
        mark = next(e for e in events if e['e'] == 'mark')
        entry, exact = index.crop_for(mark)
        self.assertEqual((entry['k'], entry['box'], exact), ('mark', [10, 10, 30, 30], True))

    def test_prune_media_keeps_newest_runs(self):
        self.plugin.config[TASK_CLASS.FRAMES_KEEP_KEY] = 2
        for i in range(4):
            stem = os.path.join(self.out_dir, f'20260101_00000{i}_old')
            for suffix in ('.jsonl', '.media', '.media.idx'):
                with open(stem + suffix, 'w') as f:
                    f.write('{}\n')
        with open(os.path.join(self.out_dir, '20250101_000000_gone.media'), 'w') as f:
            f.write('x')
        self.start()
        self.finish()
        self.assertEqual(sorted(n for n in os.listdir(self.out_dir) if '.media' in n),
                         ['20260101_000003_old.media', '20260101_000003_old.media.idx'])

    def test_hotkey_bind(self):
        thread = media.HotkeyThread(lambda: 'F8', lambda: None)
        user32 = Mock()
        user32.RegisterHotKey.return_value = 1
        REAL_BIND(thread, user32, 'F8')
        user32.RegisterHotKey.assert_called_once_with(None, media.HOTKEY_ID, media.MOD_NOREPEAT, 0x77)
        self.assertTrue(thread.bound)
        REAL_BIND(thread, user32, media.NO_HOTKEY)
        user32.UnregisterHotKey.assert_called_once_with(None, media.HOTKEY_ID)
        self.assertEqual((thread.bound, thread.current), (False, media.NO_HOTKEY))


class TestManual(PluginCase):
    """人为操作模式: tick 自己认 HUD/怪开局收尾, 按键从 InputLog 进来(采集线程另测)。"""

    def setUp(self):
        super().setUp()
        self.plugin.config[TASK_CLASS.MODE_KEY] = task_module.MODE_MANUAL
        self.plugin.config[TASK_CLASS.MANUAL_AUTO_KEY] = True  # 下面大多是测自动认; 只认热键的另测
        self.plugin.notification = Mock()
        self.plugin.info_set = Mock()
        tones = patch.object(sys.modules[package + '.manual'], 'play_tones')
        self.tones = tones.start()
        self.addCleanup(tones.stop)
        self.executor.trigger_tasks = [self.task]
        self.executor._last_frame_time = time.time() + 1e6
        self.task._enabled = False
        self.task.load_chars = Mock()
        self.driver = self.recorder.manual
        self.driver._ensure_capture = lambda: None
        self.driver._ensure_hotkey = lambda owner: None  # 不真注册 F7(ok-ww 开着时会注册不上)
        self.enemy = False
        self.driver._enemy_visible = lambda combat: self.enemy
        self.log = self.driver.log
        self.hud(0)

    def tick(self):
        self.plugin.run()

    def end_and_read(self, run):
        self.assertTrue(wait_for(lambda: run.path and any(e['e'] == 'end' for e in read_events(run.path))))
        return read_events(run.path)

    def test_auto_start_keys_and_end_when_no_enemy(self):
        self.tick()
        self.assertIsNone(self.recorder.run)
        self.enemy = True
        self.tick()
        run = self.recorder.run
        self.assertTrue(run is not None and run.manual and run.by == '看到怪')
        self.assertEqual(self.plugin.trigger_interval, 0.25)
        t = time.perf_counter()
        self.log.set_in_game(t, True)
        self.log.key(t, 'key', True, 'e')
        self.log.key(t, 'key', True, 'e')  # 自动重复
        self.log.key(t + 0.05, 'key', False, 'e')
        self.log.key(t + 0.1, 'mouse', True, 'left')
        self.log.wheel(t + 0.2, -1.0)
        self.hud(1)
        self.tick()
        self.enemy = False
        self.driver.last_enemy = time.perf_counter() - 10
        self.log.last_action = time.perf_counter() - 10  # 上面那几下出招也过去 10 秒了
        self.tick()
        self.assertIsNone(self.recorder.run)
        self.assertTrue(wait_for(lambda: run.path and any(e['e'] == 'end' for e in read_events(run.path))))
        events = read_events(run.path)

        header, end = events[0], events[-1]
        self.assertEqual((header['task'], header['mode'], header['by']), ('手打记录', 'manual', '看到怪'))
        self.assertEqual([c['n'] for c in header['team']], ['Alpha', 'Beta', 'Gamma'])
        self.assertFalse(header['reads'])
        self.assertIn('手打_', os.path.basename(run.path))
        self.assertEqual(end['reason'], '5 秒没看到怪也没出招')
        self.assertEqual((end['inputs'], end['pauses'], end['paused']), (3, 0, 0.0))
        self.assertRegex(header['local'], r'^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}$')
        self.assertNotIn('note', header)
        inputs = [(e['a'], e['k'], e.get('cut'), e.get('v')) for e in events if e['e'] == 'in']
        self.assertEqual(inputs, [('kd', 'e', None, None), ('ku', 'e', None, None), ('md', 'left', None, None),
                                  ('wheel', 'wheel', None, -1.0), ('mu', 'left', 1, None)])
        field = [(e['team'], e['i']) for e in events if e['e'] == 'field']
        self.assertEqual(field, [(True, 0), (True, 1)])
        self.task.load_chars.assert_called_once()
        summary = runs.summarize_run(run.path)
        self.assertTrue(summary.manual)
        data = runs.RunData(run.path)
        self.assertIn('手打记录(看到怪开的局)', runs.detail_lines(data)[0])

    def test_keys_before_start_are_kept(self):
        t = time.perf_counter()
        self.log.set_in_game(t - 5, True)
        self.log.key(t - 4.0, 'key', True, 'r')  # 太早, 不算
        self.log.key(t - 3.9, 'key', False, 'r')
        self.log.key(t - 1.0, 'key', True, 'q')
        self.log.key(t - 0.9, 'key', False, 'q')
        self.log.key(t - 0.5, 'key', True, 'space')
        self.enemy = True
        self.tick()
        run = self.recorder.run
        self.assertLess(run.t0, t - 0.95)
        self.log.key(time.perf_counter(), 'key', False, 'space')
        self.driver.toggle_requested.set()
        self.tick()
        events = self.end_and_read(run)
        inputs = [(e['a'], e['k']) for e in events if e['e'] == 'in']
        self.assertEqual(inputs, [('kd', 'q'), ('ku', 'q'), ('kd', 'space'), ('ku', 'space')])
        self.assertEqual(events[-1]['inputs'], 2)
        self.assertTrue(all(e['t'] >= 0 for e in events if 't' in e))
        self.assertAlmostEqual(events[0]['wall'], time.time() - (time.perf_counter() - run.t0), delta=0.5)

    def test_hotkey_run_ignores_no_enemy_and_blocks_restart(self):
        self.driver.toggle_requested.set()
        self.tick()
        run = self.recorder.run
        self.assertEqual(run.by, '热键')
        self.driver.last_enemy = time.perf_counter() - 60
        self.tick()
        self.assertIs(self.recorder.run, run)
        self.enemy = True
        self.driver.toggle_requested.set()
        self.tick()
        self.assertIsNone(self.recorder.run)
        self.assertEqual(self.end_and_read(run)[-1]['reason'], '按热键停止')
        self.tick()
        self.assertIsNone(self.recorder.run)  # 怪还在, 刚按热键停的不马上又开
        self.enemy = False
        self.driver.last_enemy = time.perf_counter() - 2
        self.tick()
        self.enemy = True
        self.tick()
        self.assertIsNotNone(self.recorder.run)

    def test_default_hotkey_only_never_ends_by_itself(self):
        manual = sys.modules[package + '.manual']
        self.plugin.config[TASK_CLASS.MANUAL_AUTO_KEY] = False
        self.enemy = True
        self.tick()
        self.assertIsNone(self.recorder.run)  # 只认热键: 看到怪也不开
        self.plugin.info_set.assert_called_with('人为操作', '等按 F7 开始')
        self.driver.toggle_requested.set()
        self.tick()
        run = self.recorder.run
        self.assertEqual(run.by, '热键')
        self.assertEqual(self.plugin.notification.call_args[0][0], '跑轴记录: 开始记手打, 再按一下 F7 结束')
        self.tones.assert_called_once_with(manual.TONES_START)
        # 没怪、没出招、HUD 不在、超过 30 分钟、执行器停了, 都不收
        self.enemy = False
        now = time.perf_counter()
        self.driver.last_enemy = now - 600
        self.log.last_action = now - 600
        self.task.find_feature = Mock(return_value=[])
        self.driver.last_hud = now - 600
        run.t0 = now - 31 * 60
        self.tick()
        self.assertIs(self.recorder.run, run)
        self.driver.last_tick = time.perf_counter() - 10
        self.assertTrue(self.driver._on_capture_idle(time.perf_counter()))
        self.assertIs(self.recorder.run, run)
        # 执行器停着时按热键, 在热键线程里收
        self.driver._on_hotkey(self.plugin)
        self.assertIsNone(self.recorder.run)
        self.assertFalse(self.driver.toggle_requested.is_set())
        self.assertEqual(self.end_and_read(run)[-1]['reason'], '按热键停止')
        self.assertRegex(self.plugin.notification.call_args[0][0], r'^跑轴记录: 手打这局记完了\(按热键停止\), 记了 31 分 \d+ 秒$')
        self.tones.assert_called_with(manual.TONES_END)

    def test_hotkey_while_ok_ww_stopped_does_not_start_later(self):
        self.plugin.config[TASK_CLASS.MANUAL_AUTO_KEY] = False
        self.driver.last_tick = time.perf_counter() - 10
        self.driver._on_hotkey(self.plugin)
        self.assertIn('ok-ww 没在运行', self.plugin.notification.call_args[0][0])
        self.tick()  # 执行器回来了, 刚才那下不算
        self.assertIsNone(self.recorder.run)
        self.driver._on_hotkey(self.plugin)  # 在跑时交给 tick
        self.assertTrue(self.driver.toggle_requested.is_set())
        self.tick()
        self.assertEqual(self.recorder.run.by, '热键')

    def test_hotkey_run_forgotten_ends_after_two_hours(self):
        self.plugin.config[TASK_CLASS.MANUAL_AUTO_KEY] = False
        self.plugin.config[TASK_CLASS.MANUAL_SOUND_KEY] = False
        self.driver.toggle_requested.set()
        self.tick()
        run = self.recorder.run
        self.tones.assert_not_called()
        run.t0 = time.perf_counter() - 2 * 3600 - 1
        self.tick()
        self.assertIsNone(self.recorder.run)
        self.assertEqual(self.end_and_read(run)[-1]['reason'], '超过 2 小时没按热键结束')

    def test_no_hotkey_falls_back_to_auto(self):
        self.plugin.config[TASK_CLASS.MANUAL_AUTO_KEY] = False
        self.plugin.config[TASK_CLASS.MANUAL_HOTKEY_KEY] = media.NO_HOTKEY
        self.enemy = True
        self.tick()
        self.assertEqual(self.recorder.run.by, '看到怪')
        self.assertEqual(self.plugin.notification.call_args[0][0], '跑轴记录: 看到怪了, 开始记手打')
        self.recorder.end_run('test end')
        self.plugin.config[TASK_CLASS.MANUAL_HOTKEY_KEY] = 'F7'
        self.driver.hotkey = SimpleNamespace(current='F7', bound=False)  # 注册不上
        self.driver.suppress = False
        self.tick()
        self.assertEqual(self.recorder.run.by, '看到怪')
        self.assertIn('(按 F7 结束)', self.plugin.notification.call_args_list[-1][0][0])
        self.driver.hotkey = None

    def test_hud_gone_ends_auto_run(self):
        self.enemy = True
        self.tick()
        run = self.recorder.run
        self.task.find_feature = Mock(return_value=[])  # 三格都认不到 = HUD 不在
        self.driver.last_hud = time.perf_counter() - 20
        self.tick()
        self.assertIsNone(self.recorder.run)
        self.assertTrue(wait_for(lambda: run.path and any(e['e'] == 'end' for e in read_events(run.path))))
        self.assertEqual(read_events(run.path)[-1]['reason'], 'HUD 15 秒没出现(打完了、结算或过场)')

    def test_combat_keys_keep_auto_run_open(self):
        self.enemy = True
        self.tick()
        run = self.recorder.run
        self.enemy = False
        t = time.perf_counter()
        self.log.set_in_game(t, True)
        self.driver.last_enemy = t - 10
        self.log.key(t, 'mouse', True, 'left')  # 还在打: Boss 战认不出怪也不收
        self.tick()
        self.assertIs(self.recorder.run, run)
        self.log.last_action = t - 10
        self.log.key(t, 'key', True, 'w')  # 走路、热键不算出招
        self.log.key(t, 'key', True, 'f8')
        self.tick()
        self.assertIsNone(self.recorder.run)
        self.assertEqual(self.end_and_read(run)[-1]['reason'], '5 秒没看到怪也没出招')
        rawinput = sys.modules[package + '.rawinput']
        self.assertTrue(rawinput.is_action('key', 'f'))  # F 处决/拾取算
        self.assertTrue(rawinput.is_action('mouse', 'right'))
        self.assertFalse(rawinput.is_action('key', 'lshift'))

    def test_auto_combat_enabled_warns_and_records_nothing(self):
        self.task._enabled = True
        self.enemy = True
        self.tick()
        self.tick()
        self.assertIsNone(self.recorder.run)
        self.plugin.notification.assert_called_once()
        self.assertIn('关掉「自动战斗」', self.plugin.notification.call_args[0][0])
        self.plugin.info_set.assert_called_with('人为操作', '自动战斗开着, 手打不记')

    def test_manual_mode_skips_code_runs_and_takeover_ends_manual(self):
        self.task._in_combat = True
        self.task.in_combat()
        self.assertIsNone(self.recorder.run)
        self.task._in_combat = False
        self.enemy = True
        self.tick()
        run = self.recorder.run
        self.task._in_combat = True
        self.task.in_combat()
        self.assertIsNone(self.recorder.run)
        self.assertTrue(wait_for(lambda: run.path and any(e['e'] == 'end' for e in read_events(run.path))))
        self.assertEqual(read_events(run.path)[-1]['reason'], '自动战斗接管了这一局')
        texts = [c[0][0] for c in self.plugin.notification.call_args_list]
        self.assertEqual(len(texts), 3)  # 开局、收尾、提醒关自动战斗
        self.assertIn('记完了(自动战斗接管了这一局)', texts[1])
        self.assertIn('关掉「自动战斗」', texts[2])

    def test_switch_back_to_code_mode(self):
        self.enemy = True
        self.tick()
        run = self.recorder.run
        self.plugin.config[TASK_CLASS.MODE_KEY] = task_module.MODE_CODE
        self.tick()
        self.assertIsNone(self.recorder.run)
        self.assertEqual(self.plugin.trigger_interval, 1)
        self.assertTrue(wait_for(lambda: run.path and any(e['e'] == 'end' for e in read_events(run.path))))
        self.assertEqual(read_events(run.path)[-1]['reason'], '切回测试代码')
        self.assertIsNone(self.driver.hotkey)
        # 测试代码模式照常记自动战斗
        self.task._enabled = True
        self.start()
        self.finish()

    def test_capture_thread_ends_run_when_ticks_stop(self):
        self.enemy = True
        self.tick()
        run = self.recorder.run
        t = time.perf_counter()
        self.log.set_in_game(t, True)
        self.log.key(t, 'key', True, 'e')
        now = time.perf_counter()
        self.driver.last_tick = now - 2
        self.assertTrue(self.driver._on_capture_idle(now))
        self.assertIs(self.recorder.run, run)
        self.driver.last_tick = now - 5
        self.assertFalse(self.driver._on_capture_idle(now))
        self.assertIsNone(self.recorder.run)
        self.assertTrue(wait_for(lambda: run.path and any(e['e'] == 'end' for e in read_events(run.path))))
        events = read_events(run.path)
        self.assertTrue(events[-1]['reason'].startswith('ok-ww 停下了'))
        inputs = [e for e in events if e['e'] == 'in']
        self.assertEqual([(e['a'], e.get('cut')) for e in inputs], [('kd', None), ('ku', 1)])
        self.assertLessEqual(inputs[0]['t'], inputs[1]['t'])

    def test_tab_out_cuts_and_marks(self):
        self.enemy = True
        self.tick()
        run = self.recorder.run
        t = time.perf_counter()
        self.log.set_in_game(t, True)
        self.log.key(t, 'key', True, 'w')
        self.log.set_in_game(t + 0.1, False)
        self.log.key(t + 0.2, 'key', False, 'w')  # 切出去之后的松开不记
        self.log.set_in_game(t + 0.3, True)
        self.driver.toggle_requested.set()
        self.tick()
        self.assertTrue(wait_for(lambda: run.path and any(e['e'] == 'end' for e in read_events(run.path))))
        events = read_events(run.path)
        self.assertEqual([(e['a'], e.get('cut')) for e in events if e['e'] == 'in'], [('kd', None), ('ku', 1)])
        self.assertEqual([(e['name'], e['v']) for e in events if e['e'] == 'mark'],
                         [('暂停', '鸣潮不在前台'), ('继续', '鸣潮回到前台')])
        self.assertEqual(events[-1]['pauses'], 1)
        self.assertAlmostEqual(events[-1]['paused'], 0.2, delta=0.01)
        data = runs.RunData(run.path)
        self.assertTrue(any('切出去 1 次' in line for line in runs.detail_lines(data)))

    def test_start_while_game_in_background_and_note(self):
        self.plugin.config[TASK_CLASS.MANUAL_NOTE_KEY] = ' 4R16居 '
        self.log.set_in_game(time.perf_counter(), False)
        self.driver.toggle_requested.set()
        self.tick()
        run = self.recorder.run
        t = time.perf_counter()
        self.log.set_in_game(t + 0.5, True)
        self.driver.toggle_requested.set()
        self.tick()
        events = self.end_and_read(run)
        self.assertEqual(events[0]['note'], '4R16居')
        self.assertEqual([(e['name'], e['v']) for e in events if e['e'] == 'mark'],
                         [('暂停', '开始时鸣潮不在前台'), ('继续', '鸣潮回到前台')])
        self.assertEqual(events[-1]['pauses'], 1)
        self.assertGreater(events[-1]['paused'], 0.4)
        self.assertTrue(runs.headline(runs.RunData(run.path)).startswith('备注: 4R16居'))

    def test_enemy_check_only_reads(self):
        check = rec.ManualDriver._enemy_visible
        has_name, _ = self.task.get_target_names()
        self.task.get_box_by_name = Mock()
        self.task.has_health_bar = Mock(return_value=False)
        self.task.is_boss = Mock(return_value=False)
        self.task.find_best_match_in_box = Mock(return_value=Box(0, 0, 5, 5, 0.9, has_name))
        self.assertTrue(check(self.task))
        self.task.find_best_match_in_box = Mock(return_value=None)
        self.assertFalse(check(self.task))
        self.task.has_health_bar = Mock(return_value=True)
        self.assertTrue(check(self.task))
        self.task.has_health_bar = Mock(return_value=False)
        self.task.is_boss = Mock(return_value=True)  # Boss 破盾/锁定标记
        self.assertTrue(check(self.task))
        self.task.send_key = Mock()
        self.task.middle_click = Mock()
        check(self.task)
        self.task.send_key.assert_not_called()
        self.task.middle_click.assert_not_called()

    def test_manual_hotkey_same_as_export_is_dropped(self):
        self.assertEqual(self.driver._hotkey_name(self.plugin), 'F7')
        self.plugin.config[TASK_CLASS.MANUAL_HOTKEY_KEY] = 'F8'
        self.assertEqual(self.driver._hotkey_name(self.plugin), media.NO_HOTKEY)

    @contextlib.contextmanager
    def _rec_patches(self, sent, estimates, video='clip.mp4', offset=12.5, flips=(), foreground=lambda: True,
                     growing=lambda: None):
        """flips: 每次按完开关键 watch_toggle 依次给的结果(用完都是 None); foreground: 鸣潮在不在前台;
        growing: 录屏软件正在录的那段(没在录 None)。"""
        def fake_align(path, run_path, estimate, stop=None, busy=None):
            estimates.append(estimate(100.0))
            return {'duration': 100.0, 'fps': 60.0, 'offset': offset, 'method': '画面对时', 'score': 0.05}

        def fake_send(keys):
            sent.append(keys)
            return time.perf_counter()

        flips = list(flips)
        with patch.object(screenrec, 'send_combo', side_effect=fake_send), \
                patch.object(screenrec, 'find_video', return_value=video), \
                patch.object(screenrec, 'align', side_effect=fake_align), \
                patch.object(screenrec, 'MEDIA_SETTLE', 0), \
                patch.object(screenrec, 'FOREGROUND_POLL', 0.02), \
                patch.object(screenrec, 'game_foreground', side_effect=lambda: foreground()), \
                patch.object(screenrec, 'STOP_GRACE_SECONDS', 0), \
                patch.object(screenrec, 'STOP_SETTLE_SECONDS', 0), \
                patch.object(screenrec, 'growing_video', side_effect=lambda folder: growing()), \
                patch.object(screenrec, 'wait_video_finished', return_value=True), \
                patch.object(screenrec, 'watch_toggle', side_effect=lambda *a: flips.pop(0) if flips else None):
            yield

    def _toggle_config(self, folder):
        self.plugin.config.update({TASK_CLASS.REC_TOOL_KEY: 'N卡 (NVIDIA App)', TASK_CLASS.REC_MODE_KEY: '开局开录、结束停',
                                   TASK_CLASS.REC_KEYS_KEY: 'Alt+F9', TASK_CLASS.REC_FOLDER_KEY: folder})

    def test_screen_record_replay_saved_at_end_and_aligned(self):
        folder = tempfile.mkdtemp(prefix='axis_rec_video_')
        self.addCleanup(shutil.rmtree, folder, True)
        self.plugin.config[TASK_CLASS.REC_TOOL_KEY] = 'Win 游戏栏'
        self.plugin.config[TASK_CLASS.REC_FOLDER_KEY] = folder
        sent, estimates = [], []
        video = os.path.join(folder, 'clip.mp4')
        with self._rec_patches(sent, estimates, video):
            self.enemy = True
            self.tick()
            run = self.recorder.run
            time.sleep(0.1)
            self.assertEqual(sent, [])  # 存回放: 开局不按
            self.driver.toggle_requested.set()
            self.tick()
            events = self.end_and_read(run)
            self.assertTrue(wait_for(lambda: os.path.isfile(screenrec.video_info_path(run.path))))
        header, end = events[0], events[-1]
        self.assertEqual(header['rec'], {'tool': 'gamebar', 'mode': 'replay', 'keys': 'win+alt+g', 'folder': folder})
        self.assertEqual((end['rec']['tool'], end['rec']['action'], end['rec']['keys']), ('gamebar', '存回放', 'win+alt+g'))
        self.assertEqual(sent, ['win+alt+g'])
        info = screenrec.load_video_info(run.path)
        self.assertEqual((info['path'], info['offset'], info['method'], info['mode']), (video, 12.5, '画面对时', 'replay'))
        self.assertAlmostEqual(estimates[0], 100.0 - info['sent'], places=3)  # 片尾 = 按存回放那一刻
        self.assertGreaterEqual(info['sent'], end['rec']['t'] - 0.001)
        data = runs.RunData(run.path)
        self.assertEqual(runs.video_clock(data, 1.0), '录屏 00:13.50')
        self.assertEqual(runs.video_clock(data, 95.0), '录屏里没有这一刻')
        self.assertTrue(any(line.startswith('录屏: clip.mp4') for line in runs.detail_lines(data)))
        self.assertIn('录屏 +12.50s(画面对时)', runs.headline(data))

    def test_screen_record_toggle_starts_with_run(self):
        folder = tempfile.mkdtemp(prefix='axis_rec_video_')
        self.addCleanup(shutil.rmtree, folder, True)
        self._toggle_config(folder)
        sent, estimates = [], []
        with self._rec_patches(sent, estimates, os.path.join(folder, 'clip.mp4')):
            self.enemy = True
            self.tick()
            run = self.recorder.run
            current = self.driver.screen.current
            self.assertTrue(wait_for(lambda: current['start'] is not None))
            self.assertEqual(sent, ['Alt+F9'])
            self.driver.toggle_requested.set()
            self.tick()
            events = self.end_and_read(run)
            self.assertTrue(wait_for(lambda: os.path.isfile(screenrec.video_info_path(run.path))))
        self.assertEqual(sent, ['Alt+F9', 'Alt+F9'])
        self.assertEqual([(e['name'], e['v']) for e in events if e['e'] == 'mark'], [('录屏', '开始录制 Alt+F9')])
        self.assertEqual(events[-1]['rec']['action'], '停止录制')
        info = screenrec.load_video_info(run.path)
        self.assertAlmostEqual(estimates[0], -info['started'], places=3)  # 片头 = 按开始录制那一刻
        self.assertLess(info['started'], info['sent'])

    def test_screen_record_stops_even_if_key_stats_fail(self):
        folder = tempfile.mkdtemp(prefix='axis_rec_video_')
        self.addCleanup(shutil.rmtree, folder, True)
        self._toggle_config(folder)
        sent, estimates = [], []
        with self._rec_patches(sent, estimates, os.path.join(folder, 'clip.mp4'), offset=np.float32(12.5)), \
                patch.object(self.log, 'end', side_effect=RuntimeError('boom')):
            self.enemy = True
            self.tick()
            run = self.recorder.run
            current = self.driver.screen.current
            self.assertTrue(wait_for(lambda: current['start'] is not None))
            self.driver.toggle_requested.set()
            self.tick()
            self.assertIsNone(self.recorder.run)
            events = self.end_and_read(run)
            self.assertTrue(wait_for(lambda: screenrec.load_video_info(run.path) is not None))
        self.assertEqual(sent, ['Alt+F9', 'Alt+F9'])  # 停录还是按了, 不然下一局的「开始」反而把录制关掉
        self.assertEqual(events[-1]['rec']['action'], '停止录制')
        self.assertEqual(screenrec.load_video_info(run.path)['offset'], 12.5)

    def test_screen_record_toggle_flipped_presses_again(self):
        folder = tempfile.mkdtemp(prefix='axis_rec_video_')
        self.addCleanup(shutil.rmtree, folder, True)
        self._toggle_config(folder)
        sent, estimates = [], []
        # 开录那下停掉了一段之前就在录的 -> 补按开始; 停录那下又新开了一段 -> 补按停掉, 这局不找录屏
        with self._rec_patches(sent, estimates, os.path.join(folder, 'clip.mp4'), flips=['stopped', 'started']):
            self.enemy = True
            self.tick()
            run = self.recorder.run
            self.driver.screen.current['starter'].join(5)
            self.assertEqual(sent, ['Alt+F9', 'Alt+F9'])
            self.driver.toggle_requested.set()
            self.tick()
            events = self.end_and_read(run)
            self.assertTrue(wait_for(lambda: screenrec.load_video_info(run.path) is not None))
        self.assertEqual(sent, ['Alt+F9'] * 4)
        marks = [e['v'] for e in events if e['e'] == 'mark']
        self.assertEqual(marks[0], '开始录制 Alt+F9')
        self.assertTrue(marks[1].startswith('补按开始录制 Alt+F9'))
        info = screenrec.load_video_info(run.path)
        self.assertEqual(info['method'], '录屏开关对不上')
        self.assertNotIn('path', info)
        self.assertEqual(estimates, [])

    def test_screen_record_toggle_waits_for_game_foreground(self):
        folder = tempfile.mkdtemp(prefix='axis_rec_video_')
        self.addCleanup(shutil.rmtree, folder, True)
        self._toggle_config(folder)
        sent, estimates = [], []
        game = [False]
        with self._rec_patches(sent, estimates, os.path.join(folder, 'clip.mp4'), foreground=lambda: game[0]):
            # 开局时人在 ok-ww 里, 打完也没回游戏: 开录不按, 停录也不按
            self.driver.toggle_requested.set()
            self.tick()
            run = self.recorder.run
            time.sleep(0.2)
            self.assertEqual(sent, [])
            self.driver.toggle_requested.set()
            self.tick()
            self.end_and_read(run)
            self.assertTrue(wait_for(lambda: screenrec.load_video_info(run.path) is not None))
            self.assertEqual(sent, [])
            self.assertEqual(screenrec.load_video_info(run.path)['method'], '没开录')
            # 在游戏里开的录, 打完人切到 ok-ww 看复盘: 停录等回到游戏才按
            game[0] = True
            self.driver.toggle_requested.set()
            self.tick()
            run = self.recorder.run
            self.assertTrue(wait_for(lambda: sent == ['Alt+F9']))
            game[0] = False
            self.driver.toggle_requested.set()
            self.tick()
            self.end_and_read(run)
            time.sleep(0.2)
            self.assertEqual(sent, ['Alt+F9'])
            game[0] = True
            self.assertTrue(wait_for(lambda: screenrec.load_video_info(run.path) is not None))
        self.assertEqual(sent, ['Alt+F9', 'Alt+F9'])
        self.assertEqual(screenrec.load_video_info(run.path)['offset'], 12.5)

    def test_screen_record_toggle_follows_recording_state(self):
        folder = tempfile.mkdtemp(prefix='axis_rec_video_')
        self.addCleanup(shutil.rmtree, folder, True)
        self._toggle_config(folder)
        clip = os.path.join(folder, 'clip.mp4')
        with open(clip, 'wb') as f:
            f.write(b'x')
        sent, estimates = [], []
        recording = [clip]
        with self._rec_patches(sent, estimates, clip, growing=lambda: recording[0]):
            # 开局时已经在录: 不按开始, 接着录; 收尾时还在录才按停止
            self.enemy = True
            self.tick()
            run = self.recorder.run
            self.driver.screen.current['starter'].join(5)
            self.assertEqual(sent, [])
            self.driver.toggle_requested.set()
            self.tick()
            events = self.end_and_read(run)
            self.assertTrue(wait_for(lambda: screenrec.load_video_info(run.path) is not None))
        self.assertEqual(sent, ['Alt+F9'])
        self.assertTrue([e['v'] for e in events if e['e'] == 'mark'][0].startswith('已经在录(clip.mp4)'))
        info = screenrec.load_video_info(run.path)
        self.assertEqual(info['flow'], screenrec.ADOPTED)
        self.assertAlmostEqual(estimates[0], screenrec.wall_time(run.t0) - screenrec.video_birth(clip), places=2)
        self.assertIn(screenrec.ADOPTED, runs.video_line(runs.RunData(run.path)))

        # 按开始后认出了新的一段, 收尾时它已经停了(被手动停了): 不按停止, 找开录之后录完的那段
        sent.clear()
        estimates.clear()
        recording[0] = None
        with self._rec_patches(sent, estimates, clip, flips=['started'], growing=lambda: recording[0]):
            self.enemy = False
            self.driver.toggle_requested.set()
            self.tick()
            run = self.recorder.run
            self.driver.screen.current['starter'].join(5)
            self.driver.toggle_requested.set()
            self.tick()
            self.end_and_read(run)
            self.assertTrue(wait_for(lambda: screenrec.load_video_info(run.path) is not None))
        self.assertEqual(sent, ['Alt+F9'])
        info = screenrec.load_video_info(run.path)
        self.assertEqual((info['flow'], info['offset']), (screenrec.ALREADY_STOPPED, 12.5))
        self.assertAlmostEqual(estimates[0], -info['started'], places=3)

    def test_screen_record_toggle_hands_over_to_next_run(self):
        folder = tempfile.mkdtemp(prefix='axis_rec_video_')
        self.addCleanup(shutil.rmtree, folder, True)
        self._toggle_config(folder)
        clip = os.path.join(folder, 'clip.mp4')
        sent, estimates = [], []
        game, recording = [True], [None]
        with self._rec_patches(sent, estimates, clip, foreground=lambda: game[0], growing=lambda: recording[0]):
            self.driver.toggle_requested.set()
            self.tick()
            first = self.recorder.run
            self.driver.screen.current['starter'].join(5)
            self.assertEqual(sent, ['Alt+F9'])
            recording[0] = clip
            game[0] = False  # 打完人去 ok-ww 看复盘, 停录在等
            self.driver.toggle_requested.set()
            self.tick()
            self.end_and_read(first)
            time.sleep(0.2)
            self.assertEqual(sent, ['Alt+F9'])
            # 还没停录就开了下一局: 上一局不停了, 这局接着录同一段, 打完才停
            self.driver.toggle_requested.set()
            self.tick()
            second = self.recorder.run
            game[0] = True
            self.driver.screen.current['starter'].join(5)
            self.assertTrue(wait_for(lambda: screenrec.load_video_info(first.path) is not None))
            self.assertEqual(sent, ['Alt+F9'])
            self.driver.toggle_requested.set()
            self.tick()
            self.end_and_read(second)
            self.assertTrue(wait_for(lambda: screenrec.load_video_info(second.path) is not None))
        self.assertEqual(sent, ['Alt+F9', 'Alt+F9'])
        info = screenrec.load_video_info(first.path)
        self.assertEqual((info['flow'], info['path'], info['offset']), (screenrec.HANDED_OVER, clip, 12.5))
        self.assertEqual(screenrec.load_video_info(second.path)['flow'], screenrec.ADOPTED)

    def test_screen_record_bad_keys_records_without_video(self):
        self.plugin.config.update({TASK_CLASS.REC_TOOL_KEY: 'A卡 (AMD Adrenalin)', TASK_CLASS.REC_KEYS_KEY: 'ctrl+banana'})
        self.enemy = True
        self.tick()
        run = self.recorder.run
        self.assertIsNone(self.driver.screen.current)
        self.driver.toggle_requested.set()
        self.tick()
        events = self.end_and_read(run)
        self.assertNotIn('rec', events[0])
        self.assertNotIn('rec', events[-1])

    def test_record_hotkey_presses_are_not_logged(self):
        self.enemy = True
        self.tick()
        run = self.recorder.run
        t = time.perf_counter()
        self.log.set_in_game(t, True)
        self.log.mute_injected(1.0)
        self.log.key(t + 0.01, 'key', True, 'alt', injected=True)
        self.log.key(t + 0.02, 'key', True, 'e')
        self.log.key(t + 0.03, 'key', False, 'alt', injected=True)
        self.log.key(t + 0.04, 'key', False, 'e')
        self.log.wheel(t + 0.05, 1.0, injected=True)
        self.log.key(t + 2.0, 'key', True, 'q', injected=True)  # 静音过了照记
        self.driver.toggle_requested.set()
        self.tick()
        events = self.end_and_read(run)
        self.assertEqual([(e['a'], e['k'], e.get('si')) for e in events if e['e'] == 'in'],
                         [('kd', 'e', None), ('ku', 'e', None), ('kd', 'q', 1), ('ku', 'q', None)])


class TestRawInput(unittest.TestCase):
    """真起采集线程(前台当成鸣潮), 自己 keybd_event 一下 F24 看收不收得到。"""

    def test_capture_receives_injected_key(self):
        rawinput = sys.modules[package + '.rawinput']
        got = []
        log = rawinput.InputLog(lambda *args: got.append(args[1:]), lambda *args: None)
        run = SimpleNamespace()
        log.begin(lambda t0: run)

        class Always:
            def foreground(self):
                return True

        thread = rawinput.RawInputThread(log, on_idle=lambda now: True, game_factory=Always)
        thread.start()
        try:
            self.assertTrue(thread.ready.wait(3))
            self.assertIsNone(thread.error)
            user32 = rawinput.user32
            user32.keybd_event(0x87, 0, 0, 0)
            user32.keybd_event(0x87, 0, 2, 0)
            self.assertTrue(wait_for(lambda: len(got) >= 2))
        finally:
            thread.stop()
        self.assertFalse(thread.is_alive())
        self.assertEqual([(g[1], g[2], g[3]) for g in got[:2]], [('kd', 'f24', True), ('ku', 'f24', True)])

    def test_record_hotkey_sendinput_reaches_raw_input(self):
        """录屏快捷键走自己的 SendInput 结构体, 真发一下 F24 看系统认不认。"""
        rawinput = sys.modules[package + '.rawinput']
        got = []
        log = rawinput.InputLog(lambda *args: got.append(args[1:]), lambda *args: None)
        log.begin(lambda t0: SimpleNamespace())

        class Always:
            def foreground(self):
                return True

        thread = rawinput.RawInputThread(log, on_idle=lambda now: True, game_factory=Always)
        thread.start()
        try:
            self.assertTrue(thread.ready.wait(3))
            pressed = screenrec.send_combo('f24', hold=0.01)
            self.assertTrue(wait_for(lambda: len(got) >= 2))
        finally:
            thread.stop()
        self.assertEqual([(g[1], g[2], g[3]) for g in got[:2]], [('kd', 'f24', True), ('ku', 'f24', True)])
        self.assertLess(abs(got[0][0] - pressed), 0.5)


def make_video(path, fps, seconds, size=(320, 180)):
    """每帧一张不一样的随机色块图(相邻帧也分得开), MJPG 编码。"""
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*'MJPG'), fps, size)
    rng = np.random.default_rng(3)
    for _ in range(int(fps * seconds)):
        blocks = rng.integers(0, 255, (9, 16, 3), dtype=np.uint8)
        writer.write(cv2.resize(blocks, size, interpolation=cv2.INTER_NEAREST))
    writer.release()


def write_frames_from(video, run_path, times, offset):
    """照插件的格式存「插件画面」: 记录 t 秒那帧 = 录屏 t+offset 秒那一刻屏幕上的帧。"""
    cap = cv2.VideoCapture(video, cv2.CAP_FFMPEG)
    fps = cap.get(cv2.CAP_PROP_FPS)
    frames = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frames.append(frame)
    cap.release()
    media_path, index_path = media.media_paths(run_path)
    with open(media_path, 'wb') as data, open(index_path, 'w', encoding='utf-8') as index:
        index.write(json.dumps({'v': 1, 'interval': 0.25}) + '\n')
        for t in times:
            frame = frames[min(len(frames) - 1, int((t + offset) * fps + 1e-6))]
            raw = cv2.imencode('.jpg', cv2.resize(frame, (960, 540)), [cv2.IMWRITE_JPEG_QUALITY, 60])[1].tobytes()
            index.write(json.dumps({'f': 1, 't': t, 'o': data.tell(), 'n': len(raw), 'w': 960, 'h': 540}) + '\n')
            data.write(raw)


class TestScreenRec(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='axis_rec_screen_')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_parse_combo_and_names(self):
        self.assertEqual(screenrec.parse_combo('Alt + F10'), ([0x12], 0x79))
        self.assertEqual(screenrec.parse_combo('win+alt+g'), ([0x5B, 0x12], ord('G')))
        for bad in ('', 'alt+', 'hyper+f1', 'alt+f25', 'alt+é'):
            with self.assertRaises(ValueError, msg=bad):
                screenrec.parse_combo(bad)
        self.assertEqual(screenrec.combo_name([0x12, 0x79]), 'alt+f10')
        self.assertEqual(screenrec.combo_name([0xA4, 0x10, 0x79]), 'alt+shift+f10')
        self.assertIsNone(screenrec.combo_name([0x12, 0xBA]))

    def test_send_combo_presses_modifiers_around_key(self):
        sent = []

        def fake(count, array, size):
            for i in range(count):
                ki = array[i].u.ki
                sent.append((ki.wVk, bool(ki.dwFlags & 2), bool(ki.dwFlags & 1)))
            return count

        with patch.object(screenrec.user32, 'SendInput', side_effect=fake), \
                patch.object(screenrec, 'wait_modifiers_released', return_value=True):
            screenrec.send_combo('win+alt+g', hold=0)
        self.assertEqual(sent, [(0x5B, False, True), (0x12, False, False), (0x47, False, False), (0x47, True, False),
                                (0x12, True, False), (0x5B, True, True)])

    def test_make_plan(self):
        self.assertIsNone(screenrec.make_plan(screenrec.REC_OFF, screenrec.MODE_REPLAY, '', ''))
        plan = screenrec.make_plan('Win 游戏栏', screenrec.MODE_TOGGLE, '', 'D:/clips')
        self.assertEqual((plan['tool'], plan['mode'], plan['keys'], plan['folder']),
                         ('gamebar', 'toggle', 'win+alt+r', 'D:/clips'))
        found = {'folder': 'E:/rec', 'buffer': 300, 'save': 'alt+f11', 'toggle': None}
        with patch.object(screenrec, 'nvidia_settings', return_value=found):
            plan = screenrec.make_plan('N卡 (NVIDIA App)', screenrec.MODE_REPLAY, '', '')
            self.assertEqual((plan['keys'], plan['folder'], plan['buffer']), ('alt+f11', 'E:/rec', 300))
            self.assertEqual(screenrec.make_plan('N卡 (NVIDIA App)', screenrec.MODE_TOGGLE, '', '')['keys'], 'alt+f9')
            self.assertEqual(screenrec.make_plan('N卡 (NVIDIA App)', screenrec.MODE_REPLAY, 'ctrl+f12', '')['keys'],
                             'ctrl+f12')
        self.assertEqual(screenrec.make_plan('A卡 (AMD Adrenalin)', screenrec.MODE_REPLAY, '', '')['keys'],
                         'ctrl+shift+s')
        with self.assertRaises(ValueError):
            screenrec.make_plan('A卡 (AMD Adrenalin)', screenrec.MODE_REPLAY, 'ctrl+banana', '')

    def test_find_video_newest_in_subfolder(self):
        old = os.path.join(self.dir, 'old.mp4')
        with open(old, 'wb') as f:
            f.write(b'x' * 10)
        os.utime(old, (time.time() - 100,) * 2)
        sub = os.path.join(self.dir, 'Wuthering Waves')
        os.makedirs(sub)
        new = os.path.join(sub, 'clip.mp4')
        with open(new, 'wb') as f:
            f.write(b'x' * 10)
        with open(os.path.join(sub, 'notes.txt'), 'w') as f:
            f.write('x')
        self.assertEqual(screenrec.find_video(self.dir, time.time() - 1, timeout=2, poll=0.05), new)
        self.assertIsNone(screenrec.find_video(self.dir, time.time() + 60, timeout=0.2, poll=0.05))
        self.assertIsNone(screenrec.find_video(os.path.join(self.dir, 'nope'), time.time(), timeout=0.2))

    def test_watch_toggle_reads_folder(self):
        def write_for(path, seconds):
            def work():
                end = time.monotonic() + seconds
                with open(path, 'ab') as f:
                    while time.monotonic() < end:
                        f.write(b'x' * 1000)
                        f.flush()
                        time.sleep(0.05)
            thread = threading.Thread(target=work)
            thread.start()
            return thread

        with patch.object(screenrec, 'TOGGLE_POLL', 0.1), patch.object(screenrec, 'TOGGLE_CHECK_SECONDS', 1.0), \
                patch.object(screenrec, 'GROWING_SECONDS', 0.3):
            self.assertIsNone(screenrec.watch_toggle(self.dir, time.perf_counter()))  # 没动静
            self.assertIsNone(screenrec.watch_toggle(os.path.join(self.dir, 'nope'), time.perf_counter()))
            # 按之前就在录的一段, 按完写完了 = 这下把它停了
            old = os.path.join(self.dir, 'old.mp4')
            with open(old, 'wb') as f:
                f.write(b'x' * 10)
            time.sleep(1.2)
            with open(old, 'ab') as f:
                f.write(b'x' * 10)
            self.assertEqual(screenrec.watch_toggle(self.dir, time.perf_counter()), 'stopped')
            # 按之前就在录、按完还在写 = 没停也没开
            busy = os.path.join(self.dir, 'busy.mp4')
            with open(busy, 'wb') as f:
                f.write(b'x' * 10)
            time.sleep(1.2)
            writer = write_for(busy, 1.5)
            self.assertIsNone(screenrec.watch_toggle(self.dir, time.perf_counter()))
            writer.join()
            os.remove(busy)  # 刚写完, 留着会被下一下当成「停掉了它」
            # 按之后新冒出一段、一直在变大 = 这下开了录
            pressed = time.perf_counter()
            writer = write_for(os.path.join(self.dir, 'new.mp4'), 1.0)
            self.assertEqual(screenrec.watch_toggle(self.dir, pressed), 'started')
            writer.join()

    def test_align_recovers_offset_from_frames(self):
        video = os.path.join(self.dir, 'clip.avi')
        make_video(video, 30, 24)
        run_path = os.path.join(self.dir, 'run.jsonl')
        open(run_path, 'w').close()
        times = [round(i * 0.25 + (i % 3) * 0.013, 4) for i in range(60)]
        write_frames_from(video, run_path, times, 5.0)
        result = screenrec.align(video, run_path, lambda duration: 5.0 + 1.7)
        self.assertEqual(result['method'], '画面对时', result)
        self.assertLessEqual(abs(result['offset'] - 5.0), 1 / 30 + 0.005)
        self.assertAlmostEqual(result['estimate'], 6.7)
        self.assertEqual((result['fps'], result['duration']), (30.0, 24.0))
        # 估计差得太远(超过宽的那趟)就不硬对, 留估计值
        far = screenrec.align(video, run_path, lambda duration: 5.0 - 17.0)
        self.assertEqual(far['method'], '按键时刻估算')
        self.assertAlmostEqual(far['offset'], -12.0)
        # 没存画面
        os.remove(media.media_paths(run_path)[0])
        none = screenrec.align(video, run_path, lambda duration: 5.0)
        self.assertEqual(none['method'], '按键时刻估算')
        self.assertIn('没存够画面', none['note'])


class TestSendInput(PluginCase):
    def test_tap_forwards_and_records(self):
        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [('dx', ctypes.c_long), ('dy', ctypes.c_long), ('mouseData', ctypes.c_ulong),
                        ('dwFlags', ctypes.c_ulong), ('time', ctypes.c_ulong), ('extra', ctypes.c_size_t)]

        class INPUT(ctypes.Structure):
            _fields_ = [('type', ctypes.c_ulong), ('mi', MOUSEINPUT)]

        original = Mock(return_value=1)
        tap = rec._SendInputTap(self.recorder, original)
        tap.argtypes = None
        self.assertIsNone(original.argtypes)
        self.start()
        inp = INPUT(type=0, mi=MOUSEINPUT(0, 0, 0, 0x0002, 0, 0))
        self.assertEqual(TEAM_CALL(tap, 1, ctypes.byref(inp), ctypes.sizeof(INPUT)), 1)
        inp.mi.dwFlags = 0x0004
        TEAM_CALL(tap, 1, ctypes.pointer(inp), ctypes.sizeof(INPUT))
        events = self.finish()
        inputs = [e for e in events if e['e'] == 'in']
        self.assertEqual([(e['a'], e['k'], e['si']) for e in inputs], [('md', 'left', 1), ('mu', 'left', 1)])
        self.assertEqual(original.call_count, 2)

    def test_decode_keyboard(self):
        union = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 4
        size = union + 24
        raw = bytearray(size)
        raw[0:4] = (1).to_bytes(4, 'little')
        raw[union:union + 2] = (0x45).to_bytes(2, 'little')
        raw[union + 4:union + 8] = (rec.KEYEVENTF_KEYUP).to_bytes(4, 'little')
        self.assertEqual(rec.decode_send_input(bytes(raw), size), [('ku', 'e')])

    def test_installed_tap_on_user32(self):
        self.assertEqual(type(ctypes.windll.user32.SendInput).__name__, '_SendInputTap')


class TestOverhead(PluginCase):
    def measure(self, function, *args, rounds=20000):
        start = time.perf_counter()
        for _ in range(rounds):
            function(*args)
        return (time.perf_counter() - start) / rounds * 1e6

    def test_overhead_per_call(self):
        bare = lambda task, *a, **k: None  # noqa: E731
        read = rec.Recorder._read_wrapper(self.recorder, 'find', rec._extract_find_one)(bare)
        key = self.recorder._input_wrapper('key', None)(lambda inter, key, down_time=0.02: None)
        # 跟实机一样: 模板在 feature_dict 里(没给 box 时要补算搜索框), 有当前帧(读数变了要截小块)
        self.executor.feature_set = SimpleNamespace(
            feature_dict={'x': SimpleNamespace(x=100, y=100, width=40, height=40, scaling=1)}, width=2560,
            height=1440, default_horizontal_variance=0.002, default_vertical_variance=0.002)
        self.executor._frame = np.zeros((1440, 2560, 4), np.uint8)[:, :, :3]
        base_read = self.measure(TEAM_CALL, bare, self.task, 'x')
        idle_read = self.measure(TEAM_CALL, read, self.task, 'x')
        self.start()
        live_read = self.measure(TEAM_CALL, read, self.task, 'x')
        live_key = self.measure(TEAM_CALL, key, self.interaction, 'e')
        frame = np.zeros((1440, 2560, 4), np.uint8)[:, :, :3]
        bare_grab = lambda executor: frame  # noqa: E731
        grab = self.recorder._next_frame_wrapper(bare_grab)
        base_grab = self.measure(bare_grab, self.executor)
        live_grab = self.measure(grab, self.executor)
        self.finish()
        print(f'\n  每次调用: 不包 {base_read:.2f}us, 包了没在录 {idle_read:.2f}us, '
              f'在录读屏 {live_read:.2f}us, 在录按键 {live_key:.2f}us, 在录取帧多 {live_grab - base_grab:.2f}us')
        self.assertLess(live_read - base_read, 25)
        self.assertLess(live_key - base_read, 25)
        self.assertLess(live_grab - base_grab, 25)


SAMPLE = [
    {'e': 'start', 'v': 1, 'wall': 1000.0, 'task': 'AutoCombatTask', 'keys': {'Liberation Key': 'r'},
     'team': [{'i': 0, 'n': 'char_hsin', 'cls': 'Hsin'}, {'i': 1, 'n': 'ShoreKeeper'}, {'i': 2, 'n': 'Suoming'}],
     'team_dir': 'Hsin__ShoreKeeper__Suoming', 'team_ver': '2.0.0', 'size': [2560, 1440]},
    {'e': 'field', 't': 0.0, 'team': True, 'i': 1, 'cnt': 3},
    {'e': 'in', 't': 0.5, 'a': 'key', 'k': 'r', 'src': 'ShoreKeeper.py:10 go'},
    {'e': 'field', 't': 1.0, 'team': False, 'i': -1, 'cnt': 1, 'prev_seen': 0.9},
    {'e': 'in', 't': 1.5, 'a': 'key', 'k': 'r'},
    {'e': 'in', 't': 1.7, 'a': 'key', 'k': 'r'},
    {'e': 'field', 't': 3.0, 'team': True, 'i': 1, 'cnt': 3, 'prev_seen': 1.4},
    {'e': 'in', 't': 3.5, 'a': 'key', 'k': '3'},
    {'e': 'field', 't': 4.0, 'team': True, 'i': 2, 'cnt': 3},
    {'e': 'in', 't': 4.2, 'a': 'md', 'k': 'left', 'si': 1},
    {'e': 'in', 't': 5.2, 'a': 'mu', 'k': 'left', 'si': 1},
    {'e': 'read', 't': 4.3, 't2': 5.0, 'n': 20, 'k': 'con', 'name': 'con', 'v': 0.5, 'lo': 0.5, 'hi': 0.99,
     'src': 'Suoming.py:20 wait_con', 'via': 'BaseChar.py:770 get_current_con'},
    {'e': 'field', 't': 6.0, 'team': True, 'i': 0, 'cnt': 3},
    {'e': 'in', 't': 6.5, 'a': 'key', 'k': 'r'},
    {'e': 'in', 't': 7.0, 'a': 'md', 'k': 'left', 'src': 'Hsin.py:1014 tap'},
    {'e': 'in', 't': 7.06, 'a': 'mu', 'k': 'left', 'src': 'Hsin.py:1019 tap'},
    {'e': 'field', 't': 9.0, 'team': True, 'i': 1, 'cnt': 3},
    {'e': 'field', 't': 14.0, 'team': True, 'i': 2, 'cnt': 3},
    {'e': 'field', 't': 15.0, 'team': True, 'i': 1, 'cnt': 2},
    {'e': 'field', 't': 15.02, 'team': True, 'i': 2, 'cnt': 3},
    {'e': 'field', 't': 16.0, 'team': True, 'i': 1, 'cnt': 3},
    {'e': 'log', 't': 2.0, 'l': 20, 'm': 'Hsin:sample log'},
    {'e': 'end', 't': 18.0, 'reason': 'target enemy failed', 'field_seen': 17.5},
]


def write_sample(directory, name='20261026_172803_Hsin__ShoreKeeper__Suoming.jsonl', extra=()):
    path = os.path.join(directory, name)
    with open(path, 'w', encoding='utf-8') as f:
        for event in SAMPLE + list(extra):
            f.write(json.dumps(event, ensure_ascii=False) + '\n')
    return path


def write_media(run_path, frame_times, crops=()):
    """按插件的格式写一份画面: 每帧纯色 960x540, crops 是 [(索引条目, (高, 宽))]; 最后加一条指到文件外的(没写完就断了)。"""
    media_path, index_path = media.media_paths(run_path)
    offset = 0
    with open(media_path, 'wb') as data, open(index_path, 'w', encoding='utf-8') as index:
        index.write(json.dumps({'v': 1, 'interval': 0.25}) + '\n')
        items = [({'f': 1, 't': t, 'w': 960, 'h': 540}, cv2.imencode('.jpg', np.full((540, 960, 3), i % 250, np.uint8))[1])
                 for i, t in enumerate(frame_times)]
        items += [(dict(entry, c=1), cv2.imencode('.png', np.full(shape + (3,), 200, np.uint8))[1])
                  for entry, shape in crops]
        for entry, buf in items:
            raw = buf.tobytes()
            data.write(raw)
            index.write(json.dumps(dict(entry, o=offset, n=len(raw)), ensure_ascii=False) + '\n')
            offset += len(raw)
        index.write(json.dumps({'f': 1, 't': 99.0, 'o': offset, 'n': 500}) + '\n')


class TestRuns(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='axis_rec_sample_')
        self.path = write_sample(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_analysis(self):
        run = runs.RunData(self.path)
        self.assertEqual(run.duration, 18.0)
        self.assertEqual(run.entries(1), [0.0, 9.0, 16.0])
        self.assertEqual([round(d, 2) for _, d in run.laps(1)], [9.0, 7.0])
        self.assertEqual(run.char_name(0), 'Hsin')
        stats = run.char_stats()
        # HUD 不在时连按的两下 R 算守岸人的, 合成一段
        self.assertEqual((stats[1]['keys']['R'], stats[0]['keys']['R'], stats[2]['keys']['R']),
                         ([3, 2], [1, 1], [0, 0]))
        self.assertAlmostEqual(run.hud_hidden_time(), 2.0)
        self.assertAlmostEqual(run.unseen_time(runs.HUD_HIDDEN), 1.6)
        self.assertEqual((run.field[0].seen, run.field[-1].seen), (0.9, 17.5))
        self.assertEqual(run.field_label_at(2.0), 'ShoreKeeper(HUD 不在)')
        # 15.0 那一帧只认到一个号码, 误报成守岸人, 不算上场
        self.assertTrue(run.field[-3].shaky)
        self.assertEqual(run.field_label_at(15.01), 'Suoming(HUD 没认全)')
        self.assertEqual([(h.t0, h.t1, h.label) for h in run.holds], [(4.2, 5.2, '按住A')])
        self.assertEqual([m.label for m in run.inputs], ['R', 'R', 'R', '切Suoming', 'A', 'R', 'A'])
        self.assertEqual([(m.hidden, m.slot) for m in run.inputs[1:3]], [(True, 1), (True, 1)])
        self.assertEqual([(g.label, g.count) for g in run.input_groups],
                         [('R', 1), ('R', 2), ('切Suoming', 1), ('A', 1), ('R', 1), ('A', 1)])
        self.assertEqual(len(run.reads), 1)
        kind, text = runs.describe(run, run.reads[0].events[0])
        self.assertIn('连读 20 次', text)
        self.assertIn('经由 BaseChar.py:770', runs.source_text(run.reads[0].events[0]))
        self.assertIn('9.00 / 7.00', runs.headline(run, 1))
        lines = runs.detail_lines(run)
        self.assertIn('其中 1.60s 轴没在看', lines[1])
        self.assertIn('R 3(2 段)', lines[4])
        # 每圈只上一次的是锁暝(2 次), 守岸人 3 次, 心 1 次分不了圈
        self.assertEqual(run.default_lap_slot(), 2)
        columns, laps = run.lap_breakdown(1)
        self.assertEqual(columns, [(1, 1), (2, 1), (0, 1)])
        self.assertEqual([round(total, 2) for _, total, _ in laps], [9.0, 7.0])
        self.assertEqual(laps[0][2][(2, 1)], (4.0, 2.0))
        self.assertEqual(run.segment_label((2, 1), columns), 'Suoming')
        self.assertEqual(run.segment_label((1, 2), [(1, 1), (1, 2)]), 'ShoreKeeper ②')
        # 两种上场顺序交替(按半圈分): 只和同顺序的比, 长的那种不会被短的拉低中位数
        short = {(1, 1): (0, 4.0), (2, 1): (4, 6.0)}
        long = {(1, 1): (0, 4.0), (2, 1): (4, 6.0), (0, 1): (10, 10.0)}
        mixed = [(0, 10.0, short), (0, 20.0, long), (0, 10.0, short), (0, 21.0, long), (0, 12.0, short)]
        baselines, groups = runs.RunData.lap_baselines([(1, 1), (2, 1), (0, 1)], mixed)
        self.assertEqual(sorted(len(m) for m in groups.values()), [2, 3])
        self.assertEqual([b[-1] if b else None for b in baselines], [10.0, None, 10.0, None, 10.0])

    def test_summary_list_and_unfinished(self):
        unfinished = os.path.join(self.dir, '20261026_180000_x.jsonl')
        with open(unfinished, 'w', encoding='utf-8') as f:
            f.write(json.dumps(SAMPLE[0]) + '\n' + json.dumps(SAMPLE[2]) + '\n{"broken')
        listed = runs.list_runs(self.dir)
        self.assertEqual([s.name for s in listed], ['20261026_180000_x.jsonl', os.path.basename(self.path)])
        self.assertFalse(listed[0].finished)
        self.assertEqual(listed[0].duration, 0.5)
        self.assertTrue(listed[1].finished)
        self.assertEqual(listed[1].team, ['Hsin', 'ShoreKeeper', 'Suoming'])
        data = runs.RunData(unfinished)
        self.assertEqual(data.bad_lines, 1)


def write_feedback_sample(directory):
    """一局(开打 2026-10-27 01:52:12, 18s) + 画面 + 两次导出(一次是别的局的) + 一份日志。返回 (记录路径, 日志路径)。"""
    import datetime
    wall = datetime.datetime(2026, 10, 27, 1, 52, 12).timestamp()
    name = '20261027_015212_Hsin__ShoreKeeper__Suoming.jsonl'
    path = os.path.join(directory, name)
    with open(path, 'w', encoding='utf-8') as f:
        for event in [dict(SAMPLE[0], wall=wall, plugin='0.2.3')] + SAMPLE[1:]:
            f.write(json.dumps(event, ensure_ascii=False) + '\n')
    write_media(path, [0.0, 0.25, 0.5])
    for folder, run_name in (('20261027_015220_mine', name), ('20261027_015300_other', 'other.jsonl')):
        os.makedirs(os.path.join(directory, media.EXPORT_DIR, folder))
        with open(os.path.join(directory, media.EXPORT_DIR, folder, '0003.450s.png'), 'wb') as f:
            f.write(b'png' * 1000)
        with open(os.path.join(directory, media.EXPORT_DIR, folder, 'info.json'), 'w', encoding='utf-8') as f:
            json.dump({'run': run_name, 'frames': [{'file': '0003.450s.png', 't': 3.45}]}, f)
    log_path = os.path.join(directory, 'logs', 'ok-script.log')
    os.makedirs(os.path.dirname(log_path))
    with open(log_path, 'w', encoding='utf-8') as f:
        f.write('2026-10-27 01:51:00,000 INFO MainThread ok:太早\n'
                '2026-10-27 01:52:00,000 ERROR MainThread Hsin:出错了\n'
                'Traceback (most recent call last):\n'
                '2026-10-27 01:52:20,500 INFO MainThread Hsin:xss opener entry\n'
                '2026-10-27 01:53:10,000 INFO MainThread ok:太晚\n')
    return path, log_path


class TestFeedback(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='axis_rec_feedback_')
        self.path, self.log_path = write_feedback_sample(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def read_zip(self, zip_path):
        import zipfile
        with zipfile.ZipFile(zip_path) as zf:
            return {i.filename: i for i in zf.infolist()}, zf.read(feedback.SUMMARY_NAME).decode('utf-8'), \
                zf.read(feedback.LOG_NAME).decode('utf-8'), json.loads(zf.read('feedback.json'))

    def test_pack_run(self):
        import zipfile
        plan = feedback.FeedbackPlan(self.path, self.dir)
        self.assertEqual((plan.frames, len(plan.exports), plan.export_images), (3, 1, 1))
        zip_path = feedback.build_feedback(plan, '第 2 圈锁暝没放 R', log_path=self.log_path)
        self.assertTrue(zip_path.endswith('20261027_015212_Hsin__ShoreKeeper__Suoming_反馈.zip'))
        entries, summary, log, info = self.read_zip(zip_path)
        stem = 'axis_runs/20261027_015212_Hsin__ShoreKeeper__Suoming'
        self.assertEqual(set(entries), {feedback.SUMMARY_NAME, feedback.LOG_NAME, 'feedback.json', stem + '.jsonl',
                                        stem + '.media', stem + '.media.idx',
                                        'axis_runs/export/20261027_015220_mine/0003.450s.png',
                                        'axis_runs/export/20261027_015220_mine/info.json'})
        self.assertEqual(entries[stem + '.media'].compress_type, zipfile.ZIP_STORED)
        self.assertEqual(entries[stem + '.jsonl'].compress_type, zipfile.ZIP_DEFLATED)
        # 开打前 30s 到结束后 30s, 堆栈行跟着上一行
        self.assertEqual(log.splitlines(), ['2026-10-27 01:52:00,000 ERROR MainThread Hsin:出错了',
                                            'Traceback (most recent call last):',
                                            '2026-10-27 01:52:20,500 INFO MainThread Hsin:xss opener entry'])
        self.assertIn('第 2 圈锁暝没放 R', summary)
        self.assertIn('Hsin__ShoreKeeper__Suoming v2.0.0', summary)
        self.assertIn('开打 2026-10-27 01:52:12 · 18.0s', summary)
        self.assertIn('F8 导出 1 次 1 张原图: 3.45~3.45s', summary)
        self.assertIn('共 3 行', summary)
        self.assertEqual((info['plugin_recorded'], info['team_ver'], info['size']), ('0.2.3', '2.0.0', [2560, 1440]))
        self.assertFalse([n for n in os.listdir(os.path.dirname(zip_path)) if n.endswith('.part')])

    def test_pack_without_exports_and_media(self):
        for p in media.media_paths(self.path):
            os.remove(p)
        plan = feedback.FeedbackPlan(self.path, self.dir)
        zip_path = feedback.build_feedback(plan, with_exports=False, log_path=self.log_path)
        entries, summary, _, info = self.read_zip(zip_path)
        self.assertFalse([n for n in entries if '/export/' in n or n.endswith('.media')])
        self.assertIn('(没填)', summary)
        self.assertIn('这局导出过 1 次, 打包时没带', summary)
        self.assertIn('整局画面: 这局没存', summary)
        self.assertEqual(info['frames'], 0)

    def test_log_files_across_midnight(self):
        import datetime
        older = os.path.join(self.dir, 'logs', 'ok-script.2026-10-26.log')
        with open(older, 'w', encoding='utf-8') as f:
            f.write('2026-10-26 23:59:50,000 INFO MainThread ok:昨天最后一行\n')
        wall = datetime.datetime(2026, 10, 27, 0, 0, 5).timestamp()
        self.assertEqual(feedback.log_files(self.log_path, wall, wall + 10), [older, self.log_path])
        self.assertEqual(len(feedback.log_excerpt(self.log_path, wall, wall + 10)), 1)

    def test_pack_with_video_and_open_pack(self):
        import zipfile
        video = os.path.join(self.dir, 'rec', 'Wuthering Waves 2026.10.27.DVR.mp4')
        os.makedirs(os.path.dirname(video))
        with open(video, 'wb') as f:
            f.write(b'\0' * 1000)
        with open(screenrec.video_info_path(self.path), 'w', encoding='utf-8') as f:
            json.dump({'path': video, 'offset': 7.5, 'duration': 38.6, 'method': '画面对时'}, f)
        plan = feedback.FeedbackPlan(self.path, self.dir)
        self.assertEqual((plan.video, plan.video_bytes), (video, 1000))
        entries, _, _, info = self.read_zip(feedback.build_feedback(plan, log_path=self.log_path))
        self.assertIsNone(info['video'])  # 默认不带录屏, 对时结果照带
        self.assertIn('axis_runs/20261027_015212_Hsin__ShoreKeeper__Suoming.video.json', entries)
        zip_path = feedback.build_feedback(plan, '锁暝没放 R\n第二行', log_path=self.log_path, with_video=True)
        entries, summary, _, info = self.read_zip(zip_path)
        self.assertEqual(entries['video/' + os.path.basename(video)].compress_type, zipfile.ZIP_STORED)
        self.assertEqual(info['video'], {'name': os.path.basename(video), 'bytes': 1000})
        self.assertIn('打开反馈包', summary)

        other = tempfile.mkdtemp(prefix='axis_rec_import_')
        self.addCleanup(shutil.rmtree, other, True)
        run_path = feedback.import_feedback(zip_path, other)
        root = os.path.join(other, 'imported', '20261027_015212_Hsin__ShoreKeeper__Suoming_反馈')
        self.assertEqual(run_path, os.path.join(root, 'axis_runs', '20261027_015212_Hsin__ShoreKeeper__Suoming.jsonl'))
        self.assertTrue(os.path.isfile(os.path.join(root, feedback.SUMMARY_NAME)))
        self.assertTrue(os.path.isfile(os.path.join(root, 'axis_runs', 'export', '20261027_015220_mine', 'info.json')))
        moved = screenrec.load_video_info(run_path)
        self.assertEqual((moved['path'], moved['orig_path']), (os.path.join(root, 'video', os.path.basename(video)), video))
        self.assertEqual(runs.list_runs(other), [])  # 不混进本地记录, 开新局时不会被删
        listed = runs.list_imported_runs(other)
        self.assertEqual([s.path for s in listed], [run_path])
        self.assertTrue(listed[0].imported)
        data = runs.RunData(run_path)
        self.assertTrue(runs.headline(data).startswith('反馈: 锁暝没放 R / 第二行'))
        self.assertTrue(any(line.startswith('反馈包: ') for line in runs.detail_lines(data)))
        self.assertEqual(runs.video_clock(data, 1.0), '录屏 00:08.50')
        self.assertEqual(len(media.MediaIndex(run_path).frames), 3)
        # 同一个包再开一次: 重新解, 不留 .part
        self.assertEqual(feedback.import_feedback(zip_path, other), run_path)
        self.assertEqual(os.listdir(os.path.join(other, 'imported')), [os.path.basename(root)])

    def test_open_pack_only_takes_feedback_files(self):
        import zipfile
        bad = os.path.join(self.dir, 'bad.zip')
        with zipfile.ZipFile(bad, 'w') as zf:
            for name in ('../evil.txt', 'axis_runs/../../evil2.txt', 'C:/evil3.txt', 'random.exe', 'video/run.exe',
                         'axis_runs/a/b/c.txt'):
                zf.writestr(name, 'x')
            zf.writestr('axis_runs/20261027_015212_Hsin.jsonl', '{"e":"start"}\n')
        other = tempfile.mkdtemp(prefix='axis_rec_import_')
        self.addCleanup(shutil.rmtree, other, True)
        run_path = feedback.import_feedback(bad, other)
        root = os.path.dirname(os.path.dirname(run_path))
        found = sorted(os.path.relpath(os.path.join(d, f), root) for d, _, files in os.walk(root) for f in files)
        self.assertEqual(found, [os.path.join('axis_runs', '20261027_015212_Hsin.jsonl')])
        self.assertEqual(sorted(os.listdir(other)), ['imported'])
        empty = os.path.join(self.dir, 'empty.zip')
        with zipfile.ZipFile(empty, 'w') as zf:
            zf.writestr('readme.txt', 'x')
        with self.assertRaises(ValueError):
            feedback.import_feedback(empty, other)

    def test_open_pack_keeps_last_ten(self):
        plan = feedback.FeedbackPlan(self.path, self.dir)
        zip_path = feedback.build_feedback(plan, log_path=self.log_path)
        other = tempfile.mkdtemp(prefix='axis_rec_import_')
        self.addCleanup(shutil.rmtree, other, True)
        for i in range(12):
            copy = os.path.join(self.dir, f'pack{i:02d}.zip')
            shutil.copy(zip_path, copy)
            feedback.import_feedback(copy, other)
            os.utime(os.path.join(other, 'imported', f'pack{i:02d}'), (1000 + i, 1000 + i))
        kept = sorted(os.listdir(os.path.join(other, 'imported')))
        self.assertEqual(kept, [f'pack{i:02d}' for i in range(2, 12)])


def task_tab_ticks():
    return sys.modules[TAB_CLASS.__module__].REFRESH_TICKS


class TestTab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_refresh_right_after_run_ends(self):
        directory = tempfile.mkdtemp(prefix='axis_rec_tick_')
        try:
            tab = TAB_CLASS(out_dir=directory)
            tab.timer.stop()
            recording = [True]
            active = Mock(isActiveWindow=Mock(return_value=True))
            with patch.object(tab, '_recording', side_effect=lambda: recording[0]),                     patch.object(tab, 'isVisible', return_value=True), patch.object(tab, 'window', return_value=active),                     patch.object(tab, 'refresh') as refresh:
                for _ in range(10):
                    tab._tick()
                refresh.assert_not_called()  # 正在录不读盘
                recording[0] = False
                tab._tick()
                refresh.assert_called_once()  # 刚收尾马上刷, 不等 3 秒
                for _ in range(task_tab_ticks() - 1):
                    tab._tick()
                self.assertEqual(refresh.call_count, 1)
                tab._tick()
                self.assertEqual(refresh.call_count, 2)  # 平时 3 秒一次
            tab.dispose()
            tab.close()
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_pack_feedback_button(self):
        directory = tempfile.mkdtemp(prefix='axis_rec_pack_')
        try:
            path, log_path = write_feedback_sample(directory)
            tab = TAB_CLASS(out_dir=directory)
            self.assertTrue(tab.pack_button.isEnabled())
            with patch.object(tab, '_recording', return_value=True), patch.object(tab, '_ask_feedback') as ask:
                tab._pack_feedback()
            ask.assert_not_called()
            with patch.object(tab, '_ask_feedback', return_value=(True, '站着不动', False, False)), \
                    patch.object(tab, '_reveal') as reveal, \
                    patch.object(feedback, 'default_log_path', return_value=log_path):
                tab._pack_feedback()
                self.assertEqual(tab.pack_button.text(), '打包中…')
                self.assertTrue(wait_for(lambda: self.app.processEvents() or tab.last_feedback, 10))
            reveal.assert_called_once_with(tab.last_feedback)
            self.assertEqual((tab.pack_button.text(), tab.pack_button.isEnabled()), ('打包反馈', True))
            import zipfile
            with zipfile.ZipFile(tab.last_feedback) as zf:
                self.assertIn('站着不动', zf.read(feedback.SUMMARY_NAME).decode('utf-8'))
                self.assertFalse([n for n in zf.namelist() if '/export/' in n])
            tab.dispose()
            tab.close()
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_tab_loads_and_links(self):
        directory = tempfile.mkdtemp(prefix='axis_rec_tab_')
        try:
            write_sample(directory)
            tab = TAB_CLASS(out_dir=directory)
            tab.resize(1200, 800)
            tab.show()
            self.app.processEvents()
            self.assertIsNotNone(tab.run)
            self.assertIn('Hsin__ShoreKeeper__Suoming v2.0.0', tab.summary.toolTip())
            self.assertEqual(tab.lap_box.currentData(), 2)  # 没选过: 默认每圈只上一次的人
            self.assertGreater(tab.model.rowCount(), 5)
            tab.lap_box.setCurrentIndex(tab.lap_box.findData(1))
            self.assertIn('9.00 / 7.00', tab.summary.text())
            self.assertEqual(tab.lap_table.rowCount(), 3)  # 两圈 + 中位数
            self.assertIn('可能是半圈', tab.lap_hint.text())  # 守岸人上场 3 次, 比锁暝 2 次多
            tab.pages.setCurrentIndex(1)
            tab._on_lap_cell(0, 2)
            self.assertAlmostEqual(tab.canvas.cursor_t, 4.0)
            image = tab.canvas.grab()
            self.assertFalse(image.isNull())
            tab._on_time_clicked(6.4)
            selected = tab.table.selectionModel().selectedRows()
            self.assertEqual(len(selected), 1)
            self.assertAlmostEqual(tab.model.rows[tab.proxy.mapToSource(selected[0]).row()][0], 6.5)
            tab.filter.setText('sample log')
            self.assertEqual(tab.proxy.rowCount(), 1)
            lane, item = tab.canvas._hit(type('P', (), {'x': lambda s: tab.canvas.x_at(0.5),
                                                       'y': lambda s: tab.canvas.lanes[2].y + 5})())
            self.assertIsNotNone(item)
            self.assertIn('R', tab.canvas._tooltip(lane, item))
            with open(os.path.join(directory, runs_prefs_name()), encoding='utf-8') as f:
                self.assertEqual(json.load(f)['Hsin__ShoreKeeper__Suoming'], 1)
            tab.dispose()
            tab.close()
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_clear_exports(self):
        directory = tempfile.mkdtemp(prefix='axis_rec_clear_')
        try:
            root = os.path.join(directory, media.EXPORT_DIR)
            for name, done in (('20261027_015220_a', True), ('20261027_015238_b', True), ('20261027_015250_c', False)):
                os.makedirs(os.path.join(root, name))
                with open(os.path.join(root, name, '0001.000s.png'), 'wb') as f:
                    f.write(b'x' * 3_000_000)
                if done:
                    with open(os.path.join(root, name, 'info.json'), 'w') as f:
                        f.write('{}')
            tab = TAB_CLASS(out_dir=directory)
            self.assertIn('2 次 · 6MB', tab.clear_button.text())
            with patch.object(tab, '_confirm', return_value=False):
                tab._clear_exports()
            self.assertEqual(len(os.listdir(root)), 3)
            with patch.object(tab, '_confirm', return_value=True) as confirm:
                tab._clear_exports()
            self.assertIn('2 次', confirm.call_args[0][1])
            # 没写完 info.json 的是正在导出的那次, 留着
            self.assertEqual(os.listdir(root), ['20261027_015250_c'])
            self.assertEqual((tab.clear_button.text(), tab.clear_button.isEnabled()), ('清空导出原图', False))
            tab.dispose()
            tab.close()
        finally:
            shutil.rmtree(directory, ignore_errors=True)


class TestFramePanel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_frames_boxes_and_crop(self):
        directory = tempfile.mkdtemp(prefix='axis_rec_tab_')
        find = {'e': 'read', 't': 6.6, 't2': 7.5, 'n': 5, 'k': 'find', 'name': 'hsin_lib2', 'v': 0.88, 'ok': True,
                'box': [2000, 1200, 200, 100], 'rb': [2050, 1220, 60, 40], 'src': 'Hsin.py:30 lib'}
        try:
            path = write_sample(directory, extra=[find])
            crop = {'t': 6.6, 'k': 'find', 'name': 'hsin_lib2', 'src': 'Hsin.py:30 lib', 'box': [2000, 1200, 200, 100],
                    'v': 0.88, 'ok': True}
            write_media(path, [i * 0.25 for i in range(33)] + [10.0], [(crop, (100, 200))])
            tab = TAB_CLASS(out_dir=directory)
            tab.resize(1400, 800)
            tab.show()
            self.app.processEvents()
            panel = tab.frame_panel
            self.assertEqual(len(tab.media.frames), 34)  # 指到文件外那条不算
            tab._on_time_clicked(6.7)
            self.assertEqual(panel.index, 26)
            self.assertFalse(panel.view.image.isNull())
            self.assertEqual(len(panel.view.boxes), 2)
            self.assertIn('6.50s', panel.caption.text())
            self.assertNotIn('没取画面', panel.caption.text())
            tab._on_time_clicked(9.0)  # 8.0 到 10.0 之间轴没取帧
            self.assertIn('之后 2.0s 轴没取画面', panel.caption.text())
            tab._on_time_clicked(6.7)
            panel.step(1)
            self.assertEqual(panel.index, 27)
            self.assertAlmostEqual(tab.canvas.cursor_t, 6.75)
            # 按住红线拖: 画面跟着走, 松手才选表格那一行
            from PySide6.QtCore import QEvent, QPointF, Qt
            from PySide6.QtGui import QMouseEvent

            def mouse(kind, t, buttons):
                point = QPointF(tab.canvas.x_at(t), tab.canvas.lanes[0].y + 5)
                button = Qt.NoButton if kind == QEvent.MouseMove else Qt.LeftButton
                return QMouseEvent(kind, point, point, button, buttons, Qt.NoModifier)

            tab.canvas.mousePressEvent(mouse(QEvent.MouseButtonPress, 2.0, Qt.LeftButton))
            tab.table.clearSelection()
            tab.canvas.mouseMoveEvent(mouse(QEvent.MouseMove, 5.1, Qt.LeftButton))
            self.assertAlmostEqual(tab.canvas.cursor_t, 5.1, places=2)
            self.assertEqual(panel.index, 20)
            self.assertEqual(tab.table.selectionModel().selectedRows(), [])
            tab.canvas.mouseReleaseEvent(mouse(QEvent.MouseButtonRelease, 5.1, Qt.NoButton))
            selected = tab.table.selectionModel().selectedRows()
            self.assertAlmostEqual(tab.model.rows[tab.proxy.mapToSource(selected[0]).row()][0], 5.2)
            self.assertFalse(tab.canvas._dragging)
            row = next(i for i, e in enumerate(tab.model.events) if e.get('name') == 'hsin_lib2')
            tab._on_row_clicked(tab.proxy.mapFromSource(tab.model.index(row, 0)))
            self.assertTrue(panel.crop_view.isVisible())
            self.assertIn('原图 200×100', panel.crop_text.text())
            self.assertFalse(panel.grab().isNull())
            # 没存画面的局
            other = write_sample(directory, name='20261026_180000_Hsin__ShoreKeeper__Suoming.jsonl')
            tab.load(other)
            self.assertFalse(panel.has_frames())
            self.assertIn('没存画面', panel.view.message)
            tab.dispose()
            tab.close()
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_video_caption_and_late_sidecar(self):
        directory = tempfile.mkdtemp(prefix='axis_rec_tab_')
        try:
            path = os.path.join(directory, '20261027_120000_手打_Hsin__ShoreKeeper__Suoming.jsonl')
            with open(path, 'w', encoding='utf-8') as f:
                header = dict(SAMPLE[0], mode='manual', rec={'tool': 'nvidia', 'mode': 'replay', 'keys': 'alt+f10'})
                for event in [header] + SAMPLE[1:]:
                    f.write(json.dumps(event, ensure_ascii=False) + '\n')
            write_media(path, [i * 0.25 for i in range(33)])
            tab = TAB_CLASS(out_dir=directory)
            tab.show()
            self.app.processEvents()
            self.assertIn('录屏对时中', tab.summary.text())
            self.assertNotIn('录屏 ', tab.frame_panel.caption.text())
            with open(screenrec.video_info_path(path), 'w', encoding='utf-8') as f:
                json.dump({'path': 'E:/录屏/clip.mp4', 'offset': 10.0, 'duration': 300.0, 'method': '画面对时'}, f)
            tab._on_time_clicked(6.7)
            tab.refresh()
            self.assertIn('录屏 +10.00s(画面对时)', tab.summary.text())
            self.assertIn('录屏 00:16.70', tab.frame_panel.caption.text())
            tab.dispose()
            tab.close()
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    def test_toolbar_rows_do_not_overlap_and_open_pack(self):
        directory = tempfile.mkdtemp(prefix='axis_rec_tab_')
        try:
            path = write_sample(directory)
            plan = feedback.FeedbackPlan(path, directory)
            zip_path = feedback.build_feedback(plan, '第 2 圈锁暝没放 R', log_path=os.path.join(directory, 'none.log'))
            tab = TAB_CLASS(out_dir=directory)
            tab.resize(1100, 700)
            tab.show()
            self.app.processEvents()
            refresh = next(b for b in tab.findChildren(type(tab.import_button)) if b.text() == '刷新')
            folder = next(b for b in tab.findChildren(type(tab.import_button)) if b.text() == '打开文件夹')
            for row in ((tab.run_box, tab.follow, refresh, tab.import_button),
                        (tab.lap_box, tab.zoom, tab.pack_button, folder, tab.clear_button)):
                for a, b in zip(row, row[1:]):
                    self.assertLess(a.geometry().right(), b.geometry().left(), (a, b))
            with patch('PySide6.QtWidgets.QFileDialog.getOpenFileName', return_value=(zip_path, '')):
                tab._open_feedback()
                end = time.time() + 5
                while tab._unpacking and time.time() < end:
                    self.app.processEvents()
                    time.sleep(0.01)
            self.assertFalse(tab._unpacking)
            self.assertIn(os.sep + 'imported' + os.sep, tab.run.path)
            self.assertFalse(tab.follow.isChecked())
            self.assertIn('[导入]', tab.run_box.currentText())
            self.assertTrue(tab.summary.text().startswith('反馈: 第 2 圈锁暝没放 R'))
            self.assertEqual(len(tab.summaries), 2)
            tab.dispose()
            tab.close()
        finally:
            shutil.rmtree(directory, ignore_errors=True)


def runs_prefs_name():
    return sys.modules[TAB_CLASS.__module__].PREFS_FILE


if __name__ == '__main__':
    with patch('ok.task.TaskExecutor.TaskExecutor.init_default_ocr', lambda self: None):
        unittest.main(verbosity=2)
