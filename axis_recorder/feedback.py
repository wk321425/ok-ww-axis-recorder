"""一键反馈包: 一局的记录、整局画面、这局按 F8 导出的原图、版本信息和这局前后的 ok-ww 日志打成一个 zip。不依赖 Qt。

包里的 axis_runs/ 和本地 logs/axis_runs/ 是一个结构, 收到的人解压进去就能在复盘页打开这一局。"""
import datetime
import json
import os
import platform
import re
import time
import zipfile

from .media import EXPORT_DIR, MediaIndex, finished_exports, folder_bytes, media_paths
from .runs import RunData, detail_lines, headline

FEEDBACK_DIR = 'feedback'
FEEDBACK_KEEP = 10
# 日志多带开打前、结束后各这么多秒: 前面有认人/选队, 后面有脱战判定
LOG_MARGIN = 30.0
LOG_STAMP = re.compile(r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} ')
CHUNK = 1 << 20
# 打包途中轴开打了, 每写一块歇这么久, 别跟轴抢 GIL
BUSY_PAUSE = 0.02
SUMMARY_NAME = '反馈说明.txt'
LOG_NAME = 'ok-script_这一局.log'
# JPEG/PNG 已经压过, 再压只费 CPU
STORED_SUFFIXES = ('.media', '.png', '.jpg')


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def run_exports(out_dir, run_name):
    """这局按 F8 导出的文件夹(info.json 里记着是哪一局), 按时间排。"""
    result = []
    for folder in sorted(finished_exports(out_dir)):
        try:
            with open(os.path.join(folder, 'info.json'), encoding='utf-8') as f:
                if json.load(f).get('run') == run_name:
                    result.append(folder)
        except (OSError, ValueError):
            pass
    return result


def _png_count(folder):
    try:
        return sum(1 for name in os.listdir(folder) if name.endswith('.png'))
    except OSError:
        return 0


class FeedbackPlan:
    """点「打包反馈」时先算好要装什么、多大, 对话框里给人看。"""

    def __init__(self, run_path, out_dir):
        self.run_path = run_path
        self.out_dir = out_dir
        self.name = os.path.basename(run_path)
        self.media = [p for p in media_paths(run_path) if os.path.isfile(p)]
        self.frames = len(MediaIndex(run_path).frames) if self.media else 0
        self.exports = run_exports(out_dir, self.name)
        self.export_images = sum(_png_count(f) for f in self.exports)
        self.base_bytes = sum(_size(p) for p in [run_path] + self.media)
        self.export_bytes = sum(folder_bytes(f) for f in self.exports)


def default_log_path():
    """ok-ww 正在写的日志; 拿不到就用它的默认位置 logs/ok-script.log。"""
    try:
        from ok.util import logger as ok_logger
        handler = getattr(ok_logger, '_file_handler', None)
        if handler is not None and handler.baseFilename:
            return handler.baseFilename
    except Exception:
        pass
    return os.path.abspath(os.path.join('logs', 'ok-script.log'))


def log_files(log_path, wall0, wall1):
    """过了午夜 ok-ww 把旧日志改名成 ok-script.<日期>.log, 一局前后可能跨两份。"""
    stem, ext = os.path.splitext(log_path)
    day = datetime.date.fromtimestamp(wall0 - LOG_MARGIN)
    last = datetime.date.fromtimestamp(wall1 + LOG_MARGIN)
    files = []
    while day <= last:
        files.append(f'{stem}.{day.isoformat()}{ext}')
        day += datetime.timedelta(days=1)
    files.append(log_path)
    return [p for p in files if os.path.isfile(p)]


def _stamp(t):
    return datetime.datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M:%S,%f')[:23]


def log_excerpt(log_path, wall0, wall1, margin=LOG_MARGIN):
    """日志里开打前 margin 秒到结束后 margin 秒的行; 异常堆栈这种没时间戳的行跟着上一行走。"""
    lo, hi = _stamp(wall0 - margin), _stamp(wall1 + margin)
    lines = []
    for path in log_files(log_path, wall0, wall1):
        keep = False
        try:
            with open(path, encoding='utf-8', errors='replace') as f:
                for line in f:
                    if LOG_STAMP.match(line):
                        stamp = line[:23]
                        if stamp > hi:
                            break
                        keep = stamp >= lo
                    if keep:
                        lines.append(line)
        except OSError:
            pass
    return lines


def _ok_version():
    try:
        from ok import og
        return (og.config or {}).get('version')
    except Exception:
        return None


def _plugin_version():
    try:
        with open(os.path.join(os.path.dirname(__file__), 'manifest.json'), encoding='utf-8') as f:
            return json.load(f).get('version')
    except (OSError, ValueError):
        return None


