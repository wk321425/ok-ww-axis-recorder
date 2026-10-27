"""画面: 整局低清帧、读屏那块原图、热键导出最近几秒原图。

热路径(执行器线程)只拿帧的引用塞进队列: 截图方式每帧给的都是新数组(WGC/BitBlt), 不用拷。
缩放、编码、写盘在后台线程; OpenCV 编码时放开 GIL, 不跟轴抢。
每局两份文件跟 JSONL 同名: .media 是 JPEG/PNG 首尾相接, .media.idx 每行一条 {时间, 偏移, 长度}。
"""
import bisect
import collections
import ctypes
import json
import os
import shutil
import threading
import time
from ctypes import wintypes

import cv2
import numpy as np

from ok import Logger

logger = Logger.get_logger(__name__)

MEDIA_VERSION = 1
FRAME_INTERVAL = 0.25
LOW_WIDTH = 960
JPEG_QUALITY = 70
# 热键导出用的原图都留在内存里(BGRA 一帧 1080p 8.3MB / 2K 14.7MB / 4K 33.2MB)。
# 这么多秒以内任何分辨率都留够(4K 约 530MB); 设得更长时, 多出来的部分不超过内存上限
RING_GUARANTEE_SECONDS = 4.0
RING_MEMORY_CAP = 320 * 1024 * 1024
# 一局打完原图再留这么久, 打完马上按热键也导得出来
RING_IDLE_KEEP = 60.0
MAX_EXPORT_SECONDS = 10

# 读屏那块原图: 读数变了才存, 同一处读屏最快每 0.2 秒一张
CROP_MIN_GAP = 0.2
CROP_PAD = 8
CROP_MAX_AREA = 640 * 360
CROP_MAX_PER_RUN = 6000
CROP_TOLERANCE = {'find': 0.05, 'feat': 0.05, 'color': 0.03, 'con': 0.02}

MEDIA_SUFFIX = '.media'
INDEX_SUFFIX = '.media.idx'
EXPORT_DIR = 'export'
EXPORT_KEEP = 20
FLUSH_EVERY = 2.0
QUEUE_LEN = 64

NO_HOTKEY = '不用'
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
HOTKEYS = {'F8': (0, 0x77), 'F7': (0, 0x76), 'F6': (0, 0x75), 'Ctrl+Alt+E': (MOD_CONTROL | MOD_ALT, 0x45)}
HOTKEY_ID = 0x5A11
WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001


def media_paths(run_path):
    stem = run_path[:-len('.jsonl')] if run_path.endswith('.jsonl') else run_path
    return stem + MEDIA_SUFFIX, stem + INDEX_SUFFIX


def _bgra_base(frame):
    """get_frame 给的是 BGRA 前三个通道的切片; 从 4 通道原数组缩放、转色快得多(跨步数组要先拷一遍)。"""
    base = frame.base
    if isinstance(base, np.ndarray) and base.ndim == 3 and base.shape[2] == 4 \
            and base.shape[:2] == frame.shape[:2] and base.flags.c_contiguous:
        return base
    return None


def frame_bytes(frame):
    base = _bgra_base(frame)
    return (base if base is not None else frame).nbytes


