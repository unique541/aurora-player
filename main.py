#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py —— Aurora Player 主程序（音频 + 视频）
================================================
多格式媒体播放器桌面应用（Python + PySide6）。

功能：
  * 音频后端三级降级：ffmpeg+sounddevice（格式最全）-> pygame -> QtMultimedia
  * 视频播放：QtMultimedia（QMediaPlayer + QVideoWidget），支持
    mp4/mkv/avi/mov/webm/flv 等常见格式；画面/音轨同步，支持播放/暂停/拖动
  * 「导入媒体」支持导入单首/多首音频视频文件或整个文件夹（自动去重）
  * 支持命令行传入媒体文件路径自动播放（可注册为系统默认媒体播放器）
  * 播放列表持久化：退出时保存 %LOCALAPPDATA%\\AuroraPlayer\\playlist.json，
    下次启动自动恢复（过滤已删除文件、自动去重）
  * 内嵌歌词读取：无同名 .lrc 时自动读取音乐文件内嵌歌词（USLT/LYRICS/©lyr）
  * 顺序播放 / 单曲循环 / 随机播放 三种模式随时切换
  * 实时 .lrc 歌词滚动与当前句高亮（音频同目录同名 .lrc）
  * 5 套配色主题一键切换 + 全局字号可调
  * 启动优化：numpy/sounddevice/QtMultimedia/mutagen 等重模块按需惰性导入，
    主窗口尽快显示；双击文件启动时在事件循环内自动播放，按钮状态同步

运行：python main.py
"""
import sys as _sys
import os as _os
import traceback as _tb
import time as _time

# PySide6 打包为 exe 后，QMediaPlayer 的多媒体后端插件探测顺序可能选中
# windowsmediaplugin（WMF），该后端在无边框窗口下视频画面黑屏仅有声音；
# 统一在 Qt 加载前强制使用 ffmpeg 后端（源码/打包均正常渲染）。
_os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")


def _write_error_log(exc_type, exc, tb):
    """把未捕获异常追写入 %LOCALAPPDATA%\\AuroraPlayer\\error.log。"""
    log_path = None
    try:
        log_dir = _os.path.join(
            _os.environ.get("LOCALAPPDATA", _os.path.expanduser("~")),
            "AuroraPlayer")
        _os.makedirs(log_dir, exist_ok=True)
        log_path = _os.path.join(log_dir, "error.log")
        payload = [
            "=" * 70,
            _time.strftime("%Y-%m-%d %H:%M:%S"),
            "type: %s" % (exc_type.__name__ if exc_type else exc_type),
            "msg : %s" % (exc or ""),
            "".join(_tb.format_tb(tb)) if tb else "(no traceback)",
        ]
        with open(log_path, "a", encoding="utf-8") as f:
            f.write("\n".join(payload) + "\n")
    except Exception:
        log_path = None
    return log_path


def install_global_excepthook():
    """全局未捕获异常兜底：写 error.log + 弹友好提示，杜绝原始 PyInstaller 错误框。"""
    def hook(exc_type, exc, tb):
        log_path = _write_error_log(exc_type, exc, tb)
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox
            if QApplication.instance() is None:
                QApplication([])
            box = QMessageBox()
            box.setIcon(QMessageBox.Critical)
            box.setWindowTitle("Aurora Player 发生错误")
            text = "程序发生未预期的错误：\n%s\n" % (exc or "")
            if log_path:
                text += "\n详细信息已写入日志：\n%s" % log_path
            box.setText(text)
            box.exec()
        except Exception:
            pass
        try:
            _sys.__excepthook__(exc_type, exc, tb)
        except Exception:
            pass

    _sys.excepthook = hook


install_global_excepthook()

import os
import random
import sys
import html
import json

from PySide6.QtCore import (Qt, QTimer, QUrl, QPropertyAnimation, QEasingCurve,
                            QEvent, QFile, QPoint, QParallelAnimationGroup)
from PySide6.QtGui import QAction, QShortcut, QKeySequence, QIcon, QCursor, QPixmap
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QToolButton, QListWidget, QListWidgetItem,
    QTextBrowser, QSlider, QSpinBox, QComboBox, QSplitter, QMenu,
    QFileDialog, QMessageBox, QStatusBar, QStackedWidget,
    QLineEdit, QTextEdit, QAbstractSpinBox, QCheckBox,
)

# 注：lrc_parser（内部可能加载 mutagen）延迟到真正需要歌词时才 import，
# 避免拖慢启动；player_core 的 numpy/sounddevice/QtMultimedia 也已是惰性加载。
import player_core
from player_core import (create_engine, PLAYABLE_EXTENSIONS,
                         VIDEO_EXTENSIONS, AUDIO_EXTENSIONS)
import themes

MODES = ("顺序播放", "单曲循环", "随机播放")
PLAYLIST_FILE_NAME = "playlist.json"


def _playlist_path():
    """播放列表持久化文件路径。"""
    return os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
        "AuroraPlayer", PLAYLIST_FILE_NAME)


def _fmt(ms):
    """毫秒 -> m:ss 字符串。"""
    s = max(0, int(ms // 1000))
    return "%d:%02d" % (s // 60, s % 60)


def _find_lyric_index(lines, pos_ms, last_index):
    """返回最后一条 time <= pos 的行号；没有返回 -1。用二分加速。"""
    if not lines:
        return -1
    lo, hi = 0, len(lines) - 1
    if pos_ms < lines[0][0]:
        return -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if lines[mid][0] <= pos_ms:
            lo = mid + 1
        else:
            hi = mid - 1
    idx = hi
    if last_index >= 0 and lines[idx][0] == lines[last_index][0]:
        return idx
    return idx


class _VideoView(QWidget):
    """支持双击全屏/还原的 QVideoWidget 容器。

    交互：双击视频画面进入全屏；全屏下再双击 或 按 Esc 退出全屏。
    全屏通过把 QVideoWidget 提升为无边框顶层窗口实现；退出时还原到
    容器原布局（复用 _lay，避免二次 setLayout 无效导致错位）。
    eventFilter 兜底同步 Qt 内置 Esc 退出全屏导致的 _fullscreen 状态。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtMultimediaWidgets import QVideoWidget
        self._inner = QVideoWidget(self)
        # 强制 native 窗口：QMainWindow 宿主下 QtMultimedia(ffmpeg) 渲染需要
        # 独立 HWND，否则视频区域纯黑（probe 系列验证结论）。
        self._inner.setAttribute(Qt.WA_NativeWindow, True)
        pal = self._inner.palette()
        pal.setColor(self._inner.backgroundRole(), Qt.black)
        self._inner.setPalette(pal)
        self._inner.setAutoFillBackground(True)
        self._inner.setFocusPolicy(Qt.StrongFocus)
        self._inner.installEventFilter(self)
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.addWidget(self._inner)
        # 「播放结束了哦」提示层：绝对定位覆盖容器，结束时盖住视频区域。
        # 显示时隐藏 _inner（引擎已停止，无渲染损失），避免 native 视频窗口
        # 遮盖普通子控件导致文字不可见。
        self._end_label = QLabel("播放结束了哦", self)
        self._end_label.setAlignment(Qt.AlignCenter)
        self._end_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._end_label.setStyleSheet(
            "QLabel { background: rgba(0,0,0,185); color: #ffffff;"
            " border-radius: 14px; font-size: 34px; font-weight: 700;"
            " padding: 26px 44px; }")
        self._end_label.hide()
        self._fullscreen = False
        self._host_parent = None
        self._host_layout = None
        self._rebind_cb = None
        self._space_cb = None

    def set_rebind_cb(self, cb):
        """注册全屏/还原后的视频输出重绑回调（用于修复 reparent 断渲染）。"""
        self._rebind_cb = cb

    def set_space_cb(self, cb):
        """注册空格键暂停/继续回调（全屏为独立顶层窗口时由容器接管空格键）。"""
        self._space_cb = cb

    def _call_rebind(self):
        if self._rebind_cb is not None:
            try:
                self._rebind_cb()
            except Exception:
                pass

    def video_widget(self):
        return self._inner

    def eventFilter(self, obj, event):
        if obj is self._inner:
            t = event.type()
            if t == QEvent.MouseButtonDblClick \
                    and event.button() == Qt.LeftButton:
                self._toggle_fullscreen()
                return True
            if t == QEvent.KeyPress and event.key() == Qt.Key_Escape \
                    and self._fullscreen:
                self._exit_fullscreen()
                return True
            if t == QEvent.WindowStateChange and self._fullscreen \
                    and not self._inner.isFullScreen():
                # Qt 内置行为（如按 Esc）已退出全屏，同步还原到容器
                self._exit_fullscreen()
            if t == QEvent.KeyPress and event.key() == Qt.Key_Space \
                    and self._fullscreen and self._space_cb is not None:
                # 空格暂停/继续；仅全屏（独立顶层）时主窗口 QShortcut 失效，
                # 由容器接管，避免与主窗口快捷键双触发
                self._space_cb()
                return True
        return super().eventFilter(obj, event)

    def mouseDoubleClickEvent(self, event):
        # 兜底：点击落在容器空白区域（非 inner）时同样支持双击全屏
        self._toggle_fullscreen()
        event.accept()

    def _toggle_fullscreen(self):
        if self._fullscreen:
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self):
        if self._fullscreen:
            return
        self._fullscreen = True
        # 记录原宿主，退出全屏时还原
        self._host_parent = self.parentWidget()
        self._host_layout = self._host_parent.layout() \
            if self._host_parent is not None else None
        # 容器整体提升为无边框顶层全屏；QVideoWidget 保持子级不 reparent，
        # 避免 QtMultimedia(ffmpeg) 渲染通道断裂导致全屏黑屏
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setParent(None)
        self.showFullScreen()
        # 全屏成为独立顶层后重建 native HWND，避免 QtMultimedia 渲染 surface 失效
        self.setAttribute(Qt.WA_NativeWindow, True)
        self._inner.setAttribute(Qt.WA_NativeWindow, True)
        self.raise_()
        self._inner.raise_()
        self.setFocus()
        self._inner.setFocus()
        # 重绑视频输出强制重建渲染通道，双保险
        self._call_rebind()

    def _exit_fullscreen(self):
        if not self._fullscreen:
            return
        self._fullscreen = False
        self.setWindowFlags(Qt.Widget)
        if self._host_parent is not None:
            self.setParent(self._host_parent)
            if self._host_layout is not None:
                self._host_layout.addWidget(self)   # 复用原布局，避免重复 setLayout
        self.showNormal()
        self._call_rebind()
        self._inner.raise_()

    def show_ended(self):
        """视频播放结束提示：隐藏视频控件，居中显示「播放结束了哦」。"""
        self._inner.hide()
        self._end_label.setGeometry(self.rect())
        self._end_label.show()
        self._end_label.raise_()

    def hide_ended(self):
        """重新播放前恢复视频控件并隐藏结束提示。"""
        self._end_label.hide()
        self._inner.show()
        self._inner.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if getattr(self, "_end_label", None) is not None:
            self._end_label.setGeometry(self.rect())
            if self._end_label.isVisible():
                self._end_label.raise_()


