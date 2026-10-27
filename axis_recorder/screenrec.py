"""手打录屏: 开局/结束时替人按显卡录屏软件的快捷键, 打完找到录屏文件, 拿插件自己存的画面和录屏逐帧比对出时间差。

- 快捷键用 SendInput 发(全局, 录屏软件的热键才收得到; PostMessage 只进游戏窗口)。用自己的 WinDLL 实例,
  不经过 ctypes.windll.user32 上记录器装的 SendInput 补丁, 也就不会被当成轴的按键记进去;
  Raw Input 那边会收到这几下(带软件模拟标记), 发键前后让 InputLog 静音一下。
- N 卡的快捷键、保存文件夹、回放时长直接读 NVIDIA 写在注册表里的设置(用户改过也跟着走), 读不到用默认。
- 开局开录是开关键, 按一下状态反一次, 插件看不到录屏软件在不在录: 只在鸣潮在前台时按, 按完看录屏文件夹
  认出按反了补按一次, 找到的录屏比开录还早就不认。存回放没有这个问题。
- 对时: 先按按键时刻估一个偏移(存回放=片子结尾是按键那一刻, 开录=片子开头是按键那一刻), 再在估计值前后几秒
  把插件存的低清画面和录屏帧缩成小灰度图逐帧比, 取差最小的偏移。比不上(窗口化录了整个桌面、录屏解不开)就留估计值。
结果写在这局旁边的 <stem>.video.json: 录屏秒 = 记录秒 + offset。
"""
import ctypes
import json
import os
import threading
import time
from ctypes import wintypes as w

from ok import Logger

from .rawinput import GameWindow

logger = Logger.get_logger(__name__)

REC_OFF = '不录'
TOOLS = {'N卡 (NVIDIA App)': 'nvidia', 'A卡 (AMD Adrenalin)': 'amd', 'Win 游戏栏': 'gamebar'}
TOOL_NAMES = {v: k for k, v in TOOLS.items()}
MODE_REPLAY = '结束时存回放'
MODE_TOGGLE = '开局开录、结束停'

DEFAULT_KEYS = {
    'nvidia': {'save': 'alt+f10', 'toggle': 'alt+f9'},
    'amd': {'save': 'ctrl+shift+s', 'toggle': 'ctrl+shift+e'},
    'gamebar': {'save': 'win+alt+g', 'toggle': 'win+alt+r'},
}
VIDEO_SUFFIX = '.video.json'
VIDEO_EXTS = ('.mp4', '.mkv', '.mov')
# 按完快捷键等录屏文件出现、写完: 存 5 分钟回放要好几秒
FIND_TIMEOUT = 120.0
# 收尾后等插件把最后几帧画面写完再读
MEDIA_SETTLE = 1.5
FIND_DEPTH = 3
# 对时: 粗找在估计值前后几秒里隔几帧比一张(2K 每帧都转成图很慢, 只解码不转快一倍), 先窄后宽;
# 细找在粗找结果前后逐帧比。按键到录屏软件响应一般差不到 1~2 秒, 窄的那趟就够
COARSE_PASSES = ((4.0, 3), (15.0, 4))
FINE_WINDOW = 0.3
COARSE_SAMPLES = 3
FINE_SAMPLES = 8
THUMB = (96, 54)
# 小灰度图标准化后的平均差: 真 DVR 上同一画面(再糊一层、压一遍 JPEG)0.04, 同一局差 30 秒的画面 0.78
MATCH_MAX_SCORE = 0.4
MATCH_MIN_CONTRAST = 0.3
FLAT_STD = 8.0
# 开录/停录只在鸣潮在前台时按: 录屏软件的快捷键管的是前台那个程序, ok-ww 在前台时按, N 卡开关的是「桌面」那段,
# 和游戏那段就对不上了(10-27 反馈包: 开录那下把一段之前就在录的停了, 停录那下又新开了一段)
FOREGROUND_POLL = 0.2
# 打完人在 ok-ww 里看复盘, 停录等回到鸣潮再按, 最多等这么久
STOP_WAIT_SECONDS = 600.0
# 打完(鸣潮在前台)再等这么久才停录: 下一局/下一波接着开打就接着录, 不停(10-27 反馈: 停录拖到下一局打着时才按, 把录屏停了)
STOP_GRACE_SECONDS = 5.0
# 在不在录: 看录屏文件夹里有没有隔这么久还在变大的文件(N 卡开录就建文件、边录边写); 只看这么久以内建的/改过的
GROW_CHECK_SECONDS = 1.0
RECENT_SECONDS = 6 * 3600.0
# 交接给下一局接着录的那段, 等它录完再对时, 最多等这么久
FOLLOW_WAIT_SECONDS = 2 * 3600.0
# 按完开录/停录看这么久录屏文件夹, 认出按反了就补按一次。N 卡是开录就建文件、边录边变大, 停了才写完
TOGGLE_CHECK_SECONDS = 8.0
TOGGLE_POLL = 1.0
# 新冒出来的文件要一直变大这么久才算新开了一段(存回放、搬文件一会儿就写完了)
GROWING_SECONDS = 3.0
# 上一局停录的文件写完之前不按开录, 不然它写完会被当成「开录那下把它停了」
STOP_SETTLE_SECONDS = 3.0
# 开局开录时找到的录屏比按开始录制还早这么多秒开录, 就不是这局的
NOT_OURS_SECONDS = 3.0
# video.json 的 flow: 这局的录屏是怎么开、怎么停的(复盘页摘要里多显示一行)
ADOPTED = '开局时已经在录, 没按开始, 接着录'
HANDED_OVER = '还没停录下一局就开打了, 没停, 下一局接着录(两局在同一段录屏里)'
ALREADY_STOPPED = '收尾时录屏已经停了(被手动停了?), 没按停止, 免得反而开出一段新的'

