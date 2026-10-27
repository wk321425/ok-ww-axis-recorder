"""「跑轴复盘」页: 选一局, 上面是时间线, 下面是能过滤的事件表和分圈对比, 右边是那一刻的画面, 点一下互相跳。"""
import bisect
import collections
import json
import os
import subprocess
import threading

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPointF, QRect, QRectF, QSortFilterProxyModel, Qt, \
    QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QImage, QPainter, QPen
from PySide6.QtWidgets import QAbstractItemView, QFrame, QHBoxLayout, QHeaderView, QSizePolicy, QSplitter, \
    QStackedWidget, QTableWidgetItem, QToolTip, QVBoxLayout, QWidget

from .feedback import FEEDBACK_DIR, FeedbackPlan, build_feedback, import_feedback
from .media import EXPORT_DIR, MediaIndex, clear_exports, export_usage
from .recorder import HOLDER_ATTR, default_out_dir
from .runs import (HUD_HIDDEN, RunData, Span, describe, detail_lines, headline, import_root, list_imported_runs,
                   list_runs, source_text, video_clock, video_pending)
from .screenrec import load_video_info, video_info_path

try:
    # Fluent 控件才跟得上 ok-ww 的深色主题; 没装 qfluentwidgets 的环境退回 Qt 原生控件。
    # ScrollArea 只是给原生滚动条套了层皮, horizontalScrollBar() 读写照旧
    from qfluentwidgets import BodyLabel, CheckBox, ComboBox, FluentIcon, InfoBar, InfoBarPosition, MessageBox, \
        MessageBoxBase, PlainTextEdit, PushButton, ScrollArea, SearchLineEdit, SegmentedWidget, Slider, \
        SubtitleLabel, TableView, TableWidget, isDarkTheme
except Exception:
    from PySide6.QtWidgets import QCheckBox as CheckBox, QComboBox as ComboBox, QLabel as BodyLabel, \
        QLineEdit as SearchLineEdit, QPlainTextEdit as PlainTextEdit, QPushButton as PushButton, \
        QScrollArea as ScrollArea, QSlider as Slider, QTableView as TableView, QTableWidget as TableWidget
    FluentIcon = SegmentedWidget = MessageBox = MessageBoxBase = InfoBar = InfoBarPosition = SubtitleLabel = None

    def isDarkTheme():
        return False

LABEL_W = 210
RULER_H = 24
PREFS_FILE = '_tab_prefs.json'
# 每 0.5 秒看一眼是不是刚收尾(刚收尾马上刷新), 平时 3 秒读一次盘
TICK_MS = 500
REFRESH_TICKS = 6
SLOT_COLORS = ['#4C8DF6', '#F28C38', '#3DB37A', '#B05CD6']
HIDDEN_COLOR = '#8A8A8A'
KEY_COLORS = {'E': '#20A4A4', 'R': '#E04848', 'Q': '#8E5BD8', 'F': '#D4A017', 'T': '#6B8E23'}
LEVEL_COLORS = {30: '#E69A28', 40: '#E04848', 50: '#E04848'}
# 分圈对比里比这一列中位数慢这么多秒就标出来
LAP_SLOW = 1.0
SLOW_COLOR = '#E8871E'
# 拖红线到可视区边上这么近就开始滚, 每次最多滚这么多像素
DRAG_EDGE = 16
DRAG_SCROLL = 40


def _add_item(box, text, data):
    # Fluent 的 ComboBox.addItem 第二个参数是图标
    if FluentIcon is not None:
        box.addItem(text, userData=data)
    else:
        box.addItem(text, data)


def _lap_item(text, t=None, slow=False):
    item = QTableWidgetItem(text)
    item.setTextAlignment(Qt.AlignCenter)
    if t is not None:
        item.setData(Qt.UserRole, t)
    if slow:
        item.setForeground(QBrush(QColor(SLOW_COLOR)))
        font = item.font()
        font.setBold(True)
        item.setFont(font)
    return item


class Lane:
    __slots__ = ('kind', 'label', 'y', 'h', 'items', 'starts')

    def __init__(self, kind, label, h, items=None):
        self.kind = kind
        self.label = label
        self.y = 0
        self.h = h
        self.items = items or []
        self.starts = []


def _start(item):
    if isinstance(item, dict):
        return item.get('t', 0)
    return item.t0 if isinstance(item, Span) else item.t


def _end(item):
    if isinstance(item, dict):
        return item.get('t2', item.get('t', 0))
    return item.t1


