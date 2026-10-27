"""采集层: 给框架挂补丁, 把战斗中的按键、在场角色、读屏、日志按局写成 JSONL。

热路径(执行器线程)只做三件事: 看有没有在录、找调用位置、往队列里塞一个元组。
格式化、合并重复读数、写盘都在后台线程。补丁里出任何错都吞掉, 原函数照常执行、照常返回。
"""
import ctypes
import inspect
import json
import logging
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path

from ok import Logger

from .media import (HOTKEYS, RING_IDLE_KEEP, FrameSink, HotkeyThread, MediaWriter, export_frames, prune_media,
                    ring_limit_note)

logger = Logger.get_logger(__name__)

FORMAT_VERSION = 1

ORIGINAL_ATTR = '_axis_rec_original'
OWNED_ATTR = '_axis_rec_owned'
# 挂在 ExecutorOperation 类上: 插件热更新会重新导入本模块, 新旧两份模块都认这一个
HOLDER_ATTR = '_axis_recorder'
MARK_ATTR = 'axis_mark'

RUN_FILE_RE = re.compile(r'^\d{8}_\d{6}_.*\.jsonl$')
MIN_KEEP_RUNS = 5
DEFAULT_FRAME_RUNS = 5

# 交互后端(PostMessage/SendInput/...)上真正发键的方法: 名字 -> (动作, 固定按钮)
INPUT_METHODS = {
    'send_key': ('key', None),
    'send_key_down': ('kd', None),
    'send_key_up': ('ku', None),
    'click': ('click', None),
    'middle_click': ('click', 'middle'),
    'right_click': ('click', 'right'),
    'mouse_down': ('md', None),
    'mouse_up': ('mu', None),
}

# 读数合并: 同一调用位置连续读到「差不多」的值就记成一段, 隔太久或变了才断开
GROUP_GAP = 0.5
GROUP_IDLE_FLUSH = 1.0
GROUP_TOLERANCE = {'color': 0.03, 'con': 0.02}

ORIGIN_MAX_WALK = 24
WRITER_YIELD_EVERY = 16

_START, _END, _INPUT, _SENDINPUT, _FIELD, _PERF, _READ, _LOG, _MARK = range(9)
_STOP = object()

MOUSE_FLAGS = {
    0x0002: ('md', 'left'), 0x0004: ('mu', 'left'),
    0x0008: ('md', 'right'), 0x0010: ('mu', 'right'),
    0x0020: ('md', 'middle'), 0x0040: ('mu', 'middle'),
}
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
VK_NAMES = {0x20: 'space', 0x10: 'shift', 0xA0: 'lshift', 0xA1: 'rshift', 0x11: 'ctrl', 0x12: 'alt',
            0x1B: 'esc', 0x09: 'tab', 0x0D: 'enter'}


def default_out_dir():
    return os.path.join(os.getcwd(), 'logs', 'axis_runs')


def list_run_files(out_dir):
    try:
        names = [name for name in os.listdir(out_dir) if RUN_FILE_RE.match(name)]
    except OSError:
        return []
    return [os.path.join(out_dir, name) for name in sorted(names)]


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


def _box(box):
    """Box -> (x, y, w, h); 框的名字原样; 其它 None。热路径上就取出来, 框架之后可能改 Box 的属性。"""
    if box is None:
        return None
    if isinstance(box, str):
        return box
    try:
        return box.x, box.y, box.width, box.height
    except AttributeError:
        return None


def _param_pos(function, name):
    """name 在位置参数里的下标(不算 self), 和它的默认值。"""
    try:
        params = list(inspect.signature(function).parameters.values())[1:]
    except (TypeError, ValueError):
        return None, None
    for i, param in enumerate(params):
        if param.name == name:
            default = None if param.default is inspect.Parameter.empty else param.default
            return i, default
    return None, None


def _arg(args, kwargs, name, pos, default):
    if name in kwargs:
        return kwargs[name]
    if pos is not None and pos < len(args):
        return args[pos]
    return default


def _input_bytes(args):
    """SendInput(n, 指针, 结构大小) 的 INPUT 数组原样拷出来。调用一返回调用方的结构就可能被回收。"""
    if len(args) < 3:
        return None
    count, pointer, size = int(args[0]), args[1], int(args[2])
    if not 0 < count <= 16 or not 0 < size <= 64:
        return None
    obj = getattr(pointer, '_obj', None)  # ctypes.byref(...)
    if obj is not None:
        address = ctypes.addressof(obj)
    elif isinstance(pointer, ctypes._Pointer):
        address = ctypes.cast(pointer, ctypes.c_void_p).value
    elif isinstance(pointer, (ctypes.Structure, ctypes.Array)):
        address = ctypes.addressof(pointer)
    else:
        return None
    if not address:
        return None
    return ctypes.string_at(address, count * size), size


