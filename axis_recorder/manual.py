"""人为操作模式: 人自己打, 插件自己取画面、读 HUD 认在场、Raw Input 记按键, 写成和自动战斗一样的一局。

自动战斗那边是挂在 in_combat 上等它开局, 这边没人调 in_combat, 只能插件自己判:
in_combat 里会按中键锁怪、按 F 处决、按 esc, 会和人抢操作, 不能借; 这里只用纯读屏的那几样
(in_team 认 HUD、锁定框模板、敌人血条), 一个键都不发。

tick() 在执行器线程里由「跑轴记录」任务每 0.25 秒调一次; 开局/收尾都在这里做。
执行器停了(用户点了停止、插件被关)tick 就不来了, 采集线程每 50ms 看一眼, 超过 TICK_LOST 秒没来就把这局收掉。
"""
import threading
import time

from ok import Logger

from .media import HOTKEYS, NO_HOTKEY, HotkeyThread
from .rawinput import InputLog, RawInputThread
from .screenrec import ScreenRec

logger = Logger.get_logger(__name__)

TICK_SECONDS = 0.25
# 采集线程判「执行器不来了」: 平时 0.25 秒一次, 别的触发任务插队也就一两秒
TICK_LOST = 3.0
# HUD 不在这么久就算打完了(大招演出、处决镜头一般 2~6 秒)
HUD_GONE_END = 15.0
MAX_RUN_SECONDS = 30 * 60
# 热键结束时怪还在, 要等这么久看不到怪才重新自动开局, 免得刚停又开
RESUME_CLEAR_SECONDS = 1.0
WARN_EVERY = 60.0

BY_ENEMY = '看到怪'
BY_HOTKEY = '热键'

AUTO_COMBAT_WARNING = ('跑轴记录: 人为操作模式要先在「触发任务」里关掉「自动战斗」'
                       '(开着它一进战斗就会接管), 关掉之前手打不记')