def _resource_path(name):
    """返回资源文件绝对路径：优先 PyInstaller 内置资源目录，源码运行回退到脚本同目录。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return os.path.join(base, name)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


def _window_icon():
    """加载应用窗口图标（build_res/app_icon.ico），不存在时返回 None。"""
    from PySide6.QtGui import QIcon as _QIcon
    p = _resource_path(os.path.join("build_res", "app_icon.ico"))
    if os.path.exists(p):
        return _QIcon(p)
    return None


class PlayerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)   # 支持把媒体文件/文件夹拖入窗口
        self.app = QApplication.instance()

        self.engine = None            # 当前活动引擎（音频引擎或视频引擎）
        self.backend_name = ""
        self.audio_engine = None      # 音频引擎（ffmpeg 优先）
        self.video_engine = None      # 视频引擎（QtMultimedia，惰性创建）
        self._current_is_video = False

        self.songs = []               # 播放列表绝对路径
        self.current_index = -1
        self.mode = MODES[0]
        self.theme_name = "深空暗蓝"
        self.font_size = 13
        self.lyric_fullscreen = True   # 歌词默认全屏播放（可在设置中取消）
        self._load_settings()

        self.lyric_file = None
        self.lyric_lines = []         # [(time_ms, text), ...]
        self.lyric_timed = True       # True=时间轴歌词可滚动；False=内嵌纯文本静态展示
        self.static_lyric_lines = []  # 内嵌纯文本歌词（逐行）
        self.cur_lyric_idx = -1
        self._seeking = False
        self._paused_pos_ms = 0  # 暂停时缓存的有效播放位置（恢复续播兜底）
        self._lyric_anim = None   # 当前句居中滑动的 QPropertyAnimation
        self._lyric_anim_seq = 0  # 动画代际号（换曲/新动画使旧回调失效）

        # ---- 本轮 UI 改造新增状态 ----
        self._settings_active = False   # 设置页是否显示（激活时不弹播放列表）
        self._settings_anim = None
        self._settings_anim_seq = 0
        self._drag_mode = "none"        # none/pending/window：顶部按下待判定手势
        self._drag_start_global = None
        self._playlist_open = False     # 播放列表是否滑出
        self._playlist_w = 280          # 播放列表滑出宽度
        self._playlist_anim = None
        self._lyric_fs_active = False   # 歌词全屏中
        self._lyric_fs = None
        self._lyric_fs_host_widget = None
        self._lyric_fs_host_lay = None
        self._lyric_fs_esc = None

        self._init_ui()
        self._apply_theme()
        self._update_stats()
        self._idle_state()

        # 引擎创建（numpy/sounddevice/QtMultimedia 惰性化后耗时 <100ms）
        self._init_audio_engine()

        self._ticker = QTimer(self)
        self._ticker.setInterval(200)
        self._ticker.timeout.connect(self._on_tick)
        self._ticker.start()

        self.statusBar().showMessage(
            "音频后端：%s" % self.backend_name, 6000)

        # 播放列表持久化恢复（延迟到事件循环，不阻塞窗口显示）
        QTimer.singleShot(0, self._restore_playlist)

    # ------------------------------------------------------------- 引擎管理
    def _init_audio_engine(self):
        """创建音频引擎（ffmpeg + sounddevice 优先）。"""
        if self.audio_engine is not None:
            return True
        eng, name = create_engine(self)
        if eng is None:
            self.engine = None
            self.backend_name = "无可用后端"
            self.statusBar().showMessage(
                "未找到可用播放后端，请安装 ffmpeg/sounddevice 或 PySide6", 8000)
            return False
        self.audio_engine = eng
        self.engine = eng
        self.backend_name = name
        eng.playbackFinished.connect(self._on_finished)
        eng.playbackError.connect(self._on_engine_error)
        self.statusBar().showMessage("音频后端：%s" % name, 6000)
        return True

    def _init_video_engine(self):
        """惰性创建视频引擎（QtMultimedia + QVideoWidget 绑定）。"""
        if self.video_engine is not None:
            return True
        try:
            import player_core as pc
            eng = pc.QtEngine(self)
            eng.set_video_output(self.video_view.video_widget())
            eng.set_volume(self.vol_slider.value() / 100.0)
            eng.playbackFinished.connect(self._on_finished)
            eng.playbackError.connect(self._on_engine_error)
            self.video_engine = eng
            return True
        except Exception as e:
            self._append_error_log("视频引擎初始化失败: %s" % (e,))
            return False

    # ------------------------------------------------------------- 界面构建
    def _init_ui(self):
        self.setWindowTitle("Aurora Player")
        # 无边框窗口：去除系统白色标题栏，改用应用内自定义头部（含窗口按钮）
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self._drag_pos = None   # 自定义标题栏拖动起点
        icon = _window_icon()
        if icon is not None:
            self.setWindowIcon(icon)
        self.resize(1080, 680)
        self.setMinimumSize(880, 540)

        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        self._central = central
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)

        root.addLayout(self._build_header())

        body = QSplitter(Qt.Horizontal)
        body.setObjectName("card")
        body.setHandleWidth(8)
        self.playlist_panel = self._build_playlist_panel()
        body.addWidget(self.playlist_panel)
        body.addWidget(self._build_right_panel())
        body.setStretchFactor(0, 2)
        body.setStretchFactor(1, 3)
        root.addWidget(body, 1)

        root.addWidget(self._build_control_bar())

        # 设置页：覆盖在中央区域之上，平时隐藏，下拉手势滑入
        self.settings_page = self._build_settings_page()
        self.settings_page.setParent(central)
        self.settings_page.setGeometry(central.rect())
        self.settings_page.hide()

        # 播放列表平时隐藏（宽度 0），鼠标移到窗口左边缘时平滑滑出
        self.playlist_panel.setMinimumWidth(0)
        self.playlist_panel.setMaximumWidth(0)
        self._edge_timer = QTimer(self)
        self._edge_timer.setInterval(80)
        self._edge_timer.timeout.connect(self._check_playlist_edge)
        self._edge_timer.start()

    def _build_header(self):
        lay = QHBoxLayout()
        lay.setSpacing(10)

        brand = QLabel("Aurora Player")
        brand.setObjectName("brand")
        lay.addWidget(brand)

        btn_import = QPushButton("导入媒体")
        menu_import = QMenu(self)
        act_files = menu_import.addAction("导入媒体文件…")
        act_files.triggered.connect(self._import_media_files)
        act_folder = menu_import.addAction("导入媒体文件夹…")
        act_folder.triggered.connect(self._import_folder)
        btn_import.setMenu(menu_import)
        lay.addWidget(btn_import)

        btn_clear = QPushButton("清空播放列表")
        btn_clear.setObjectName("muted1")
        btn_clear.clicked.connect(self._clear_playlist)
        lay.addWidget(btn_clear)

        lay.addStretch(1)

        # 自定义标题栏窗口控制按钮：无边框窗口下固定在应用右上角，
        # 颜色由主题 QSS 的 winBtn* 规则控制，随主题切换自动适配
        self.win_btn_min = QPushButton("—")
        self.win_btn_min.setObjectName("winBtnMin")
        self.win_btn_min.setFixedSize(34, 26)
        self.win_btn_min.setToolTip("最小化")
        self.win_btn_min.clicked.connect(self.showMinimized)
        lay.addWidget(self.win_btn_min)

        self.win_btn_max = QPushButton("□")
        self.win_btn_max.setObjectName("winBtnMax")
        self.win_btn_max.setFixedSize(34, 26)
        self.win_btn_max.setToolTip("最大化")
        self.win_btn_max.clicked.connect(self._toggle_maximize)
        lay.addWidget(self.win_btn_max)

        self.win_btn_close = QPushButton("✕")
        self.win_btn_close.setObjectName("winBtnClose")
        self.win_btn_close.setFixedSize(34, 26)
        self.win_btn_close.setToolTip("关闭")
        self.win_btn_close.clicked.connect(self.close)
        lay.addWidget(self.win_btn_close)
        return lay

    # ------------------------------------------------------ 自定义标题栏行为
    def _toggle_maximize(self):
        """最大化/还原切换（无边框窗口由本方法接管）。"""
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self._update_win_btn_icon()

    def _update_win_btn_icon(self):
        """根据窗口状态刷新最大化/还原按钮图标。"""
        if not hasattr(self, "win_btn_max"):
            return
        if self.isMaximized():
            self.win_btn_max.setText("❐")
            self.win_btn_max.setToolTip("还原")
        else:
            self.win_btn_max.setText("□")
            self.win_btn_max.setToolTip("最大化")

    def _fit_maximized_geometry(self):
        """无边框最大化时收缩到屏幕可用工作区，避免遮挡任务栏。"""
        area = self.screen().availableGeometry()
        self.setGeometry(area)

    def _hit_titlebar(self, pos):
        """点击位置是否位于自定义标题栏区域（头部布局高度内）。"""
        return 0 <= pos.y() <= 52

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._hit_titlebar(event.pos()):
            # 顶部按下：先进入待判定（pending），下拉进设置，否则拖动窗口
            self._drag_mode = "pending"
            self._drag_start_global = event.globalPosition().toPoint()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_mode == "pending" and event.buttons() & Qt.LeftButton:
            gp = event.globalPosition().toPoint()
            dy = gp.y() - self._drag_start_global.y()
            dx = gp.x() - self._drag_start_global.x()
            if dy > 24:
                # 按住最上端往下拉 -> 进入设置页
                self._drag_mode = "none"
                self._open_settings()
                event.accept()
                return
            if dy < -4 or abs(dx) > 4:
                # 判定为窗口拖动（向上或水平移动）
                self._drag_mode = "window"
                self._drag_pos = gp - self.frameGeometry().topLeft()
        if self._drag_mode == "window" and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_mode = "none"
        self._drag_start_global = None
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if getattr(self, "_central", None) is not None \
                and getattr(self, "settings_page", None) is not None:
            self.settings_page.setGeometry(self._central.rect())
            if self._settings_active:
                self.settings_page.raise_()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton and self._hit_titlebar(event.pos()):
            self._toggle_maximize()
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            if self.isMaximized():
                QTimer.singleShot(0, self._fit_maximized_geometry)
            self._update_win_btn_icon()

    def _build_playlist_panel(self):
        panel = QWidget()
        panel.setObjectName("panel")
        v = QVBoxLayout(panel)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel("播放列表")
        title.setObjectName("sectionTitle")
        head.addWidget(title)
        head.addStretch(1)
        self.stats_label = QLabel("共 0 项")
        self.stats_label.setObjectName("muted")
        head.addWidget(self.stats_label)
        v.addLayout(head)

        self.song_list = QListWidget()
        self.song_list.itemDoubleClicked.connect(self._on_song_activated)
        self.song_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.song_list.customContextMenuRequested.connect(
            self._on_playlist_menu)
        v.addWidget(self.song_list, 1)

        tip = QLabel("双击列表中的歌曲/视频开始播放")
        tip.setObjectName("muted")
        tip.setAlignment(Qt.AlignLeft)
        v.addWidget(tip)
        return panel

    def _build_right_panel(self):
        """右侧：歌词页 与 视频页 用 StackedWidget 切换。"""
        self.right_stack = QStackedWidget()
        self.lyric_page = self._build_lyric_panel()
        self.video_page = self._build_video_panel()
        self.right_stack.addWidget(self.lyric_page)
        self.right_stack.addWidget(self.video_page)
        self.right_stack.setCurrentWidget(self.lyric_page)
        return self.right_stack

    def _build_lyric_panel(self):
        panel = QWidget()
        panel.setObjectName("card")
        v = QVBoxLayout(panel)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(6)

        hea = QHBoxLayout()
        t = QLabel("歌词 · Lyrics")
        t.setObjectName("sectionTitle")
        hea.addWidget(t)
        hea.addStretch(1)
        self.lyric_status = QLabel("")
        self.lyric_status.setObjectName("muted")
        hea.addWidget(self.lyric_status)
        self.lyric_fs_btn = QToolButton()
        self.lyric_fs_btn.setText("⛶ 全屏")
        self.lyric_fs_btn.setToolTip("歌词全屏（Esc 或按钮退出）")
        self.lyric_fs_btn.clicked.connect(self._toggle_lyric_fullscreen)
        hea.addWidget(self.lyric_fs_btn)
        v.addLayout(hea)

        self.lyric_view = QTextBrowser()
        self.lyric_view.setObjectName("lyric")
        self.lyric_view.setOpenLinks(False)
        self.lyric_view.setOpenExternalLinks(False)
        # 双击歌词跳转到对应时间播放：事件发在 viewport() 上，由 eventFilter 拦截
        self.lyric_view.viewport().installEventFilter(self)
        v.addWidget(self.lyric_view, 1)
        return panel

    def _build_video_panel(self):
        panel = QWidget()
        panel.setObjectName("card")
        v = QVBoxLayout(panel)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(6)

        hea = QHBoxLayout()
        t = QLabel("视频画面 · Video")
        t.setObjectName("sectionTitle")
        hea.addWidget(t)
        hea.addStretch(1)
        self.video_status = QLabel("")
        self.video_status.setObjectName("muted")
        hea.addWidget(self.video_status)
        v.addLayout(hea)

        self.video_view = _VideoView()
        self.video_view.setMinimumSize(320, 200)
        self.video_view.set_rebind_cb(self._rebind_video_output)
        self.video_view.set_space_cb(self._on_space_key)
        v.addWidget(self.video_view, 1)
        return panel

    def _rebind_video_output(self):
        """全屏/还原后重绑视频输出，修复 QVideoWidget reparent 导致的断渲染。"""
        if self.video_engine is not None:
            try:
                self.video_engine.set_video_output(
                    self.video_view.video_widget())
            except Exception:
                pass

    # ------------------------------------------------ 设置页（顶部下拉进入）
    def _settings_path(self):
        return os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "AuroraPlayer", "settings.json")

    def _load_settings(self):
        """读取持久化设置：主题 / 字号 / 歌词默认全屏。"""
        try:
            with open(self._settings_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data.get("theme"), str) \
                    and data["theme"] in themes.THEMES:
                self.theme_name = data["theme"]
            if isinstance(data.get("font_size"), int):
                self.font_size = max(11, min(22, data["font_size"]))
            self.lyric_fullscreen = bool(data.get("lyric_fullscreen", True))
        except Exception:
            pass

    def _save_settings(self):
        try:
            p = self._settings_path()
            _os.makedirs(_os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump({
                    "theme": self.theme_name,
                    "font_size": self.font_size,
                    "lyric_fullscreen": self.lyric_fullscreen,
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _build_settings_page(self):
        page = QWidget()
        page.setObjectName("settingsPage")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(22, 14, 22, 20)
        lay.setSpacing(14)

        top = QHBoxLayout()
        self.settings_back_btn = QPushButton("← 返回")
        self.settings_back_btn.setObjectName("modeBtn")
        self.settings_back_btn.setToolTip("返回主界面")
        self.settings_back_btn.clicked.connect(self._close_settings)
        top.addWidget(self.settings_back_btn)
        t = QLabel("设置")
        t.setObjectName("sectionTitle")
        top.addWidget(t)
        top.addStretch(1)
        lay.addLayout(top)

        row1 = QHBoxLayout()
        l1 = QLabel("主题")
        l1.setObjectName("muted")
        row1.addWidget(l1)
        self.theme_combo = QComboBox()
        self.theme_combo.addItems(list(themes.THEMES))
        self.theme_combo.setCurrentText(self.theme_name)
        self.theme_combo.currentTextChanged.connect(self._on_theme_changed)
        row1.addWidget(self.theme_combo, 1)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        l2 = QLabel("字号")
        l2.setObjectName("muted")
        row2.addWidget(l2)
        self.size_spin = QSpinBox()
        self.size_spin.setRange(11, 22)
        self.size_spin.setValue(self.font_size)
        self.size_spin.valueChanged.connect(self._on_font_changed)
        row2.addWidget(self.size_spin, 1)
        lay.addLayout(row2)

        row3 = QHBoxLayout()
        l3 = QLabel("歌词")
        l3.setObjectName("muted")
        row3.addWidget(l3)
        self.lyric_fs_check = QCheckBox("歌词取消全屏播放")
        self.lyric_fs_check.setChecked(not self.lyric_fullscreen)
        self.lyric_fs_check.toggled.connect(self._on_lyric_fs_setting_changed)
        row3.addWidget(self.lyric_fs_check, 1)
        lay.addLayout(row3)

        lay.addStretch(1)
        ver = QLabel("Aurora Player · 顶部下拉进入设置 · 左上角返回")
        ver.setObjectName("muted")
        lay.addWidget(ver)
        return page

    def _on_lyric_fs_setting_changed(self, checked):
        # 勾选 = 取消歌词默认全屏
        self.lyric_fullscreen = not checked
        self._save_settings()

    def _open_settings(self):
        if self._settings_active:
            return
        self._settings_active = True
        self._animate_playlist(False)
        self.settings_page.show()
        self.settings_page.raise_()
        self._settings_anim_seq += 1
        seq = self._settings_anim_seq
        if self._settings_anim is not None:
            self._settings_anim.stop()
        anim = QPropertyAnimation(self.settings_page, b"pos", self)
        anim.setDuration(280)
        anim.setStartValue(QPoint(0, -self.settings_page.height()))
        anim.setEndValue(QPoint(0, 0))
        anim.setEasingCurve(QEasingCurve.OutCubic)
        self._settings_anim = anim
        anim.finished.connect(lambda: self._settings_anim_done(seq))
        anim.start()

    def _close_settings(self):
        if not self._settings_active:
            return
        self._settings_active = False
        self._settings_anim_seq += 1
        seq = self._settings_anim_seq
        if self._settings_anim is not None:
            self._settings_anim.stop()
        anim = QPropertyAnimation(self.settings_page, b"pos", self)
        anim.setDuration(260)
        anim.setStartValue(self.settings_page.pos())
        anim.setEndValue(QPoint(0, -self.settings_page.height()))
        anim.setEasingCurve(QEasingCurve.InCubic)
        self._settings_anim = anim
        anim.finished.connect(lambda: self._settings_anim_done(seq))
        anim.start()

    def _settings_anim_done(self, seq):
        if seq != self._settings_anim_seq:
            return
        if not self._settings_active:
            self.settings_page.hide()

    # ------------------------------------------------ 播放列表左缘滑出
    def _animate_playlist(self, open_):
        if open_ == self._playlist_open:
            return
        self._playlist_open = open_
        target = self._playlist_w if open_ else 0
        if self._playlist_anim is not None:
            self._playlist_anim.stop()
        group = QParallelAnimationGroup(self)
        for prop in (b"minimumWidth", b"maximumWidth"):
            a = QPropertyAnimation(self.playlist_panel, prop, self)
            a.setDuration(240)
            a.setStartValue(
                self.playlist_panel.minimumWidth()
                if prop == b"minimumWidth"
                else self.playlist_panel.maximumWidth())
            a.setEndValue(target)
            a.setEasingCurve(QEasingCurve.OutCubic)
            group.addAnimation(a)
        self._playlist_anim = group
        group.start()

    def _check_playlist_edge(self):
        # 设置页激活 / 歌词全屏时禁止左缘弹出；视频全屏时同样不弹
        if self._settings_active or self._lyric_fs_active:
            self._animate_playlist(False)
            return
        if not self.isVisible() or self.isMinimized():
            return
        if self._current_is_video and self.video_view._fullscreen:
            self._animate_playlist(False)
            return
        pos = QCursor.pos()
        wrect = self.frameGeometry()
        left_hot = pos.x() - wrect.left()
        if 0 <= left_hot < 12 and wrect.top() <= pos.y() <= wrect.bottom():
            self._animate_playlist(True)
            return
        # 鼠标停留在播放列表区域内时保持展开
        if self.playlist_panel.isVisible():
            local = self.playlist_panel.mapFromGlobal(pos)
            if 0 <= local.x() <= self.playlist_panel.width() \
                    and 0 <= local.y() <= self.playlist_panel.height():
                self._animate_playlist(True)
                return
        self._animate_playlist(False)

    # ------------------------------------------------ 歌词全屏
    def _toggle_lyric_fullscreen(self):
        if self._lyric_fs_active:
            self._exit_lyric_fullscreen()
        else:
            self._enter_lyric_fullscreen()

    def _enter_lyric_fullscreen(self):
        if self._lyric_fs_active:
            return
        if not self.lyric_page.isVisible():
            return
        self._lyric_fs_active = True
        fs = QWidget()
        fs.setObjectName("lyricFs")
        fs.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        fs.setAttribute(Qt.WA_NativeWindow, True)
        fs.setStyleSheet(
            "QWidget#lyricFs { background: %s; }"
            % themes.THEMES[self.theme_name]["bg"])
        lay = QVBoxLayout(fs)
        lay.setContentsMargins(28, 18, 28, 18)
        lay.setSpacing(10)
        # 先登记全屏容器
        self._lyric_fs = fs
        fs.installEventFilter(self)
        top = QHBoxLayout()
        btn = QPushButton("← 退出歌词全屏")
        btn.setObjectName("modeBtn")
        btn.clicked.connect(self._exit_lyric_fullscreen)
        top.addWidget(btn)
        top.addStretch(1)
        lab = QLabel("歌词全屏")
        lab.setObjectName("muted")
        top.addWidget(lab)
        lay.addLayout(top)
        self._lyric_fs_host_widget = self.lyric_view.parentWidget()
        self._lyric_fs_host_lay = self.lyric_page.layout()
        self.lyric_view.setParent(fs)
        lay.addWidget(self.lyric_view, 1)
        self._lyric_fs = fs
        self._lyric_fs_esc = QShortcut(QKeySequence(Qt.Key_Escape), fs)
        self._lyric_fs_esc.activated.connect(self._exit_lyric_fullscreen)
        fs.showFullScreen()
        self.lyric_view.setFocus()

    def _exit_lyric_fullscreen(self):
        if not self._lyric_fs_active:
            return
        self._lyric_fs_active = False
        fs = self._lyric_fs
        if fs is not None:
            fs.hide()
        self.lyric_view.setParent(self._lyric_fs_host_widget)
        if self._lyric_fs_host_lay is not None:
            self._lyric_fs_host_lay.addWidget(self.lyric_view, 1)
        if fs is not None:
            fs.deleteLater()
        self._lyric_fs = None
        self._lyric_fs_esc = None

    def _build_control_bar(self):
        bar = QWidget()
        bar.setObjectName("card")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(12)

        # 模式按钮
        self.mode_btn = QPushButton(self.mode)
        self.mode_btn.setObjectName("modeBtn")
        self.mode_btn.clicked.connect(self._cycle_mode)
        lay.addWidget(self.mode_btn)

        # 上一曲 / 播放暂停 / 下一曲
        btn_prev = QToolButton()
        btn_prev.setText("⏮")
        btn_prev.setToolTip("上一曲")
        btn_prev.clicked.connect(self._prev)
        lay.addWidget(btn_prev)

        self.play_btn = QPushButton("▶")
        self.play_btn.setObjectName("playBtn")
        self.play_btn.setToolTip("播放 / 暂停")
        self.play_btn.clicked.connect(self._on_play_pause)
        lay.addWidget(self.play_btn)

        # 空格键 暂停/继续播放（输入类控件聚焦时不抢键）
        self._space_shortcut = QShortcut(QKeySequence(Qt.Key_Space), self,
                                         context=Qt.WindowShortcut)
        self._space_shortcut.activated.connect(self._on_space_key)

        btn_next = QToolButton()
        btn_next.setText("⏭")
        btn_next.setToolTip("下一曲")
        btn_next.clicked.connect(self._next)
        lay.addWidget(btn_next)

        # 当前曲目
        self.track_label = QLabel("未在播放")
        self.track_label.setObjectName("sectionTitle")
        self.track_label.setMaximumWidth(260)
        self.track_label.setToolTip("")
        lay.addWidget(self.track_label, 1)

        # 进度条 + 时间
        self.seek_slider = QSlider(Qt.Horizontal)
        self.seek_slider.setRange(0, 0)
        self.seek_slider.sliderPressed.connect(self._on_slider_pressed)
        self.seek_slider.sliderMoved.connect(self._on_slider_moved)
        self.seek_slider.sliderReleased.connect(self._on_slider_released)
        self.seek_slider.setMinimumWidth(200)
        lay.addWidget(self.seek_slider, 2)

        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setObjectName("timeLabel")
        lay.addWidget(self.time_label)

        # 音量
        self.vol_btn = QToolButton()
        self.vol_btn.setText("🔊")
        self.vol_btn.setToolTip("静音 / 恢复")
        self.vol_btn.clicked.connect(self._toggle_mute)
        lay.addWidget(self.vol_btn)

        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(80)
        self.vol_slider.setFixedWidth(100)
        self.vol_slider.valueChanged.connect(self._on_volume_changed)
        lay.addWidget(self.vol_slider)

        return bar

    # ------------------------------------------------------------- 状态与UI
    def _idle_state(self):
        """无播放时的初始展示。"""
        self.track_label.setText("未在播放")
        self.seek_slider.setRange(0, 0)
        self.seek_slider.setValue(0)
        self.time_label.setText("0:00 / 0:00")
        self._render_lyric_placeholder()
        self._set_play_icon(False)
        self._current_is_video = False

    def _set_play_icon(self, playing):
        self.play_btn.setText("⏸" if playing else "▶")

    def _apply_theme(self):
        qss = themes.build_qss(self.theme_name, self.font_size)
        self.app.setStyleSheet(qss)
        self._redraw_lyrics()

    def _on_theme_changed(self, name):
        if name:
            self.theme_name = name
            self._apply_theme()
            self._save_settings()

    def _on_font_changed(self, v):
        self.font_size = int(v)
        self._apply_theme()
        self._save_settings()

    def _update_stats(self):
        self.stats_label.setText("共 %d 项" % len(self.songs))

    # ------------------------------------------------------------- 播放列表
    def _import_media_files(self):
        """导入单首/多首媒体文件（音频+视频格式过滤，多选）。"""
        files, _ = QFileDialog.getOpenFileNames(
            self, "导入媒体文件",
            _os.path.expanduser("~"),
            "媒体文件 (*.mp3 *.flac *.wav *.ogg *.oga *.m4a *.aac *.opus "
            "*.wma *.ape *.aiff *.aif *.mp4 *.mkv *.avi *.mov *.webm *.flv "
            "*.wmv *.m4v *.mpg *.mpeg);;所有文件 (*.*)")
        if not files:
            return
        added, indexes = self.import_files(files)
        if added > 0:
            self.statusBar().showMessage(
                "已导入 %d 项媒体" % added, 4000)
        else:
            self.statusBar().showMessage(
                "所选文件未发现新的媒体（可能已全部导入）", 4000)
        if indexes:
            self._play_index(indexes[0])   # 导入后自动开始播放

    def _import_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "选择媒体文件夹", _os.path.expanduser("~"))
        if not folder:
            return
        added, indexes = self.import_files(
            player_core.scan_media_files(folder))
        if added > 0:
            self.statusBar().showMessage(
                "已从 %s 导入 %d 项媒体" % (folder, added), 4000)
        else:
            self.statusBar().showMessage(
                "未发现新的媒体文件（可能已全部导入）", 4000)
        if indexes:
            self._play_index(indexes[0])   # 导入后自动开始播放

    def import_files(self, paths):
        """把媒体文件路径列表追加到播放列表（自动去重、过滤非媒体文件）。
        返回 (新增数量, 各路径在列表中的索引列表)。"""
        existing = set(self.songs)
        added = 0
        indexes = []
        changed = False
        for p in paths:
            p = os.path.normpath(p)
            if not os.path.isfile(p):
                continue
            if os.path.splitext(p)[1].lower() not in PLAYABLE_EXTENSIONS:
                continue
            if p in existing:
                indexes.append(self.songs.index(p))
                continue
            self.songs.append(p)
            existing.add(p)
            indexes.append(len(self.songs) - 1)
            added += 1
            changed = True
            item = QListWidgetItem(
                "%d. %s" % (len(self.songs), os.path.basename(p)))
            item.setToolTip(p)
            self.song_list.addItem(item)
        self._update_stats()
        if changed:
            self._save_playlist()
        return added, indexes

    # ------------------------------------------------------------- 拖拽
    def dragEnterEvent(self, event):
        """拖入时若带本地路径则接受，否则忽略。"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    @staticmethod
    def _coerce_urls_to_media(urls):
        """把拖入的 URL 列表转成媒体绝对路径（文件夹递归展开、过滤非媒体）。"""
        paths = []
        for u in urls:
            pth = u.toLocalFile()
            if not pth:
                continue
            if os.path.isdir(pth):
                paths.extend(player_core.scan_media_files(pth))
            elif os.path.isfile(pth) and \
                    os.path.splitext(pth)[1].lower() in PLAYABLE_EXTENSIONS:
                paths.append(pth)
        return paths

    def dropEvent(self, event):
        """拖入媒体/文件夹：加入播放列表并自动播放首个拖入的文件。"""
        urls = event.mimeData().urls()
        if not urls:
            event.ignore()
            return
        media_paths = self._coerce_urls_to_media(urls)
        event.acceptProposedAction()
        if not media_paths:
            self.statusBar().showMessage(
                "拖入内容中未发现可播放的媒体文件", 4000)
            return
        added, indexes = self.import_files(media_paths)
        self.statusBar().showMessage(
            "已拖入 %d 项媒体（新增 %d 项）" % (len(media_paths), added), 4000)
        if indexes:
            self._play_index(indexes[0])   # 自动播放首个拖入文件

    def _clear_playlist(self):
        if self.engine is not None:
            try:
                self.engine.stop_stream()
            except Exception:
                pass
        self.songs.clear()
        self.song_list.clear()
        self.current_index = -1
        self.lyric_lines = []
        self.lyric_timed = True
        self.static_lyric_lines = []
        self.cur_lyric_idx = -1
        self.lyric_file = None
        self._current_is_video = False
        self._idle_state()
        self._update_stats()
        self._save_playlist()
        self.statusBar().showMessage("播放列表已清空", 3000)

    def _on_playlist_menu(self, pos):
        """播放列表右键菜单：选中条目提供「从列表移除」「删除文件」。

        仅当右键落在条目上时弹出菜单；空白处不弹。
        """
        item = self.song_list.itemAt(pos)
        if item is None:
            return
        row = self.song_list.row(item)
        menu = QMenu(self)
        act_remove = menu.addAction("从列表移除")
        act_delete = menu.addAction("删除文件")
        action = menu.exec(self.song_list.mapToGlobal(pos))
        if action is None:
            return
        if action is act_remove:
            self._remove_playlist_item(row, delete_file=False)
        elif action is act_delete:
            self._remove_playlist_item(row, delete_file=True)

    def _remove_playlist_item(self, row, delete_file=False):
        """从播放列表移除第 row 项；delete_file=True 时先把磁盘文件移入回收站。

        「从列表移除」仅修改播放列表，不触碰磁盘文件；
        「删除文件」删除前弹确认框，确认后把文件移入回收站并同步移除列表项，
        取消则不执行任何变更。
        """
        if not (0 <= row < len(self.songs)):
            return
        path = self.songs[row]
        if delete_file:
            ret = QMessageBox.question(
                self, "删除文件",
                "确定要删除该文件吗？\n%s\n\n"
                "文件将被移入回收站，并从播放列表中移除。" % path,
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ret != QMessageBox.Yes:
                self.statusBar().showMessage("已取消删除文件", 3000)
                return
            # 删除当前正在播放的文件前先停止引擎，避免文件句柄占用导致删除失败
            if self.current_index == row and self.engine is not None:
                try:
                    self.engine.stop_stream()
                except Exception:
                    pass
            if not QFile.moveToTrash(path):
                self.statusBar().showMessage(
                    "删除文件失败，文件可能正被占用或不存在", 4000)
                return
        # 从列表移除（songs 与 QListWidget 同步）
        self.songs.pop(row)
        it = self.song_list.takeItem(row)
        if it is not None:
            del it
        # 重编号后续条目（"N. 文件名"）
        for i in range(row, self.song_list.count()):
            self.song_list.item(i).setText(
                "%d. %s" % (i + 1, os.path.basename(self.songs[i])))
        # 校正当前播放索引
        if self.current_index == row:
            self.current_index = -1
            self._current_is_video = False
            self.lyric_lines = []
            self.lyric_timed = True
            self.static_lyric_lines = []
            self.cur_lyric_idx = -1
            self.lyric_file = None
            self._idle_state()
        elif self.current_index > row:
            self.current_index -= 1
        self._update_stats()
        self._save_playlist()
        self.statusBar().showMessage(
            "已从列表移除：%s" % os.path.basename(path), 3000)

    def _on_song_activated(self, item):
        row = self.song_list.row(item)
        if 0 <= row < len(self.songs):
            self._play_index(row)

    # ------------------------------------------------------------- 播放控制
    def _play_index(self, idx):
        if not (0 <= idx < len(self.songs)):
            return
        path = self.songs[idx]
        ext = os.path.splitext(path)[1].lower()
        is_video = ext in VIDEO_EXTENSIONS

        # 先停止当前活动引擎（音频或视频），避免双引擎同时发声
        if self.engine is not None:
            try:
                self.engine.stop_stream()
            except Exception:
                pass

        self.current_index = idx
        self.song_list.setCurrentRow(idx)

        name = _os.path.basename(path)
        self.track_label.setText(name)
        self.track_label.setToolTip(path)

        if is_video:
            if not self._init_video_engine():
                self.statusBar().showMessage(
                    "当前环境不支持视频播放（QtMultimedia 不可用）", 6000)
                self._set_play_icon(False)
                return
            self.engine = self.video_engine
            self.backend_name = "QtMultimedia"
            self._current_is_video = True
            self._switch_to_video_page()
            self.engine.open_file(path)
            self.engine.set_volume(self.vol_slider.value() / 100.0)
            self._paused_pos_ms = 0
            self.engine.play_stream(0)
            self._set_play_icon(True)
            self.lyric_lines = []
            self.lyric_timed = True
            self.static_lyric_lines = []
            self.cur_lyric_idx = -1
            self.video_status.setText(_os.path.basename(path))
            self.lyric_status.setText("视频文件 · 无歌词")
        else:
            if self.audio_engine is None and not self._init_audio_engine():
                self.statusBar().showMessage("无可用音频后端", 6000)
                self._set_play_icon(False)
                return
            self.engine = self.audio_engine
            self.backend_name = self.audio_engine.backend_name
            self._current_is_video = False
            self._switch_to_lyric_page()
            self.engine.open_file(path)
            self.engine.set_volume(self.vol_slider.value() / 100.0)
            self._load_lyrics(path)
            self._paused_pos_ms = 0
            self.engine.play_stream(0)
            self._set_play_icon(True)
            # 歌词默认全屏播放（设置中可取消）
            if self.lyric_fullscreen and not self._lyric_fs_active:
                QTimer.singleShot(80, self._enter_lyric_fullscreen)

        dur = self.engine.duration_ms()
        self.seek_slider.setRange(0, max(1, dur))
        self.seek_slider.setValue(0)
        self.time_label.setText("0:00 / %s" % _fmt(dur))
        self.statusBar().showMessage(
            "正在播放：%s    [后端 %s]" % (name, self.backend_name), 3000)

    def _switch_to_video_page(self):
        self.right_stack.setCurrentWidget(self.video_page)

    def _switch_to_lyric_page(self):
        self.right_stack.setCurrentWidget(self.lyric_page)

    def _on_space_key(self):
        """空格键 暂停/继续播放（与播放按钮等价，输入控件聚焦时不抢键）。"""
        if QApplication.focusWidget() is not None \
                and isinstance(QApplication.focusWidget(),
                               (QLineEdit, QTextEdit, QAbstractSpinBox)):
            return
        self._on_play_pause()

    def _on_play_pause(self):
        if self.current_index < 0 or self.engine is None \
                or not self.engine.current_path:
            return
        if self.engine.is_playing and not self.engine.is_paused:
            # ① 暂停：缓存当前有效播放位置，UI（滑块/歌词）保持当前值
            self.engine.pause_stream()
            pos = self.engine.position_ms()
            if pos > 0:
                self._paused_pos_ms = pos
            self._set_play_icon(False)
        else:
            if self.engine.is_paused:
                # ② 恢复：直接续播原位置，绝不回 0
                self.engine.resume_stream()
            else:
                # ③ 兜底（引擎被重置/降级/播放线程中断后 is_playing=False 且未暂停）：
                #    从缓存位置续播，禁止用 position_ms() 可能返回的 0
                pos = self._paused_pos_ms if self._paused_pos_ms > 0 \
                    else self.engine.position_ms()
                self.engine.play_stream(max(0, pos))
            self._set_play_icon(True)

    def _cycle_mode(self):
        self.mode = MODES[(MODES.index(self.mode) + 1) % len(MODES)]
        self.mode_btn.setText(self.mode)
        self.statusBar().showMessage("播放模式：%s" % self.mode, 2000)

    def _next(self):
        if not self.songs:
            return
        n = len(self.songs)
        if self.current_index < 0:
            self._play_index(0)
            return
        if self.mode == MODES[2]:  # 随机
            candidates = [i for i in range(n) if i != self.current_index]
            self._play_index(random.choice(candidates) if candidates else 0)
        else:
            self._play_index((self.current_index + 1) % n)

    def _prev(self):
        if not self.songs:
            return
        if self.current_index < 0:
            self._play_index(0)
            return
        n = len(self.songs)
        self._play_index((self.current_index - 1) % n)

    def _on_finished(self):
        # 视频自然播完：若在全屏则自动退出全屏，回到普通窗口
        if self._current_is_video and self.video_view._fullscreen:
            self.video_view._exit_fullscreen()
        if not self.songs:
            self._set_play_icon(False)
            return
        if self.current_index < 0:
            return
        if self.mode == MODES[1]:  # 单曲循环：重新播放当前曲
            if self.engine is not None:
                self.engine.play_stream(0)
            self._set_play_icon(True)
            self.seek_slider.setValue(0)
            self.cur_lyric_idx = -1
            if self._current_is_video:
                self.video_view.hide_ended()
            return
        if self.mode == MODES[2]:  # 随机
            self._next()
            return
        # 顺序播放：到末尾停止
        if self.current_index + 1 < len(self.songs):
            self._play_index(self.current_index + 1)
        else:
            self._set_play_icon(False)
            self.seek_slider.setValue(0)
            self.statusBar().showMessage("播放列表已到末尾", 3000)
            if self._current_is_video:
                self.video_view.show_ended()

    def _append_error_log(self, text):
        """把播放错误（含原始英文、文件路径）追写入 %LOCALAPPDATA%\AuroraPlayer\error.log。"""
        try:
            log_dir = _os.path.join(
                _os.environ.get("LOCALAPPDATA", _os.path.expanduser("~")),
                "AuroraPlayer")
            _os.makedirs(log_dir, exist_ok=True)
            with open(_os.path.join(log_dir, "error.log"), "a",
                      encoding="utf-8") as f:
                f.write("=" * 70 + "\n"
                        + _time.strftime("%Y-%m-%d %H:%M:%S")
                        + "\n播放错误: %s\n" % text)
        except Exception:
            pass

    def _build_fallback_engine(self):
        """构建 ffmpeg 之外的后备引擎，用于声道/采样率不匹配时自动降级重播。"""
        import player_core as pc
        try:
            import pygame  # noqa: F401
            eng = pc.PygameEngine(self)
            return eng, "pygame"
        except Exception:
            pass
        if pc._get_qtm()[0] is not None:
            try:
                return pc.QtEngine(self), "QtMultimedia"
            except Exception:
                pass
        return None, ""

    def _on_engine_error(self, msg):
        self._set_play_icon(False)
        idx = self.current_index
        path = self.songs[idx] if 0 <= idx < len(self.songs) else None
        # 原始英文错误只进日志，不在界面展示
        self._append_error_log(
            "后端[%s] 文件[%s] 错误[%s]"
            % (self.backend_name, path, msg))
        # 视频播放失败：不降级（无替代视频后端），直接提示
        if self._current_is_video:
            self.statusBar().showMessage(
                "无法播放该视频（格式不受支持或文件损坏）", 6000)
            return
        # 声道/采样率等播放异常：ffmpeg 后端自动降级到 pygame / QtMultimedia 重播
        if (self.backend_name.startswith("ffmpeg") and path
                and not getattr(self, "_engine_fallback_done", False)):
            eng, name = self._build_fallback_engine()
            if eng is not None:
                self._engine_fallback_done = True
                try:
                    self.engine.playbackFinished.disconnect()
                    self.engine.playbackError.disconnect()
                except Exception:
                    pass
                # 记录旧引擎的续播位置与暂停态，避免降级后从 0 重播把滑块/歌词归零
                resume_ms = self.engine.position_ms()
                was_paused = bool(self.engine.is_paused)
                try:
                    self.engine.stop_stream()
                except Exception:
                    pass
                self.engine = eng
                self.audio_engine = eng
                self.backend_name = name
                self.engine.playbackFinished.connect(self._on_finished)
                self.engine.playbackError.connect(self._on_engine_error)
                try:
                    self.engine.open_file(path)
                    dur = self.engine.duration_ms()
                    self.seek_slider.setRange(0, max(1, dur))
                    if was_paused:
                        # 暂停态降级：不自动播放，保持暂停画面，续播位置留给"恢复"时承接
                        self._paused_pos_ms = max(0, resume_ms)
                        self.statusBar().showMessage(
                            "已自动切换后端 [%s]（保持暂停，点播放从原处继续）"
                            % name, 6000)
                    else:
                        self.engine.play_stream(max(0, resume_ms))
                        self._paused_pos_ms = max(0, resume_ms)
                        self._set_play_icon(True)
                        self.statusBar().showMessage(
                            "已自动切换后端 [%s] 继续播放：%s"
                            % (name, _os.path.basename(path)), 6000)
                    # 不清零滑块/时间标签/歌词：由 _sync_slider/_sync_lyrics 按真实 position 刷新
                    return
                except Exception as e2:
                    self._append_error_log("降级播放也失败: %s" % (e2,))
        # 无可用降级后端，仅展示友好中文提示
        self.statusBar().showMessage(
            "无法播放该音频（声道/格式不支持）", 6000)

    # ------------------------------------------------------------- 进度
    def _on_slider_pressed(self):
        self._seeking = True

    def _on_slider_moved(self, v):
        dur = self.engine.duration_ms() if self.engine \
            and self.engine.current_path else 0
        self.time_label.setText(
            "%s / %s" % (_fmt(v), _fmt(dur)))

    def _on_slider_released(self):
        self._seeking = False
        if self.engine is not None and self.engine.current_path:
            self.engine.seek_to(self.seek_slider.value())
            self._set_play_icon(True)

    def _on_volume_changed(self, v):
        if self.engine is not None:
            self.engine.set_volume(v / 100.0)
        self.vol_btn.setText("🔇" if v == 0 else "🔊")

    def _toggle_mute(self):
        if self.vol_slider.value() > 0:
            self.vol_slider.setValue(0)
        else:
            self.vol_slider.setValue(80)
        self._on_volume_changed(self.vol_slider.value())

    # ------------------------------------------------------------- 定时刷新
    def _on_tick(self):
        self._sync_play_button()
        self._sync_slider()
        self._sync_lyrics()

    def _sync_play_button(self):
        """按引擎真实状态校正播放/暂停图标（修复双击启动等场景的状态不同步）。"""
        eng = self.engine
        if eng is None or not eng.current_path:
            if self.play_btn.text() != "▶":
                self._set_play_icon(False)
            return
        if self.current_index < 0:
            return
        playing = bool(eng.is_playing) and not bool(eng.is_paused)
        want = "⏸" if playing else "▶"
        if self.play_btn.text() != want:
            self._set_play_icon(playing)

    def _sync_slider(self):
        if self._seeking or self.engine is None or not self.engine.current_path:
            return
        dur = self.engine.duration_ms()
        pos = self.engine.position_ms()
        if dur > 0 and self.seek_slider.maximum() != dur:
            self.seek_slider.setRange(0, dur)
        self.seek_slider.setValue(max(0, min(pos, dur))) if dur > 0 \
            else self.seek_slider.setValue(pos)
        self.time_label.setText("%s / %s" % (_fmt(pos), _fmt(dur)))

    # ------------------------------------------------------------- 歌词
    def _load_lyrics(self, path):
        """加载歌词，优先级：本地同名 .lrc > 内嵌歌词 > 占位提示。

        lrc_parser 延迟导入（内部可能加载 mutagen，避免拖慢启动）。
        """
        import lrc_parser  # 延迟导入
        self.lyric_file, self.lyric_lines = lrc_parser.load_lyrics(path)
        self.lyric_timed = True
        self.static_lyric_lines = []
        self.cur_lyric_idx = -1
        if self.lyric_lines:
            self.lyric_status.setText(os.path.basename(self.lyric_file))
            self._render_lyrics(-1)
            return

        # 无本地 .lrc：尝试读取内嵌歌词（音乐与歌词一体）
        desc, lines, timed = lrc_parser.load_embedded_lyrics(path)
        if desc:
            self.lyric_file = "内嵌:%s" % _os.path.basename(path)
            self.lyric_timed = timed
            if timed:
                self.lyric_lines = lines
                self.lyric_status.setText(desc)
                self._render_lyrics(-1)
            else:
                self.static_lyric_lines = lines
                self.lyric_status.setText(desc)
                self._render_static_lyrics()
            return

        self.lyric_status.setText("无歌词文件")
        self._render_lyric_placeholder()

    def _render_lyrics(self, idx):
        colors = themes.THEMES[self.theme_name]
        primary, muted = colors["primary"], colors["muted"]
        size = self.font_size + 7
        rows = []
        for i, (_t, text) in enumerate(self.lyric_lines):
            anchor = '<a name="L%d"></a>' % i
            if i == idx:
                style = ("color:%s; font-weight:700; font-size:%dpx;" % (
                    primary, size + 2))
            else:
                style = "color:%s; font-size:%dpx;" % (muted, size)
            rows.append('<p style="margin:5px 0;">%s<span style="%s">%s</span></p>' % (
                anchor, style, html.escape(text)))
        body = "".join(rows)

        # 记录渲染前滚动位置：setHtml 会清空文档并把滚动条归零，
        # 先恢复旧位置，再由动画把当前句平滑带到视口正中。
        bar = self.lyric_view.verticalScrollBar()
        prev = bar.value()
        self.lyric_view.setHtml(
            "<div align='center' style='line-height:1.9;'>%s</div>" % body)
        if idx >= 0:
            if prev > 0:
                bar.setValue(prev)
            self._smooth_scroll_lyrics(idx)
        else:
            if self._lyric_anim is not None:
                self._lyric_anim.stop()
                self._lyric_anim = None
            bar.setValue(0)

    def _lyric_block(self, idx):
        """返回第 idx 句在 QTextDocument 中对应的 QTextBlock；越界返回 None。
        按『非空文本块』顺序与歌词行一一对应（跳过 div 等空壳块）。
        """
        doc = self.lyric_view.document()
        count = 0
        block = doc.begin()
        while block.isValid():
            if block.text().strip():
                if count == idx:
                    return block
                count += 1
            block = block.next()
        return None

    def _block_index(self, block):
        """由 QTextBlock 反查它在『非空文本块』序列中的序号（即歌词行号）；找不到返回 -1。"""
        if block is None or not block.isValid():
            return -1
        doc = self.lyric_view.document()
        count = 0
        b = doc.begin()
        while b.isValid():
            if b.text().strip():
                if b == block:
                    return count
                count += 1
            b = b.next()
        return -1

    def _seek_lyric_to(self, idx, ts_ms):
        """把播放位置跳转到第 idx 句对应的时间继续播放，并同步进度条/高亮/时间标签。"""
        if self.engine is None or not self.engine.current_path:
            return
        dur = self.engine.duration_ms()
        pos = min(int(ts_ms), dur) if dur > 0 else int(ts_ms)
        # 各后端 seek_to 均从目标位置继续播放
        self.engine.seek_to(pos)
        self._paused_pos_ms = pos
        self._set_play_icon(True)
        # 高亮当前句并居中平滑滚动
        self.cur_lyric_idx = idx
        self._render_lyrics(idx)
        # 进度条 / 时间标签随动
        self._seeking = False
        self._sync_slider()
        self.statusBar().showMessage(
            "已跳转到歌词：%s" % (self.lyric_lines[idx][1]
            if 0 <= idx < len(self.lyric_lines) else ""), 3000)

    def _on_lyric_dbl_click_block(self, block):
        """双击歌词块：取该行时间戳并跳转播放；无时间轴/未加载/越界等场景直接忽略不报错。"""
        if not self.lyric_timed or not self.lyric_lines \
                or self.engine is None or not self.engine.current_path:
            return
        idx = self._block_index(block)
        if idx < 0 or idx >= len(self.lyric_lines):
            return
        self._seek_lyric_to(idx, self.lyric_lines[idx][0])

    def eventFilter(self, obj, event):
        if obj is self.lyric_view.viewport() \
                and event.type() == QEvent.Type.MouseButtonDblClick:
            cursor = self.lyric_view.cursorForPosition(event.pos())
            self._on_lyric_dbl_click_block(cursor.block())
            return False
        return super().eventFilter(obj, event)

    def _lyric_center_target(self, idx):
        """计算第 idx 句『居中于视口』时应有的垂直滚动值；无可定位返回 None。"""
        if idx < 0 or idx >= len(self.lyric_lines):
            return None
        doc = self.lyric_view.document()
        layout = doc.documentLayout()
        # 确保按视口宽度做布局，块坐标才有效
        doc.setTextWidth(max(1, self.lyric_view.viewport().width()))
        layout.setPaintDevice(self.lyric_view.viewport())
        block = self._lyric_block(idx)
        if block is None or not block.isValid():
            return None
        rect = layout.blockBoundingRect(block)
        if rect.isNull():
            return None
        viewport_h = max(1, self.lyric_view.viewport().height())
        doc_h = layout.documentSize().height()
        bar = self.lyric_view.verticalScrollBar()
        max_v = max(0, int(doc_h - viewport_h))
        # 把该行中心对齐到视口中心
        center = int(rect.center().y() - viewport_h / 2.0)
        return max(0, min(center, max_v))

    def _smooth_scroll_lyrics(self, idx, duration=420):
        """把当前句平滑滚动到居中的目标位置（easeOutCubic 缓动），不做瞬间跳变。"""
        target = self._lyric_center_target(idx)
        if target is None:
            return
        bar = self.lyric_view.verticalScrollBar()
        self._lyric_anim_seq += 1
        seq = self._lyric_anim_seq
        src = bar.value()
        if abs(target - src) <= 1:
            bar.setValue(target)
            return
        if self._lyric_anim is not None:
            self._lyric_anim.stop()
        anim = QPropertyAnimation(bar, b"value", self.lyric_view)
        anim.setDuration(duration)
        anim.setStartValue(src)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        self._lyric_anim = anim
        anim.start()

    def _render_lyric_placeholder(self):
        muted = themes.THEMES[self.theme_name]["muted"]
        primary = themes.THEMES[self.theme_name]["primary"]
        size = self.font_size + 4
        self.lyric_view.setHtml(
            "<div align='center' style='margin-top:60px;'>"
            "<div style='color:%s; font-size:%dpx;'>想实时看到滚动歌词？</div>"
            "<div style='color:%s; font-size:%dpx; margin-top:12px;'>"
            "在歌曲同目录放置同名 .lrc 歌词文件（例如 song.mp3 对应 song.lrc），"
            "或使用含内嵌歌词（音乐歌词一体）的音乐文件，即可自动显示。</div>"
            "</div>" % (muted, size, primary, size - 2))

    def _render_static_lyrics(self):
        """静态纯文本歌词：多行居中展示，不随播放滚动。"""
        colors = themes.THEMES[self.theme_name]
        muted, primary = colors["muted"], colors["primary"]
        size = self.font_size + 6
        rows = [
            "<div style='color:%s; font-size:%dpx; margin:6px 0;'>%s</div>"
            % (muted, size, html.escape(ln))
            for ln in self.static_lyric_lines
        ]
        body = "".join(rows)
        self.lyric_view.setHtml(
            "<div align='center' style='line-height:1.9;'>%s</div>" % body)

    def _redraw_lyrics(self):
        if self.static_lyric_lines:
            self._render_static_lyrics()
        elif self.lyric_lines:
            self._render_lyrics(self.cur_lyric_idx)
        else:
            self._render_lyric_placeholder()

    def _sync_lyrics(self):
        if not self.lyric_timed or not self.lyric_lines \
                or self.engine is None or not self.engine.current_path:
            return
        if self._current_is_video:
            return
        if not (self.engine.is_playing and not self.engine.is_paused):
            return
        pos = self.engine.position_ms()
        idx = _find_lyric_index(self.lyric_lines, pos, self.cur_lyric_idx)
        if idx != self.cur_lyric_idx:
            self.cur_lyric_idx = idx
            self._render_lyrics(idx if idx >= 0 else -1)

    # ------------------------------------------------------------- 持久化
    def _save_playlist(self):
        """把当前播放列表写入 %LOCALAPPDATA%\AuroraPlayer\playlist.json。"""
        try:
            path = _playlist_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "items": self.songs}, f,
                          ensure_ascii=False)
            os.replace(tmp, path)
        except Exception:
            pass

    def _restore_playlist(self):
        """启动时恢复上次的播放列表（过滤已删除文件、自动去重）。"""
        try:
            path = _playlist_path()
            if not os.path.isfile(path):
                return
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            items = data.get("items", [])
            items = [i for i in items
                     if isinstance(i, str) and os.path.isfile(i)]
            if not items:
                return
            added, _indexes = self.import_files(items)
            if added > 0:
                self.statusBar().showMessage(
                    "已恢复上次的播放列表（%d 项）" % added, 4000)
        except Exception:
            pass

    def closeEvent(self, event):
        """退出前保存播放列表并停止引擎。"""
        self._save_playlist()
        if self.engine is not None:
            try:
                self.engine.stop_stream()
            except Exception:
                pass
        super().closeEvent(event)


def _autoplay_paths(win, paths):
    """把命令行传入的媒体文件加入列表并自动播放。

    必须在事件循环内（QTimer.singleShot 延迟到 exec 之后）执行：
    否则在 QApplication.exec() 之前调用 QMediaPlayer.play() 会被 Qt
    事件系统丢弃，导致『双击音频文件后按钮显示播放中但实际不播』或
    『按钮状态与真实播放不同步』的经典问题。
    """
    _added, indexes = win.import_files(paths)
    if indexes:
        win._play_index(indexes[0])
        win._sync_play_button()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Aurora Player")
    app.setOrganizationName("Marvis")
    win = PlayerWindow()
    win.show()

    # 命令行传入的媒体路径：自动加入列表并立即播放
    # （注册为系统默认媒体播放器后，双击音频/视频文件即由此触发）
    argv_paths = [a for a in sys.argv[1:]
                  if os.path.isfile(a)
                  and os.path.splitext(a)[1].lower()
                  in PLAYABLE_EXTENSIONS]
    if argv_paths:
        QTimer.singleShot(0, lambda: _autoplay_paths(win, argv_paths))

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
