"""手打录屏: 开局/结束时替人按显卡录屏软件的快捷键, 打完找到录屏文件, 拿插件自己存的画面和录屏逐帧比对出时间差。

- 快捷键用 SendInput 发(全局, 录屏软件的热键才收得到; PostMessage 只进游戏窗口)。用自己的 WinDLL 实例,
  不经过 ctypes.windll.user32 上记录器装的 SendInput 补丁, 也就不会被当成轴的按键记进去;
  Raw Input 那边会收到这几下(带软件模拟标记), 发键前后让 InputLog 静音一下。
- N 卡的快捷键、保存文件夹、回放时长直接读 NVIDIA 写在注册表里的设置(用户改过也跟着走), 读不到用默认。
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
                    stat = entry.stat()
                    if stat.st_mtime >= after and (best is None or stat.st_mtime > best[0]):
                        best = (stat.st_mtime, entry.path, stat.st_size)
            except OSError:
                continue
    return None if best is None else (best[1], best[2])


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
        current = {'run': run, 'plan': plan, 'start': None, 'starter': None}
        if plan['mode'] == 'toggle':
            starter = threading.Thread(target=self._press_start, args=(current,), name='axis_recorder_rec',
                                       daemon=True)
            current['starter'] = starter
            starter.start()
        self.current = current

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
        pressed = self.press(current['plan']['keys'])
        if pressed is not None:
            current['start'] = pressed
            self.mark(current['run'], pressed, '录屏', f"开始录制 {current['plan']['keys']}")


class VideoJob(threading.Thread):
    """一局手打打完: 按停止录制/存回放 -> 等录屏文件写完 -> 对时 -> 写 <stem>.video.json。"""

    def __init__(self, run, plan, current, owner):
        super().__init__(name='axis_recorder_video', daemon=True)
        self.run_ref = run
        self.plan = plan
        self.current = current
        self.owner = owner
        self.stop_event = threading.Event()

    def stop(self):
        self.stop_event.set()

    def run(self):
        run, plan, current = self.run_ref, self.plan, self.current
        info = {'v': 1, 'tool': plan['tool'], 'mode': plan['mode'], 'keys': plan['keys'], 'folder': plan['folder']}
        try:
            starter = current.get('starter')
            if starter is not None:
                starter.join(10)
            pressed = self.owner.press(plan['keys'])
            if pressed is None:
                info.update(method='没按出快捷键', note='SendInput 发键失败, 看 ok-ww 日志')
            else:
                info['sent'] = round(pressed - run.t0, 4)
                start = current.get('start')
                if start is not None:
                    info['started'] = round(start - run.t0, 4)
                self._find_and_align(info, pressed, start)
        except Exception as e:  # noqa: BLE001 对时失败不影响记录本身
            info.update(method=info.get('method') or '对时出错', note=f'{type(e).__name__}: {e}')
        try:
            self._write(info)
        except Exception as e:
            logger.error('跑轴记录: 写录屏对时结果出错', e)

    def _find_and_align(self, info, pressed, start):
        run, plan = self.run_ref, self.plan
        sent_wall = time.time() - (time.perf_counter() - pressed)
        path = find_video(plan['folder'], sent_wall, stop=self.stop_event)
        if path is None:
            info.update(method='没找到录屏', note=f"{FIND_TIMEOUT:g} 秒内 {plan['folder']} 里没有新的录屏文件"
                                             '(快捷键没被录屏软件认到? 即时重放没开? 文件夹不对?)')
            return
        info['path'] = path
        sent_rel = pressed - run.t0
        if plan['mode'] == 'toggle' and start is not None:
            start_rel = start - run.t0

            def estimate(duration):
                return -start_rel  # 片头 = 按开始录制那一刻
        else:
            def estimate(duration):
                return duration - sent_rel  # 片尾 = 按存回放/停止录制那一刻
        for _ in range(100):  # 写盘线程开文件比这里慢的极端情况
            if run.path:
                break
            time.sleep(0.1)
        if not run.path:
            return
        time.sleep(MEDIA_SETTLE)
        info.update(align(path, run.path, estimate, self.stop_event, self.owner.busy))

    def _write(self, info):
        run = self.run_ref
        if not run.path:
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
