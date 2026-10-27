"""读一局的记录文件, 整理成时间线要用的段落和统计。不依赖 Qt, 测试能直接跑。"""
import bisect
import json
import os
import statistics

from .recorder import list_run_files
from .screenrec import TOOL_NAMES, format_clock, load_video_info

KEY_ROLES = {'Resonance Key': 'E', 'Liberation Key': 'R', 'Echo Key': 'Q', 'Tool Key': 'T'}
DEFAULT_KEYS = {'e': 'E', 'r': 'R', 'q': 'Q', 't': 'T'}
SPECIAL_KEYS = {'space': '跳', 'f': 'F', 'shift': '闪', 'lshift': '闪', 'esc': 'Esc', 'tab': 'Tab'}
MOUSE_LABELS = {'left': 'A', 'right': '右键', 'middle': '中键', 'wheel': '滚轮'}
HUD_HIDDEN = -1
# 打开的反馈包解压到 <记录目录>/imported/<包名>/, 里面一份 axis_runs/ 和对方的 feedback.json
IMPORT_DIR = 'imported'
FEEDBACK_INFO = 'feedback.json'
# 按下到松开不到这么久算点按(轴里的普攻是按住几十毫秒的 tap), 不画成「按住」
TAP_MAX = 0.2
# 同一个键隔这么近接着按算连按(切人键、R 会一直按到生效), 时间线上合成一个 ×N
REPEAT_GAP = 0.35


def char_label(char):
    name = char.get('n')
    if name and name.startswith('char_') and char.get('cls') not in (None, '', 'BaseChar'):
        return char['cls']  # 0.1.0 记的是模板名
    return name


def is_manual(header):
    return header.get('mode') == 'manual' or header.get('tool') == 'key_recorder'


class RunSummary:
    """列表里一行: 只读开头和结尾, 不解析整局。"""

    def __init__(self, path, header, end, last_t):
        self.path = path
        self.header = header or {}
        self.end = end
        self.last_t = last_t

    @property
    def name(self):
        return os.path.basename(self.path)

    @property
    def team(self):
        return [char_label(c) if c else '-' for c in self.header.get('team') or ()]

    @property
    def manual(self):
        """人手打的: 插件人为操作模式记的, 或者 WWKeyRecorder 记的。"""
        return is_manual(self.header)

    @property
    def imported(self):
        return import_root(self.path) is not None

    @property
    def duration(self):
        if self.end is not None:
            return self.end.get('t', self.last_t)
        return self.last_t

    @property
    def finished(self):
        return self.end is not None

    @property
    def reason(self):
        return (self.end or {}).get('reason', '进行中' if self.end is None else '')


def _tail_lines(path, size=8192):
    with open(path, 'rb') as f:
        f.seek(0, os.SEEK_END)
        length = f.tell()
        f.seek(max(0, length - size))
        data = f.read().decode('utf-8', errors='replace')
    lines = data.splitlines()
    if length > size and lines:
        lines = lines[1:]
    return lines


def summarize_run(path):
    try:
        with open(path, encoding='utf-8') as f:
            header = json.loads(f.readline() or '{}')
        end, last_t = None, 0.0
        for line in reversed(_tail_lines(path)):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            last_t = max(last_t, float(event.get('t2', event.get('t', 0)) or 0))
            if event.get('e') == 'end':
                end = event
                break
        return RunSummary(path, header, end, last_t)
    except (OSError, ValueError):
        return None


def list_runs(out_dir):
    runs = [summarize_run(path) for path in list_run_files(out_dir)]
    return [run for run in reversed(runs) if run is not None]


def import_root(path):
    """<记录目录>/imported/<包名>/axis_runs/<局>.jsonl -> 那个包的文件夹; 本地记的局返回 None。"""
    folder = os.path.dirname(os.path.abspath(path))
    pack = os.path.dirname(folder)
    if os.path.basename(folder) == 'axis_runs' and os.path.basename(os.path.dirname(pack)) == IMPORT_DIR:
        return pack
    return None


def list_imported_runs(out_dir):
    """打开过的反馈包里的局, 最近打开的在前。"""
    try:
        packs = sorted((e.path for e in os.scandir(os.path.join(out_dir, IMPORT_DIR))
                        if e.is_dir() and not e.name.endswith('.part')), key=os.path.getmtime, reverse=True)
    except OSError:
        return []
    runs = [summarize_run(path) for pack in packs for path in reversed(list_run_files(os.path.join(pack, 'axis_runs')))]
    return [run for run in runs if run is not None]