def ring_frames(want, nbytes):
    """热键导出留几帧: 保证的秒数以内照设置留, 再长按内存上限截。"""
    step = max(FRAME_INTERVAL, 0.05)
    guaranteed = int(round(RING_GUARANTEE_SECONDS / step))
    return max(1, min(want, max(guaranteed, RING_MEMORY_CAP // max(1, nbytes))))


def shrink(frame, width=LOW_WIDTH):
    """缩到 width 宽的 BGR。先按整数倍 INTER_AREA(快), 剩下一点用线性: 2K 单核约 2.5ms。"""
    src = _bgra_base(frame)
    if src is None:
        src = frame
    h, w = src.shape[:2]
    factor = w // width
    if factor >= 2:
        src = cv2.resize(src, (w // factor, h // factor), interpolation=cv2.INTER_AREA)
        h, w = src.shape[:2]
    if w > width:
        src = cv2.resize(src, (width, max(1, round(h * width / w))), interpolation=cv2.INTER_LINEAR)
    if src.ndim == 3 and src.shape[2] == 4:
        src = cv2.cvtColor(src, cv2.COLOR_BGRA2BGR)
    return src


def full_bgr(frame):
    base = _bgra_base(frame)
    if base is not None:
        return cv2.cvtColor(base, cv2.COLOR_BGRA2BGR)
    return np.ascontiguousarray(frame)


def _numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _same_reading(kind, value, last):
    if _numeric(value) and _numeric(last):
        if kind == 'cd':
            return (value > 0) == (last > 0)
        return abs(value - last) <= CROP_TOLERANCE.get(kind, 0.03)
    return value == last


def _jsonable(value):
    item = getattr(value, 'item', None)
    if item is not None and getattr(value, 'ndim', None) == 0:
        value = item()  # numpy 标量
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return str(value)


class FrameSink:
    """一局的画面。热路径往里塞帧引用; 文件名等写盘线程开了 JSONL(run.path)再定。"""

    def __init__(self, run, save_frames, crops, export_seconds):
        self.run = run
        self.save_frames = save_frames
        self.crops = crops
        self.export_seconds = max(0.0, min(float(export_seconds), MAX_EXPORT_SECONDS))
        self.due = 0.0
        self.ring = None
        # 内存上限截短了才有值: (留得下几秒, 一帧多少字节, 宽, 高)
        self.ring_limit = None
        self.last_src = None
        self.crop_state = {}
        self.crop_count = 0
        self.frame_count = 0
        self.ended = False
        # 下面只有写盘线程碰
        self.handle = None
        self.index = None
        self.offset = 0
        self.closed = False
        self.flushed_at = 0.0

    # ---------- 热路径 ----------

    def offer(self, t, frame, media):
        self.due = t + FRAME_INTERVAL
        src = frame
        last = self.last_src
        if last is not None and (src is last or (src.base is not None and src.base is last.base)):
            # 截图方式复用同一块缓冲区: 只能当场拷, 不然存下来的全是最新那帧
            frame = np.array(src)
        self.last_src = src
        if self.export_seconds > 0:
            ring = self.ring
            if ring is None:
                step = max(FRAME_INTERVAL, 0.05)
                want = max(1, int(round(self.export_seconds / step)))
                nbytes = frame_bytes(frame)
                keep = ring_frames(want, nbytes)
                ring = self.ring = collections.deque(maxlen=keep)
                if keep < want:
                    self.ring_limit = (keep * step, nbytes, frame.shape[1], frame.shape[0])
            ring.append((t, frame))
        if self.save_frames:
            media.put(('f', self, t, frame))

    def maybe_crop(self, task, t, kind, info, where, kwargs, media):
        """读数跟这处上次存图时比变了(找到/没找到翻转、数值超出容差), 把读的那块原图拷下来。"""
        name, box, _, value, found, result_box = info
        regions = []
        if type(box) is tuple:
            regions.append(box)
        if type(result_box) is tuple:
            # 搜索框太大(整屏、半屏找图)时退而截找到的那块
            x, y, w, h = result_box
            regions.append((x - CROP_PAD, y - CROP_PAD, w + 2 * CROP_PAD, h + 2 * CROP_PAD))
        if not regions:
            return
        code = where[0] if where else None
        key = (kind, name, code.co_filename if code is not None else None, where[1] if where else None)
        state = self.crop_state.get(key)
        if state is not None and (t - state[0] < CROP_MIN_GAP
                                  or (found == state[1] and _same_reading(kind, value, state[2]))):
            return
        self.crop_state[key] = (t, found, value)
        frame = getattr(getattr(task, 'executor', None), '_frame', None)
        given = kwargs.get('frame')
        if given is not None and given is not frame:
            return
        if not isinstance(frame, np.ndarray):
            return
        height, width = frame.shape[:2]
        for region in regions:
            x, y, w, h = (int(v) for v in region)
            x0, y0, x1, y1 = max(0, x), max(0, y), min(width, x + w), min(height, y + h)
            if x1 > x0 and y1 > y0 and (x1 - x0) * (y1 - y0) <= CROP_MAX_AREA:
                break
        else:
            return
        crop = frame[y0:y1, x0:x1].copy()
        self.crop_count += 1
        if self.crop_count >= CROP_MAX_PER_RUN:
            self.crops = False
        media.put(('c', self, t, kind, name, where, (x0, y0, x1 - x0, y1 - y0), crop, value, found))

    def recent(self):
        ring = self.ring
        return list(ring) if ring else []


class MediaWriter:
    """后台线程: 缩帧、编码、追加写 .media 和 .media.idx。队列满了丢最旧的, 不让热路径等。"""

    def __init__(self, label):
        self.label = label
        self.queue = collections.deque(maxlen=QUEUE_LEN)
        self.wake = threading.Event()
        self.sinks = []
        self.thread = None
        self.stopping = False
        self.error = None

    def start(self):
        self.thread = threading.Thread(target=self._loop, name='axis_recorder_media', daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping = True
        self.wake.set()
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None

    def put(self, item):
        self.queue.append(item)
        self.wake.set()

    def idle(self):
        return not self.queue and not any(not s.closed and s.ended for s in self.sinks)

    def _loop(self):
        while True:
            self.wake.wait(0.5)
            self.wake.clear()
            while True:
                try:
                    item = self.queue.popleft()
                except IndexError:
                    break
                try:
                    self._handle(item)
                except Exception as e:
                    self.error = e
                    logger.error('跑轴记录: 存画面出错', e)
            now = time.monotonic()
            for sink in tuple(self.sinks):
                try:
                    if sink.ended:
                        self._close(sink)
                    elif now - sink.flushed_at >= FLUSH_EVERY:
                        self._flush(sink, now)
                except Exception as e:
                    self.error = e
                    logger.error('跑轴记录: 存画面出错', e)
            if self.stopping:
                break
        for sink in tuple(self.sinks):
            self._close(sink)

    def _open(self, sink):
        if sink.handle is not None:
            return True
        if sink.closed:
            return False
        # JSONL 是记录线程开的, 开局后一般几毫秒就有
        deadline = time.monotonic() + 2.0
        while sink.run.path is None and time.monotonic() < deadline and not self.stopping:
            time.sleep(0.02)
        if sink.run.path is None:
            sink.closed = True
            return False
        media_path, index_path = media_paths(sink.run.path)
        sink.handle = open(media_path, 'wb')
        sink.index = open(index_path, 'w', encoding='utf-8', newline='\n')
        sink.index.write(json.dumps({'v': MEDIA_VERSION, 'interval': FRAME_INTERVAL}) + '\n')
        sink.flushed_at = time.monotonic()
        self.sinks.append(sink)
        return True

    def _close(self, sink):
        if sink in self.sinks:
            self.sinks.remove(sink)
        sink.closed = True
        for handle in (sink.handle, sink.index):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass

    @staticmethod
    def _flush(sink, now):
        sink.flushed_at = now
        try:
            # 先数据后索引: 中途崩了索引也不会指到文件外面
            sink.handle.flush()
            sink.index.flush()
        except OSError:
            pass

    def _handle(self, item):
        kind, sink, t = item[0], item[1], item[2]
        if not self._open(sink):
            return
        rel = round(t - sink.run.t0, 4)
        if kind == 'f':
            small = shrink(item[3])
            ok, buf = cv2.imencode('.jpg', small, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if ok:
                sink.frame_count += 1
                self._append(sink, buf, {'f': 1, 't': rel, 'w': int(small.shape[1]), 'h': int(small.shape[0])})
        elif kind == 'c':
            _, _, _, read_kind, name, where, box, crop, value, found = item
            ok, buf = cv2.imencode('.png', crop)
            if not ok:
                return
            entry = {'c': 1, 't': rel, 'k': read_kind, 'name': _jsonable(name), 'box': list(box)}
            if value is not None:
                entry['v'] = _jsonable(value)
            if found is not None:
                entry['ok'] = found
            if where and where[0] is not None:
                entry['src'] = self.label(where[0], where[1])
            self._append(sink, buf, entry)

    @staticmethod
    def _append(sink, buf, entry):
        data = buf.tobytes()
        entry['o'] = sink.offset
        entry['n'] = len(data)
        # 索引那行先拼好: 拼不出来时数据也别写, 不然后面每条的偏移都错
        line = json.dumps(entry, ensure_ascii=False, separators=(',', ':'), default=str) + '\n'
        sink.handle.write(data)
        sink.offset += len(data)
        sink.index.write(line)


def prune_media(out_dir, run_paths, keep):
    """只给最新 keep 局留画面; JSONL 已经删掉的局, 画面也删。"""
    kept = {media_paths(path)[0] for path in run_paths[-keep:]} if keep > 0 else set()
    try:
        names = os.listdir(out_dir)
    except OSError:
        return
    for name in names:
        if not name.endswith(MEDIA_SUFFIX):
            continue
        media = os.path.join(out_dir, name)
        if media in kept:
            continue
        for path in (media, media + '.idx'):
            try:
                os.remove(path)
            except OSError:
                pass


def export_frames(out_dir, sink, frames, team_dir=None):
    """frames 是 [(perf_counter 时刻, 原图)]: 存成 PNG(原分辨率, 不缩), 旁边一份 info.json 说明每张是这局第几秒。"""
    run = sink.run
    run_name = os.path.basename(run.path) if run.path else ''
    label = run_name[16:-6] if run_name.endswith('.jsonl') else 'run'
    root = os.path.join(out_dir, EXPORT_DIR)
    folder = os.path.join(root, f"{time.strftime('%Y%m%d_%H%M%S')}_{label}")
    suffix = 1
    while os.path.exists(folder):
        suffix += 1
        folder = os.path.join(root, f"{time.strftime('%Y%m%d_%H%M%S')}_{label}_{suffix}")
    os.makedirs(folder)
    files, size = [], None
    for t, frame in frames:
        rel = t - run.t0
        name = f'{rel:08.3f}s.png'
        image = full_bgr(frame)
        size = [int(image.shape[1]), int(image.shape[0])]
        ok, buf = cv2.imencode('.png', image, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        if ok:
            # 路径可能有中文, 不用 cv2.imwrite
            buf.tofile(os.path.join(folder, name))
            files.append({'file': name, 't': round(rel, 3)})
    info = {'run': run_name, 'team_dir': team_dir, 'size': size, 'frames': files,
            'export_seconds': sink.export_seconds,
            'span': round(files[-1]['t'] - files[0]['t'], 3) if files else 0,
            'judge_test': f"judge_test.py at {team_dir or '<队伍目录>'} --image "
                          + ' '.join(f['file'] for f in files) + ' -e "<表达式>"'}
    note = ring_limit_note(sink)
    if note:
        info['ring_seconds'] = sink.ring_limit[0]
        info['note'] = note
    with open(os.path.join(folder, 'info.json'), 'w', encoding='utf-8') as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    _prune_exports(root)
    return folder, files


def ring_limit_note(sink):
    if sink.ring_limit is None:
        return ''
    seconds, nbytes, width, height = sink.ring_limit
    return (f'设的 {sink.export_seconds:g} 秒超过内存上限: {width}x{height} 一帧原图 {nbytes / 1e6:.1f}MB, '
            f'只留得下最近 {seconds:g} 秒')


def _prune_exports(root):
    try:
        names = sorted(n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n)))
    except OSError:
        return
    for name in names[:max(0, len(names) - EXPORT_KEEP)]:
        shutil.rmtree(os.path.join(root, name), ignore_errors=True)


def finished_exports(out_dir):
    # info.json 最后写: 没有它的是正在导出的那次, 不算也不删
    root = os.path.join(out_dir, EXPORT_DIR)
    try:
        names = os.listdir(root)
    except OSError:
        return []
    return [os.path.join(root, n) for n in names if os.path.isfile(os.path.join(root, n, 'info.json'))]


def folder_bytes(folder):
    total = 0
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def export_usage(out_dir):
    """(导出过几次, 一共多少字节)"""
    folders = finished_exports(out_dir)
    return len(folders), sum(folder_bytes(f) for f in folders)


def clear_exports(out_dir):
    """删掉所有导出完的原图文件夹, 返回 (删了几次, 腾出多少字节)。"""
    count = freed = 0
    for folder in finished_exports(out_dir):
        size = folder_bytes(folder)
        shutil.rmtree(folder, ignore_errors=True)
        if not os.path.exists(folder):
            count += 1
            freed += size
    return count, freed


class HotkeyThread(threading.Thread):
    """RegisterHotKey 只认注册它的线程的消息队列, 所以单开一个线程注册和收消息; 设置改了自动换键。"""

    def __init__(self, get_key, on_press, label='导出原图'):
        super().__init__(name='axis_recorder_hotkey', daemon=True)
        self.get_key = get_key
        self.on_press = on_press
        self.label = label
        self.stop_event = threading.Event()
        self.current = None
        self.bound = False

    def stop(self):
        self.stop_event.set()
        self.join(timeout=2)

    def run(self):
        try:
            user32 = ctypes.windll.user32
        except (AttributeError, OSError):
            return
        msg = wintypes.MSG()
        try:
            while not self.stop_event.is_set():
                try:
                    wanted = self.get_key()
                except Exception:
                    wanted = self.current
                if wanted != self.current:
                    self._bind(user32, wanted)
                while user32.PeekMessageW(ctypes.byref(msg), None, WM_HOTKEY, WM_HOTKEY, PM_REMOVE):
                    if msg.wParam == HOTKEY_ID:
                        try:
                            self.on_press()
                        except Exception as e:
                            logger.error(f'跑轴记录: {self.label}热键出错', e)
                self.stop_event.wait(0.1)
        finally:
            if self.bound:
                user32.UnregisterHotKey(None, HOTKEY_ID)
                self.bound = False

    def _bind(self, user32, wanted):
        if self.bound:
            user32.UnregisterHotKey(None, HOTKEY_ID)
            self.bound = False
        self.current = wanted
        spec = HOTKEYS.get(wanted)
        if spec is None:
            return
        if user32.RegisterHotKey(None, HOTKEY_ID, spec[0] | MOD_NOREPEAT, spec[1]):
            self.bound = True
        else:
            logger.warning(f'跑轴记录: {self.label}热键 {wanted} 注册不上(多半被别的程序占了), 在设置里换一个')


class MediaIndex:
    """页签用: 读一局的 .media.idx。帧按时间排好, 读屏小块按 (类型, 名字, 代码位置) 分组。"""

    def __init__(self, run_path):
        self.path, index_path = media_paths(run_path)
        self.frames = []
        self.crops = {}
        self.interval = FRAME_INTERVAL
        try:
            size = os.path.getsize(self.path)
            with open(index_path, encoding='utf-8') as f:
                lines = f.readlines()
        except OSError:
            size, lines = 0, []
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if 'v' in entry and 'o' not in entry:
                self.interval = entry.get('interval', FRAME_INTERVAL)
                continue
            if entry.get('o', 0) + entry.get('n', 0) > size:
                continue  # 没写完就断了
            if entry.get('f'):
                self.frames.append(entry)
            elif entry.get('c'):
                key = (entry.get('k'), json.dumps(entry.get('name'), ensure_ascii=False), entry.get('src'))
                self.crops.setdefault(key, []).append(entry)
        self.frames.sort(key=lambda e: e['t'])
        self.times = [e['t'] for e in self.frames]
        for entries in self.crops.values():
            entries.sort(key=lambda e: e['t'])

    def __bool__(self):
        return bool(self.frames) or bool(self.crops)

    def frame_index(self, t):
        """t 时刻屏幕上的那帧: 不晚于 t 的最后一帧(t 在第一帧之前就给第一帧)。"""
        if not self.times:
            return -1
        return max(0, bisect.bisect_right(self.times, t + 1e-6) - 1)

    def read(self, entry):
        with open(self.path, 'rb') as f:
            f.seek(entry['o'])
            return f.read(entry['n'])

    def crop_for(self, event):
        """事件对应的那块原图: 这一段里存的那张; 没有就是这一段之前同一处最后一张(读数一直没变)。"""
        key = (event.get('k') if event.get('e') == 'read' else 'mark',
               json.dumps(event.get('name'), ensure_ascii=False), event.get('src'))
        entries = self.crops.get(key)
        if not entries:
            return None, False
        # 一段读数的开头和那张图是同一次读屏, 时间戳一样; 段中间读数又变了存的图排在后面
        t0 = event.get('t', 0)
        t1 = event.get('t2', t0)
        inside = before = None
        for entry in entries:
            if abs(entry['t'] - t0) < 0.0005:
                return entry, True
            if inside is None and t0 <= entry['t'] <= t1 + 0.0005:
                inside = entry
            if entry['t'] < t0:
                before = entry
        if inside is not None:
            return inside, True
        return before, False