def summary_text(info, run, lap_slot):
    exports = info['exports']
    log = info['log']
    lines = [
        f"跑轴反馈包 · 跑轴记录 v{info['plugin_packed'] or '?'} · 打包于 {info['packed']}",
        '',
        '【哪里不对】',
        info['note'].strip() or '(没填)',
        '',
        '【这一局】',
        f"开打 {info['start']} · {info['duration']:.1f}s · 结束: {info['reason']}",
        headline(run, lap_slot),
        *detail_lines(run),
        '',
        '【环境】',
        f"ok-ww {info['ok_ww'] or '?'} · 录这局的插件 v{info['plugin_recorded'] or '?'} · {info['os']}",
        '',
        '【包里有】',
        f"axis_runs/{info['run']}  这局的按键、读屏、在场、日志记录",
        (f"axis_runs/{info['run'][:-6]}.media  整局画面 {info['frames']} 帧(960x540)" if info['frames']
         else '整局画面: 这局没存(设置里关了「整局存画面」, 或者已经超出保留局数被删了)'),
    ]
    if exports:
        lines.append(f"axis_runs/{EXPORT_DIR}/  F8 导出 {len(exports)} 次 {sum(e['images'] for e in exports)} 张原图: "
                     + ', '.join(f"{e['from']:.2f}~{e['to']:.2f}s" for e in exports))
    else:
        lines.append('F8 导出的原图: ' + ('这局没导出' if not info['exports_skipped'] else
                                          f"这局导出过 {info['exports_skipped']} 次, 打包时没带"))
    lines.append(f"{LOG_NAME}  ok-ww 日志 {log['from'][11:19]} ~ {log['to'][11:19]} 共 {log['lines']} 行"
                 if log['lines'] else '日志: 没找到这局的日志(日志文件不在, 或者 ok-ww 关了写日志)')
    lines += [
        '',
        '【怎么看】',
        '把 axis_runs 文件夹解压到 ok-ww 的 logs 下面(和原来的合并), 在「跑轴复盘」页选这一局。',
        '插件开新局时按时间删旧记录(默认留 50 局, 画面留 5 局): 收到的这局比本地的都旧的话, 看完前别开打, 或者另存一份。',
    ]
    return '\n'.join(lines) + '\n'


def _add(zf, src, arcname, pause):
    entry = zipfile.ZipInfo.from_file(src, arcname)
    entry.compress_type = zipfile.ZIP_STORED if src.endswith(STORED_SUFFIXES) else zipfile.ZIP_DEFLATED
    with open(src, 'rb') as f, zf.open(entry, 'w') as out:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            out.write(chunk)
            pause()


def _export_info(folder):
    try:
        with open(os.path.join(folder, 'info.json'), encoding='utf-8') as f:
            frames = json.load(f).get('frames') or []
    except (OSError, ValueError):
        frames = []
    return {'folder': os.path.basename(folder), 'images': _png_count(folder),
            'from': frames[0]['t'] if frames else 0.0, 'to': frames[-1]['t'] if frames else 0.0}


def _prune(folder):
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if name.endswith('.part'):
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass
    zips = sorted((n for n in names if n.endswith('.zip')), key=lambda n: _mtime(os.path.join(folder, n)))
    for name in zips[:max(0, len(zips) - FEEDBACK_KEEP)]:
        try:
            os.remove(os.path.join(folder, name))
        except OSError:
            pass


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def build_feedback(plan, note='', with_exports=True, lap_slot=None, log_path=None, busy=None):
    """打成 <记录目录>/feedback/<这局>_反馈.zip, 返回路径。busy() 为真(轴开打了)时每写一块歇一下。"""
    run = RunData(plan.run_path)
    header = run.header
    wall0 = float(header.get('wall') or _mtime(plan.run_path) - run.duration)
    wall1 = wall0 + run.duration
    log_lines = log_excerpt(log_path or default_log_path(), wall0, wall1)
    exports = plan.exports if with_exports else []
    if lap_slot is None:
        lap_slot = run.default_lap_slot()
    info = {
        'note': note or '', 'run': plan.name, 'start': _stamp(wall0)[:19], 'packed': _stamp(time.time())[:19],
        'duration': round(run.duration, 2), 'reason': run.reason,
        'team_dir': header.get('team_dir'), 'team_ver': header.get('team_ver'),
        'team': [run.char_name(i) for i in sorted(run.names)], 'size': header.get('size'),
        'plugin_recorded': header.get('plugin'), 'plugin_packed': _plugin_version(), 'ok_ww': _ok_version(),
        'os': platform.platform(), 'frames': plan.frames,
        'exports': [_export_info(f) for f in exports], 'exports_skipped': len(plan.exports) - len(exports),
        'log': {'from': _stamp(wall0 - LOG_MARGIN), 'to': _stamp(wall1 + LOG_MARGIN), 'lines': len(log_lines)},
    }

    def pause():
        if busy is not None and busy():
            time.sleep(BUSY_PAUSE)

    folder = os.path.join(plan.out_dir, FEEDBACK_DIR)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f'{plan.name[:-6]}_反馈.zip')
    part = path + '.part'
    try:
        with zipfile.ZipFile(part, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(SUMMARY_NAME, summary_text(info, run, lap_slot))
            zf.writestr('feedback.json', json.dumps(info, ensure_ascii=False, indent=1))
            zf.writestr(LOG_NAME, ''.join(log_lines))
            for src in [plan.run_path] + plan.media:
                _add(zf, src, 'axis_runs/' + os.path.basename(src), pause)
            for export in exports:
                for name in sorted(os.listdir(export)):
                    _add(zf, os.path.join(export, name), f'axis_runs/{EXPORT_DIR}/{os.path.basename(export)}/{name}',
                         pause)
        os.replace(part, path)
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise
    _prune(folder)
    return path