def load_feedback_info(path):
    root = import_root(path)
    if root is None:
        return None
    try:
        with open(os.path.join(root, FEEDBACK_INFO), encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


class InputMark:
    """一次按键(连按合并后是一组, count/t1 是次数和最后一下); slot 算在谁头上, HUD 不在时算最后一个看到的角色。"""
    __slots__ = ('t', 'label', 'event', 'slot', 'hidden', 'count', 't1')

    def __init__(self, t, label, event, slot, hidden=False):
        self.t = t
        self.label = label
        self.event = event
        self.slot = slot
        self.hidden = hidden
        self.count = 1
        self.t1 = t


class Span:
    """在场段专用的两个字段:
    seen: 最后一次真看到 HUD 的时刻, 之后到 t1 轴没再看过, 实际什么时候变的不知道。
    shaky: HUD 淡入淡出那一帧只认到一个号码, in_team 把第一个没认到的格子当当前角色, 这一段认的人不可信。"""
    __slots__ = ('t0', 't1', 'label', 'value', 'event', 'seen', 'shaky')

    def __init__(self, t0, t1, label, value=None, event=None, seen=None, shaky=False):
        self.t0 = t0
        self.t1 = t1
        self.label = label
        self.value = value
        self.event = event
        self.seen = t1 if seen is None else seen
        self.shaky = shaky

    @property
    def certain(self):
        """认准了某个角色在场(HUD 在且号码认全)。"""
        return self.value != HUD_HIDDEN and not self.shaky


class ReadRow:
    def __init__(self, key, label):
        self.key = key
        self.label = label
        self.events = []

    @property
    def count(self):
        return sum(e.get('n', 1) for e in self.events)


class RunData:
    def __init__(self, path):
        self.path = path
        self.header = {}
        self.end = None
        self.events = []
        self.bad_lines = 0
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    self.bad_lines += 1
                    continue
                kind = event.get('e')
                if kind == 'start':
                    self.header = event
                elif kind == 'end':
                    self.end = event
                else:
                    self.events.append(event)
        self.events.sort(key=lambda e: e.get('t', 0))
        last = max((e.get('t2', e.get('t', 0)) for e in self.events), default=0.0)
        self.duration = max(float((self.end or {}).get('t', 0) or 0), float(last or 0))
        self.names = {}
        for char in self.header.get('team') or ():
            if char:
                self.names[char.get('i')] = char_label(char)
        # in_team 返回的格数 = 认到的号码数 + 1, 号码认全时等于队伍人数
        self.team_size = len(self.names) or 3
        self.key_labels = dict(DEFAULT_KEYS)
        for role, label in KEY_ROLES.items():
            key = (self.header.get('keys') or {}).get(role)
            if key:
                self.key_labels[str(key).lower()] = label
        self.field = self._field_spans()
        self._field_starts = [span.t0 for span in self.field]
        self.video = load_video_info(path)
        self.feedback = load_feedback_info(path)
        self.perform = self._perform_spans()
        self.inputs, self.holds = self._inputs()
        self.input_groups = self._group_inputs()
        self.reads = self._read_rows()
        self.logs = [e for e in self.events if e.get('e') == 'log']
        self.marks = [e for e in self.events if e.get('e') == 'mark']

    @property
    def reason(self):
        return (self.end or {}).get('reason', '没有结束记录(进行中或程序退出)')

    def char_name(self, slot):
        if slot == HUD_HIDDEN:
            return 'HUD 不在'
        return self.names.get(slot) or f'{slot + 1}号位'

    def _span_index(self, t):
        i = bisect.bisect_right(self._field_starts, t) - 1
        if i >= 0 and t < self.field[i].t1:
            return i
        return None

    def slot_at(self, t):
        i = self._span_index(t)
        return None if i is None else self.field[i].value

    def owner_at(self, t):
        """t 时刻的按键算谁的: 认准在场的人; HUD 不在或没认全时往前找最后一个认准的人。"""
        i = self._span_index(t)
        while i is not None and i >= 0:
            if self.field[i].certain:
                return self.field[i].value
            i -= 1
        return None

    def uncertain_at(self, t):
        i = self._span_index(t)
        return i is not None and not self.field[i].certain

    def field_label_at(self, t):
        i = self._span_index(t)
        if i is None:
            return ''
        span = self.field[i]
        if span.certain:
            return span.label
        why = 'HUD 不在' if span.value == HUD_HIDDEN else 'HUD 没认全'
        owner = self.owner_at(t)
        return why if owner is None else f'{self.char_name(owner)}({why})'

    def perf_name(self, event):
        slot = event.get('i')
        return self.names.get(slot) or event.get('c') or self.char_name(slot)

    def input_label(self, event):
        action, key = event.get('a'), event.get('k') or ''
        if action in ('click', 'md', 'mu'):
            return MOUSE_LABELS.get(key, key)
        if action == 'wheel':
            return '滚轮'
        if key in ('1', '2', '3', '4'):
            return '切' + self.char_name(int(key) - 1)
        return self.key_labels.get(key) or SPECIAL_KEYS.get(key) or key.upper()

    def _field_spans(self):
        spans = []
        changes = [e for e in self.events if e.get('e') == 'field']
        for i, event in enumerate(changes):
            t0 = event['t']
            if i + 1 < len(changes):
                t1, seen = changes[i + 1]['t'], changes[i + 1].get('prev_seen')
            else:
                t1, seen = self.duration, (self.end or {}).get('field_seen')
            if seen is not None:
                seen = min(max(seen, t0), t1)
            slot = event.get('i', HUD_HIDDEN) if event.get('team') else HUD_HIDDEN
            shaky = slot != HUD_HIDDEN and event.get('cnt', self.team_size) < self.team_size
            if t1 > t0:
                label = self.char_name(slot) + ('?' if shaky else '')
                spans.append(Span(t0, t1, label, slot, event, seen, shaky))
        return spans

    def _perform_spans(self):
        spans, open_ones = [], {}
        for event in self.events:
            if event.get('e') != 'perf':
                continue
            key = event.get('i')
            if event.get('p') == 'in':
                open_ones[key] = event
            else:
                start = open_ones.pop(key, None)
                if start is not None:
                    label = self.perf_name(start)
                    spans.append(Span(start['t'], event['t'], label, event.get('x'), event))
        for key, start in open_ones.items():
            spans.append(Span(start['t'], self.duration, self.perf_name(start), None, start))
        spans.sort(key=lambda s: s.t0)
        return spans

    def _inputs(self):
        marks, holds, open_ones = [], [], {}
        for event in self.events:
            if event.get('e') != 'in':
                continue
            action = event.get('a')
            label = self.input_label(event)
            t = event['t']
            device = 'mouse' if action in ('click', 'md', 'mu', 'wheel') else 'key'
            hold_key = (device, event.get('k'))
            if action in ('kd', 'md'):
                if hold_key not in open_ones:
                    open_ones[hold_key] = event
                    marks.append(InputMark(t, label, event, self.owner_at(t), self.uncertain_at(t)))
            elif action in ('ku', 'mu'):
                start = open_ones.pop(hold_key, None)
                if start is not None and t - start['t'] >= TAP_MAX:
                    holds.append(Span(start['t'], t, '按住' + label, None, start))
            else:
                marks.append(InputMark(t, label, event, self.owner_at(t), self.uncertain_at(t)))
        for start in open_ones.values():
            holds.append(Span(start['t'], self.duration, '按住' + self.input_label(start) + '(没松)', None, start))
        holds.sort(key=lambda s: s.t0)
        return marks, holds

    def _group_inputs(self):
        """连按合成一组, 只给时间线画; 统计和表格还是一次一条。"""
        groups, last = [], {}
        for mark in self.inputs:
            device = 'mouse' if mark.event.get('a') in ('click', 'md', 'wheel') else 'key'
            key = (device, mark.label)
            group = last.get(key)
            if group is not None and mark.t - group.t1 <= REPEAT_GAP:
                group.count += 1
                group.t1 = mark.t
                continue
            group = InputMark(mark.t, mark.label, mark.event, mark.slot, mark.hidden)
            last[key] = group
            groups.append(group)
        return groups

    def _read_rows(self):
        rows = {}
        for event in self.events:
            if event.get('e') != 'read':
                continue
            src = event.get('src', '')
            key = (event.get('k'), str(event.get('name')), src)
            row = rows.get(key)
            if row is None:
                function = src.split(' ', 1)[1] if ' ' in src else src
                row = rows[key] = ReadRow(key, f"{event.get('name')} · {function}")
            row.events.append(event)
        return sorted(rows.values(), key=lambda r: r.events[0]['t'])

    def entries(self, slot):
        """slot 号位每次真正上场(从别人换过来)的时刻; 大招演出 HUD 消失又回来、号码没认全的那一帧都不算。"""
        times, previous = [], None
        for span in self.field:
            if not span.certain:
                continue
            if span.value == slot and previous != slot:
                times.append(span.t0)
            previous = span.value
        return times

    def laps(self, slot):
        times = self.entries(slot)
        return [(times[i], times[i + 1] - times[i]) for i in range(len(times) - 1)]

    def default_lap_slot(self):
        """没选过时按每圈只上场一次的角色分圈: 上场次数最少(至少两次)的那个。"""
        candidates = [(len(self.entries(slot)), slot) for slot in sorted(self.names)]
        candidates = [c for c in candidates if c[0] >= 2]
        return min(candidates)[1] if candidates else None

    def lap_breakdown(self, slot):
        """按 slot 上场分圈, 每圈拆成依次上场的各段。
        返回 (列, 圈): 列是 (号位, 这圈第几次上场) 按最常见的上场顺序排; 圈是 [(开始, 整圈时长, {列: (开始, 时长)})]。"""
        changes, previous = [], None
        for span in self.field:
            if span.certain and span.value != previous:
                changes.append((span.t0, span.value))
                previous = span.value
        starts = [i for i, (_, value) in enumerate(changes) if value == slot]
        laps, orders = [], []
        for a, b in zip(starts, starts[1:]):
            segments, count, order = {}, {}, []
            for i in range(a, b):
                t0, value = changes[i]
                count[value] = count.get(value, 0) + 1
                key = (value, count[value])
                segments[key] = (t0, changes[i + 1][0] - t0)
                order.append(key)
            laps.append((changes[a][0], changes[b][0] - changes[a][0], segments))
            orders.append(tuple(order))
        columns = list(max(orders, key=orders.count)) if orders else []
        for order in orders:
            columns += [key for key in order if key not in columns]
        return columns, laps

    @staticmethod
    def lap_baselines(columns, laps, min_laps=3):
        """每圈只和上场顺序相同的圈比(按半圈分时两种圈交替, 混着算中位数会把长的那种全标成慢)。
        返回 (每圈各列加整圈的中位数, 同顺序不到 min_laps 圈的给 None; 按顺序分的组 {顺序: [圈号]})。"""
        groups = {}
        for i, (_, _, segments) in enumerate(laps):
            groups.setdefault(tuple(segments), []).append(i)
        baselines = [None] * len(laps)
        for members in groups.values():
            if len(members) >= min_laps:
                medians = RunData.group_medians(columns, laps, members)
                for i in members:
                    baselines[i] = medians
        return baselines, groups

    @staticmethod
    def group_medians(columns, laps, members):
        medians = []
        for key in list(columns) + [None]:
            if key is None:
                durations = [laps[i][1] for i in members]
            else:
                durations = [laps[i][2][key][1] for i in members if key in laps[i][2]]
            medians.append(statistics.median(durations) if durations else None)
        return medians

    def segment_label(self, key, columns):
        slot, nth = key
        if any(other[0] == slot and other[1] > 1 for other in columns):
            return f'{self.char_name(slot)} {"①②③④⑤⑥⑦⑧⑨"[nth - 1] if nth <= 9 else nth}'
        return self.char_name(slot)

    def char_stats(self):
        """E/R/Q 的按键次数和连按合并后的段数; HUD 不在时按的算在最后一个看到的角色头上。"""
        stats = {}
        for slot in sorted(self.names):
            stats[slot] = {'name': self.char_name(slot), 'field': 0.0, 'entries': len(self.entries(slot)),
                           'keys': {label: [0, 0] for label in ('E', 'R', 'Q')}}
        for span in self.field:
            if span.certain and span.value in stats:
                stats[span.value]['field'] += span.t1 - span.t0
        for marks, column in ((self.inputs, 0), (self.input_groups, 1)):
            for mark in marks:
                if mark.slot in stats and mark.label in ('E', 'R', 'Q'):
                    stats[mark.slot]['keys'][mark.label][column] += 1
        return stats

    def hud_hidden_time(self):
        return sum(s.t1 - s.t0 for s in self.field if s.value == HUD_HIDDEN)

    def unseen_time(self, slot=None):
        """最后一次看到之后轴没再看 HUD 的时长; slot=HUD_HIDDEN 只算 HUD 不在的段(HUD 可能早就回来了)。"""
        return sum(s.t1 - s.seen for s in self.field if slot is None or s.value == slot)


EVENT_KINDS = {'in': '按键', 'field': '在场', 'perf': '出招', 'read': '读屏', 'log': '日志', 'mark': '打点'}
READ_KINDS = {'find': '找图', 'feat': '找图', 'color': '颜色', 'cd': '冷却', 'con': '协奏'}
ACTIONS = {'key': '按', 'kd': '按下', 'ku': '松开', 'click': '点', 'md': '按下', 'mu': '松开', 'wheel': '滚'}
LOG_LEVELS = {10: 'DEBUG', 20: 'INFO', 30: 'WARN', 40: 'ERROR', 50: 'FATAL'}


def source_text(event):
    parts = []
    if event.get('src'):
        parts.append(event['src'])
    if event.get('src2'):
        parts.append('← ' + event['src2'])
    if event.get('via'):
        parts.append('经由 ' + event['via'])
    return '  '.join(parts)


def _num(value):
    if isinstance(value, bool) or value is None:
        return str(value)
    if isinstance(value, float):
        return f'{value:.3f}'.rstrip('0').rstrip('.') if value else '0'
    return str(value)


def describe(run, event):
    """事件 -> (类型, 内容), 表格和悬停提示共用。"""
    kind = event.get('e')
    if kind == 'in':
        text = f"{ACTIONS.get(event.get('a'), event.get('a'))} {run.input_label(event)}"
        if event.get('dt') is not None:
            text += f"  按住 {event['dt']:.2f}s"
        extras = []
        is_mouse = event.get('a') in ('click', 'md', 'mu', 'wheel')
        if event.get('k') and not is_mouse and run.input_label(event) != str(event['k']).upper():
            extras.append(f"键 {event['k']}")
        if event.get('th'):
            extras.append(f"线程 {event['th']}")
        if event.get('si'):
            extras.append('SendInput')
        if event.get('cut'):
            extras.append('收尾/切出游戏时还按着')
        if event.get('lag'):
            extras.append(f"晚到 {event['lag']}ms, 时刻不准")
        if extras:
            text += '  (' + ', '.join(extras) + ')'
        return '按键', text
    if kind == 'field':
        if not event.get('team'):
            return '在场', f"HUD 不在 (认到 {event.get('cnt', '?')} 格)"
        return '在场', f"{run.char_name(event.get('i'))} (认到 {event.get('cnt', '?')} 格)"
    if kind == 'perf':
        phase = '开始' if event.get('p') == 'in' else '结束'
        text = f"{run.perf_name(event)} 出招{phase}"
        if event.get('x'):
            text += f"  ({event['x']})"
        return '出招', text
    if kind == 'read':
        k = event.get('k')
        text = f"{READ_KINDS.get(k, k)} {event.get('name')} = {_num(event.get('v'))}"
        if 'ok' in event:
            text += '  认到' if event['ok'] else '  没认到'
        if 'lo' in event or 'hi' in event:
            text += f"  [{_num(event.get('lo'))}~{_num(event.get('hi'))}]"
        if 'last' in event:
            text += f"  最后 {_num(event['last'])}"
        if event.get('th'):
            text += f"  阈值 {_num(event['th'])}"
        if event.get('n', 1) > 1:
            text += f"  连读 {event['n']} 次 {event.get('t2', event['t']) - event['t']:.2f}s"
        return '读屏', text
    if kind == 'log':
        return '日志', f"[{LOG_LEVELS.get(event.get('l'), event.get('l'))}] {event.get('m', '')}"
    if kind == 'mark':
        return '打点', f"{event.get('name')} = {_num(event.get('v'))}"
    return kind or '?', json.dumps(event, ensure_ascii=False)


def lap_line(run, lap_slot):
    if lap_slot is None:
        return ''
    laps = run.laps(lap_slot)
    if not laps:
        return f'按 {run.char_name(lap_slot)} 分圈: 不到两次上场, 分不了圈'
    durations = [d for _, d in laps]
    shown = ' / '.join(f'{d:.2f}' for d in durations[:8]) + (' …' if len(durations) > 8 else '')
    return (f'按 {run.char_name(lap_slot)} 分 {len(laps)} 圈: {shown}'
            f'  (平均 {sum(durations) / len(durations):.2f}s)')


def video_pending(run):
    """这局要录屏, 但对时结果还没写出来(打完后一两分钟)。"""
    return bool(run.header.get('rec')) and run.video is None


def video_offset(run):
    video = run.video or {}
    return video.get('offset') if video.get('path') else None


def video_clock(run, t):
    """记录里 t 秒对应录屏的哪一刻; 没录屏返回 None。"""
    offset = video_offset(run)
    if offset is None:
        return None
    at = t + offset
    duration = (run.video or {}).get('duration') or 0
    if at < 0 or (duration and at > duration):
        return '录屏里没有这一刻'
    return f'录屏 {format_clock(at)}'


def video_line(run):
    video = run.video
    if video is None:
        if video_pending(run):
            rec = run.header['rec']
            return f"录屏: {TOOL_NAMES.get(rec.get('tool'), rec.get('tool'))} 打完后在找录屏文件、对时(一两分钟)"
        return None
    if video_offset(run) is None:
        line = f"录屏: {video.get('method')}: {video.get('note', '')}"
    else:
        line = (f"录屏: {os.path.basename(video['path'])}  ·  录屏秒 = 记录秒 {video['offset']:+.3f}"
                f"({video.get('method')})")
        if video.get('note'):
            line += f"\n    {video['note']}"
    if video.get('flow'):
        line += f"\n    {video['flow']}"
    return line


def headline(run, lap_slot=None):
    """页面上常驻的一行; 其余的放悬停提示(detail_lines)。"""
    parts = [f'总时长 {run.duration:.2f}s', f'结束: {run.reason}']
    if run.header.get('note'):
        parts.insert(0, f"备注: {run.header['note']}")
    if run.feedback is not None:
        note = ' / '.join(line.strip() for line in str(run.feedback.get('note') or '').splitlines() if line.strip())
        parts.insert(0, f'反馈: {note}' if note else '导入的反馈包(没写哪里不对)')
    if video_offset(run) is not None:
        parts.append(f"录屏 {run.video['offset']:+.2f}s({run.video.get('method')})")
    elif run.video is not None or video_pending(run):
        parts.append('录屏对时中' if run.video is None else f"录屏 {run.video.get('method')}")
    lap = lap_line(run, lap_slot)
    if lap:
        parts.append(lap)
    if run.bad_lines:
        parts.append(f'有 {run.bad_lines} 行读不出来(可能是程序中途退出)')
    return '  ·  '.join(parts)


def detail_lines(run):
    header = run.header
    unseen = run.unseen_time(HUD_HIDDEN)
    team_ver = f" v{header['team_ver']}" if header.get('team_ver') else ''
    size = header.get('size')
    task = header.get('task', '')
    if header.get('by'):
        task += f"({header['by']}开的局)"
    lines = [
        f"{header.get('team_dir') or ' / '.join(run.char_name(i) for i in sorted(run.names))}{team_ver}"
        f"  ·  {task}" + (f"  ·  {size[0]}x{size[1]}" if size else ''),
        f"HUD 不在 {run.hud_hidden_time():.2f}s" + (f"(其中 {unseen:.2f}s 轴没在看)" if unseen >= 0.05 else ''),
        f"按键 {len(run.inputs)}  ·  读屏 {sum(r.count for r in run.reads)} 次({len(run.reads)} 处)  ·  日志 {len(run.logs)}",
    ]
    end = run.end or {}
    if end.get('pauses'):
        lines.append(f"鸣潮切出去 {end['pauses']} 次, 一共 {end.get('paused', 0):.1f}s(这几段不记按键)")
    video = video_line(run)
    if video:
        lines.append(video)
    feedback = run.feedback
    if feedback is not None:
        lines.append(f"反馈包: {feedback.get('packed', '?')} 打的  ·  ok-ww {feedback.get('ok_ww') or '?'}  ·  "
                     f"插件 v{feedback.get('plugin_packed') or '?'}  ·  {feedback.get('os') or '?'}")
    stats = run.char_stats()
    if stats:
        def keys(s):
            return ' '.join(f'{label} {presses}' + (f'({groups} 段)' if groups != presses else '')
                            for label, (presses, groups) in s['keys'].items())

        lines += [f"{s['name']}: 在场 {s['field']:.1f}s, 上场 {s['entries']} 次, 按键 {keys(s)}"
                  for s in stats.values()]
    return lines