user32 = ctypes.WinDLL('user32', use_last_error=True)

VK = {'ctrl': 0x11, 'control': 0x11, 'alt': 0x12, 'shift': 0x10, 'win': 0x5B,
      'space': 0x20, 'tab': 0x09, 'enter': 0x0D, 'esc': 0x1B, 'home': 0x24, 'end': 0x23, 'insert': 0x2D,
      'delete': 0x2E, 'pageup': 0x21, 'pagedown': 0x22, 'printscreen': 0x2C, 'pause': 0x13}
VK_NAMES = {0x11: 'ctrl', 0x12: 'alt', 0x10: 'shift', 0x5B: 'win', 0x5C: 'win', 0xA0: 'shift', 0xA1: 'shift',
            0xA2: 'ctrl', 0xA3: 'ctrl', 0xA4: 'alt', 0xA5: 'alt'}
MODIFIERS = ('ctrl', 'control', 'alt', 'shift', 'win')
EXTENDED = {0x5B, 0x5C, 0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22, 0x2C}
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
INPUT_KEYBOARD = 1


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', w.WORD), ('wScan', w.WORD), ('dwFlags', w.DWORD), ('time', w.DWORD),
                ('dwExtraInfo', ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', w.LONG), ('dy', w.LONG), ('mouseData', w.DWORD), ('dwFlags', w.DWORD), ('time', w.DWORD),
                ('dwExtraInfo', ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [('ki', KEYBDINPUT), ('mi', MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [('type', w.DWORD), ('u', _INPUTUNION)]


user32.SendInput.restype = w.UINT
user32.SendInput.argtypes = (w.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.MapVirtualKeyW.restype = w.UINT
user32.MapVirtualKeyW.argtypes = (w.UINT, w.UINT)
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)


def parse_combo(text):
    """'alt+f10' -> ([修饰键 VK...], 主键 VK)。认不出抛 ValueError。"""
    parts = [p.strip().lower() for p in str(text or '').replace(' ', '').split('+') if p.strip()]
    if not parts:
        raise ValueError('快捷键是空的')
    *mods, key = parts
    for mod in mods:
        if mod not in MODIFIERS:
            raise ValueError(f'认不出修饰键 {mod}(只认 ctrl/alt/shift/win)')
    return [VK[m] for m in mods], _key_vk(key)


def _key_vk(key):
    if key in VK and key not in MODIFIERS:
        return VK[key]
    if len(key) == 1 and (key.isdigit() or 'a' <= key <= 'z'):
        return ord(key.upper())
    if key.startswith('f') and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return 0x6F + int(key[1:])
    raise ValueError(f'认不出按键 {key}')


def combo_name(vks):
    """注册表里的 VK 列表 -> 'alt+f10'。"""
    names = []
    for vk in vks:
        if vk in VK_NAMES:
            names.append(VK_NAMES[vk])
        elif 0x70 <= vk <= 0x87:
            names.append(f'f{vk - 0x6F}')
        elif 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A:
            names.append(chr(vk).lower())
        else:
            return None
    return '+'.join(names) if names else None


def wait_modifiers_released(keep=(), timeout=3.0):
    """人还按着别的修饰键时先等他松开: 按着 Shift 再发 Alt+F10 就成了 N 卡的 Alt+Shift+F10(关掉即时重放)。"""
    others = [vk for vk in (0x10, 0x11, 0x12, 0x5B, 0x5C) if vk not in keep and not (vk == 0x5C and 0x5B in keep)]
    deadline = time.monotonic() + timeout
    while any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in others):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def send_combo(text, hold=0.06):
    """按一下组合键(修饰键先下后上)。返回主键按下那一刻的 perf_counter。"""
    mods, key = parse_combo(text)
    if not wait_modifiers_released(mods):
        logger.warning(f'跑轴记录: 等了 3 秒还有修饰键按着, 照样按 {text}(可能被录屏软件认成别的快捷键)')

    def event(vk, up):
        flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in EXTENDED else 0)
        item = INPUT(type=INPUT_KEYBOARD)
        item.u.ki = KEYBDINPUT(vk, user32.MapVirtualKeyW(vk, 0), flags, 0, 0)
        return item

    def send(*items):
        array = (INPUT * len(items))(*items)
        if user32.SendInput(len(items), array, ctypes.sizeof(INPUT)) != len(items):
            raise OSError(f'SendInput 失败 ({ctypes.get_last_error()})')

    try:
        for vk in mods:
            send(event(vk, False))
            time.sleep(0.02)
        pressed = time.perf_counter()
        send(event(key, False))
        time.sleep(hold)
        send(event(key, True))
    finally:
        for vk in reversed(mods):
            time.sleep(0.02)
            send(event(vk, True))
    return pressed


# ---------- 各家设置 ----------

def nvidia_settings():
    """NVIDIA App / GeForce Experience 写在注册表的: 保存文件夹、回放秒数、存回放和录制的快捷键。读不到的项是 None。"""
    found = {'folder': None, 'buffer': None, 'save': None, 'toggle': None}
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'SOFTWARE\NVIDIA Corporation\Global\ShadowPlay\NVSPCAPS') as key:
            def raw(name):
                try:
                    return winreg.QueryValueEx(key, name)[0]
                except OSError:
                    return None

            def number(name):
                value = raw(name)
                if isinstance(value, bytes) and len(value) >= 4:
                    return int.from_bytes(value[:4], 'little')
                return value if isinstance(value, int) else None

            path = raw('DefaultPathW')
            if isinstance(path, bytes):
                found['folder'] = path.decode('utf-16-le', errors='ignore').split('\0')[0] or None
            found['buffer'] = number('DVRBufferLen')
            for name, prefix in (('save', 'DVRHKey'), ('toggle', 'ManualHKey')):
                count = number(prefix + 'Count') or 0
                vks = [number(f'{prefix}{i}') for i in range(count)]
                if vks and all(v for v in vks):
                    found[name] = combo_name(vks)
    except OSError:
        pass
    return found


def gamebar_folder():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as key:
            value = winreg.QueryValueEx(key, '{EDC0FE71-98D8-4F4A-B920-C8DC133CB165}')[0]
            if value:
                return os.path.expandvars(value)
    except OSError:
        pass
    return os.path.join(os.path.expanduser('~'), 'Videos', 'Captures')


def make_plan(tool_label, mode_label, keys_text, folder_text):
    """设置 -> 这局的录屏计划 dict; 不录返回 None。快捷键认不出抛 ValueError。"""
    tool = TOOLS.get(tool_label)
    if tool is None:
        return None
    mode = 'toggle' if mode_label == MODE_TOGGLE else 'replay'
    which = 'toggle' if mode == 'toggle' else 'save'
    plan = {'tool': tool, 'mode': mode, 'keys': None, 'folder': None, 'buffer': None}
    if tool == 'nvidia':
        found = nvidia_settings()
        plan['keys'], plan['folder'], plan['buffer'] = found[which], found['folder'], found['buffer']
    elif tool == 'gamebar':
        plan['folder'] = gamebar_folder()
    else:
        plan['folder'] = os.path.join(os.path.expanduser('~'), 'Videos')
    keys = str(keys_text or '').strip()
    plan['keys'] = keys or plan['keys'] or DEFAULT_KEYS[tool][which]
    parse_combo(plan['keys'])
    folder = str(folder_text or '').strip()
    if folder:
        plan['folder'] = folder
    return plan


# ---------- 找录屏文件 ----------

def find_video(folder, since_wall, timeout=FIND_TIMEOUT, stop=None, poll=1.0):
    """since_wall 之后写完的最新一个录屏文件(往下找 FIND_DEPTH 层子文件夹, N 卡按游戏名分子文件夹)。"""
    if not folder or not os.path.isdir(folder):
        return None
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline and not (stop and stop.is_set()):
        newest = _newest_video(folder, since_wall - 3)
        if newest is not None:
            path, size = newest
            if last == (path, size) and size > 0:
                return path  # 两次看到大小没变 = 写完了
            last = (path, size)
        time.sleep(poll)
    return None


def _newest_video(folder, after):
    best = None
    for path, (_, mtime, size) in _video_files(folder).items():
        if mtime >= after and (best is None or mtime > best[0]):
            best = (mtime, path, size)
    return None if best is None else (best[1], best[2])


def _video_files(folder):
    """{路径: (建文件时刻, 修改时刻, 大小)}, 往下找 FIND_DEPTH 层。"""
    found = {}
    pending = [(folder, 0)]
    while pending:
        current, depth = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth < FIND_DEPTH:
                        pending.append((entry.path, depth + 1))
                elif entry.name.lower().endswith(VIDEO_EXTS):
                    found[entry.path] = _times(entry.stat())
            except OSError:
                continue
    return found


def _times(stat):
    return getattr(stat, 'st_birthtime', stat.st_ctime), stat.st_mtime, stat.st_size


def video_birth(path):
    try:
        return _times(os.stat(path))[0]
    except (OSError, TypeError):
        return None


def wall_time(perf):
    return time.time() - (time.perf_counter() - perf)


def game_foreground():
    try:
        return GameWindow().foreground()
    except Exception:  # noqa: BLE001 查不了就当在前台, 照旧按
        return True


def wait_game_foreground(keep_waiting, timeout):
    """等鸣潮到前台; keep_waiting() 为假或超时返回 False。"""
    deadline = time.monotonic() + timeout
    while not game_foreground():
        if not keep_waiting() or time.monotonic() >= deadline:
            return False
        time.sleep(FOREGROUND_POLL)
    return True


def growing_video(folder):
    """录屏软件正在录的那段(隔 GROW_CHECK_SECONDS 还在变大的录屏文件), 没有返回 None。
    目录里的大小可能不跟着正在写的文件走, 用 os.stat 现查。"""
    if not folder or not os.path.isdir(folder):
        return None
    recent = time.time() - RECENT_SECONDS
    sizes = {}
    for path, (birth, mtime, _) in _video_files(folder).items():
        if max(birth, mtime) >= recent:
            try:
                sizes[path] = os.stat(path).st_size
            except OSError:
                continue
    if not sizes:
        return None
    time.sleep(GROW_CHECK_SECONDS)
    for path, size in sizes.items():
        try:
            if os.stat(path).st_size > size:
                return path
        except OSError:
            continue
    return None


def wait_video_finished(path, stop=None, timeout=FOLLOW_WAIT_SECONDS, poll=2.0):
    """等这段录屏不再变大(录完了); 超时、被叫停或文件没了返回 False。"""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline and not (stop and stop.is_set()):
        try:
            stat = os.stat(path)
        except OSError:
            return False
        now = (stat.st_size, stat.st_mtime)
        if now == last and stat.st_size > 0:
            return True
        last = now
        time.sleep(poll)
    return False


def watch_toggle(folder, pressed, stop=None):
    """按完开录/停录(pressed 是 perf_counter)看录屏文件夹:
    'stopped' = 一段按之前就在录的写完了(这下把它停了); 'started' = 按之后新冒出一段、一直在变大(这下开了录);
    看不出返回 None。目录里的大小可能不跟着正在写的文件走, 候选文件用 os.stat 现查。"""
    if not folder or not os.path.isdir(folder):
        return None
    press = wall_time(pressed)
    deadline = time.monotonic() + TOGGLE_CHECK_SECONDS
    seen = {}
    while not (stop and stop.is_set()):
        now = time.monotonic()
        for path, (birth, mtime, _) in _video_files(folder).items():
            if max(birth, mtime) < press - 1:
                continue
            try:
                birth, mtime, size = _times(os.stat(path))
            except OSError:
                continue
            prev = seen.get(path)
            if birth < press - 1:
                if mtime >= press - 0.5 and size > 0 and prev is not None and prev[:2] == (size, mtime):
                    return 'stopped'
                seen[path] = (size, mtime, now)
            elif prev is None or size <= prev[0]:
                seen[path] = (size, mtime, now)  # 刚看到或者没变大, 从现在重新计
            else:
                seen[path] = (size, mtime, prev[2])
                if now - prev[2] >= GROWING_SECONDS:
                    return 'started'
        if now >= deadline:
            return None
        time.sleep(TOGGLE_POLL)
    return None


# ---------- 对时 ----------

def thumb(image):
    """BGR/灰度图 -> 标准化后的小灰度图(去掉亮度/对比度差别); 几乎纯色的画面返回 None。"""
    import cv2
    import numpy as np
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, THUMB, interpolation=cv2.INTER_AREA).astype(np.float32)
    std = float(small.std())
    if std < FLAT_STD:
        return None
    return (small - small.mean()) / std