class TimelineCanvas(QWidget):
    timeClicked = Signal(float)
    timeDragged = Signal(float)
    eventClicked = Signal(object)

    def __init__(self, scroll_area):
        super().__init__()
        self.scroll_area = scroll_area
        self.run = None
        self.lanes = []
        self.pps = 60.0
        self.cursor_t = None
        self.dark = False
        self._label_rows = {}
        self._dragging = False
        self._drag_moved = False
        self.setMouseTracking(True)
        scroll_area.horizontalScrollBar().valueChanged.connect(self.update)
        scroll_area.verticalScrollBar().valueChanged.connect(self.update)

    # ---------- 数据 ----------

    def set_run(self, run):
        self.run = run
        self.cursor_t = None
        self.lanes = self._build_lanes(run) if run is not None else []
        self._resize()
        self.update()

    def _build_lanes(self, run):
        keys = [m for m in run.input_groups if m.event.get('a') not in ('click', 'md', 'mu', 'wheel')]
        mouse = [m for m in run.input_groups if m.event.get('a') in ('click', 'md', 'mu', 'wheel')]
        key_holds = [h for h in run.holds if h.event.get('a') not in ('md', 'mu')]
        mouse_holds = [h for h in run.holds if h.event.get('a') in ('md', 'mu')]
        lanes = [
            Lane('field', '在场', 22, run.field),
            Lane('perform', '出招(perform)', 16, run.perform),
            Lane('keys', '按键', 28, key_holds + keys),
            Lane('mouse', '鼠标', 22, mouse_holds + mouse),
        ]
        if run.marks:
            lanes.append(Lane('marks', '打点', 18, run.marks))
        for row in run.reads:
            lanes.append(Lane('read', row.label, 14, row.events))
        if run.logs:
            lanes.append(Lane('logs', '日志', 18, run.logs))
        y = RULER_H + 4
        for lane in lanes:
            lane.y = y
            y += lane.h + 3
            lane.items.sort(key=_start)
            lane.starts = [_start(item) for item in lane.items]
        return lanes

    def _resize(self):
        duration = self.run.duration if self.run is not None else 0
        height = (self.lanes[-1].y + self.lanes[-1].h + 12) if self.lanes else 200
        self.resize(int(LABEL_W + max(1.0, duration) * self.pps + 60), int(height))

    def set_zoom(self, pps, anchor_t=None):
        bar = self.scroll_area.horizontalScrollBar()
        viewport = self.scroll_area.viewport().width()
        if anchor_t is None:
            anchor_t = self.t_at(bar.value() + LABEL_W + (viewport - LABEL_W) / 2)
        offset = self.x_at(anchor_t) - bar.value()
        self.pps = max(5.0, min(2000.0, pps))
        self._resize()
        bar.setValue(int(self.x_at(anchor_t) - offset))
        self.update()

    def x_at(self, t):
        return LABEL_W + t * self.pps

    def t_at(self, x):
        return (x - LABEL_W) / self.pps

    def center_on(self, t):
        bar = self.scroll_area.horizontalScrollBar()
        viewport = self.scroll_area.viewport().width()
        bar.setValue(int(self.x_at(t) - LABEL_W - (viewport - LABEL_W) / 2))
        self.cursor_t = t
        self.update()

    # ---------- 画 ----------

    def _colors(self):
        dark = isDarkTheme()
        return {
            'bg': QColor('#202020' if dark else '#FFFFFF'),
            'lane': QColor('#2A2A2A' if dark else '#F5F6F8'),
            'text': QColor('#E6E6E6' if dark else '#202020'),
            'dim': QColor('#8C8C8C' if dark else '#8A8F98'),
            'grid': QColor('#3A3A3A' if dark else '#E3E5E8'),
            'header': QColor('#262626' if dark else '#FAFAFB'),
            'off': QColor('#555555' if dark else '#D5D8DD'),
        }

    def paintEvent(self, event):
        painter = QPainter(self)
        colors = self._colors()
        rect = event.rect()
        painter.fillRect(rect, colors['bg'])
        if self.run is None:
            painter.setPen(colors['dim'])
            painter.drawText(self.rect(), Qt.AlignCenter, '还没有记录。开着插件打一局自动战斗, 打完点「刷新」。')
            return
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, small.pointSizeF() * 0.85))
        painter.setFont(small)
        metrics = QFontMetrics(small)
        t_lo = max(0.0, self.t_at(rect.left()) - 0.5)
        t_hi = self.t_at(rect.right()) + 0.5
        self._draw_grid(painter, colors, rect, t_lo, t_hi)
        self._label_rows = {}
        for lane in self.lanes:
            if lane.y > rect.bottom() or lane.y + lane.h < rect.top():
                continue
            painter.fillRect(QRectF(max(rect.left(), LABEL_W), lane.y, rect.width(), lane.h), colors['lane'])
            draw = getattr(self, '_draw_' + lane.kind)
            for item in self._visible(lane, t_lo, t_hi):
                draw(painter, colors, metrics, lane, item)
        if self.cursor_t is not None:
            painter.setPen(QPen(QColor('#E04848'), 1))
            x = self.x_at(self.cursor_t)
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        self._draw_sticky(painter, colors, metrics, t_lo, t_hi)

    def _visible(self, lane, t_lo, t_hi):
        starts = lane.starts
        if lane.kind in ('keys', 'mouse'):
            # 按住的段可能很长, 和点按混在一起, 数量也不多, 直接全扫
            for item in lane.items:
                if _start(item) <= t_hi and _end(item) >= t_lo:
                    yield item
            return
        i = max(0, bisect.bisect_left(starts, t_lo) - 1)
        while i < len(starts) and starts[i] <= t_hi:
            item = lane.items[i]
            if _end(item) >= t_lo:
                yield item
            i += 1

    def _draw_grid(self, painter, colors, rect, t_lo, t_hi):
        step = self._tick_step()
        painter.setPen(QPen(colors['grid'], 1))
        t = int(t_lo / step) * step
        while t <= t_hi:
            x = self.x_at(t)
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            t += step

    def _tick_step(self):
        for step in (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60):
            if step * self.pps >= 70:
                return step
        return 120

    def _span_rect(self, lane, t0, t1, min_w=2.0):
        x0 = self.x_at(t0)
        return QRectF(x0, lane.y + 1, max(min_w, self.x_at(t1) - x0), lane.h - 2)

    def _text_in(self, painter, color, metrics, rect, text):
        if rect.width() < 14:
            return
        painter.setPen(color)
        painter.drawText(rect.adjusted(3, 0, -2, 0), Qt.AlignVCenter | Qt.AlignLeft,
                         metrics.elidedText(text, Qt.ElideRight, int(rect.width() - 5)))

    def _draw_field(self, painter, colors, metrics, lane, span):
        color = QColor(HIDDEN_COLOR if span.value == HUD_HIDDEN else SLOT_COLORS[span.value % len(SLOT_COLORS)])
        rect = self._span_rect(lane, span.t0, span.t1)
        if span.shaky:
            painter.fillRect(rect, QBrush(color, Qt.Dense5Pattern))
            self._text_in(painter, colors['text'], metrics, rect, span.label)
            return
        if span.seen < span.t1 - 0.02:
            # 最后一次看到之后轴没再看 HUD, 这一截画淡: 实际可能早就变了
            faded = QColor(color)
            faded.setAlpha(70)
            painter.fillRect(rect, faded)
            seen = self._span_rect(lane, span.t0, span.seen)
            painter.fillRect(seen, color)
            self._text_in(painter, QColor('#FFFFFF') if seen.width() >= 30 else colors['text'], metrics, rect,
                          span.label)
            return
        painter.fillRect(rect, color)
        self._text_in(painter, QColor('#FFFFFF'), metrics, rect, span.label)

    def _draw_perform(self, painter, colors, metrics, lane, span):
        slot = span.event.get('i') if span.event else 0
        color = QColor(SLOT_COLORS[(slot or 0) % len(SLOT_COLORS)])
        color.setAlpha(110)
        rect = self._span_rect(lane, span.t0, span.t1)
        painter.fillRect(rect, color)
        self._text_in(painter, colors['text'], metrics, rect, span.label + (f' ({span.value})' if span.value else ''))

    def _key_color(self, label):
        if label.startswith('切'):
            for slot, name in self.run.names.items():
                if label == '切' + (name or ''):
                    return QColor(SLOT_COLORS[slot % len(SLOT_COLORS)])
        return QColor(KEY_COLORS.get(label.replace('按住', '').replace('(没松)', ''), '#7A8088'))

    def _draw_keys(self, painter, colors, metrics, lane, item):
        self._draw_input(painter, colors, metrics, lane, item)

    def _draw_mouse(self, painter, colors, metrics, lane, item):
        self._draw_input(painter, colors, metrics, lane, item)

    def _draw_input(self, painter, colors, metrics, lane, item):
        if isinstance(item, Span):
            color = self._key_color(item.label)
            color.setAlpha(90)
            rect = self._span_rect(lane, item.t0, item.t1)
            painter.fillRect(rect, color)
            self._text_in(painter, colors['text'], metrics, rect, item.label)
            return
        color = self._key_color(item.label)
        x = self.x_at(item.t)
        if item.count > 1:
            # 连按: 起止之间一条淡底, 标 ×N
            band = QColor(color)
            band.setAlpha(55)
            painter.fillRect(self._span_rect(lane, item.t, item.t1), band)
        painter.setPen(QPen(color, 2))
        painter.drawLine(QPointF(x, lane.y + 1), QPointF(x, lane.y + lane.h - 1))
        if self.pps >= 25 and (item.label != 'A' or item.count > 1):
            # 切人只写名字前几个字母, 免得压住紧跟着的 E/R; 全名在悬停提示里
            text = '切' + item.label[1:5] if item.label.startswith('切') else item.label
            if item.count > 1:
                text += f'×{item.count}'
            # 挨得近的标签放上一行, 两行都占了就不写(悬停还看得到)
            rows = self._label_rows.setdefault(lane.kind, [float('-inf'), float('-inf')])
            baselines = [lane.y + lane.h - 6] + ([lane.y + 12] if lane.h >= 26 else [])
            for row, baseline in enumerate(baselines):
                if x + 3 >= rows[row]:
                    painter.setPen(colors['text'])
                    painter.drawText(QPointF(x + 3, baseline), text)
                    rows[row] = x + 3 + metrics.horizontalAdvance(text) + 4
                    break

    def _draw_marks(self, painter, colors, metrics, lane, event):
        x = self.x_at(event['t'])
        painter.setPen(QPen(QColor('#D4A017'), 2))
        painter.drawLine(QPointF(x, lane.y + 1), QPointF(x, lane.y + lane.h - 1))
        painter.setPen(colors['text'])
        painter.drawText(QPointF(x + 3, lane.y + lane.h - 5), str(event.get('name')))

    def _read_color(self, event):
        kind, value = event.get('k'), event.get('v')
        if kind in ('find', 'feat'):
            return QColor('#3DB37A') if event.get('ok') else QColor(self._colors()['off'])
        if not isinstance(value, (int, float)):
            return QColor('#7A8088')
        if kind == 'cd':
            return QColor('#F28C38') if value > 0 else QColor('#3DB37A')
        if kind == 'con':
            color = QColor('#4C8DF6')
            color.setAlpha(255 if value >= 0.99 else int(50 + 170 * max(0.0, min(1.0, value))))
            return color
        color = QColor('#B05CD6')
        color.setAlpha(int(50 + 205 * max(0.0, min(1.0, value * 2))))
        return color

    def _draw_read(self, painter, colors, metrics, lane, event):
        t0 = event['t']
        t1 = event.get('t2', t0)
        rect = self._span_rect(lane, t0, t1, min_w=3.0)
        painter.fillRect(rect, self._read_color(event))

    def _draw_logs(self, painter, colors, metrics, lane, event):
        x = self.x_at(event['t'])
        color = QColor(LEVEL_COLORS.get(event.get('l'), colors['dim'].name()))
        painter.setPen(QPen(color, 1))
        painter.drawLine(QPointF(x, lane.y + 3), QPointF(x, lane.y + lane.h - 3))

    def _draw_sticky(self, painter, colors, metrics, t_lo, t_hi):
        hbar = self.scroll_area.horizontalScrollBar().value()
        vbar = self.scroll_area.verticalScrollBar().value()
        viewport = self.scroll_area.viewport()
        # 左边的泳道名
        painter.fillRect(QRectF(hbar, vbar, LABEL_W, viewport.height()), colors['header'])
        for lane in self.lanes:
            if lane.y + lane.h < vbar + RULER_H or lane.y > vbar + viewport.height():
                continue
            rect = QRectF(hbar + 6, lane.y, LABEL_W - 10, lane.h)
            painter.setPen(colors['text'] if lane.kind != 'read' else colors['dim'])
            painter.drawText(rect, Qt.AlignVCenter | Qt.AlignLeft,
                             metrics.elidedText(lane.label, Qt.ElideMiddle, int(rect.width())))
        # 顶上的时间刻度
        painter.fillRect(QRectF(hbar, vbar, viewport.width(), RULER_H), colors['header'])
        step = self._tick_step()
        t = int(t_lo / step) * step
        painter.setPen(colors['dim'])
        while t <= t_hi:
            x = self.x_at(t)
            if x >= hbar + LABEL_W:
                label = f'{t:.2f}'.rstrip('0').rstrip('.') + 's'
                painter.drawLine(QPointF(x, vbar + RULER_H - 6), QPointF(x, vbar + RULER_H))
                painter.drawText(QPointF(x + 3, vbar + RULER_H - 8), label)
            t += step
        if self.cursor_t is not None and self.x_at(self.cursor_t) >= hbar + LABEL_W:
            painter.setPen(QColor('#E04848'))
            painter.drawText(QPointF(self.x_at(self.cursor_t) + 3, vbar + 10), f'{self.cursor_t:.3f}s')
        painter.fillRect(QRectF(hbar, vbar, LABEL_W, RULER_H), colors['header'])
        painter.setPen(colors['dim'])
        painter.drawText(QRectF(hbar + 6, vbar, LABEL_W - 10, RULER_H), Qt.AlignVCenter | Qt.AlignLeft,
                         'Ctrl+滚轮缩放  点或拖红线定位')

    # ---------- 鼠标 ----------

    def _hit(self, pos):
        x, y = pos.x(), pos.y()
        hbar = self.scroll_area.horizontalScrollBar().value()
        if self.run is None or x < hbar + LABEL_W:
            return None, None
        t = self.t_at(x)
        tolerance = 5 / self.pps
        for lane in self.lanes:
            if lane.y <= y <= lane.y + lane.h:
                best, best_d = None, None
                for item in self._visible(lane, t - tolerance, t + tolerance):
                    t0, t1 = _start(item), _end(item)
                    d = 0 if t0 <= t <= t1 else min(abs(t - t0), abs(t - t1))
                    if d <= tolerance and (best_d is None or d < best_d):
                        best, best_d = item, d
                return lane, best
        return None, None

    def _tooltip(self, lane, item):
        run = self.run
        if isinstance(item, dict):
            kind, text = describe(run, item)
            source = source_text(item)
            return f"{item.get('t', 0):.3f}s  {kind}  {text}" + (f'\n{source}' if source else '')
        if isinstance(item, Span):
            text = f'{item.label}  {item.t0:.3f}–{item.t1:.3f}s  ({item.t1 - item.t0:.3f}s)'
            if lane.kind == 'field' and item.shaky:
                text += ('\nHUD 淡入淡出, 只认到一个号码: 框架把第一个没认到的格子当当前角色, 这一段认的人不可信'
                         '\n分圈、在场时长、按键归属都不算它')
            if lane.kind == 'field' and item.seen < item.t1 - 0.02:
                text += f'\n最后一次看到 {item.seen:.3f}s, 之后 {item.t1 - item.seen:.2f}s 轴没看 HUD(画淡的部分)'
            if item.event is not None and lane.kind in ('keys', 'mouse'):
                source = source_text(item.event)
                if source:
                    text += '\n' + source
            return text
        kind, text = describe(run, item.event)
        if item.count > 1:
            text = f'{item.label} 连按 {item.count} 下  {item.t:.3f}–{item.t1:.3f}s'
        if item.hidden and item.slot is not None:
            text += f'  (HUD 不在或没认全, 算{run.char_name(item.slot)}的)'
        source = source_text(item.event)
        return f'{item.t:.3f}s  {text}' + (f'\n{source}' if source else '')

    def _cursor_rect(self, t):
        if t is None:
            return None
        vbar = self.scroll_area.verticalScrollBar().value()
        # 竖线加顶上那个时间标签
        return QRect(int(self.x_at(t)) - 4, vbar, 96, self.scroll_area.viewport().height())

    def _move_cursor(self, t):
        # 拖的时候只重画新旧两条线附近, 不整片重画
        old, self.cursor_t = self.cursor_t, t
        for rect in (self._cursor_rect(old), self._cursor_rect(t)):
            if rect is not None:
                self.update(rect)

    def _near_cursor(self, x):
        return self.cursor_t is not None and abs(x - self.x_at(self.cursor_t)) <= 5

    def _drag_t(self, x):
        """拖到可视区左右边上顺带滚动。"""
        bar = self.scroll_area.horizontalScrollBar()
        left = bar.value() + LABEL_W
        right = bar.value() + self.scroll_area.viewport().width()
        if x > right - DRAG_EDGE:
            bar.setValue(int(bar.value() + min(DRAG_SCROLL, x - (right - DRAG_EDGE))))
        elif x < left + DRAG_EDGE:
            bar.setValue(int(bar.value() - min(DRAG_SCROLL, left + DRAG_EDGE - x)))
        x = max(x, bar.value() + LABEL_W)
        return max(0.0, min(self.run.duration, self.t_at(x)))

    def mouseMoveEvent(self, event):
        x = event.position().x()
        if self._dragging:
            if event.buttons() & Qt.LeftButton:
                self._drag_moved = True
                self._move_cursor(self._drag_t(x))
                self.timeDragged.emit(self.cursor_t)
            return
        self.setCursor(Qt.SizeHorCursor if self._near_cursor(x) else Qt.ArrowCursor)
        lane, item = self._hit(event.position())
        if item is not None:
            QToolTip.showText(event.globalPosition().toPoint(), self._tooltip(lane, item), self)
        else:
            QToolTip.hideText()

    def mousePressEvent(self, event):
        x = event.position().x()
        if event.button() != Qt.LeftButton or self.run is None \
                or x < self.scroll_area.horizontalScrollBar().value() + LABEL_W:
            return
        # 点哪儿红线到哪儿, 按住不放就能拖
        self._dragging, self._drag_moved = True, False
        self.setCursor(Qt.SizeHorCursor)
        QToolTip.hideText()
        self._move_cursor(max(0.0, self.t_at(x)))
        self.timeClicked.emit(self.cursor_t)
        _, item = self._hit(event.position())
        if isinstance(item, dict):
            self.eventClicked.emit(item)

    def mouseReleaseEvent(self, event):
        if not self._dragging:
            return
        self._dragging = False
        self.setCursor(Qt.SizeHorCursor if self._near_cursor(event.position().x()) else Qt.ArrowCursor)
        if self._drag_moved:
            # 松手才对表格: 拖的途中选行、滚表格太费
            self.timeClicked.emit(self.cursor_t)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            factor = 1.25 if event.angleDelta().y() > 0 else 0.8
            self.set_zoom(self.pps * factor, self.t_at(event.position().x()))
            event.accept()
            return
        super().wheelEvent(event)


