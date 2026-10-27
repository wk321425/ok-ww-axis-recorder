"""人为操作模式的按键采集(从 WWKeyRecorder 搬过来的 Raw Input)。

Raw Input 是系统把键盘鼠标的原始事件「抄送」一份给登记过的窗口, 不在输入链路上: 这边卡住了游戏照样收得到按键。
鼠标每动一下也会来一条(回报率多高就多少条), 所以只在人为操作模式、自动战斗关着时才登记, 测试代码时一条都不收。
用自己的 WinDLL 实例声明函数签名, 不碰 ctypes.windll.user32(ok-ww 和队伍代码在用, 改了 argtypes 会坏)。
"""
import ctypes
import os
import threading
import time
from collections import deque
from ctypes import wintypes as w

user32 = ctypes.WinDLL('user32', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

# 国服国际服一样; ok-ww 的 config.py 里也是这两个
GAME_EXE = 'client-win64-shipping.exe'
GAME_CLASS = 'UnrealWindow'

WM_INPUT = 0x00FF
WM_APP = 0x8000
WM_STOP = WM_APP + 1
PM_REMOVE = 1
QS_ALLINPUT = 0x04FF
RIM_TYPEMOUSE = 0
RIM_TYPEKEYBOARD = 1
RID_INPUT = 0x10000003
RIDEV_REMOVE = 0x00000001
RIDEV_INPUTSINK = 0x00000100
RI_KEY_BREAK = 1
RI_KEY_E0 = 2
HID_USAGE_PAGE_GENERIC = 0x01
HID_USAGE_MOUSE = 0x02
HID_USAGE_KEYBOARD = 0x06
HWND_MESSAGE = -3
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

LRESULT = w.LPARAM
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [('style', w.UINT), ('lpfnWndProc', WNDPROC), ('cbClsExtra', ctypes.c_int),
                ('cbWndExtra', ctypes.c_int), ('hInstance', w.HINSTANCE), ('hIcon', w.HICON),
                ('hCursor', w.HANDLE), ('hbrBackground', w.HBRUSH), ('lpszMenuName', w.LPCWSTR),
                ('lpszClassName', w.LPCWSTR)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [('usUsagePage', w.USHORT), ('usUsage', w.USHORT), ('dwFlags', w.DWORD), ('hwndTarget', w.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [('dwType', w.DWORD), ('dwSize', w.DWORD), ('hDevice', w.HANDLE), ('wParam', w.WPARAM)]


class RAWMOUSE(ctypes.Structure):
    # usFlags 后面那个联合体按 ULONG 对齐, 中间空 2 字节
    _fields_ = [('usFlags', w.USHORT), ('_pad', w.USHORT), ('usButtonFlags', w.USHORT),
                ('usButtonData', ctypes.c_short), ('ulRawButtons', w.ULONG), ('lLastX', w.LONG),
                ('lLastY', w.LONG), ('ulExtraInformation', w.ULONG)]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [('MakeCode', w.USHORT), ('Flags', w.USHORT), ('Reserved', w.USHORT), ('VKey', w.USHORT),
                ('Message', w.UINT), ('ExtraInformation', w.ULONG)]


class _RAWDATA(ctypes.Union):
    _fields_ = [('mouse', RAWMOUSE), ('keyboard', RAWKEYBOARD), ('_hid', ctypes.c_byte * 64)]


class RAWINPUT(ctypes.Structure):
    _fields_ = [('header', RAWINPUTHEADER), ('data', _RAWDATA)]


def _sig(dll, name, restype, *argtypes):
    function = getattr(dll, name)
    function.restype = restype
    function.argtypes = argtypes
    return function


_sig(user32, 'DefWindowProcW', LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
_sig(user32, 'RegisterClassW', w.ATOM, ctypes.POINTER(WNDCLASSW))
_sig(user32, 'UnregisterClassW', w.BOOL, w.LPCWSTR, w.HINSTANCE)
_sig(user32, 'CreateWindowExW', w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
     ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID)
_sig(user32, 'DestroyWindow', w.BOOL, w.HWND)
_sig(user32, 'RegisterRawInputDevices', w.BOOL, ctypes.POINTER(RAWINPUTDEVICE), w.UINT, w.UINT)
_sig(user32, 'GetRawInputData', w.UINT, w.HANDLE, w.UINT, w.LPVOID, ctypes.POINTER(w.UINT), w.UINT)
_sig(user32, 'MsgWaitForMultipleObjects', w.DWORD, w.DWORD, w.LPVOID, w.BOOL, w.DWORD, w.DWORD)
_sig(user32, 'PeekMessageW', w.BOOL, ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT)
_sig(user32, 'TranslateMessage', w.BOOL, ctypes.POINTER(w.MSG))
_sig(user32, 'DispatchMessageW', LRESULT, ctypes.POINTER(w.MSG))
_sig(user32, 'PostMessageW', w.BOOL, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
_sig(user32, 'GetForegroundWindow', w.HWND)
_sig(user32, 'GetWindowThreadProcessId', w.DWORD, w.HWND, ctypes.POINTER(w.DWORD))
_sig(user32, 'GetClassNameW', ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int)
_sig(kernel32, 'GetModuleHandleW', w.HMODULE, w.LPCWSTR)
_sig(kernel32, 'GetTickCount', w.DWORD)
_sig(kernel32, 'OpenProcess', w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
_sig(kernel32, 'QueryFullProcessImageNameW', w.BOOL, w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD))
_sig(kernel32, 'CloseHandle', w.BOOL, w.HANDLE)

# 窗口过程直接用 DefWindowProcW: WM_INPUT 在消息循环里取, 不用 Python 回调
DEF_WINDOW_PROC = WNDPROC(ctypes.cast(user32.DefWindowProcW, ctypes.c_void_p).value)

VK_NAMES = {0x08: 'backspace', 0x09: 'tab', 0x0D: 'enter', 0x13: 'pause', 0x14: 'capslock', 0x1B: 'esc',
            0x20: 'space', 0x21: 'pageup', 0x22: 'pagedown', 0x23: 'end', 0x24: 'home', 0x25: 'left',
            0x26: 'up', 0x27: 'right', 0x28: 'down', 0x2C: 'printscreen', 0x2D: 'insert', 0x2E: 'delete',
            0x5B: 'lwin', 0x5C: 'rwin', 0x5D: 'apps', 0x6A: 'num*', 0x6B: 'num+', 0x6D: 'num-', 0x6E: 'num.',
            0x6F: 'num/', 0x90: 'numlock', 0x91: 'scrolllock', 0xA0: 'lshift', 0xA1: 'rshift', 0xA2: 'ctrl',
            0xA3: 'rctrl', 0xA4: 'alt', 0xA5: 'ralt', 0xBA: ';', 0xBB: '=', 0xBC: ',', 0xBD: '-', 0xBE: '.',
            0xBF: '/', 0xC0: '`', 0xDB: '[', 0xDC: '\\', 0xDD: ']', 0xDE: "'"}
VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
RSHIFT_SCAN = 0x36

MOUSE_BUTTONS = ((0x0001, True, 'left'), (0x0002, False, 'left'), (0x0004, True, 'right'), (0x0008, False, 'right'),
                 (0x0010, True, 'middle'), (0x0020, False, 'middle'), (0x0040, True, 'x1'), (0x0080, False, 'x1'),
                 (0x0100, True, 'x2'), (0x0200, False, 'x2'))
RI_MOUSE_WHEEL = 0x0400
MOUSE_BUTTON_MASK = 0x07FF
WHEEL_DELTA = 120

# 消息从系统发出到这边取到超过这么多毫秒, 在事件上记 lag(GetTickCount 精度约 16ms, 再小的看不出来)
LAG_REPORT_MS = 32
# 开局前这几秒的按键也算进这一局: 自动认开局要等锁定框/血条出来, 人往往先出了手
PREROLL_SECONDS = 3.0
# 这些键按了不算出招(走路、跳、冲刺、系统键; F1~F12 另算, 是插件和录屏的热键), 其余键和鼠标键都算
NOT_ACTION_KEYS = frozenset({'w', 'a', 's', 'd', 'space', 'lshift', 'rshift', 'ctrl', 'rctrl', 'alt', 'ralt',
                             'lwin', 'rwin', 'apps', 'tab', 'esc', 'capslock', 'printscreen'})


def is_action(device, key):
    if device != 'key':
        return True
    return key not in NOT_ACTION_KEYS and not (key[:1] == 'f' and key[1:].isdigit())


def key_name(vk, make_code, flags):
    """键名和 ok-ww 发键用的一致: 小写字母数字、space、lshift..., 左 Ctrl/Alt 叫 ctrl/alt。"""
    if vk == VK_SHIFT:
        return 'rshift' if make_code == RSHIFT_SCAN else 'lshift'
    if vk == VK_CONTROL:
        return 'rctrl' if flags & RI_KEY_E0 else 'ctrl'
    if vk == VK_MENU:
        return 'ralt' if flags & RI_KEY_E0 else 'alt'
    if 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
        return chr(vk).lower()
    if 0x60 <= vk <= 0x69:
        return f'num{vk - 0x60}'
    if 0x70 <= vk <= 0x87:
        return f'f{vk - 0x6F}'
    return VK_NAMES.get(vk, f'vk{vk:02x}')


def _process_name(pid):
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid) if pid else None
    if not handle:
        return None
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = w.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return os.path.basename(buffer.value).lower()
    finally:
        kernel32.CloseHandle(handle)


class GameWindow:
    """前台窗口是不是鸣潮。按进程号缓存进程名(10 秒清一次); 不是线程安全的, 只在采集线程里用。"""

    CACHE_SECONDS = 10

    def __init__(self):
        self._names = {}
        self._cleared_at = time.monotonic()

    def foreground(self):
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        now = time.monotonic()
        if now - self._cleared_at > self.CACHE_SECONDS:
            self._names.clear()
            self._cleared_at = now
        name = self._names.get(pid.value)
        if name is None:
            name = _process_name(pid.value)
            if name is None:
                # 进程名查不到(被保护的进程)时退回看窗口类名
                buffer = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, buffer, len(buffer))
                name = '?' + buffer.value
            self._names[pid.value] = name
        return name == GAME_EXE or name == '?' + GAME_CLASS


class InputLog:
    """采集线程写入, 执行器线程开局/收尾时接上/断开。一把锁管按住的键、开局前的缓冲和当前这局。

    emit(run, t, action, key, injected, lag, value, cut) 往记录器的写盘队列里塞一条;
    mark(run, t, name, value) 记切出/切回游戏。
    """

    def __init__(self, emit, mark):
        self.emit = emit
        self.mark = mark
        self.lock = threading.Lock()
        self.run = None
        self.held = {}
        self.written = set()
        self.buffer = deque()
        self.in_game = None
        # 这一局的统计, 写进 end(和 WWKeyRecorder 一样: 按下次数、切出几次、切出去一共多久)
        self.inputs = 0
        self.pauses = 0
        self.paused_total = 0.0
        self.paused_since = None
        # 插件自己替人按录屏快捷键时, 这段时间里软件模拟的按键不记
        self.mute_until = 0.0
        # 最后一次出招(is_action 的键按下)的时刻, 不管有没有在记; 手打收局拿它判人还在不在打
        self.last_action = 0.0

    def mute_injected(self, seconds):
        with self.lock:
            self.mute_until = max(self.mute_until, time.perf_counter() + seconds)

    def begin(self, make_run, now=None):
        """开局: 起点往前挪到缓冲里最早那下按键(最多 PREROLL_SECONDS), make_run(t0) 建好这局后把缓冲补进去。"""
        with self.lock:
            now = time.perf_counter() if now is None else now
            events = [e for e in self.buffer if e[0] >= now - PREROLL_SECONDS]
            t0 = min(now, events[0][0] - 0.05) if events else now
            run = make_run(t0)
            self.written.clear()
            self.inputs = self.pauses = 0
            self.paused_total = 0.0
            self.paused_since = None
            for event in events:
                self._emit(run, event)
            self.run = run
            if self.in_game is False:
                self.paused_since = now
                self.pauses = 1
                self.mark(run, now, '暂停', '开始时鸣潮不在前台')
            return run

    def end(self, run, t):
        """收尾: 还按着的键记成松开(带 cut), 不然时间线上会一直按到下一局。返回这局的按键次数和暂停。"""
        with self.lock:
            if self.run is not run:
                return {}
            self._cut(run, t)
            if self.paused_since is not None:
                self.paused_total += t - self.paused_since
                self.paused_since = None
            self.run = None
            return {'inputs': self.inputs, 'pauses': self.pauses, 'paused': round(self.paused_total, 3)}

    def key(self, t, device, down, key, injected=False, lag=None):
        with self.lock:
            held_key = (device, key)
            if down:
                if held_key in self.held:
                    return  # 按住不放时的自动重复
                if injected and t < self.mute_until:
                    return  # 录屏快捷键, 松开那下也因为没记按下被丢掉
                self.held[held_key] = t
                if not injected and is_action(device, key):
                    self.last_action = t
            elif self.held.pop(held_key, None) is None:
                return  # 开始采集前、切出游戏前就按着的键
            if device == 'key':
                action = 'kd' if down else 'ku'
            else:
                action = 'md' if down else 'mu'
            self._record((t, action, key, injected, lag, None, held_key))

    def wheel(self, t, notches, injected=False, lag=None):
        with self.lock:
            if injected and t < self.mute_until:
                return
            self._record((t, 'wheel', 'wheel', injected, lag, notches, None))

    def set_in_game(self, t, in_game):
        with self.lock:
            if in_game == self.in_game:
                return
            self.in_game = in_game
            run = self.run
            if in_game:
                if run is not None and self.paused_since is not None:
                    self.paused_total += t - self.paused_since
                    self.paused_since = None
                    self.mark(run, t, '继续', '鸣潮回到前台')
                return
            if run is not None:
                self._cut(run, t)
                self.paused_since = t
                self.pauses += 1
                self.mark(run, t, '暂停', '鸣潮不在前台')
            self.held.clear()

    def _record(self, event):
        buffer = self.buffer
        buffer.append(event)
        while buffer and event[0] - buffer[0][0] > PREROLL_SECONDS:
            buffer.popleft()
        if self.run is not None:
            self._emit(self.run, event)

    def _emit(self, run, event):
        t, action, key, injected, lag, value, held_key = event
        if action in ('kd', 'md'):
            self.written.add(held_key)
            self.inputs += 1
        elif action in ('ku', 'mu'):
            if held_key not in self.written:
                return  # 按下是在这局开始之前
            self.written.discard(held_key)
        else:
            self.inputs += 1
        self.emit(run, t, action, key, injected, lag, value, False)

    def _cut(self, run, t):
        for device, key in tuple(self.written):
            self.emit(run, t, 'ku' if device == 'key' else 'mu', key, False, None, None, True)
        self.written.clear()


def _devices(flags, hwnd):
    devices = (RAWINPUTDEVICE * 2)()
    for device, usage in zip(devices, (HID_USAGE_KEYBOARD, HID_USAGE_MOUSE)):
        device.usUsagePage = HID_USAGE_PAGE_GENERIC
        device.usUsage = usage
        device.dwFlags = flags
        device.hwndTarget = hwnd
    return devices


class RawInputThread(threading.Thread):
    """收 Raw Input, 鸣潮在前台才交给 InputLog。每 50ms 醒一次: 看前台、调 on_idle(看执行器还在不在跑)。

    on_idle 返回假就自己退出(注销 Raw Input), 由执行器那边下次需要时再起一个。
    """

    POLL_MS = 50

    def __init__(self, log, on_idle=None, game_factory=GameWindow):
        super().__init__(name='axis_recorder_rawinput', daemon=True)
        self.log = log
        self.on_idle = on_idle
        self.game_factory = game_factory
        self.ready = threading.Event()
        self.error = None
        self.raw_seen = 0
        self._hwnd = None
        self._stopping = False

    def stop(self, wait=True):
        self._stopping = True
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_STOP, 0, 0)
        if wait and self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=2)

    def run(self):
        instance = kernel32.GetModuleHandleW(None)
        class_name = f'AxisRecorderRawInput{id(self)}'
        window_class = WNDCLASSW(lpfnWndProc=DEF_WINDOW_PROC, hInstance=instance, lpszClassName=class_name)
        registered = False
        try:
            if not user32.RegisterClassW(ctypes.byref(window_class)):
                raise OSError(f'RegisterClassW 失败 ({ctypes.get_last_error()})')
            self._hwnd = user32.CreateWindowExW(0, class_name, class_name, 0, 0, 0, 0, 0,
                                                HWND_MESSAGE, None, instance, None)
            if not self._hwnd:
                raise OSError(f'CreateWindowExW 失败 ({ctypes.get_last_error()})')
            devices = _devices(RIDEV_INPUTSINK, self._hwnd)
            if not user32.RegisterRawInputDevices(devices, len(devices), ctypes.sizeof(RAWINPUTDEVICE)):
                raise OSError(f'RegisterRawInputDevices 失败 ({ctypes.get_last_error()})')
            registered = True
            self.ready.set()
            self._loop()
        except Exception as e:  # noqa: BLE001 记录器要报原因
            self.error = str(e)
        finally:
            self.ready.set()
            if registered:
                devices = _devices(RIDEV_REMOVE, None)
                user32.RegisterRawInputDevices(devices, len(devices), ctypes.sizeof(RAWINPUTDEVICE))
            if self._hwnd:
                user32.DestroyWindow(self._hwnd)
                self._hwnd = None
            user32.UnregisterClassW(class_name, instance)

    def _loop(self):
        log = self.log
        game = self.game_factory()
        raw = RAWINPUT()
        raw_size = ctypes.sizeof(raw)
        header_size = ctypes.sizeof(RAWINPUTHEADER)
        size = w.UINT()
        msg = w.MSG()
        message = ctypes.byref(msg)
        log.set_in_game(time.perf_counter(), game.foreground())
        while not self._stopping:
            user32.MsgWaitForMultipleObjects(0, None, False, self.POLL_MS, QS_ALLINPUT)
            while user32.PeekMessageW(message, None, 0, 0, PM_REMOVE):
                if msg.message == WM_INPUT:
                    t = time.perf_counter()
                    size.value = raw_size
                    got = user32.GetRawInputData(msg.lParam, RID_INPUT, ctypes.byref(raw), ctypes.byref(size),
                                                 header_size)
                    if got and got != 0xFFFFFFFF:
                        self._on_raw(raw, t, msg.time, game)
                    user32.DefWindowProcW(msg.hWnd, msg.message, msg.wParam, msg.lParam)
                elif msg.message != WM_STOP:
                    user32.TranslateMessage(message)
                    user32.DispatchMessageW(message)
            now = time.perf_counter()
            log.set_in_game(now, game.foreground())
            if self.on_idle is not None and not self.on_idle(now):
                break

    def _on_raw(self, raw, t, posted_at, game):
        log = self.log
        if log.in_game:
            self.raw_seen += 1
        kind = raw.header.dwType
        if kind == RIM_TYPEKEYBOARD:
            keyboard = raw.data.keyboard
            vk = keyboard.VKey
            # 0xFF 是扫描码前缀之类的假键; 带 E0 的 Shift 是小键盘/方向键配 NumLock 时系统补的假 Shift
            if vk == 0xFF or (vk == VK_SHIFT and keyboard.Flags & RI_KEY_E0):
                return
            in_game = game.foreground()
            log.set_in_game(t, in_game)
            if in_game:
                log.key(t, 'key', not keyboard.Flags & RI_KEY_BREAK, key_name(vk, keyboard.MakeCode, keyboard.Flags),
                        not raw.header.hDevice, self._lag(posted_at))
        elif kind == RIM_TYPEMOUSE:
            flags = raw.data.mouse.usButtonFlags
            if not flags & MOUSE_BUTTON_MASK:
                return  # 只是移动
            in_game = game.foreground()
            log.set_in_game(t, in_game)
            if not in_game:
                return
            injected, lag = not raw.header.hDevice, self._lag(posted_at)
            for bit, down, button in MOUSE_BUTTONS:
                if flags & bit:
                    log.key(t, 'mouse', down, button, injected, lag)
            if flags & RI_MOUSE_WHEEL:
                log.wheel(t, round(raw.data.mouse.usButtonData / WHEEL_DELTA, 2), injected, lag)

    @staticmethod
    def _lag(posted_at):
        lag = (kernel32.GetTickCount() - posted_at) & 0xFFFFFFFF
        return lag if LAG_REPORT_MS < lag < 60000 else None