def plugin_frames(run_path, count):
    """插件存的画面里挑 count 帧(均匀分布, 跳过黑屏白屏): [(记录秒, 小图)]。"""
    import cv2
    import numpy as np
    from .media import MediaIndex
    media = MediaIndex(run_path)
    frames = media.frames
    if not frames:
        return []
    picked, used = [], set()
    step = max(1, len(frames) // max(1, count * 2))
    for i in range(0, len(frames), step):
        entry = frames[i]
        try:
            data = np.frombuffer(media.read(entry), dtype=np.uint8)
            image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        except (OSError, ValueError):
            continue
        if image is None:
            continue
        small = thumb(image)
        if small is not None and entry['o'] not in used:
            used.add(entry['o'])
            picked.append((entry['t'], small))
    if len(picked) > count:
        picked = [picked[round(i * (len(picked) - 1) / (count - 1))] for i in range(count)] if count > 1 \
            else picked[len(picked) // 2:len(picked) // 2 + 1]
    return picked


class VideoReader:
    def __init__(self, path):
        import cv2
        self.cv2 = cv2
        try:
            self.cap = cv2.VideoCapture(path, cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 2])
        except TypeError:
            self.cap = cv2.VideoCapture(path)
        if not self.cap.isOpened():
            raise OSError('录屏打不开(编码不支持或文件没写完)')
        self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 60.0
        frames = self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        self.duration = frames / self.fps if frames > 0 else 0.0

    def window(self, start, end, every=1):
        """[start, end] 秒里的帧: [(录屏秒, 小图或 None)]。every=2 隔一帧取一张(粗找用)。"""
        cv2 = self.cv2
        start = max(0.0, start)
        self.cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
        out, i = [], 0
        while True:
            if not self.cap.grab():
                break
            pos = self.cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
            if pos > end:
                break
            if i % every == 0 and pos >= start - 0.5 / self.fps:
                ok, image = self.cap.retrieve()
                out.append((pos, thumb(image) if ok else None))
            i += 1
        return out

    def close(self):
        self.cap.release()


def _best_offset(samples, windows, center, half, step):
    """samples=[(记录秒, 小图)], windows=对应的录屏帧; 在 center±half 里按 step 试偏移, 返回 (偏移, 分数, 对比度, 用了几帧)。"""
    import numpy as np
    prepared = []
    for (t, small), frames in zip(samples, windows):
        frames = [(pos, f) for pos, f in frames if f is not None]
        if len(frames) < 2:
            continue
        times = np.array([pos for pos, _ in frames])
        diffs = np.array([float(np.abs(small - f).mean()) for _, f in frames])
        prepared.append((t, times, diffs))
    if not prepared:
        return None
    grid = np.arange(center - half, center + half + step / 2, step)
    scores = np.zeros(len(grid))
    counts = np.zeros(len(grid))
    for t, times, diffs in prepared:
        target = t + grid
        idx = np.clip(np.searchsorted(times, target), 1, len(times) - 1)
        left = idx - 1
        nearer = np.where(np.abs(times[left] - target) <= np.abs(times[idx] - target), left, idx)
        ok = np.abs(times[nearer] - target) <= 1.5 * step + 0.02
        scores += np.where(ok, diffs[nearer], 0)
        counts += ok
    valid = counts >= max(1, len(prepared) // 2)
    if not valid.any():
        return None
    mean = np.where(valid, scores / np.maximum(counts, 1), np.inf)
    best = int(np.argmin(mean))
    median = float(np.median(mean[valid]))
    contrast = (median - mean[best]) / median if median > 0 else 0.0
    return float(grid[best]), float(mean[best]), float(contrast), len(prepared)


def align(video_path, run_path, estimate, stop=None, busy=None):
    """录屏秒 = 记录秒 + offset。返回 dict(method/offset/score/...)。busy() 为真(又开打了)时每解一段停下等。"""
    def halted():
        while busy is not None and busy() and not (stop and stop.is_set()):
            time.sleep(1)
        return bool(stop and stop.is_set())

    reader = VideoReader(video_path)
    try:
        result = {'duration': round(reader.duration, 3), 'fps': round(reader.fps, 3)}
        est = estimate(reader.duration)
        result.update(estimate=round(est, 3), offset=round(est, 3), method='按键时刻估算')
        samples = plugin_frames(run_path, COARSE_SAMPLES + FINE_SAMPLES)
        inside = [s for s in samples if 0 <= s[0] + est <= reader.duration] or samples
        if len(inside) < 2:
            result['note'] = '插件这局没存够画面, 只按按键时刻估了偏移'
            return result
        coarse = [inside[round(i * (len(inside) - 1) / (COARSE_SAMPLES - 1))] for i in range(COARSE_SAMPLES)] \
            if len(inside) >= COARSE_SAMPLES else inside
        step = 1.0 / reader.fps
        found = None
        for half, every in COARSE_PASSES:
            windows = []
            for t, _ in coarse:
                if halted():
                    return result
                windows.append(reader.window(t + est - half, t + est + half, every=every))
            tried = _best_offset(coarse, windows, est, half, every * step)
            if tried is not None and (found is None or tried[1] < found[1]):
                found = tried
            if found is not None and found[1] <= MATCH_MAX_SCORE and found[2] >= MATCH_MIN_CONTRAST:
                break
        if found is None:
            result['note'] = '录屏里对不上插件的画面(录屏是不是录了整个桌面?), 只按按键时刻估了偏移'
            return result
        windows = []
        for t, _ in inside:
            if halted():
                return result
            windows.append(reader.window(t + found[0] - FINE_WINDOW, t + found[0] + FINE_WINDOW))
        fine = _best_offset(inside, windows, found[0], FINE_WINDOW, step) or found
        offset, score, _, used = fine
        result.update(score=round(score, 3), contrast=round(found[2], 3), samples=used)
        if score <= MATCH_MAX_SCORE and found[2] >= MATCH_MIN_CONTRAST:
            result.update(offset=round(offset, 4), method='画面对时')
        else:
            result['note'] = (f'画面比对不够确定(差 {score:.2f}, 区分度 {found[2]:.2f}), 先用按键时刻估的偏移; '
                              f'比对给的是 {offset:+.3f}s')
        return result
    finally:
        reader.close()


def _json_default(value):
    tolist = getattr(value, 'tolist', None)  # numpy 数
    return tolist() if callable(tolist) else str(value)


def video_info_path(run_path):
    stem = run_path[:-len('.jsonl')] if run_path.endswith('.jsonl') else run_path
    return stem + VIDEO_SUFFIX


def load_video_info(run_path):
    try:
        with open(video_info_path(run_path), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def format_clock(seconds):
    sign = '-' if seconds < 0 else ''
    seconds = abs(seconds)
    return f'{sign}{int(seconds // 60):02d}:{seconds % 60:05.2f}'




class ScreenRec:
    """手打每局的录屏。begin/end 在开局、收尾的线程里调(执行器线程, 收尾也可能是采集线程),
    按快捷键和对时都丢给后台线程, 不拦执行器。

    mute(seconds): 让 Raw Input 别把这几下模拟按键记进这局; mark(run, t, name, value) 往记录里打点;
    busy(): 有一局正在打, 对时先停下别抢 CPU。
    """

    def __init__(self, mute, mark, busy):
        self.mute = mute
        self.mark = mark
        self.busy = busy
        self.current = None
        self.last_error = None
        self.jobs = []

    def plan(self, owner):
        """按插件设置算这局的录屏计划; 不录或设置有错返回 None(错只报一次)。"""
        config = owner.config
        try:
            plan = make_plan(config.get(owner.REC_TOOL_KEY, REC_OFF), config.get(owner.REC_MODE_KEY, MODE_REPLAY),
                             config.get(owner.REC_KEYS_KEY, ''), config.get(owner.REC_FOLDER_KEY, ''))
        except ValueError as e:
            if str(e) != self.last_error:
                self.last_error = str(e)
                logger.error(f'跑轴记录: 录屏快捷键设置不对, 这局不录({e})')
            return None
        self.last_error = None
        return plan

    def begin(self, run, plan):
        self.current = None
        if plan is None:
            return
        # start: 按开始录制的时刻; adopted: 开局时已经在录的那段(没按); seen: 看得到在不在录(按完开录认出了新的一段)
        current = {'run': run, 'plan': plan, 'start': None, 'adopted': None, 'seen': False, 'starter': None}
        self.current = current  # 先登记: 开录线程拿「还是不是这局」决定等不等鸣潮到前台
        if plan['mode'] == 'toggle':
            for job in self.jobs:
                job.handover.set()  # 上一局还没停录的就不停了, 这局接着录
            starter = threading.Thread(target=self._press_start, args=(current,), name='axis_recorder_rec',
                                       daemon=True)
            current['starter'] = starter
            starter.start()

    def end(self, run, t):
        """收尾: 起后台线程按停止录制/存回放、找文件、对时。返回要写进 end 的 rec。"""
        current, self.current = self.current, None
        if current is None or current['run'] is not run:
            return {}
        plan = current['plan']
        length = t - run.t0
        rec = {'tool': plan['tool'], 'action': '停止录制' if plan['mode'] == 'toggle' else '存回放',
               'keys': plan['keys'], 't': round(length, 4)}
        if plan['mode'] == 'replay' and plan.get('buffer') and length > plan['buffer']:
            logger.warning(f"跑轴记录: 这局 {length:.0f} 秒, 即时重放只存最近 {plan['buffer']} 秒, 开头录不到"
                           '(在 NVIDIA App 的即时重放里把时长调长)')
        job = VideoJob(run, plan, current, self)
        self.jobs = [j for j in self.jobs if j.is_alive()] + [job]
        job.start()
        return {'rec': rec}

    def press(self, keys):
        self.mute(2.0 + 3.0)  # 发键前可能还要等人松开修饰键(最多 3 秒)
        try:
            return send_combo(keys)
        except (OSError, ValueError) as e:
            logger.error(f'跑轴记录: 按录屏快捷键 {keys} 出错', e)
            return None

    def _press_start(self, current):
        run, plan = current['run'], current['plan']
        keys, folder = plan['keys'], plan['folder']
        for job in list(self.jobs):
            job.pressed.wait(30)  # 上一局停录按完或者交接给这局了
            if job.pressed_at is not None:
                time.sleep(max(0.0, job.pressed_at + STOP_SETTLE_SECONDS - time.perf_counter()))
        if not wait_game_foreground(lambda: self.current is current, float('inf')):
            return  # 打完鸣潮都没到前台, 开录没按, 收尾也就不按停
        recording = growing_video(folder)
        if recording is not None:
            current['adopted'] = recording
            current['seen'] = True
            self.mark(run, time.perf_counter(), '录屏', f'已经在录({os.path.basename(recording)}), 不按开始')
            return
        pressed = self.press(keys)
        if pressed is None:
            return
        current['start'] = pressed
        self.mark(run, pressed, '录屏', f'开始录制 {keys}')
        result = watch_toggle(folder, pressed)
        current['seen'] = result == 'started'
        if result == 'stopped':
            again = self.press(keys)
            if again is not None:
                current['start'] = again
                self.mark(run, again, '录屏', f'补按开始录制 {keys}(刚才那下把一段之前就在录的停了)')
                logger.warning(f'跑轴记录: 按开始录制 {keys} 反而停掉了一段正在录的, 录屏软件的开关和插件反了, 已补按一次')


class VideoJob(threading.Thread):
    """一局手打打完: 按停止录制/存回放 -> 等录屏文件写完 -> 对时 -> 写 <stem>.video.json。"""

    def __init__(self, run, plan, current, owner):
        super().__init__(name='axis_recorder_video', daemon=True)
        self.run_ref = run
        self.plan = plan
        self.current = current
        self.owner = owner
        self.stop_event = threading.Event()
        # 收尾那下快捷键按完了(或者不按了); 下一局的开录等它
        self.pressed = threading.Event()
        self.pressed_at = None
        # 还没停录下一局就开打了: 不停, 交给下一局接着录
        self.handover = threading.Event()

    def stop(self):
        self.stop_event.set()

    def run(self):
        run, plan, current = self.run_ref, self.plan, self.current
        info = {'v': 1, 'tool': plan['tool'], 'mode': plan['mode'], 'keys': plan['keys'], 'folder': plan['folder']}
        try:
            starter = current.get('starter')
            if starter is not None:
                starter.join(TOGGLE_CHECK_SECONDS + 20)
            start = current.get('start')
            if start is not None:
                info['started'] = round(start - run.t0, 4)
            try:
                pressed = self.pressed_at = self._press_end(info)
                if pressed is not None:
                    info['sent'] = round(pressed - run.t0, 4)
                    if plan['mode'] == 'toggle' and not self._check_stop(info, pressed):
                        pressed = None
            finally:
                self.pressed.set()  # 下一局开录只等到这里, 找文件对时不用等
            path = self._video_path(info, pressed)
            if path is not None:
                self._find_and_align(info, path, pressed)
        except Exception as e:  # noqa: BLE001 对时失败不影响记录本身
            info.update(method=info.get('method') or '对时出错', note=f'{type(e).__name__}: {e}')
        finally:
            self.pressed.set()
        try:
            self._write(info)
        except Exception as e:
            logger.error('跑轴记录: 写录屏对时结果出错', e)

    def _press_end(self, info):
        """按停止录制/存回放, 返回按下的时刻; 不按返回 None(原因写进 info, 要接着找录屏的写 info['flow'])。"""
        plan, current, keys = self.plan, self.current, self.plan['keys']
        if plan['mode'] == 'toggle':
            if current.get('start') is None and current.get('adopted') is None:
                info.update(method='没开录', note='这局打着的时候鸣潮一直不在前台(或者开录没按出来), 停止录制也就不按')
                return None
            waited = self._wait_to_stop()
            if waited == 'handover':
                info.update(flow=HANDED_OVER, recording=growing_video(plan['folder']))
                return None
            if waited != 'ok':
                info.update(method='没按停止录制',
                            note=f'鸣潮 {STOP_WAIT_SECONDS / 60:g} 分钟都没回到前台, 录屏软件可能还在录, 回游戏手动按一次 {keys}')
                logger.warning(f"跑轴记录: {info['note']}")
                return None
            if current.get('seen') and growing_video(plan['folder']) is None:
                info.update(flow=ALREADY_STOPPED)  # 不按: 按了反而开出一段新的
                return None
        pressed = self.owner.press(keys)
        if pressed is None:
            info.update(method='没按出快捷键', note='SendInput 发键失败, 看 ok-ww 日志')
        return pressed

    def _wait_to_stop(self):
        """等鸣潮在前台连续 STOP_GRACE_SECONDS 秒: 'ok' 能按了, 'handover' 下一局开打了, 'timeout' / 'stopped'。"""
        deadline = time.monotonic() + STOP_WAIT_SECONDS
        since = None
        while not self.stop_event.is_set():
            if self.handover.is_set():
                return 'handover'
            now = time.monotonic()
            if game_foreground():
                since = now if since is None else since
                if now - since >= STOP_GRACE_SECONDS:
                    return 'ok'
            else:
                since = None
                if now >= deadline:
                    return 'timeout'
            time.sleep(FOREGROUND_POLL)
        return 'stopped'

    def _video_path(self, info, pressed):
        """这局的录屏文件; 找不到返回 None(原因写进 info)。"""
        plan, current, run = self.plan, self.current, self.run_ref
        flow = info.get('flow')
        if pressed is not None:
            path = find_video(plan['folder'], wall_time(pressed), stop=self.stop_event)
        elif flow == HANDED_OVER:
            # 下一局停录时这段才录完, 等它
            path = info.pop('recording', None)
            if path is not None and not wait_video_finished(path, self.stop_event):
                info.update(method='没等到录完', note=f'{os.path.basename(path)} 一直在录, 没对时')
                return None
        elif flow == ALREADY_STOPPED:
            # 被手动停了之类: 找开录之后录完的那段
            began = current.get('start') if current.get('start') is not None else run.t0
            path = find_video(plan['folder'], wall_time(began), timeout=10, stop=self.stop_event)
        else:
            return None
        if path is None:
            info.update(method='没找到录屏', note=f"{plan['folder']} 里没有这局的录屏文件"
                                             '(快捷键没被录屏软件认到? 即时重放没开? 文件夹不对?)')
        return path

    def _check_stop(self, info, pressed):
        """开局开录: 停录那下要是反而新开了一段(这局其实没在录), 补按一次停掉, 这局不找录屏。"""
        keys = self.plan['keys']
        if watch_toggle(self.plan['folder'], pressed, self.stop_event) != 'started':
            return True
        again = self.owner.press(keys)
        if again is not None:
            self.pressed_at = again
        info.update(method='录屏开关对不上',
                    note=f'按停止录制 {keys} 反而开始了一段录制(这局其实没在录, 开局那下没开成或被手动停了), '
                         + ('已补按一次停掉; 这局没有录屏' if again is not None else '补按也没按出来, 录屏软件可能还在录'))
        logger.warning(f"跑轴记录: {info['note']}")
        return False

    def _find_and_align(self, info, path, pressed):
        run, plan, current = self.run_ref, self.plan, self.current
        info['path'] = path
        start = current.get('start')
        birth = video_birth(path)
        if plan['mode'] == 'toggle' and start is not None and birth is not None \
                and birth < wall_time(start) - NOT_OURS_SECONDS:
            # 按键估算拿别人的片子算出来的偏移是错的, 宁可不给
            info.update(method='录屏对不上', note=f'找到的 {os.path.basename(path)} 在按开始录制之前 '
                                               f'{wall_time(start) - birth:.0f} 秒就开录了, 不是这局的(录屏软件的开关和插件反了)')
            return
        if plan['mode'] != 'toggle':
            sent_rel = pressed - run.t0

            def estimate(duration):
                return duration - sent_rel  # 片尾 = 按存回放那一刻
        elif start is not None:
            start_rel = start - run.t0

            def estimate(duration):
                return -start_rel  # 片头 = 按开始录制那一刻
        else:
            info.setdefault('flow', ADOPTED)
            t0_wall = wall_time(run.t0)

            def estimate(duration):
                return t0_wall - birth if birth is not None else 0.0  # 片头 ≈ 文件建出来那一刻
        if not self._wait_path():
            return
        time.sleep(MEDIA_SETTLE)
        info.update(align(path, run.path, estimate, self.stop_event, self.owner.busy))

    def _wait_path(self):
        run = self.run_ref
        for _ in range(100):  # 写盘线程开文件比这里慢的极端情况(一局很短、没找录屏就直接写结果)
            if run.path:
                return True
            time.sleep(0.1)
        return bool(run.path)

    def _write(self, info):
        run = self.run_ref
        if not self._wait_path():
            return
        # 先拼好再写: 写到一半抛错会留下半个 JSON, 页面就一直显示对时中
        text = json.dumps(info, ensure_ascii=False, indent=1, default=_json_default)
        try:
            with open(video_info_path(run.path), 'w', encoding='utf-8') as f:
                f.write(text)
        except OSError as e:
            logger.error('跑轴记录: 写录屏对时结果出错', e)
            return
        if info.get('path') and info.get('offset') is not None:
            logger.info(f"跑轴记录: 录屏 {os.path.basename(info['path'])} {info['method']}, "
                        f"录屏秒 = 记录秒 {info['offset']:+.3f}")
        else:
            logger.warning(f"跑轴记录: 录屏 {info.get('method')}: {info.get('note')}")