def decode_send_input(raw, size):
    """INPUT 数组 -> [(动作, 按键)]。只认鼠标按下/抬起和键盘, 纯移动不记。"""
    union_offset = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 4
    events = []
    for start in range(0, len(raw) - size + 1, size):
        chunk = raw[start:start + size]
        kind = int.from_bytes(chunk[0:4], 'little')
        body = chunk[union_offset:]
        if kind == 0 and len(body) >= 16:
            flags = int.from_bytes(body[12:16], 'little')
            for bit, event in MOUSE_FLAGS.items():
                if flags & bit:
                    events.append(event)
            if flags & 0x0800:
                events.append(('wheel', 'wheel'))
        elif kind == 1 and len(body) >= 8:
            vk = int.from_bytes(body[0:2], 'little')
            scan = int.from_bytes(body[2:4], 'little')
            flags = int.from_bytes(body[4:8], 'little')
            action = 'ku' if flags & KEYEVENTF_KEYUP else 'kd'
            if vk == 0 and flags & KEYEVENTF_SCANCODE:
                key = f'sc{scan:02x}'
            elif 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
                key = chr(vk).lower()
            else:
                key = VK_NAMES.get(vk, f'vk{vk:02x}')
            events.append((action, key))
    return events


def _wrap(owner, name, make_wrapper):
    """幂等地给 owner.name 套一层: 重装时总是从最初的原函数重新包, 不会越叠越厚。"""
    current = getattr(owner, name)
    original = getattr(current, ORIGINAL_ATTR, current)
    owned = getattr(current, OWNED_ATTR, name in owner.__dict__)
    wrapper = make_wrapper(original)
    setattr(wrapper, ORIGINAL_ATTR, original)
    setattr(wrapper, OWNED_ATTR, owned)
    setattr(owner, name, wrapper)


def _unwrap(owner, name):
    current = owner.__dict__.get(name)
    if current is None or not hasattr(current, ORIGINAL_ATTR):
        return
    if getattr(current, OWNED_ATTR, True):
        setattr(owner, name, getattr(current, ORIGINAL_ATTR))
    else:
        delattr(owner, name)


class _SendInputTap:
    """替换 ctypes.windll.user32.SendInput: 照常转发, 顺手记下发了什么。

    argtypes/restype 这类属性读写都转给原函数, 别处设置它们不受影响。
    """

    def __init__(self, recorder, original):
        object.__setattr__(self, '_recorder', recorder)
        object.__setattr__(self, '_original', original)

    def __call__(self, *args):
        t = time.perf_counter()
        result = self._original(*args)
        recorder = self._recorder
        run = recorder.run
        if run is not None:
            try:
                copied = _input_bytes(args)
                if copied is not None:
                    recorder.put_send_input(run, t, copied, sys._getframe(1))
            except Exception:
                pass
        return result

    def __getattr__(self, name):
        return getattr(self._original, name)

    def __setattr__(self, name, value):
        setattr(self._original, name, value)


class _LogTap(logging.Handler):
    def __init__(self, recorder):
        super().__init__(logging.DEBUG)
        self.recorder = recorder

    def emit(self, record):
        recorder = self.recorder
        run = recorder.run
        if run is None or not run.logs:
            return
        try:
            recorder.queue.put((_LOG, run, record.created, record.levelno, record.getMessage()))
        except Exception:
            pass


class Run:
    """一局。只在执行器线程里创建和结束, 后台线程只读它的时间基准。"""

    def __init__(self, task, reads, logs):
        self.task = task
        self.t0 = time.perf_counter()
        self.wall0 = time.time()
        self.thread = threading.get_ident()
        self.reads = reads
        self.logs = logs
        self.field = None
        self.field_seen = None
        self.path = None
        self.team_dir = None


class _Group:
    __slots__ = ('event', 'last_t', 'first_value', 'seen_at')

    def __init__(self, event, t, value):
        self.event = event
        self.last_t = t
        self.first_value = value
        self.seen_at = time.monotonic()


class _RunFile:
    def __init__(self, path, handle):
        self.path = path
        self.handle = handle
        self.groups = {}
        self.dirty = False


