from ok import Logger, TriggerTask

from .manual import TICK_SECONDS
from .media import HOTKEYS, MAX_EXPORT_SECONDS, NO_HOTKEY, RING_GUARANTEE_SECONDS
from .recorder import HOLDER_ATTR, Recorder, default_out_dir
from .screenrec import MODE_REPLAY, MODE_TOGGLE, REC_OFF, TOOLS

logger = Logger.get_logger(__name__)

PLUGIN_VERSION = '0.4.7'

MODE_CODE = '测试代码'
MODE_MANUAL = '人为操作'

# 要打补丁的上游函数, 上游改名或删掉时插件不装, 只报错
PATCH_POINTS = {
    ('ok.task.task', 'FindFeature'): ('find_one', 'find_feature'),
    ('ok.task.task', 'ExecutorOperation'): ('calculate_color_percentage',),
    ('src.task.BaseCombatTask', 'BaseCombatTask'): ('get_cd', 'get_current_con'),
    ('src.task.BaseWWTask', 'BaseWWTask'): ('in_team',),
    ('src.char.BaseChar', 'BaseChar'): ('perform',),
    ('src.combat.CombatCheck', 'CombatCheck'): ('in_combat', 'reset_to_false'),
}


def _missing_patch_points():
    import importlib
    missing = []
    for (module_name, class_name), names in PATCH_POINTS.items():
        owner = getattr(importlib.import_module(module_name), class_name, None)
        for name in names:
            if owner is None or not callable(getattr(owner, name, None)):
                missing.append(f'{class_name}.{name}')
    return missing