class ManualDriver:
    def __init__(self, recorder):
        self.recorder = recorder
        self.log = InputLog(self._emit, self._mark)
        # 录屏的对时在另一局打着的时候先停下
        self.screen = ScreenRec(self.log.mute_injected, self._mark, busy=lambda: recorder.run is not None)
        self.capture = None
        self.hotkey = None
        self.toggle_requested = threading.Event()
        self.last_tick = 0.0
        self.last_enemy = 0.0
        self.last_hud = 0.0
        self.suppress = False
        self.warned_at = -WARN_EVERY
        self.state = None
        self._capture_error_logged = None

    # ---------- 执行器线程 ----------

    def tick(self, owner, manual):
        recorder = self.recorder
        now = time.perf_counter()
        self.last_tick = now
        run = recorder.run
        if not manual:
            if run is not None and run.manual:
                recorder.end_run('切回测试代码')
            self.stop()
            return
        self._ensure_hotkey(owner)
        combat = self._combat_task(owner)
        if combat is None:
            return
        if combat.enabled:
            if run is not None and run.manual:
                recorder.end_run('自动战斗打开了')
            self._stop_capture()
            self.warn(owner)
            self._set_state(owner, '自动战斗开着, 手打不记')
            return
        self._ensure_capture()
        self._fresh_frame(owner)
        hud = combat.in_team()
        in_team = bool(hud and hud[0])
        enemy = in_team and self._enemy_visible(combat)
        if in_team:
            self.last_hud = now
        if enemy:
            self.last_enemy = now
        toggle = self.toggle_requested.is_set()
        self.toggle_requested.clear()
        run = recorder.run
        if run is not None and not run.manual:
            return
        if run is None:
            if self.suppress and in_team and not enemy and now - self.last_enemy >= RESUME_CLEAR_SECONDS:
                self.suppress = False
            if toggle:
                self._start(owner, combat, BY_HOTKEY, hud)
            elif enemy and not self.suppress:
                self._start(owner, combat, BY_ENEMY, hud)
            else:
                self._set_state(owner, '等开打')
            return
        if toggle:
            recorder.end_run('按热键停止')  # 和 WWKeyRecorder 同一个写法
            self.suppress = enemy
        elif run.by == BY_ENEMY and in_team and now - self.last_enemy >= self._idle_seconds(owner):
            recorder.end_run(f'{self._idle_seconds(owner):g} 秒没看到怪')
        elif run.by == BY_ENEMY and not in_team and now - self.last_hud >= HUD_GONE_END:
            recorder.end_run(f'HUD {HUD_GONE_END:g} 秒没出现')
        elif now - run.t0 >= MAX_RUN_SECONDS:
            recorder.end_run(f'超过 {MAX_RUN_SECONDS // 60} 分钟')
        if recorder.run is None:
            self._set_state(owner, '等开打')

    def _start(self, owner, combat, by, hud):
        recorder = self.recorder
        try:
            # 认队伍(和自动战斗开局一样); 认不到也照记, 表头里就是上次认到的人
            combat.load_chars()
        except Exception as e:
            recorder._log_error('跑轴记录: 手打开局认队伍出错', e)
        plan = None
        try:
            plan = self.screen.plan(owner)
        except Exception as e:
            recorder._log_error('跑轴记录: 读录屏设置出错, 这局不录', e)
        rec = None if plan is None else {k: plan[k] for k in ('tool', 'mode', 'keys', 'folder')}
        run = self.log.begin(lambda t0: recorder.start_manual_run(combat, by, t0, rec))
        self.screen.begin(run, plan)
        self.last_enemy = time.perf_counter()
        if hud:
            recorder._note_field(run, hud)
        self._set_state(owner, f'记录中({by}开的局)')
        logger.info(f'跑轴记录: 手打开局({by})')

    def end(self, run, t):
        """记录器收尾时调: 先把按着的键记成松开, 再按录屏的停止/存回放; 返回要写进 end 的按键次数、暂停和录屏。"""
        try:
            stats = self.log.end(run, t)
        except Exception as e:
            self.recorder._log_error('跑轴记录: 收尾按键统计出错', e)
            stats = {}
        try:
            # 按键统计出错也要按: 开局开录的不停掉, 下一局按的「开始」反而把录制关了
            stats.update(self.screen.end(run, t))
        except Exception as e:
            self.recorder._log_error('跑轴记录: 收尾录屏出错', e)
        return stats

    def _combat_task(self, owner):
        executor = getattr(owner, 'executor', None)
        cls = self.recorder._auto_combat
        for task in getattr(executor, 'trigger_tasks', None) or ():
            if cls is not None and isinstance(task, cls):
                return task
        return None

    @staticmethod
    def _fresh_frame(owner):
        last = getattr(owner.executor, '_last_frame_time', 0)
        if not isinstance(last, (int, float)) or time.time() - last > 0.1:
            owner.next_frame()

    @staticmethod
    def _enemy_visible(combat):
        """锁定框或敌人血条, 只读屏。照 CombatCheck.has_target 的前三个框, 但不走它按 esc 修熊的那支。"""
        has_name, no_name = combat.get_target_names()
        names = [has_name, no_name]
        scale = 1.2 if combat.is_browser() else 1.1
        for box in (combat.get_box_by_name(has_name).scale(scale), combat.get_box_by_name('box_target_enemy_long'),
                    combat.get_box_by_name('target_box_long2')):
            best = combat.find_best_match_in_box(box, names, threshold=0.6)
            if best:
                if best.name == has_name:
                    return True
                break
        return bool(combat.has_health_bar())

    @staticmethod
    def _idle_seconds(owner):
        try:
            return max(1.0, float(owner.config.get(owner.MANUAL_IDLE_KEY, 5)))
        except (TypeError, ValueError):
            return 5.0

    def warn(self, owner):
        now = time.monotonic()
        if now - self.warned_at < WARN_EVERY:
            return
        self.warned_at = now
        logger.warning(AUTO_COMBAT_WARNING)
        try:
            owner.notification(AUTO_COMBAT_WARNING, tray=True)
        except Exception:
            pass

    def _set_state(self, owner, state):
        if state == self.state:
            return
        self.state = state
        try:
            owner.info_set('人为操作', state)
        except Exception:
            pass

    # ---------- 线程 ----------

    def _ensure_capture(self):
        capture = self.capture
        if capture is not None and capture.is_alive():
            return
        if capture is not None and capture.error and capture.error != self._capture_error_logged:
            self._capture_error_logged = capture.error
            logger.error(f'跑轴记录: 收不到按键({capture.error}), 手打只记画面和在场')
        self.capture = RawInputThread(self.log, on_idle=self._on_capture_idle)
        self.capture.start()

    def _stop_capture(self):
        capture, self.capture = self.capture, None
        if capture is not None:
            capture.stop()

    def _ensure_hotkey(self, owner):
        if self.hotkey is None and self.recorder.media is not None:
            self.hotkey = HotkeyThread(lambda: self._hotkey_name(owner), self.toggle_requested.set, label='手打开始/结束')
            self.hotkey.start()

    def _hotkey_name(self, owner):
        try:
            wanted = owner.config.get(owner.MANUAL_HOTKEY_KEY, 'F7')
            export = owner.config.get(owner.HOTKEY_KEY, 'F8')
        except Exception:
            return NO_HOTKEY
        if wanted not in HOTKEYS or wanted == export:
            return NO_HOTKEY  # 和导出原图同一个键时只留导出
        return wanted

    def stop(self):
        self._stop_capture()
        hotkey, self.hotkey = self.hotkey, None
        if hotkey is not None:
            hotkey.stop()
        self.state = None

    # ---------- 采集线程 ----------

    def _on_capture_idle(self, now):
        if now - self.last_tick < TICK_LOST:
            return True
        run = self.recorder.run
        if run is not None and run.manual:
            self.recorder.end_run('ok-ww 停下了(执行器暂停或插件被关)')
        return False

    def _emit(self, run, t, action, key, injected, lag, value, cut):
        self.recorder.put_raw_input(run, t, action, key, injected, lag, value, cut)

    def _mark(self, run, t, name, value):
        self.recorder.put_mark(run, t, name, value)