BOX_COLORS = {'found': '#3DB37A', 'missed': '#E8871E', 'color': '#B05CD6', 'mark': '#D4A017'}
# 光标前后这么久内读过的框都叠到画面上(读屏是一段一段合并的, 一帧 0.25 秒)
BOX_WINDOW = 0.15
FRAME_CACHE = 24
# 两帧隔这么久就说明轴那段时间没取画面
FRAME_GAP_NOTE = 0.6


def _num_text(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    return f'{value:.2f}'


class FrameView(QWidget):
    """一帧低清画面, 上面叠这一刻读过的框。框是原分辨率坐标, 按画面宽度缩。"""

    def __init__(self):
        super().__init__()
        self.image = None
        self.source_w = None
        self.boxes = []
        self.message = ''
        self.setMinimumSize(240, 135)
        # 高度跟着宽度走(16:9), 下面紧贴着放原图小块, 不留一大片空
        policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return int(width * 9 / 16)

    def sizeHint(self):
        size = super().sizeHint()
        size.setHeight(self.heightForWidth(max(size.width(), 480)))
        return size

    def set_frame(self, image, source_w=None, boxes=(), message=''):
        self.image, self.source_w, self.boxes, self.message = image, source_w, list(boxes), message
        self.update()

    def image_rect(self):
        if self.image is None or self.image.isNull():
            return None
        iw, ih = self.image.width(), self.image.height()
        scale = min(self.width() / iw, self.height() / ih)
        w, h = iw * scale, ih * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.fillRect(self.rect(), QColor('#1A1A1A' if isDarkTheme() else '#E9EBEE'))
        target = self.image_rect()
        if target is None:
            painter.setPen(QColor('#8C8C8C'))
            painter.drawText(self.rect().adjusted(12, 0, -12, 0), Qt.AlignCenter | Qt.TextWordWrap, self.message)
            return
        painter.drawImage(target, self.image)
        if not self.source_w:
            return
        k = target.width() / self.source_w
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, small.pointSizeF() * 0.8))
        painter.setFont(small)
        metrics = QFontMetrics(small)
        for (x, y, w, h), color, label in self.boxes:
            rect = QRectF(target.x() + x * k, target.y() + y * k, max(2.0, w * k), max(2.0, h * k))
            painter.setPen(QPen(QColor(color), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(rect)
            if label:
                text = metrics.elidedText(label, Qt.ElideRight, 160)
                back = QRectF(rect.x(), rect.y() - metrics.height() - 1, metrics.horizontalAdvance(text) + 6,
                              metrics.height())
                if back.y() < target.y():
                    back.moveTop(rect.bottom() + 1)
                fill = QColor(color)
                fill.setAlpha(200)
                painter.fillRect(back, fill)
                painter.setPen(QColor('#FFFFFF'))
                painter.drawText(back.adjusted(3, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, text)


class CropView(QWidget):
    """读屏那块原图, 按整数倍放大(不插值, 像素看得清)。"""

    def __init__(self):
        super().__init__()
        self.image = None
        self.setFixedHeight(120)
        self.setMinimumWidth(120)

    def set_image(self, image):
        self.image = image
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor('#1A1A1A' if isDarkTheme() else '#E9EBEE'))
        if self.image is None or self.image.isNull():
            return
        iw, ih = self.image.width(), self.image.height()
        fit = min(self.width() / iw, self.height() / ih)
        scale = max(1, int(fit)) if fit >= 1 else fit
        w, h = iw * scale, ih * scale
        painter.drawImage(QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h), self.image)


class FramePanel(QWidget):
    """右边那一列: 光标那一刻的画面(带读屏框)、上一帧/下一帧、点读屏事件时那块原图。"""
    stepped = Signal(float)

    def __init__(self):
        super().__init__()
        self.run = None
        self.media = None
        self.index = -1
        self.t = 0.0
        self.boxed = []
        self._cache = collections.OrderedDict()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        self.caption = BodyLabel('画面')
        bar.addWidget(self.caption, 1)
        self.prev_button = PushButton('◀')
        self.next_button = PushButton('▶')
        for button, step in ((self.prev_button, -1), (self.next_button, 1)):
            button.setFixedWidth(44)
            button.clicked.connect(lambda _=False, s=step: self.step(s))
            bar.addWidget(button)
        layout.addLayout(bar)
        self.view = FrameView()
        layout.addWidget(self.view)
        self.crop_view = CropView()
        layout.addWidget(self.crop_view)
        self.crop_text = BodyLabel()
        self.crop_text.setWordWrap(True)
        self.crop_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.crop_text)
        layout.addStretch(1)
        self._clear_crop('')

    def set_run(self, run, media):
        self.run, self.media = run, media
        self._cache.clear()
        self.boxed = []
        if run is not None:
            self.boxed = [e for e in run.events if e.get('e') in ('read', 'mark')
                          and (isinstance(e.get('box'), list) or isinstance(e.get('rb'), list))]
        self._clear_crop('点时间线上的读屏或打点、或者下面表格里的读屏行, 这里显示读的那块原图(不缩)。'
                         if media is not None and media.crops else '')
        self.show_time(0.0)

    def has_frames(self):
        return self.media is not None and bool(self.media.frames)

    def show_time(self, t):
        if not self.has_frames():
            self.index = -1
            self.caption.setText('画面')
            if self.run is None:
                message = ''
            else:
                message = ('这局没存画面。插件设置里开着「整局存画面」打的局才有, 只留最近几局。')
            self.view.set_frame(None, message=message)
            return
        self._show(self.media.frame_index(t), t)

    def step(self, delta):
        if not self.has_frames():
            return
        index = max(0, min(len(self.media.frames) - 1, self.index + delta))
        self.stepped.emit(self.media.frames[index]['t'])

    def refresh_caption(self):
        if self.has_frames() and self.index >= 0:
            self._show(self.index, self.t)

    def _show(self, index, t):
        frames = self.media.frames
        self.index, self.t = index, t
        entry = frames[index]
        caption = f"画面 {entry['t']:.2f}s"
        if t - entry['t'] > 0.02:
            caption += f'(光标 {t:.2f}s)'
        caption += f'   {index + 1}/{len(frames)}'
        clock = video_clock(self.run, t) if self.run is not None else None
        if clock:
            caption += f'   ·  {clock}'  # 剪视频时拿这个时间去录屏里找
        gap = (frames[index + 1]['t'] if index + 1 < len(frames) else t) - entry['t']
        if gap > FRAME_GAP_NOTE and t - entry['t'] > FRAME_GAP_NOTE / 2:
            # 画面是轴自己取的帧: 定时连点、等演出这类不读屏的时候就没有, 别把旧帧当成那一刻
            caption += f'\n这帧之后 {gap:.1f}s 轴没取画面(定时操作或等演出时不读屏)'
        self.caption.setText(caption)
        size = self.run.header.get('size') if self.run is not None else None
        source_w = size[0] if isinstance(size, list) and size else None
        image = self._image(entry)
        self.view.set_frame(image, source_w, self._boxes_at(t),
                            message='' if image is not None else '这一帧读不出来(文件不完整)')

    def _image(self, entry):
        key = entry['o']
        image = self._cache.get(key)
        if image is None:
            try:
                image = QImage.fromData(self.media.read(entry))
            except OSError:
                return None
            if image.isNull():
                return None
            self._cache[key] = image
            if len(self._cache) > FRAME_CACHE:
                self._cache.popitem(last=False)
        else:
            self._cache.move_to_end(key)
        return image

    def _boxes_at(self, t):
        boxes = []
        for event in self.boxed:
            t0 = event.get('t', 0)
            if not t0 - BOX_WINDOW <= t <= event.get('t2', t0) + BOX_WINDOW:
                continue
            name, value = event.get('name'), event.get('v')
            box, found_box = event.get('box'), event.get('rb')
            if event.get('e') == 'mark':
                color, label = BOX_COLORS['mark'], f'{name}={_num_text(value)}'
            elif event.get('k') == 'color':
                color, label = BOX_COLORS['color'], f'{name or "颜色"} {_num_text(value)}'
            elif event.get('ok'):
                color, label = BOX_COLORS['found'], f'{name} {_num_text(value)}'
            else:
                color, label = BOX_COLORS['missed'], f'{name} 没找到'
            if isinstance(box, list) and len(box) == 4:
                boxes.append((box, color, label))
                label = ''
            if isinstance(found_box, list) and len(found_box) == 4:
                boxes.append((found_box, BOX_COLORS['found'], label))
        return boxes[:40]

    def _clear_crop(self, text):
        self.crop_view.set_image(None)
        self.crop_view.setVisible(False)
        self.crop_text.setText(text)
        self.crop_text.setVisible(bool(text))

    def show_event(self, event):
        if not isinstance(event, dict) or event.get('e') not in ('read', 'mark') or self.media is None:
            return
        entry, exact = self.media.crop_for(event)
        if entry is None:
            self._clear_crop(f"{event.get('name')}: 这一处没存原图(框太大、没给框、或者这局没开「存读屏那块原图」)。")
            return
        try:
            image = QImage.fromData(self.media.read(entry))
        except OSError:
            image = None
        x, y, w, h = entry.get('box') or (0, 0, 0, 0)
        text = f"{entry.get('name')}  原图 {w}×{h} @({x}, {y})  {entry['t']:.2f}s 存的"
        if 'v' in entry:
            text += f"  读数 {_num_text(entry['v'])}"
        if 'ok' in entry:
            text += '  找到' if entry['ok'] else '  没找到'
        if not exact:
            text += '\n这一段里读数没变, 显示的是之前最后一次变化时存的那张'
        self.crop_view.set_image(image)
        self.crop_view.setVisible(True)
        self.crop_text.setText(text)
        self.crop_text.setVisible(True)


