"""人为操作模式: 人自己打, 插件自己取画面、读 HUD 认在场、Raw Input 记按键, 写成和自动战斗一样的一局。

自动战斗那边是挂在 in_combat 上等它开局, 这边没人调 in_combat, 只能插件自己判:
in_combat 里会按中键锁怪、按 F 处决、按 esc, 会和人抢操作, 不能借; 这里只用纯读屏的那几样
(in_team 认 HUD、锁定框模板、敌人血条), 一个键都不发。

tick() 在执行器线程里由「跑轴记录」任务每 0.25 秒调一次; 开局/收尾都在这里做。
默认只认热键: 按一下开始、再按一下结束, 中间不自己收。设置里打开「看到怪自动开局」(或热键用不了)才按怪和 HUD 自动开局收尾。
执行器停了(用户点了停止、插件被关)tick 就不来了, 采集线程每 50ms 看一眼: 自动开的局超过 TICK_LOST 秒没来就收掉,
热键开的局接着记按键, 等热键线程那边按热键收。
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
# 热键开的局只认热键收; 这个只防忘了按、一直记下去把盘写满
MAX_HOTKEY_RUN_SECONDS = 2 * 3600
# 热键结束时怪还在, 要等这么久看不到怪才重新自动开局, 免得刚停又开
RESUME_CLEAR_SECONDS = 1.0
WARN_EVERY = 60.0

BY_ENEMY = '看到怪'
BY_HOTKEY = '热键'
HOTKEY_STOP = '按热键停止'  # 和 WWKeyRecorder 同一个写法

# (频率, 毫秒): 开始由低到高, 结束由高到低
TONES_START = ((880, 90), (1320, 140))
TONES_END = ((1320, 90), (880, 140))

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
        auto = self._auto_detect(owner)
        if run is None:
            if self.suppress and in_team and not enemy and now - self.last_enemy >= RESUME_CLEAR_SECONDS:
                self.suppress = False
            if toggle:
                self._start(owner, combat, BY_HOTKEY, hud)
            elif auto and enemy and not self.suppress:
                self._start(owner, combat, BY_ENEMY, hud)
            else:
                self._set_state(owner, self._waiting_state(owner, auto))
            return
        idle = self._idle_seconds(owner)
        if toggle:
            recorder.end_run(HOTKEY_STOP)
            self.suppress = enemy
        elif run.by == BY_HOTKEY:
            if now - run.t0 >= MAX_HOTKEY_RUN_SECONDS:
                recorder.end_run(f'超过 {MAX_HOTKEY_RUN_SECONDS // 3600} 小时没按热键结束')
        elif in_team and now - max(self.last_enemy, self.log.last_action) >= idle:
            # 人还在出招就不收: Boss 战锁定框和血条常常好一阵认不出来, 只看怪会打到一半被收掉
            recorder.end_run(f'{idle:g} 秒没看到怪也没出招')
        elif not in_team and now - self.last_hud >= HUD_GONE_END:
            recorder.end_run(f'HUD {HUD_GONE_END:g} 秒没出现(打完了、结算或过场)')
        elif now - run.t0 >= MAX_RUN_SECONDS:
            recorder.end_run(f'超过 {MAX_RUN_SECONDS // 60} 分钟')
        if recorder.run is None:
            self._set_state(owner, self._waiting_state(owner, auto))

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
        key = self._hotkey_name(owner)
        self._set_state(owner, f'记录中({by}开的局)')
        if by == BY_HOTKEY:
            text = f'跑轴记录: 开始记手打, 再按一下 {key} 结束'
        else:
            text = '跑轴记录: 看到怪了, 开始记手打' + ('' if key == NO_HOTKEY else f'(按 {key} 结束)')
        self._announce(owner, text, TONES_START)

    def end(self, run, t, reason=''):
        """记录器收尾时调: 先把按着的键记成松开, 再按录屏的停止/存回放; 返回要写进 end 的按键次数、暂停和录屏。"""
        self._announce(self.recorder.owner, f'跑轴记录: 手打这局记完了({reason}), 记了 {_clock(t - run.t0)}',
                       TONES_END)
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
        """锁定框、敌人血条或 Boss 破盾/锁定标记, 只读屏。照 CombatCheck.has_target 的前三个框,
        但不走它按 esc 修熊的那支; Boss 那两个模板是 check_health_bar 里的 is_boss。"""
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
        if combat.has_health_bar():
            return True
        is_boss = getattr(combat, 'is_boss', None)
        return bool(is_boss and is_boss())

    def _auto_detect(self, owner):
        """看到怪自动开局、没怪自动收: 设置里打开了, 或者热键用不了(选了不用、和导出撞、注册不上)时只能靠它。"""
        try:
            if owner.config.get(owner.MANUAL_AUTO_KEY, False):
                return True
        except Exception:
            return True
        name = self._hotkey_name(owner)
        hotkey = self.hotkey
        return name == NO_HOTKEY or (hotkey is not None and hotkey.current == name and not hotkey.bound)

    def _waiting_state(self, owner, auto):
        return '等开打' if auto else f'等按 {self._hotkey_name(owner)} 开始'

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

    @staticmethod
    def _announce(owner, text, tones):
        """开局/收尾提示: 托盘通知 + 响两声。游戏全屏时 Windows 勿扰常把通知横幅藏起来, 所以还要响。"""
        logger.info(text)
        if owner is None:
            return
        try:
            owner.notification(text, tray=True)
        except Exception:
            pass
        try:
            sound = owner.config.get(owner.MANUAL_SOUND_KEY, True)
        except Exception:
            sound = True
        if sound:
            play_tones(tones)

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
            self.hotkey = HotkeyThread(lambda: self._hotkey_name(owner), lambda: self._on_hotkey(owner),
                                       label='手打开始/结束')
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

    def _on_hotkey(self, owner):
        """热键线程调。执行器在跑就交给 tick; 停着(点了停止、插件被关)tick 不来, 正在记的局在这里收,
        没在记就不开(开局要读画面认队伍), 也不留着等执行器回来再开。"""
        if time.perf_counter() - self.last_tick < TICK_LOST:
            self.toggle_requested.set()
            return
        run = self.recorder.run
        if run is not None and run.manual:
            self.recorder.end_run(HOTKEY_STOP)
            return
        text = '跑轴记录: ok-ww 没在运行, 这下没开始记(先点 ok-ww 的开始再按)'
        logger.warning(text)
        try:
            owner.notification(text, tray=True)
        except Exception:
            pass

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
            if run.by == BY_HOTKEY:
                return True  # 热键开的局只认热键收, 接着记按键; 热键在 _on_hotkey 里收
            self.recorder.end_run('ok-ww 停下了(执行器暂停或插件被关)')
        return False

    def _emit(self, run, t, action, key, injected, lag, value, cut):
        self.recorder.put_raw_input(run, t, action, key, injected, lag, value, cut)

    def _mark(self, run, t, name, value):
        self.recorder.put_mark(run, t, name, value)


def _clock(seconds):
    seconds = max(0, int(seconds))
    return f'{seconds // 60} 分 {seconds % 60} 秒' if seconds >= 60 else f'{seconds} 秒'


def play_tones(tones):
    """另起一个线程响(Beep 会卡住调用它的线程)。走默认声卡, 显卡录屏会一起录进去。"""
    def beep():
        try:
            import winsound
            for freq, ms in tones:
                winsound.Beep(freq, ms)
        except Exception:
            pass

    threading.Thread(target=beep, name='axis_recorder_beep', daemon=True).start()
