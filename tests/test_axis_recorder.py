"""在 ok-ww 源码目录下运行: python <本文件> <ok-ww 源码目录> <.okscript 路径>"""
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
                             interaction=self.interaction)
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
        with patch.object(media, 'FRAME_INTERVAL', 0.0), patch.object(media, 'RING_MEMORY_CAP', 3 * 108 * 192 * 4):
            self.start()
            self.feed([self.screen(v, 108, 192) for v in range(6)])
            self.assertEqual([int(f[0, 0, 0]) for _, f in self.recorder.sink.recent()], [3, 4, 5])
            self.finish()

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


class TestTab(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_pack_feedback_button(self):
        directory = tempfile.mkdtemp(prefix='axis_rec_pack_')
        try:
            path, log_path = write_feedback_sample(directory)
            tab = TAB_CLASS(out_dir=directory)
            self.assertTrue(tab.pack_button.isEnabled())
            with patch.object(tab, '_recording', return_value=True), patch.object(tab, '_ask_feedback') as ask:
                tab._pack_feedback()
            ask.assert_not_called()
            with patch.object(tab, '_ask_feedback', return_value=(True, '站着不动', False)), \
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


def runs_prefs_name():
    return sys.modules[TAB_CLASS.__module__].PREFS_FILE


if __name__ == '__main__':
    with patch('ok.task.TaskExecutor.TaskExecutor.init_default_ocr', lambda self: None):
        unittest.main(verbosity=2)