class EventModel(QAbstractTableModel):
    HEADERS = ['时间(s)', '在场', '类型', '内容', '来源']

    def __init__(self):
        super().__init__()
        self.run = None
        self.rows = []
        self.events = []
        self.times = []

    def set_run(self, run):
        self.beginResetModel()
        self.run = run
        self.rows = []
        self.events = list(run.events) if run is not None else []
        if run is not None:
            for event in run.events:
                kind, text = describe(run, event)
                self.rows.append((event.get('t', 0), run.field_label_at(event.get('t', 0)), kind, text,
                                  source_text(event)))
        self.times = [row[0] for row in self.rows]
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        if role == Qt.DisplayRole:
            value = row[index.column()]
            return f'{value:.3f}' if index.column() == 0 else value
        if role == Qt.ToolTipRole and index.column() >= 3:
            return row[index.column()]
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return None

    def row_near(self, t):
        if not self.times:
            return -1
        i = bisect.bisect_left(self.times, t)
        if i >= len(self.times):
            return len(self.times) - 1
        if i > 0 and t - self.times[i - 1] < self.times[i] - t:
            return i - 1
        return i


class AxisRecorderTab(QWidget):
    # 打包/解包线程做完发回界面线程: zip 路径(解包是那一局的路径), 出错时是异常
    packed = Signal(object)
    unpacked = Signal(object)

    def __init__(self, out_dir=None):
        super().__init__()
        # 插件的设置任务在侧栏另有一个「跑轴记录」分组, 这页换个名字分开
        self.name = '跑轴复盘'
        if FluentIcon is not None:
            self.icon = getattr(FluentIcon, 'HISTORY', FluentIcon.APPLICATION)
        self._out_dir = out_dir
        self.run = None
        self.media = None
        self.summaries = []
        self._listing = None
        self._packing = False
        self._unpacking = False
        self._was_recording = False
        self._refresh_due = REFRESH_TICKS
        self.last_feedback = None
        self.packed.connect(self._on_packed)
        self.unpacked.connect(self._on_unpacked)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        # 两行: 上面选哪一局, 下面是这一局的操作。挤在一行时窗口一窄 Qt 就把按钮叠到下拉框上
        top = QHBoxLayout()
        self.run_box = ComboBox()
        self.run_box.setMinimumWidth(420)
        self.run_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.run_box.currentIndexChanged.connect(self._on_run_selected)
        refresh = PushButton('刷新')
        refresh.clicked.connect(lambda: self.refresh(force=True))
        folder = PushButton('打开文件夹')
        folder.clicked.connect(self._open_folder)
        self.import_button = PushButton('打开反馈包')
        self.import_button.clicked.connect(self._open_feedback)
        self.import_button.setToolTip('选别人发来的反馈包 zip, 解压到记录目录下的 imported 文件夹(只留最近 10 个)直接打开看,\n'
                                      '和本地记录分开放, 开新局时不会被删')
        self.pack_button = PushButton('打包反馈')
        self.pack_button.clicked.connect(self._pack_feedback)
        self.clear_button = PushButton('清空导出原图')
        self.clear_button.clicked.connect(self._clear_exports)
        self.follow = CheckBox('跟随最新一局')
        self.follow.setChecked(True)
        self.lap_box = ComboBox()
        self.lap_box.setMinimumWidth(130)
        self.lap_box.currentIndexChanged.connect(self._on_lap_changed)
        self.zoom = Slider(Qt.Horizontal)
        self.zoom.setRange(5, 600)
        self.zoom.setValue(60)
        self.zoom.setFixedWidth(140)
        self.zoom.valueChanged.connect(lambda v: self.canvas.set_zoom(float(v)))
        # 按钮和勾选框按自己的字宽固定, 窄了只缩下拉框(它按最长那一项要宽度, 不固定的话会把旁边的字挤掉)
        for widget in (self.follow, refresh, folder, self.import_button, self.pack_button, self.clear_button):
            widget.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        top.addWidget(self.run_box, 1)
        for widget in (self.follow, refresh, self.import_button):
            top.addWidget(widget)
        layout.addLayout(top)
        actions = QHBoxLayout()
        for widget in (BodyLabel('分圈按'), self.lap_box, BodyLabel('缩放'), self.zoom):
            actions.addWidget(widget)
        actions.addStretch(1)
        for widget in (self.pack_button, folder, self.clear_button):
            actions.addWidget(widget)
        layout.addLayout(actions)

        # 常驻一行, 队伍/分辨率/按键读屏次数/各角色统计放悬停提示
        self.summary = BodyLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.summary)

        splitter = QSplitter(Qt.Vertical)
        splitter.setHandleWidth(8)
        splitter.setStyleSheet('QSplitter::handle { background: transparent; }')  # 默认是浅色, 深色主题下一道白线
        self.scroll = ScrollArea()
        self.scroll.setWidgetResizable(False)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setStyleSheet('QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }')
        self.canvas = TimelineCanvas(self.scroll)
        self.scroll.setWidget(self.canvas)
        self.canvas.timeClicked.connect(self._on_time_clicked)
        splitter.addWidget(self.scroll)

        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 6, 0, 0)
        bar = QHBoxLayout()
        self.pages = QStackedWidget()
        if SegmentedWidget is not None:
            self.switch = SegmentedWidget()
            self.switch.addItem('events', '事件', lambda: self._show_page(0))
            self.switch.addItem('laps', '分圈对比', lambda: self._show_page(1))
            self.switch.setCurrentItem('events')
        else:
            self.switch = ComboBox()
            self.switch.addItems(['事件', '分圈对比'])
            self.switch.currentIndexChanged.connect(self._show_page)
        self.switch.setFixedWidth(200)
        bar.addWidget(self.switch)
        self.filter = SearchLineEdit()
        self.filter.setPlaceholderText('过滤: 输入角色名、按键、模板名、函数名、日志内容…')
        bar.addWidget(self.filter, 100)
        bar.addStretch(1)  # 分圈页把过滤框藏起来时撑住, 免得切换条被拉满一整行
        bottom_layout.addLayout(bar)
        bottom_layout.addWidget(self.pages, 1)

        self.model = EventModel()
        self.proxy = QSortFilterProxyModel()
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.proxy.setFilterKeyColumn(-1)
        self.filter.textChanged.connect(self.proxy.setFilterFixedString)
        self.table = TableView()
        self.table.setModel(self.proxy)
        self.table.setWordWrap(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(26)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        self.table.clicked.connect(self._on_row_clicked)
        self.pages.addWidget(self.table)

        lap_page = QWidget()
        lap_layout = QVBoxLayout(lap_page)
        lap_layout.setContentsMargins(0, 0, 0, 0)
        self.lap_hint = BodyLabel()
        self.lap_hint.setWordWrap(True)
        lap_layout.addWidget(self.lap_hint)
        self.lap_table = TableWidget()
        self.lap_table.setWordWrap(False)
        self.lap_table.verticalHeader().setVisible(False)
        self.lap_table.verticalHeader().setDefaultSectionSize(28)
        self.lap_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.lap_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.lap_table.cellClicked.connect(self._on_lap_cell)
        lap_layout.addWidget(self.lap_table, 1)
        self.pages.addWidget(lap_page)

        splitter.addWidget(bottom)
        splitter.setSizes([420, 260])

        # 画面占右边一整列: 放在下半截就只剩一百多像素高
        self.frame_panel = FramePanel()
        self.frame_panel.stepped.connect(self._on_frame_step)
        self.canvas.eventClicked.connect(self.frame_panel.show_event)
        self.canvas.timeDragged.connect(self.frame_panel.show_time)
        main = QSplitter(Qt.Horizontal)
        main.setHandleWidth(8)
        main.setStyleSheet('QSplitter::handle { background: transparent; }')
        main.addWidget(splitter)
        main.addWidget(self.frame_panel)
        main.setStretchFactor(0, 3)
        main.setStretchFactor(1, 2)
        main.setSizes([1000, 480])
        layout.addWidget(main, 1)

        self.timer = QTimer(self)
        self.timer.setInterval(TICK_MS)
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self.refresh(force=True)

    # ---------- 数据 ----------

    @property
    def out_dir(self):
        if self._out_dir:
            return self._out_dir
        try:
            from ok.task.task import ExecutorOperation
            recorder = ExecutorOperation.__dict__.get(HOLDER_ATTR)
            if recorder is not None:
                return recorder.out_dir
        except Exception:
            pass
        return default_out_dir()

    def _tick(self):
        # 这页和轴在同一个进程里抢 GIL: 人在游戏里(ok-ww 不是前台窗口)或者正打着, 都不读盘
        recording = self._recording()
        if self._was_recording and not recording:
            self._refresh_due = 0  # 一局刚收尾, 能刷就马上刷, 别让人以为还在录
        self._was_recording = recording
        self._refresh_due -= 1
        if self._refresh_due <= 0 and not recording and self.isVisible() and self.window().isActiveWindow():
            self._refresh_due = REFRESH_TICKS
            self.refresh()

    @staticmethod
    def _recording():
        try:
            from ok.task.task import ExecutorOperation
            recorder = ExecutorOperation.__dict__.get(HOLDER_ATTR)
            return recorder is not None and recorder.run is not None
        except Exception:
            return False

    def refresh(self, force=False):
        self._update_export_button()
        run = self.run
        if run is not None and video_pending(run) and os.path.isfile(video_info_path(run.path)):
            run.video = load_video_info(run.path)  # 打完一两分钟后对时结果才写出来
            self._update_summary()
            self.frame_panel.refresh_caption()
        summaries = list_runs(self.out_dir) + list_imported_runs(self.out_dir)
        listing = [(s.path, s.finished, round(s.duration, 1)) for s in summaries]
        if not force and listing == self._listing:
            return
        newest_changed = self._listing is None or [x[0] for x in listing[:1]] != [x[0] for x in self._listing[:1]]
        self._listing = listing
        current = self.run.path if self.run is not None else None
        self.summaries = summaries
        self.run_box.blockSignals(True)
        self.run_box.clear()
        for summary in summaries:
            stamp = summary.name[:15]
            when = f'{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}'
            state = '' if summary.finished else '  (进行中)'
            kind = ('[导入] ' if summary.imported else '') + ('[手打] ' if summary.manual else '')
            _add_item(self.run_box, f"{when}  {kind}{' / '.join(summary.team)}  {summary.duration:.1f}s  "
                                    f"{summary.reason}{state}", summary.path)
        self.run_box.blockSignals(False)
        target = current
        if self.follow.isChecked() and summaries and (newest_changed or current is None):
            target = summaries[0].path
        index = next((i for i, s in enumerate(summaries) if s.path == target), 0 if summaries else -1)
        if index >= 0:
            self.run_box.setCurrentIndex(index)
            stale = self.run is not None and self.run.end is None and summaries[index].finished
            if force or target != current or stale:
                self.load(summaries[index].path)
        else:
            self.load(None)

    def load(self, path):
        error = None
        try:
            self.run = RunData(path) if path else None
        except (OSError, ValueError) as e:
            self.run, error = None, e
        self.media = None
        if self.run is not None:
            try:
                self.media = MediaIndex(path) or None
            except Exception:
                self.media = None
        self._fill_lap_box()
        self.canvas.set_run(self.run)
        self.model.set_run(self.run)
        self.frame_panel.set_run(self.run, self.media)
        for column, width in enumerate((80, 150, 64, 520)):
            self.table.setColumnWidth(column, width)
        self._update_summary()
        if error is not None:
            self.summary.setText(f'读不了这份记录: {error}')
        self._fill_lap_table()
        self._update_pack_button()

    def _fill_lap_box(self):
        self.lap_box.blockSignals(True)
        self.lap_box.clear()
        _add_item(self.lap_box, '不分圈', None)
        if self.run is not None:
            for slot in sorted(self.run.names):
                _add_item(self.lap_box, self.run.char_name(slot), slot)
            prefs = self._prefs()
            key = self._team_key()
            slot = prefs[key] if key in prefs else self.run.default_lap_slot()
            index = self.lap_box.findData(slot) if slot is not None else 0
            self.lap_box.setCurrentIndex(max(0, index))
        self.lap_box.blockSignals(False)

    def _team_key(self):
        if self.run is None:
            return ''
        return self.run.header.get('team_dir') or '__'.join(self.run.char_name(i) for i in sorted(self.run.names))

    def _prefs(self):
        try:
            with open(os.path.join(self.out_dir, PREFS_FILE), encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_pref(self, slot):
        prefs = self._prefs()
        prefs[self._team_key()] = slot
        self._write_prefs(prefs)

    def _write_prefs(self, prefs):
        try:
            os.makedirs(self.out_dir, exist_ok=True)
            with open(os.path.join(self.out_dir, PREFS_FILE), 'w', encoding='utf-8') as f:
                json.dump(prefs, f, ensure_ascii=False)
        except OSError:
            pass

    def _update_summary(self):
        if self.run is None:
            self.summary.setText(f'还没有记录。记录目录: {self.out_dir}')
            self.summary.setToolTip('')
            return
        self.summary.setText(headline(self.run, self.lap_box.currentData()))
        self.summary.setToolTip('\n'.join(detail_lines(self.run)))

    def _fill_lap_table(self):
        table = self.lap_table
        table.clear()
        table.setRowCount(0)
        table.setColumnCount(0)
        slot = self.lap_box.currentData() if self.run is not None else None
        if slot is None:
            self.lap_hint.setText('上面「分圈按」选一个角色, 按这个角色每次上场切圈, 这里逐段对比每圈里各角色在场多久。')
            return
        columns, laps = self.run.lap_breakdown(slot)
        name = self.run.char_name(slot)
        if not laps:
            self.lap_hint.setText(f'{name} 不到两次上场, 分不了圈。')
            return
        baselines, groups = self.run.lap_baselines(columns, laps)
        main = max(groups.values(), key=len)
        mark = any(b is not None for b in baselines)
        best = self.run.default_lap_slot()
        half = best is not None and best != slot and len(self.run.entries(slot)) > len(self.run.entries(best))
        hint = f'按 {name} 上场分圈, 每格是这一段在场的秒数(含大招演出)。'
        if mark:
            hint += f'比同样上场顺序那几圈的中位数慢 {LAP_SLOW:g} 秒以上的标橙。'
        else:
            hint += '同样上场顺序的圈不到 3 圈, 不标快慢。'
        hint += '点格子, 时间线跳到那一段开头。'
        if len(groups) > 1:
            hint += f'\n这局有 {len(groups)} 种上场顺序, 各自分开比; 最下面的中位数是最常见那种({len(main)} 圈)。'
        if half:
            hint += (f'\n注意: {name} 上场 {len(self.run.entries(slot))} 次, 比 {self.run.char_name(best)}'
                     f'({len(self.run.entries(best))} 次)多, 按这个角色分出来的可能是半圈; 看整圈选 {self.run.char_name(best)}。')
        self.lap_hint.setText(hint)
        rows = [[segments.get(key) for key in columns] + [(start, total)] for start, total, segments in laps]
        medians = self.run.group_medians(columns, laps, main)
        headers = ['圈'] + [self.run.segment_label(key, columns) for key in columns] + ['整圈']
        table.setColumnCount(len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setRowCount(len(rows) + 1)
        for r, row in enumerate(rows):
            table.setItem(r, 0, _lap_item(f'第 {r + 1} 圈', row[-1][0]))
            for c, cell in enumerate(row):
                if cell is None:
                    table.setItem(r, c + 1, _lap_item('-'))
                    continue
                t0, duration = cell
                base = baselines[r][c] if baselines[r] is not None else None
                over = duration - base if base is not None else 0.0
                slow = over >= LAP_SLOW
                table.setItem(r, c + 1, _lap_item(f'{duration:.2f}' + (f'  (+{over:.1f})' if slow else ''), t0, slow))
        table.setItem(len(rows), 0, _lap_item('中位数'))
        for c, median in enumerate(medians):
            table.setItem(len(rows), c + 1, _lap_item('-' if median is None else f'{median:.2f}'))
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        for c in range(len(headers)):
            table.setColumnWidth(c, 80 if c == 0 else 118)

    # ---------- 交互 ----------

    def _show_page(self, index):
        self.pages.setCurrentIndex(index)
        self.filter.setVisible(index == 0)

    def _on_run_selected(self, index):
        if 0 <= index < len(self.summaries):
            self.load(self.summaries[index].path)

    def _on_lap_changed(self, _index):
        if self.run is not None:
            self._save_pref(self.lap_box.currentData())
        self._update_summary()
        self._fill_lap_table()

    def _on_lap_cell(self, row, column):
        item = self.lap_table.item(row, column)
        t = item.data(Qt.UserRole) if item is not None else None
        if t is not None:
            self.canvas.center_on(t)
            self._on_time_clicked(t)

    def _on_time_clicked(self, t):
        self.frame_panel.show_time(t)
        row = self.model.row_near(t)
        if row < 0:
            return
        index = self.proxy.mapFromSource(self.model.index(row, 0))
        if index.isValid():
            self.table.selectRow(index.row())
            self.table.scrollTo(index, QAbstractItemView.PositionAtCenter)

    def _on_frame_step(self, t):
        self.canvas.center_on(t)
        self._on_time_clicked(t)

    def _on_row_clicked(self, index):
        row = self.proxy.mapToSource(index).row()
        if 0 <= row < len(self.model.rows):
            t = self.model.rows[row][0]
            self.canvas.center_on(t)
            self.frame_panel.show_time(t)
            self.frame_panel.show_event(self.model.events[row])

    def _open_folder(self):
        # 看的是导入的反馈包就打开那个包解出来的文件夹(反馈说明、日志都在里面)
        folder = (import_root(self.run.path) if self.run is not None else None) or self.out_dir
        os.makedirs(folder, exist_ok=True)
        try:
            os.startfile(folder)
        except Exception:
            pass

    # ---------- 打开反馈包 ----------

    def _open_feedback(self):
        if self._unpacking:
            return
        from PySide6.QtWidgets import QFileDialog
        prefs = self._prefs()
        start = prefs.get('_import_dir') or os.path.join(os.path.expanduser('~'), 'Downloads')
        path, _ = QFileDialog.getOpenFileName(self, '打开反馈包', start, '反馈包 (*.zip)')
        if not path:
            return
        prefs['_import_dir'] = os.path.dirname(path)
        self._write_prefs(prefs)
        self._unpacking = True
        self.import_button.setEnabled(False)
        self.import_button.setText('解压中…')
        threading.Thread(target=self._unpack_worker, args=(path,), name='axis_recorder_import', daemon=True).start()

    def _unpack_worker(self, path):
        try:
            result = import_feedback(path, self.out_dir, busy=self._recording)
        except Exception as e:
            result = e
        try:
            self.unpacked.emit(result)
        except RuntimeError:
            pass

    def _on_unpacked(self, result):
        self._unpacking = False
        self.import_button.setEnabled(True)
        self.import_button.setText('打开反馈包')
        if isinstance(result, Exception):
            self._toast('error', '打不开这个反馈包', str(result))
            return
        # 看别人的局时别被新打的局切走
        self.follow.setChecked(False)
        self.refresh(force=True)
        index = next((i for i, s in enumerate(self.summaries) if s.path == result), -1)
        if index >= 0:
            self.run_box.setCurrentIndex(index)
        self.load(result)

    def _update_export_button(self):
        count, size = export_usage(self.out_dir)
        text = f'清空导出原图 ({count} 次 · {size / 1e6:.0f}MB)' if count else '清空导出原图'
        if self.clear_button.text() != text:
            self.clear_button.setText(text)
        self.clear_button.setEnabled(bool(count))
        self.clear_button.setToolTip(f'F8 导出的原图在 {os.path.join(self.out_dir, EXPORT_DIR)}\n'
                                     '平时自动只留最近 20 次; 这里一次删光(正在导出的那次不动)')

    def _confirm(self, title, text):
        if MessageBox is not None:
            return bool(MessageBox(title, text, self.window()).exec())
        from PySide6.QtWidgets import QMessageBox
        return QMessageBox.question(self, title, text) == QMessageBox.Yes

    def _clear_exports(self):
        count, size = export_usage(self.out_dir)
        if not count or not self._confirm('清空导出原图',
                                          f'删掉 F8 导出的 {count} 次原图, 共 {size / 1e6:.0f}MB?\n'
                                          '只删导出文件夹, 每局的记录和低清画面不动。'):
            return
        clear_exports(self.out_dir)
        self._update_export_button()

    # ---------- 打包反馈 ----------

    def _update_pack_button(self):
        self.pack_button.setText('打包中…' if self._packing else '打包反馈')
        self.pack_button.setEnabled(self.run is not None and not self._packing)
        self.pack_button.setToolTip('把选中的这一局(记录、整局画面、这局 F8 导出的原图、版本、前后的 ok-ww 日志, 可选带上录屏)打成一个 zip\n'
                                    f'存在 {os.path.join(self.out_dir, FEEDBACK_DIR)}, 留最近 10 个')

    def _pack_feedback(self):
        if self.run is None or self._packing:
            return
        if self._recording():
            self._toast('warning', '正在录一局', '打完再打包, 免得跟轴抢资源')
            return
        try:
            plan = FeedbackPlan(self.run.path, self.out_dir)
        except Exception as e:
            self._toast('error', '打包失败', str(e))
            return
        accepted, note, with_exports, with_video = self._ask_feedback(plan)
        if not accepted:
            return
        self._packing = True
        self._update_pack_button()
        threading.Thread(target=self._pack_worker,
                         args=(plan, note, with_exports, self.lap_box.currentData(), with_video),
                         name='axis_recorder_feedback', daemon=True).start()

    def _ask_feedback(self, plan):
        """返回 (打不打, 哪里不对, 带不带导出原图, 带不带录屏)。"""
        stamp = plan.name[:15]
        when = f'{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}'
        team = ' / '.join(self.run.char_name(i) for i in sorted(self.run.names))
        frames = f' + 整局画面 {plan.frames} 帧' if plan.frames else ''
        intro = BodyLabel(f'把这一局打成一个 zip, 发给做轴的人看:\n{when} · {team} · {self.run.duration:.1f}s\n'
                          f'记录{frames} + 版本 + 这局前后的 ok-ww 日志, 约 {plan.base_bytes / 1e6:.1f}MB')
        note = PlainTextEdit()
        note.setPlaceholderText('哪里不对? 比如「第 2 圈 1:05 锁暝没放 R, 站着不动」(可以不填)')
        note.setFixedHeight(110)
        widgets = [intro, note]
        check = None
        if plan.exports:
            check = CheckBox(f'带上这局 F8 导出的原图({len(plan.exports)} 次 · {plan.export_images} 张 · '
                             f'{plan.export_bytes / 1e6:.0f}MB)')
            check.setChecked(True)
            widgets.append(check)
        video_check = None
        if plan.video:
            length = f' · {plan.video_duration:.0f}s' if plan.video_duration else ''
            video_check = CheckBox(f'带上录屏 {os.path.basename(plan.video)}({plan.video_bytes / 1e6:.0f}MB{length})')
            video_check.setToolTip('录屏很大, 对方要看画面细节时再带; 不带的话对方也能看到每一刻对应的录屏时间')
            widgets.append(video_check)
        if MessageBoxBase is not None:
            box = MessageBoxBase(self.window())
            box.viewLayout.addWidget(SubtitleLabel('打包反馈'))
            for widget in widgets:
                box.viewLayout.addWidget(widget)
            box.yesButton.setText('打包')
            box.cancelButton.setText('取消')
            box.widget.setMinimumWidth(520)
            accepted = bool(box.exec())
        else:
            from PySide6.QtWidgets import QDialog, QDialogButtonBox
            box = QDialog(self)
            box.setWindowTitle('打包反馈')
            layout = QVBoxLayout(box)
            for widget in widgets:
                layout.addWidget(widget)
            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            buttons.accepted.connect(box.accept)
            buttons.rejected.connect(box.reject)
            layout.addWidget(buttons)
            accepted = box.exec() == QDialog.Accepted
        return (accepted, note.toPlainText(), check is not None and check.isChecked(),
                video_check is not None and video_check.isChecked())

    def _pack_worker(self, plan, note, with_exports, lap_slot, with_video=False):
        try:
            result = build_feedback(plan, note, with_exports, lap_slot, busy=self._recording, with_video=with_video)
        except Exception as e:
            result = e
        try:
            self.packed.emit(result)
        except RuntimeError:
            pass  # 页面已经关了

    def _on_packed(self, result):
        self._packing = False
        self._update_pack_button()
        if isinstance(result, Exception):
            self._toast('error', '打包失败', str(result))
            return
        self.last_feedback = result
        size = os.path.getsize(result) if os.path.isfile(result) else 0
        self._toast('success', '反馈包打好了', f'{os.path.basename(result)}  {size / 1e6:.1f}MB, 已在文件夹里选中')
        self._reveal(result)

    def _toast(self, kind, title, text):
        if InfoBar is not None:
            getattr(InfoBar, kind)(title, text, parent=self, position=InfoBarPosition.TOP, duration=6000)
        else:
            self.summary.setText(f'{title}: {text}')

    @staticmethod
    def _reveal(path):
        try:
            subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
        except Exception:
            pass

    def dispose(self):
        self.timer.stop()
