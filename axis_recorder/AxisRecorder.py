from ok import Logger, TriggerTask

from .media import HOTKEYS, MAX_EXPORT_SECONDS, NO_HOTKEY
from .recorder import HOLDER_ATTR, Recorder, default_out_dir

logger = Logger.get_logger(__name__)

PLUGIN_VERSION = '0.3.0'

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
        self.description = ('自动战斗每打一局, 把按键、切人、在场角色、读屏读数、日志和画面记成一份时间线, '
                            '在「跑轴复盘」页里看。存在 logs/axis_runs')
        self.default_config['_enabled'] = True
        self.default_config.update({
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
            self.KEEP_KEY: '超过这个数就删最早的记录(每局几百 KB 到几 MB)',
            self.READS_KEY: '记录队伍代码每次读屏的结果(找图、颜色占比、冷却、协奏), 连续读到一样的合成一段',
            self.LOGS_KEY: '把这一局里打的日志一起记进时间线',
            self.ALL_TASKS_KEY: '默认只记自动战斗; 打开后刷声骸、讨伐这类任务的战斗也记',
            self.FRAMES_KEY: '每秒 4 帧缩到 960×540 存下来, 复盘页里画面跟着时间线走; 每局几十 MB',
            self.FRAMES_KEEP_KEY: '只给最近几局留画面(按键读屏记录还是按上面的局数留)',
            self.CROPS_KEY: '读屏结果变了的那一刻, 把读的那块原图(不缩)存下来, 复盘页点读屏事件能看到',
            self.HOTKEY_KEY: '战斗里按一下, 把最近几秒的原图(不缩)导出到 logs/axis_runs/export, '
                             '拿去离线测判据。插件开着时这个键会被占住',
            self.EXPORT_SECONDS_KEY: f'原图只在打的时候留在内存里, 2K 每秒约 60MB, 最多 {MAX_EXPORT_SECONDS} 秒',
        }
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

    def on_create(self):
        super().on_create()
        self._try_install(quiet=True)

    def run(self):
        # 必须返回假值: 返回真会让执行器重排触发任务并 reset_scene
        self._try_install(quiet=False)

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