class Recorder:
    def __init__(self, out_dir=None, plugin_version=''):
        self.out_dir = out_dir or default_out_dir()
        self.plugin_version = plugin_version
        self.owner = None
        self.run = None
        self.queue = queue.SimpleQueue()
        self.local = threading.local()
        self.last_path = None
        self._patched = []
        self._send_input_patched = False
        self._log_tap = None
        self._kinds = {}
        self._labels = {}
        self._paths = None
        self._writer = None
        self._auto_combat = None
        self._interaction_base = None
        self._error_logged_at = 0.0
        # 画面: 正在录的这局、刚打完那局(热键还能导出)、后台编码线程、热键线程
        self.sink = None
        self.idle_sink = None
        self.idle_sink_until = 0.0
        self.media = None
        self.hotkey = None
        self.last_export = None
        self._exporting = False

    # ---------- 安装 / 卸载 ----------

    def install(self):
        from ok.device.interaction_methods.base import BaseInteraction
        from ok.task.task import ExecutorOperation, FindFeature
        from src.char.BaseChar import BaseChar
        from src.combat.CombatCheck import CombatCheck
        from src.task.AutoCombatTask import AutoCombatTask
        from src.task.BaseCombatTask import BaseCombatTask
        from src.task.BaseWWTask import BaseWWTask

        old = ExecutorOperation.__dict__.get(HOLDER_ATTR)
        if old is not None and old is not self:
            try:
                old.uninstall()
            except Exception as e:
                logger.error('跑轴记录: 卸旧版补丁出错', e)

        self._paths = self._compute_paths()
        self._auto_combat = AutoCombatTask
        self._interaction_base = BaseInteraction

        self._patch(FindFeature, 'find_one', self._read_wrapper('find', _extract_find_one))
        self._patch(FindFeature, 'find_feature', self._read_wrapper('feat', _extract_find_feature))
        self._patch(ExecutorOperation, 'calculate_color_percentage', self._read_wrapper('color', _extract_color))
        self._patch(BaseCombatTask, 'get_cd', self._read_wrapper('cd', _extract_cd))
        self._patch(BaseCombatTask, 'get_current_con', self._read_wrapper('con', _extract_con))
        self._patch(BaseWWTask, 'in_team', self._in_team_wrapper)
        self._patch(BaseChar, 'perform', self._perform_wrapper)
        self._patch(CombatCheck, 'in_combat', self._in_combat_wrapper)
        self._patch(CombatCheck, 'reset_to_false', self._reset_wrapper)
        for cls in self._interaction_classes(BaseInteraction):
            self._patch_interaction(cls)
        setattr(ExecutorOperation, MARK_ATTR, self._mark_method())
        self._patch_send_input()
        self._log_tap = _LogTap(self)
        logging.getLogger('ok').addHandler(self._log_tap)
        self._patch_frames()

        self._writer = threading.Thread(target=self._writer_loop, name='axis_recorder_writer', daemon=True)
        self._writer.start()
        setattr(ExecutorOperation, HOLDER_ATTR, self)

    def uninstall(self, reason='插件卸载'):
        from ok.task.task import ExecutorOperation
        if self.run is not None:
            self.end_run(reason)
        self.idle_sink = None
        if self.hotkey is not None:
            self.hotkey.stop()
            self.hotkey = None
        if self.media is not None:
            self.media.stop()
            self.media = None
        for owner, name in reversed(self._patched):
            try:
                _unwrap(owner, name)
            except Exception as e:
                logger.error(f'跑轴记录: 卸 {owner.__name__}.{name} 出错', e)
        self._patched.clear()
        mark = ExecutorOperation.__dict__.get(MARK_ATTR)
        if mark is not None and getattr(mark, '_axis_rec_owner', None) is self:
            delattr(ExecutorOperation, MARK_ATTR)
        self._unpatch_send_input()
        if self._log_tap is not None:
            logging.getLogger('ok').removeHandler(self._log_tap)
            self._log_tap = None
        if self._writer is not None:
            self.queue.put(_STOP)
            self._writer.join(timeout=3)
            self._writer = None
        if ExecutorOperation.__dict__.get(HOLDER_ATTR) is self:
            setattr(ExecutorOperation, HOLDER_ATTR, None)

    def _patch(self, owner, name, make_wrapper):
        _wrap(owner, name, make_wrapper)
        self._patched.append((owner, name))

    def _patch_frames(self):
        """存画面挂在执行器取帧上。上游改了取帧函数就只是没有画面, 按键读屏照记。"""
        try:
            from ok.task.TaskExecutor import TaskExecutor
            if not callable(getattr(TaskExecutor, 'next_frame', None)):
                raise AttributeError('TaskExecutor.next_frame')
            self._patch(TaskExecutor, 'next_frame', self._next_frame_wrapper)
        except Exception as e:
            logger.error('跑轴记录: 挂不上取帧, 这一版不存画面', e)
            return
        self.media = MediaWriter(self._label)
        self.media.start()
        self.hotkey = HotkeyThread(lambda: self._option('HOTKEY_KEY', 'F8'), self.export_recent)
        self.hotkey.start()

    def _option(self, attr, default):
        owner = self.owner
        key = getattr(owner, attr, None) if owner is not None else None
        if not key:
            return default
        try:
            return owner.config.get(key, default)
        except Exception:
            return default

    @staticmethod
    def _interaction_classes(base):
        found, pending = [], [base]
        while pending:
            cls = pending.pop()
            if cls not in found:
                found.append(cls)
                pending.extend(cls.__subclasses__())
        return found

    def _patch_interaction(self, cls):
        for name, (action, button) in INPUT_METHODS.items():
            if name in cls.__dict__ and (cls, name) not in self._patched:
                self._patch(cls, name, self._input_wrapper(action, button))

    def ensure_interaction_patched(self, interaction):
        """执行器用的交互后端类可能在装补丁之后才导入, 开局时补一次。"""
        for cls in type(interaction).__mro__:
            if self._interaction_base is not None and isinstance(cls, type) \
                    and issubclass(cls, self._interaction_base):
                self._patch_interaction(cls)

    def _patch_send_input(self):
        try:
            user32 = ctypes.windll.user32
            current = user32.SendInput
        except (AttributeError, OSError):
            return
        if isinstance(current, _SendInputTap) or type(current).__name__ == '_SendInputTap':
            current = object.__getattribute__(current, '_original')
        user32.SendInput = _SendInputTap(self, current)
        self._send_input_patched = True

    def _unpatch_send_input(self):
        if not self._send_input_patched:
            return
        try:
            user32 = ctypes.windll.user32
            current = user32.__dict__.get('SendInput')
            if current is not None and type(current).__name__ == '_SendInputTap' \
                    and object.__getattribute__(current, '_recorder') is self:
                user32.SendInput = object.__getattribute__(current, '_original')
        except Exception as e:
            logger.error('跑轴记录: 还原 SendInput 出错', e)
        self._send_input_patched = False

    def _compute_paths(self):
        import ok
        import src
        src_root = _norm(list(src.__path__)[0]) + os.sep
        return {
            'ok': _norm(os.path.dirname(ok.__file__)) + os.sep,
            'src': src_root,
            'char': src_root + 'char' + os.sep,
            'plumb': src_root + 'combat' + os.sep,
            'scene': src_root + 'scene' + os.sep,
            'plugin': _norm(os.path.dirname(__file__)) + os.sep,
            'stdlib': _norm(os.path.dirname(os.__file__)) + os.sep,
        }

    # ---------- 调用位置 ----------

    def classify(self, filename):
        """ok: 框架库; plumb: 判脱战/找怪/场景缓存; fw: 其它框架代码; base: 角色基类; char: 角色和队伍代码。"""
        if filename.startswith('<'):
            return 'skip'
        path = _norm(filename)
        paths = self._paths
        if path.startswith(paths['plugin']):
            return 'skip'
        if path.startswith(paths['ok']):
            return 'ok'
        if path.startswith(paths['char']):
            base = os.path.basename(path)
            return 'base' if base in ('basechar.py', 'charfactory.py', '__init__.py') else 'char'
        if path.startswith(paths['plumb']) or path.startswith(paths['scene']):
            return 'plumb'
        if path.startswith(paths['src']):
            return 'fw'
        if 'custom_teams' in path:
            return 'char'
        if path.startswith(paths['stdlib']):
            return 'skip'
        return 'other'

    def origin(self, frame, for_read):
        """调用位置 (代码1, 行1, 代码2, 行2, 经由代码, 经由行), 没有的位置是 None。

        代码1 是最里层的角色/队伍代码, 代码2 是再往上一层的角色/队伍代码(谁调的这个辅助函数),
        经由是两者之间最里层的框架函数(比如 available、is_con_full、target_enemy)。
        读屏如果是判脱战/找怪发起的, 或者压根不在角色代码里, 返回 None 不记。
        """
        kinds = self._kinds
        via = char = None
        depth = 0
        while frame is not None and depth < ORIGIN_MAX_WALK:
            code = frame.f_code
            # 按文件名缓存: 两个文件里一模一样的函数(队伍代码互相复制的辅助函数), 代码对象会判相等
            filename = code.co_filename
            kind = kinds.get(filename)
            if kind is None:
                kind = kinds[filename] = self.classify(filename)
            if kind == 'char' or kind == 'other':
                if char is not None:
                    return char + (code, frame.f_lineno) + (via or (None, None))
                char = (code, frame.f_lineno)
            elif kind != 'ok' and kind != 'skip' and char is None:
                if kind == 'plumb' and for_read:
                    return None
                if via is None:
                    via = (code, frame.f_lineno)
            frame = frame.f_back
            depth += 1
        if char is not None:
            return char + (None, None) + (via or (None, None))
        if for_read or via is None:
            return None
        return (None, None, None, None) + via

    # ---------- 一局的开始 / 结束 ----------

    def should_record(self, task):
        owner = self.owner
        if owner is None or not owner.enabled:
            return False
        if isinstance(task, self._auto_combat):
            return True
        return bool(owner.config.get(owner.ALL_TASKS_KEY, False))

    def observe_combat(self, task):
        inside = bool(getattr(task, '_in_combat', False))
        run = self.run
        if inside:
            if run is None and self.should_record(task):
                self.start_run(task)
            elif run is not None and run.task is task and not self.should_record(task):
                self.end_run('记录已关闭')
        elif run is not None and run.task is task:
            self.end_run(getattr(task, 'out_of_combat_reason', '') or 'in_combat False')

    def start_run(self, task):
        owner = self.owner
        config = owner.config if owner is not None else {}
        run = Run(task, reads=bool(config.get(getattr(owner, 'READS_KEY', ''), True)),
                  logs=bool(config.get(getattr(owner, 'LOGS_KEY', ''), True)))
        try:
            self.ensure_interaction_patched(task.executor.interaction)
        except Exception:
            pass
        self.queue.put((_START, run, self._header(task, run)))
        self.idle_sink = None
        try:
            self.sink = self._new_sink(run)
        except Exception as e:
            self.sink = None
            self._log_error('跑轴记录: 这局不存画面', e)
        self.run = run
        try:
            cached = task.scene._in_team
            if cached is not None:
                self._note_field(run, cached)
        except Exception:
            pass

    def end_run(self, reason):
        run = self.run
        if run is None:
            return
        self.run = None
        self.queue.put((_END, run, time.perf_counter(), str(reason), run.field_seen))
        sink = self.sink
        if sink is not None:
            self.sink = None
            sink.ended = True
            if sink.ring:
                self.idle_sink, self.idle_sink_until = sink, time.monotonic() + RING_IDLE_KEEP
            if self.media is not None:
                self.media.wake.set()

    def _new_sink(self, run):
        if self.media is None:
            return None
        save = bool(self._option('FRAMES_KEY', True))
        crops = bool(self._option('CROPS_KEY', True)) and run.reads
        seconds = 0.0
        if self._option('HOTKEY_KEY', 'F8') in HOTKEYS:
            try:
                seconds = float(self._option('EXPORT_SECONDS_KEY', 4))
            except (TypeError, ValueError):
                seconds = 4.0
        if not (save or crops or seconds > 0):
            return None
        return FrameSink(run, save, crops, seconds)

    # ---------- 热键导出 ----------

    def export_recent(self):
        """热键线程调: 把最近几秒原图交给另一个线程存 PNG。返回那个线程(测试用), 没东西可导返回 None。"""
        sink = self.sink
        if sink is None and time.monotonic() < self.idle_sink_until:
            sink = self.idle_sink
        frames = sink.recent() if sink is not None else []
        if not frames:
            self._notify('跑轴记录: 没有可导出的画面。只在记录中的那一局和打完后 '
                         f'{RING_IDLE_KEEP:g} 秒内留着最近几秒原图')
            return None
        if self._exporting:
            return None
        self._exporting = True
        thread = threading.Thread(target=self._export, args=(sink, frames), name='axis_recorder_export',
                                  daemon=True)
        thread.start()
        return thread

    def _export(self, sink, frames):
        try:
            folder, files = export_frames(self.out_dir, sink, frames, sink.run.team_dir)
            self.last_export = folder
            if files:
                note = ring_limit_note(sink)
                self._notify(f"跑轴记录: 导出 {len(files)} 张原图({files[0]['t']:.2f}~{files[-1]['t']:.2f}s)到 {folder}"
                             + (f'。{note}' if note else ''))
        except Exception as e:
            logger.error('跑轴记录: 导出出错', e)
        finally:
            self._exporting = False

    def _notify(self, message):
        logger.info(message)
        owner = self.owner
        try:
            if owner is not None:
                owner.notification(message, tray=True)
        except Exception:
            pass

    def _header(self, task, run):
        chars = []
        for char in getattr(task, 'chars', None) or ():
            if char is None:
                chars.append(None)
                continue
            # 认不出的角色框架给通用 BaseChar, 这时类名没信息量, 用模板名
            cls_name = type(char).__name__
            chars.append({
                'i': getattr(char, 'index', -1),
                'n': cls_name if cls_name != 'BaseChar' else getattr(char, 'char_name', None) or cls_name,
                'char': getattr(char, 'char_name', None),
                'cls': type(char).__name__,
                'conf': _round(getattr(char, 'confidence', 0)),
                'file': _class_file(type(char)),
            })
        keys = {}
        try:
            keys = {str(k): str(v) for k, v in dict(task.key_config).items()}
        except Exception:
            pass
        size = None
        try:
            size = [int(task.width), int(task.height)]
        except Exception:
            pass
        return {'e': 'start', 'v': FORMAT_VERSION, 'wall': run.wall0, 'task': type(task).__name__,
                'team': chars, 'keys': keys, 'size': size, 'plugin': self.plugin_version,
                'reads': run.reads, 'logs': run.logs}

    # ---------- 热路径 ----------

    def _thread_tag(self, run):
        return None if threading.get_ident() == run.thread else threading.current_thread().name

    def put_send_input(self, run, t, copied, frame):
        self.queue.put((_SENDINPUT, run, t, copied, self.origin(frame, False), self._thread_tag(run)))

    def _note_field(self, run, result):
        # 状态没变只记「最后一次看到」的时刻: 两次变化之间轴可能很久没看 HUD, 页面上要分得出来
        state = (bool(result[0]), int(result[1]), int(result[2]))
        now = time.perf_counter()
        if state != run.field:
            self.queue.put((_FIELD, run, now, state, run.field_seen))
            run.field = state
        run.field_seen = now

    def _input_wrapper(self, action, button):
        recorder = self
        local = self.local

        def make(original):
            key_pos, key_default = _param_pos(original, 'key')
            dt_pos, _ = _param_pos(original, 'down_time')
            fixed = button

            def wrapper(interaction, *args, **kwargs):
                run = recorder.run
                if run is None or getattr(local, 'inp', 0):
                    return original(interaction, *args, **kwargs)
                local.inp = 1
                t = time.perf_counter()
                try:
                    return original(interaction, *args, **kwargs)
                finally:
                    local.inp = 0
                    try:
                        if action in ('key', 'kd', 'ku'):
                            key = _arg(args, kwargs, 'key', key_pos, None)
                        else:
                            key = fixed or _arg(args, kwargs, 'key', key_pos, key_default or 'left')
                        down_time = _arg(args, kwargs, 'down_time', dt_pos, None)
                        recorder.queue.put((_INPUT, run, t, action, key, down_time,
                                            recorder.origin(sys._getframe(1), False), recorder._thread_tag(run)))
                    except Exception:
                        pass

            return wrapper

        return make

    def _read_wrapper(self, kind, extract):
        recorder = self
        local = self.local

        def make(original):
            def wrapper(task, *args, **kwargs):
                run = recorder.run
                if run is None or not run.reads or getattr(local, 'rd', 0):
                    return original(task, *args, **kwargs)
                local.rd = 1
                t = time.perf_counter()
                try:
                    result = original(task, *args, **kwargs)
                finally:
                    local.rd = 0
                try:
                    where = recorder.origin(sys._getframe(1), True)
                    if where is not None:
                        info = extract(args, kwargs, result)
                        if info[1] is None and (kind == 'find' or kind == 'feat'):
                            box = _feature_search_box(task, info[0], args, kwargs)
                            if box is not None:
                                info = (info[0], box) + info[2:]
                        recorder.queue.put((_READ, run, t, kind) + info + (where,))
                        sink = recorder.sink
                        if sink is not None and sink.crops:
                            sink.maybe_crop(task, t, kind, info, where, kwargs, recorder.media)
                except Exception:
                    pass
                return result

            return wrapper

        return make

    def _in_team_wrapper(self, original):
        recorder = self
        local = self.local

        def in_team(task, *args, **kwargs):
            run = recorder.run
            if run is None:
                return original(task, *args, **kwargs)
            # 里面那三次 find_one 是认 HUD, 不算轴的读屏
            local.rd = getattr(local, 'rd', 0) + 1
            try:
                result = original(task, *args, **kwargs)
            finally:
                local.rd -= 1
            try:
                recorder._note_field(run, result)
            except Exception:
                pass
            return result

        return in_team

    def _perform_wrapper(self, original):
        recorder = self

        def perform(char, *args, **kwargs):
            run = recorder.run
            if run is None:
                return original(char, *args, **kwargs)
            name = getattr(char, 'char_name', None)
            index = getattr(char, 'index', -1)
            recorder.queue.put((_PERF, run, time.perf_counter(), name, index, 'in', None))
            failed = None
            try:
                return original(char, *args, **kwargs)
            except BaseException as e:
                failed = type(e).__name__
                raise
            finally:
                recorder.queue.put((_PERF, run, time.perf_counter(), name, index, 'out', failed))

        return perform

    def _in_combat_wrapper(self, original):
        recorder = self

        def in_combat(task, *args, **kwargs):
            result = original(task, *args, **kwargs)
            try:
                recorder.observe_combat(task)
            except Exception as e:
                recorder._log_error('跑轴记录: 判开局出错', e)
            return result

        return in_combat

    def _reset_wrapper(self, original):
        recorder = self

        def reset_to_false(task, *args, **kwargs):
            result = original(task, *args, **kwargs)
            try:
                run = recorder.run
                if run is not None and run.task is task:
                    recorder.end_run(getattr(task, 'out_of_combat_reason', '') or 'reset_to_false')
            except Exception as e:
                recorder._log_error('跑轴记录: 判结束出错', e)
            return result

        return reset_to_false

    def _next_frame_wrapper(self, original):
        recorder = self

        def next_frame(executor, *args, **kwargs):
            frame = original(executor, *args, **kwargs)
            sink = recorder.sink
            if sink is not None and frame is not None:
                try:
                    t = time.perf_counter()
                    if t >= sink.due:
                        sink.offer(t, frame, recorder.media)
                except Exception:
                    pass
            return frame

        return next_frame

    def _mark_method(self):
        recorder = self

        def axis_mark(task, name, value=None, box=None):
            """给跑轴记录打一个点。队伍代码这样用: mark = getattr(self.task, 'axis_mark', None); mark and mark('光圈', ok)"""
            run = recorder.run
            if run is None:
                return
            try:
                t = time.perf_counter()
                where = recorder.origin(sys._getframe(1), False)
                recorder.queue.put((_MARK, run, t, str(name), value, _box(box), where))
                sink = recorder.sink
                if sink is not None and sink.crops and box is not None:
                    sink.maybe_crop(task, t, 'mark', (str(name), _box(box), None, value, None, None), where, {},
                                    recorder.media)
            except Exception:
                pass

        axis_mark._axis_rec_owner = self
        return axis_mark

    # ---------- 后台写盘 ----------

    def _log_error(self, message, error=None):
        now = time.monotonic()
        if now - self._error_logged_at < 10:
            return
        self._error_logged_at = now
        logger.error(message, error)

    def _writer_loop(self):
        files = {}
        last_scan = time.monotonic()
        handled = 0
        while True:
            try:
                item = self.queue.get(timeout=0.25)
            except queue.Empty:
                item = None
            if item is _STOP:
                break
            if item is not None:
                self._handle_safely(files, item)
                handled += 1
                if handled % WRITER_YIELD_EVERY == 0:
                    # 积压时别一口气占着 GIL 写几百条, 让执行器线程先走
                    time.sleep(0)
            now = time.monotonic()
            if now - last_scan >= 0.5:
                last_scan = now
                if self.idle_sink is not None and now >= self.idle_sink_until:
                    self.idle_sink = None  # 放掉最近几秒原图占的内存
                for run_file in tuple(files.values()):
                    self._flush_idle(run_file, now)
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                break
            if item is not _STOP:
                self._handle_safely(files, item)
        for run, run_file in tuple(files.items()):
            self._close(files, run, {'e': 'end', 't': _rel(run, time.perf_counter()), 'reason': '记录线程退出'})

    def _handle_safely(self, files, item):
        try:
            self._handle(files, item)
        except Exception as e:
            self._log_error('跑轴记录: 写盘出错', e)

    def _handle(self, files, item):
        code, run = item[0], item[1]
        if code == _START:
            self._open(files, run, item[2])
            return
        run_file = files.get(run)
        if run_file is None:
            return
        if code == _END:
            end = {'e': 'end', 't': _rel(run, item[2]), 'reason': item[3]}
            if item[4] is not None:
                end['field_seen'] = _rel(run, item[4])
            self._close(files, run, end)
        elif code == _INPUT:
            _, _, t, action, key, down_time, where, thread = item
            event = {'e': 'in', 't': _rel(run, t), 'a': action, 'k': _key_name(key)}
            if down_time is not None and action == 'key':
                event['dt'] = _round(down_time)
            self._add_origin(event, where)
            if thread:
                event['th'] = thread
            self._write(run_file, event)
        elif code == _SENDINPUT:
            _, _, t, (raw, size), where, thread = item
            for action, key in decode_send_input(raw, size):
                event = {'e': 'in', 't': _rel(run, t), 'a': action, 'k': key, 'si': 1}
                self._add_origin(event, where)
                if thread:
                    event['th'] = thread
                self._write(run_file, event)
        elif code == _FIELD:
            _, _, t, (in_team, index, count), previous_seen = item
            event = {'e': 'field', 't': _rel(run, t), 'team': in_team, 'i': index, 'cnt': count}
            if previous_seen is not None:
                event['prev_seen'] = _rel(run, previous_seen)
            self._write(run_file, event)
        elif code == _PERF:
            _, _, t, name, index, phase, failed = item
            event = {'e': 'perf', 't': _rel(run, t), 'c': name, 'i': index, 'p': phase}
            if failed:
                event['x'] = failed
            self._write(run_file, event)
        elif code == _READ:
            self._add_read(run, run_file, item)
        elif code == _LOG:
            _, _, created, level, message = item
            self._write(run_file, {'e': 'log', 't': round(created - run.wall0, 4), 'l': level, 'm': message})
        elif code == _MARK:
            _, _, t, name, value, box, where = item
            event = {'e': 'mark', 't': _rel(run, t), 'name': name, 'v': _jsonable(value)}
            if box is not None:
                event['box'] = _box_json(box)
            self._add_origin(event, where)
            self._write(run_file, event)

    def _open(self, files, run, header):
        os.makedirs(self.out_dir, exist_ok=True)
        team_dir, team_meta = _team_info(header.get('team') or ())
        if team_dir:
            header['team_dir'] = team_dir
            run.team_dir = team_dir
        if team_meta:
            header['team_ver'] = team_meta.get('version', '')
        for char in header.get('team') or ():
            if char:
                path = char.pop('file', '')
                char['custom'] = 'custom_teams' in Path(path).parts if path else False
        names = [c['n'] for c in header.get('team') or () if c]
        label = re.sub(r'[^\w.-]+', '_', '__'.join(names)) or 'unknown'
        stamp = time.strftime('%Y%m%d_%H%M%S', time.localtime(run.wall0))
        path = os.path.join(self.out_dir, f'{stamp}_{label}.jsonl')
        suffix = 1
        while os.path.exists(path):
            suffix += 1
            path = os.path.join(self.out_dir, f'{stamp}_{label}_{suffix}.jsonl')
        handle = open(path, 'w', encoding='utf-8', newline='\n')
        run_file = _RunFile(path, handle)
        files[run] = run_file
        run.path = path
        self.last_path = path
        self._write(run_file, header)

    def _close(self, files, run, end_event):
        run_file = files.pop(run, None)
        if run_file is None:
            return
        for key in tuple(run_file.groups):
            self._flush_group(run_file, key)
        self._write(run_file, end_event)
        try:
            run_file.handle.close()
        except OSError:
            pass
        self._prune()

    def _prune(self):
        owner = self.owner
        keep = MIN_KEEP_RUNS
        if owner is not None:
            try:
                keep = max(MIN_KEEP_RUNS, int(owner.config.get(owner.KEEP_KEY, 50)))
            except (TypeError, ValueError):
                pass
        paths = list_run_files(self.out_dir)
        for path in paths[:max(0, len(paths) - keep)]:
            try:
                os.remove(path)
            except OSError:
                pass
        try:
            frame_runs = max(1, int(self._option('FRAMES_KEEP_KEY', DEFAULT_FRAME_RUNS)))
        except (TypeError, ValueError):
            frame_runs = DEFAULT_FRAME_RUNS
        prune_media(self.out_dir, list_run_files(self.out_dir), frame_runs)

    def _write(self, run_file, event):
        run_file.handle.write(json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n')
        run_file.dirty = True

    def _flush_idle(self, run_file, now):
        for key, group in tuple(run_file.groups.items()):
            if now - group.seen_at >= GROUP_IDLE_FLUSH:
                self._flush_group(run_file, key)
        if run_file.dirty:
            try:
                run_file.handle.flush()
            except OSError:
                pass
            run_file.dirty = False

    def _flush_group(self, run_file, key):
        group = run_file.groups.pop(key, None)
        if group is None:
            return
        event = group.event
        if event['n'] > 1:
            event['t2'] = round(group.last_t, 4)
        else:
            for name in ('n', 'lo', 'hi', 'last'):
                event.pop(name, None)
        if event.get('lo') == event.get('hi'):
            event.pop('lo', None)
            event.pop('hi', None)
        if 'last' in event and event['last'] == event.get('v'):
            event.pop('last')
        self._write(run_file, event)

    def _add_read(self, run, run_file, item):
        _, _, t, kind, name, box, threshold, value, found, result_box, where = item
        t = _rel(run, t)
        value = _round(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else value
        name = _jsonable(name)
        key = (kind, str(name), where[0].co_filename, where[1])
        group = run_file.groups.get(key)
        if group is not None and _same_group(kind, group, t, value, found):
            event = group.event
            event['n'] += 1
            group.last_t = t
            group.seen_at = time.monotonic()
            if isinstance(value, float):
                event['lo'] = min(event.get('lo', value), value)
                event['hi'] = max(event.get('hi', value), value)
                event['last'] = value
            return
        if group is not None:
            self._flush_group(run_file, key)
        event = {'e': 'read', 't': t, 'k': kind, 'name': name, 'v': value, 'n': 1}
        if isinstance(value, float):
            event.update(lo=value, hi=value, last=value)
        if found is not None:
            event['ok'] = found
        if threshold:
            event['th'] = _round(threshold)
        if box is not None:
            event['box'] = _box_json(box)
        if result_box is not None:
            event['rb'] = _box_json(result_box)
        self._add_origin(event, where)
        run_file.groups[key] = _Group(event, t, value)

    def _add_origin(self, event, where):
        if not where:
            return
        for name, i in (('src', 0), ('src2', 2), ('via', 4)):
            if where[i] is not None:
                event[name] = self._label(where[i], where[i + 1])

    def _label(self, code, line):
        filename = code.co_filename
        base = self._labels.get(filename)
        if base is None:
            base = self._labels[filename] = os.path.basename(filename)
        return f'{base}:{line} {code.co_name}'


def _same_group(kind, group, t, value, found):
    if t - group.last_t > GROUP_GAP:
        return False
    event = group.event
    if found is not None and event.get('ok') != found:
        return False
    first = group.first_value
    if kind == 'cd':
        return isinstance(value, (int, float)) and isinstance(first, (int, float)) and (value > 0) == (first > 0)
    tolerance = GROUP_TOLERANCE.get(kind)
    if tolerance is not None:
        return isinstance(value, (int, float)) and isinstance(first, (int, float)) \
            and abs(value - first) <= tolerance
    return True


def _extract_find_one(args, kwargs, result):
    name = args[0] if args else kwargs.get('feature_name')
    threshold = _arg(args, kwargs, 'threshold', 3, 0)
    box = _box(_arg(args, kwargs, 'box', 5, None))
    if result is None:
        return name, box, threshold, None, False, None
    return name, box, threshold, getattr(result, 'confidence', None), True, _box(result)


def _extract_find_feature(args, kwargs, result):
    name = args[0] if args else kwargs.get('feature_name')
    threshold = _arg(args, kwargs, 'threshold', 3, 0)
    box = _box(_arg(args, kwargs, 'box', 11, None))
    if not result:
        return name, box, threshold, None, False, None
    best = max(result, key=lambda b: getattr(b, 'confidence', 0))
    return name, box, threshold, getattr(best, 'confidence', None), True, _box(best)


def _feature_search_box(task, name, args, kwargs):
    """没给 box 时框架在模板原位置 ± 偏差里找(FeatureSet.find_one_feature): 照它的算法算出搜索框。"""
    if kwargs.get('template') is not None or kwargs.get('x', -1) != -1:
        return None
    try:
        feature_set = task.executor.feature_set
        feature = feature_set.feature_dict.get(name)
        if feature is None:
            return None
        h_var = _arg(args, kwargs, 'horizontal_variance', 1, 0) or feature_set.default_horizontal_variance
        v_var = _arg(args, kwargs, 'vertical_variance', 2, 0) or feature_set.default_vertical_variance
        x_off, y_off = feature_set.width * h_var, feature_set.height * v_var
        if feature.scaling != 1:
            x_off, y_off = x_off or 1, y_off or 1
        x1 = max(0, round(feature.x - x_off))
        y1 = max(0, round(feature.y - y_off))
        x2 = min(feature_set.width, round(feature.x + feature.width + x_off))
        y2 = min(feature_set.height, round(feature.y + feature.height + y_off))
        return (int(x1), int(y1), int(x2 - x1), int(y2 - y1)) if x2 > x1 and y2 > y1 else None
    except Exception:
        return None


def _extract_color(args, kwargs, result):
    box = _arg(args, kwargs, 'box', 1, None)
    name = box if isinstance(box, str) else getattr(box, 'name', None)
    return name, _box(box), None, result, None, None


def _extract_cd(args, kwargs, result):
    name = _arg(args, kwargs, 'box_name', 0, None)
    index = _arg(args, kwargs, 'char_index', 1, None)
    if index is not None:
        name = f'{name}@{index}'
    return name, None, None, result, None, None


def _extract_con(args, kwargs, result):
    return 'con', None, None, result, None, None


def _rel(run, t):
    return round(t - run.t0, 4)


def _round(value):
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _key_name(key):
    if key is None:
        return None
    return str(key).lower()


def _box_json(box):
    return box if isinstance(box, str) else list(box)


def _jsonable(value):
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


def _class_file(cls):
    """类定义所在的文件。自定义队伍的类是 spec_from_file_location 加载的, 不在 sys.modules 里, 从方法上取。"""
    for value in vars(cls).values():
        function = getattr(value, '__func__', value)
        code = getattr(function, '__code__', None)
        if code is not None:
            return code.co_filename
    module = sys.modules.get(cls.__module__)
    return getattr(module, '__file__', '') or ''


def _team_info(chars):
    for char in chars:
        path = (char or {}).get('file') or ''
        parts = Path(path).parts
        if 'custom_teams' in parts:
            index = parts.index('custom_teams')
            if index + 1 < len(parts) - 1:
                team_dir = Path(*parts[:index + 2])
                meta = {}
                try:
                    with open(team_dir / 'team.json', encoding='utf-8-sig') as f:
                        meta = json.load(f)
                except (OSError, ValueError):
                    pass
                return parts[index + 1], meta
    return None, None