class AxisRecorderTask(TriggerTask):
    MODE_KEY = '记录模式'
    MANUAL_HOTKEY_KEY = '手打开始/结束热键'
    MANUAL_SOUND_KEY = '手打开始/结束响一声'
    MANUAL_AUTO_KEY = '看到怪自动开局'
    MANUAL_IDLE_KEY = '手打没怪几秒算打完'
    MANUAL_NOTE_KEY = '手打备注'
    REC_TOOL_KEY = '手打录屏'
    REC_MODE_KEY = '录屏方式'
    REC_KEYS_KEY = '录屏快捷键'
    REC_FOLDER_KEY = '录屏文件夹'
    KEEP_KEY = '保留最近几局'
    READS_KEY = '记录读屏'
    LOGS_KEY = '记录日志'
    ALL_TASKS_KEY = '也记录其它战斗任务'
    FRAMES_KEY = '整局存画面'
    FRAMES_KEEP_KEY = '画面保留最近几局'
    CROPS_KEY = '存读屏那块原图'
    HOTKEY_KEY = '导出原图热键'
    EXPORT_SECONDS_KEY = '热键导出最近几秒'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = '跑轴记录'
        self.description = ('把每一局的按键、切人、在场角色、读屏读数、日志和画面记成一份时间线, 在「跑轴复盘」页里看。'
                            '两种模式: 测试代码记自动战斗打的; 人为操作记自己手打的(要先关掉「自动战斗」)。'
                            '存在 logs/axis_runs')
        self.default_config['_enabled'] = True
        self.default_config.update({
            self.MODE_KEY: MODE_CODE,
            self.MANUAL_HOTKEY_KEY: 'F7',
            self.MANUAL_SOUND_KEY: True,
            self.MANUAL_AUTO_KEY: False,
            self.MANUAL_IDLE_KEY: 5,
            self.MANUAL_NOTE_KEY: '',
            self.REC_TOOL_KEY: REC_OFF,
            self.REC_MODE_KEY: MODE_REPLAY,
            self.REC_KEYS_KEY: '',
            self.REC_FOLDER_KEY: '',
            self.KEEP_KEY: 50,
            self.READS_KEY: True,
            self.LOGS_KEY: True,
            self.ALL_TASKS_KEY: False,
            self.FRAMES_KEY: True,
            self.FRAMES_KEEP_KEY: 5,
            self.CROPS_KEY: True,
            self.HOTKEY_KEY: 'F8',
            self.EXPORT_SECONDS_KEY: 4,
        })
        self.config_description = {
            self.MODE_KEY: (f'{MODE_CODE}: 记自动战斗打的每一局, 看轴代码按了什么、读到了什么。'
                            f'{MODE_MANUAL}: 记自己手打的每一局(按键用 Raw Input 收, 含按住多久; 在场角色读 HUD; 画面和 F8 导出照旧)。'
                            f'选{MODE_MANUAL}前要先在「触发任务」里关掉「自动战斗」, 开着它一进战斗就会接管, 关掉之前手打不记; '
                            f'ok-ww 也要保持在运行(触发任务在跑)'),
            self.MANUAL_HOTKEY_KEY: (f'{MODE_MANUAL}时按一下开始记、再按一下结束; 中间不管有没有怪、HUD 在不在都不会自己停'
                                     f'(只有切回{MODE_CODE}、打开自动战斗、关掉 ok-ww 会收掉, 忘了按的 2 小时后收)。'
                                     f'开始和结束都会弹通知、响一声。和导出原图热键选成同一个时只留导出'),
            self.MANUAL_SOUND_KEY: ('开始响两声由低到高, 结束由高到低。游戏全屏时 Windows 勿扰常把通知横幅藏起来, '
                                    '听声音最准; 开着显卡录屏的话这两声也会录进去'),
            self.MANUAL_AUTO_KEY: (f'默认关: {MODE_MANUAL}只认上面的热键开始/结束。打开后不按热键也会自己认: '
                                   f'看到锁定框、敌人血条或 Boss 标记就开局, 没怪了、HUD 不在了自动结束(热键开的局还是只认热键)。'
                                   f'热键选了不用、和导出撞了或注册不上时按打开算'),
            self.MANUAL_IDLE_KEY: (f'打开「看到怪自动开局」时, 自动开的局: HUD 在、但连续这么多秒既没看到锁定框/血条、也没出招'
                                   f'(走路跳跃冲刺不算)就算打完(换波刷怪慢的本可以调大)。'
                                   f'HUD 不在(大招演出、结算、过场)是另一条: 连续 15 秒不在才结束, 不看这个设置'),
            self.MANUAL_NOTE_KEY: (f'{MODE_MANUAL}时写进每一局的表头(比如「4R16居」「第二版起手」), '
                                   f'复盘页摘要里显示; 不改就一直用这句, 换打法时记得改'),
            self.REC_TOOL_KEY: (f'{MODE_MANUAL}时替你按显卡录屏软件的快捷键, 打完找到录屏文件, 拿插件存的画面和录屏逐帧比出时间差, '
                                f'复盘页光标那一刻显示对应的录屏时间(剪教学视频用)。'
                                f'N 卡要在 NVIDIA App 里开着「即时重放」, A 卡在 Adrenalin 里开「即时重放」, '
                                f'Win 游戏栏要在 设置>游戏>捕获 里开「录制所发生的事情」。录屏软件认不认模拟按键, 先实机试一局'),
            self.REC_MODE_KEY: (f'{MODE_REPLAY}: 一局打完按一下存回放, 回放时长要盖过一局(N 卡默认 5 分钟; 游戏栏默认 30 秒, 要调长)。'
                                f'{MODE_TOGGLE}: 开局按开始录、打完按停止, 按之前先看录屏文件夹里有没有正在录的(在录就不按开始, '
                                f'已经停了就不按停止); 只在鸣潮在前台时按; 打完等 5 秒再停, 这期间下一局开打就接着录不停。'
                                f'录屏软件没开着或快捷键不对时还是可能错位, 所以默认用存回放'),
            self.REC_KEYS_KEY: ('留空用默认: N 卡读 NVIDIA App 里设的(出厂 存回放 Alt+F10 / 录制 Alt+F9), '
                                'A 卡 Ctrl+Shift+S / Ctrl+Shift+E, 游戏栏 Win+Alt+G / Win+Alt+R。'
                                '自己改过的写成 alt+f10 这样, 填上面选的录屏方式用的那个'),
            self.REC_FOLDER_KEY: ('录屏存在哪(会往下找 3 层子文件夹)。留空: N 卡读 NVIDIA App 设置里的, '
                                  '游戏栏是 视频\\捕获, A 卡是 视频 文件夹(Adrenalin 里改过位置的要填)'),
            self.KEEP_KEY: '超过这个数就删最早的记录(每局几百 KB 到几 MB)',
            self.READS_KEY: '记录队伍代码每次读屏的结果(找图、颜色占比、冷却、协奏), 连续读到一样的合成一段',
            self.LOGS_KEY: '把这一局里打的日志一起记进时间线',
            self.ALL_TASKS_KEY: '默认只记自动战斗; 打开后刷声骸、讨伐这类任务的战斗也记',
            self.FRAMES_KEY: '每秒 4 帧缩到 960×540 存下来, 复盘页里画面跟着时间线走; 每局几十 MB',
            self.FRAMES_KEEP_KEY: '只给最近几局留画面(按键读屏记录还是按上面的局数留)',
            self.CROPS_KEY: '读屏结果变了的那一刻, 把读的那块原图(不缩)存下来, 复盘页点读屏事件能看到',
            self.HOTKEY_KEY: '战斗里按一下, 把最近几秒的原图(不缩)导出到 logs/axis_runs/export, '
                             '拿去离线测判据。插件开着时这个键会被占住',
            self.EXPORT_SECONDS_KEY: (f'原图只在打的时候留在内存里, 每秒约 1080p 33MB / 2K 59MB / 4K 133MB。'
                                      f'{RING_GUARANTEE_SECONDS:g} 秒以内任何分辨率都留够, 再长受 320MB 上限管'
                                      f'(2K 最多 5.5 秒、4K 只有 {RING_GUARANTEE_SECONDS:g} 秒), 最多 {MAX_EXPORT_SECONDS} 秒'),
        }
        self.config_type[self.MODE_KEY] = {'type': 'drop_down', 'options': [MODE_CODE, MODE_MANUAL]}
        self.config_type[self.MANUAL_HOTKEY_KEY] = {'type': 'drop_down', 'options': list(HOTKEYS) + [NO_HOTKEY]}
        self.config_type[self.REC_TOOL_KEY] = {'type': 'drop_down', 'options': [REC_OFF] + list(TOOLS)}
        self.config_type[self.REC_MODE_KEY] = {'type': 'drop_down', 'options': [MODE_REPLAY, MODE_TOGGLE]}
        self.config_type[self.HOTKEY_KEY] = {'type': 'drop_down', 'options': list(HOTKEYS) + [NO_HOTKEY]}
        try:
            # 默认是同步图标, 和「F开始战斗」撞; 复盘页用的是 HISTORY
            from qfluentwidgets import FluentIcon
            self.group_icon = FluentIcon.SAVE
        except Exception:
            pass
        self.trigger_interval = 1
        self.recorder = None
        self._failed_reason = None

    @property
    def out_dir(self):
        return self.recorder.out_dir if self.recorder is not None else default_out_dir()

    @property
    def manual_mode(self):
        try:
            return self.config.get(self.MODE_KEY, MODE_CODE) == MODE_MANUAL
        except Exception:
            return False

    def on_create(self):
        super().on_create()
        self._try_install(quiet=True)

    def run(self):
        # 必须返回假值: 返回真会让执行器重排触发任务并 reset_scene
        self._try_install(quiet=False)
        recorder = self.recorder
        if recorder is None:
            return
        manual = self.manual_mode
        # 人为操作时这个任务自己取画面认 HUD, 每 0.25 秒来一次; 测试代码时只是兜底重装, 1 秒一次
        self.trigger_interval = TICK_SECONDS if manual else 1
        try:
            recorder.manual.tick(self, manual)
        except Exception as e:
            recorder._log_error('跑轴记录: 人为操作模式出错', e)

    def on_destroy(self):
        if self.recorder is None:
            return
        try:
            self.recorder.uninstall()
            logger.info('跑轴记录: 补丁已卸下')
        except Exception as e:
            logger.error('跑轴记录: 卸载补丁出错', e)
        self.recorder = None

    def _other_copy(self):
        executor = getattr(self, 'executor', None)
        for group in ('trigger_tasks', 'onetime_tasks'):
            for other in getattr(executor, group, None) or ():
                if other is not self and type(other).__name__ == type(self).__name__:
                    return other
        return None

    def _try_install(self, quiet):
        if self.recorder is not None or self._failed_reason:
            return
        try:
            missing = _missing_patch_points()
            if missing:
                self._fail(f'ok-ww 改了这些函数, 插件 v{PLUGIN_VERSION} 没装上, 请等插件更新: {missing}')
                return
            other = self._other_copy()
            if other is not None and getattr(other, 'recorder', None) is not None:
                self._fail('还有另一份「跑轴记录」在运行, 请删掉其中一份再重启')
                return
            recorder = Recorder(plugin_version=PLUGIN_VERSION)
            recorder.owner = self
            try:
                recorder.install()
            except Exception:
                recorder.uninstall()
                raise
        except Exception as e:
            if quiet:
                logger.debug(f'跑轴记录: 启动时没装上, 稍后重试 ({e})')
            else:
                logger.error('跑轴记录: 安装失败, 稍后重试', e)
            return
        self.recorder = recorder
        logger.info(f'跑轴记录 v{PLUGIN_VERSION}: 补丁已挂上, 记录存到 {recorder.out_dir}')

    def _fail(self, reason):
        self._failed_reason = reason
        logger.error(f'跑轴记录: {reason}')


def current_recorder():
    """页签用: 拿到正在工作的记录器(热更新后也是同一个)。"""
    try:
        from ok.task.task import ExecutorOperation
        return ExecutorOperation.__dict__.get(HOLDER_ATTR)
    except Exception:
        return None
