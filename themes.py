# -*- coding: utf-8 -*-
"""
themes.py —— 界面主题（QSS 配色）与生成函数
==============================================
内置 5 套配色主题，可随时通过下拉框切换；
所有控件的字体大小跟随全局字号变量统一缩放。

说明：QSS 模板为 string.Template 语法（${var}），
避免 CSS 自身花括号与 str.format 的字段语法冲突。
"""
import string

THEMES = {
    "深空暗蓝": dict(
        bg="#0f1420", panel="#141b2b", card="#1b2436",
        fg="#e8ecf5", muted="#8a94ad", primary="#5aa9ff",
        accent="#ffb454", border="#27334f", hover="#233252",
        selbg="#2c3d63", danger="#ff6b6b",
    ),
    "曜夜纯黑": dict(
        bg="#0a0a0c", panel="#111114", card="#17171b",
        fg="#f2f2f2", muted="#7d7d86", primary="#9b7dff",
        accent="#3fd9a5", border="#26262c", hover="#222229",
        selbg="#2c2c37", danger="#ff5f5f",
    ),
    "樱粉轻语": dict(
        bg="#fdf2f5", panel="#ffeef4", card="#ffffff",
        fg="#43343e", muted="#9d8492", primary="#ff7ba9",
        accent="#6c8cff", border="#f2d5e0", hover="#ffe3ec",
        selbg="#ffd7e4", danger="#e34a6f",
    ),
    "薄荷青柠": dict(
        bg="#f0faf6", panel="#e4f6ee", card="#ffffff",
        fg="#2e3d37", muted="#7ca392", primary="#12b886",
        accent="#f0b429", border="#cceadd", hover="#dbf5ea",
        selbg="#bfebd7", danger="#e8503a",
    ),
    "奶油暖阳": dict(
        bg="#fbf6ee", panel="#f6eddd", card="#fffdf8",
        fg="#4a4037", muted="#a6927f", primary="#e8842c",
        accent="#3aa0ff", border="#ecdcbf", hover="#f7e9d2",
        selbg="#f7dfbe", danger="#d64545",
    ),
}

_QSS_TEMPLATE = string.Template("""
* {
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: ${base}px;
}
QMainWindow, #central { background: ${bg}; }
QWidget { background: transparent; color: ${fg}; }
/* 设置页整页覆盖在中央区域之上，使用面板色保证不透明 */
QWidget#settingsPage { background: ${panel}; }
/* QVideoWidget 使用原生渲染通道，不受 QWidget 透明背景规则影响；
   显式声明黑底避免全局样式覆盖导致视频画面不渲染（黑屏）。 */
QVideoWidget { background: black; }

QFrame#card, QFrame#panel {
    background: ${card}; border: 1px solid ${border}; border-radius: 14px;
}
QFrame#panel { background: ${panel}; }

QLabel#brand {
    font-size: ${b6}px; font-weight: 700; color: ${primary};
    letter-spacing: 1px;
}
QLabel#sectionTitle { font-size: ${b2}px; font-weight: 600; color: ${fg}; }
QLabel#muted { color: ${muted}; }
QLabel#timeLabel { color: ${muted}; font-variant-numeric: tabular-nums; }

QPushButton, QToolButton, QComboBox, QSpinBox {
    background: ${card}; color: ${fg};
    border: 1px solid ${border}; border-radius: 8px;
    padding: 5px 12px;
}
QPushButton:hover, QToolButton:hover {
    background: ${hover}; border: 1px solid ${primary};
}
QPushButton:pressed, QToolButton:pressed { background: ${selbg}; }
QToolButton { font-size: ${b6}px; padding: 4px 10px; }

QPushButton#playBtn {
    border-radius: 20px; min-width: 40px; min-height: 40px;
    background: ${primary}; color: #ffffff; border: none;
    font-size: ${b6}px; font-weight: 700;
}
QPushButton#playBtn:hover { background: ${accent}; color: #1c1c1c; }

QPushButton#modeBtn {
    background: ${panel}; border: 1px solid ${primary}; border-radius: 14px;
    color: ${primary}; padding: 5px 10px; font-weight: 600;
    min-width: 74px;
}
QPushButton#modeBtn:hover { background: ${hover}; }

/* 自定义标题栏窗口控制按钮：颜色取自主题变量，切换主题自动适配 */
QPushButton#winBtnMin, QPushButton#winBtnMax, QPushButton#winBtnClose {
    background: transparent; border: none; border-radius: 6px;
    color: ${fg}; font-size: ${b6}px; font-weight: 600;
    padding: 0; margin: 0;
}
QPushButton#winBtnMin:hover, QPushButton#winBtnMax:hover {
    background: ${hover}; color: ${fg}; border: none;
}
QPushButton#winBtnClose:hover {
    background: ${danger}; color: #ffffff; border: none;
}
QPushButton#winBtnClose:pressed {
    background: ${danger}; color: #ffffff; border: none;
}

QListWidget {
    background: transparent; border: none; outline: none;
    padding: 4px;
}
QListWidget::item {
    color: ${fg}; border-radius: 8px; padding: 6px 10px; margin: 1px 2px;
}
QListWidget::item:hover { background: ${hover}; }
QListWidget::item:selected {
    background: ${selbg}; color: ${primary}; font-weight: 600;
}

QTextBrowser#lyric { background: transparent; border: none; }

QComboBox::drop-down, QSpinBox::up-button, QSpinBox::down-button {
    border: none; width: 18px;
}
QComboBox QAbstractItemView {
    background: ${card}; border: 1px solid ${border}; border-radius: 8px;
    selection-background-color: ${selbg}; selection-color: ${primary};
}

QSlider::groove:horizontal {
    height: 6px; background: ${border}; border-radius: 3px;
}
QSlider::sub-page:horizontal {
    background: ${primary}; border-radius: 3px;
}
QSlider::handle:horizontal {
    width: 16px; height: 16px; margin: -5px 0;
    background: #ffffff; border: 2px solid ${primary}; border-radius: 8px;
}
QSlider::handle:horizontal:hover { background: ${accent}; }

QScrollBar:vertical {
    background: transparent; width: 10px; margin: 2px;
}
QScrollBar::handle:vertical {
    background: ${border}; border-radius: 4px; min-height: 30px;
}
QScrollBar::handle:vertical:hover { background: ${primary}; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; }
QScrollBar::handle:horizontal {
    background: ${border}; border-radius: 4px; min-width: 30px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

QSplitter::handle { background: ${border}; width: 6px; border-radius: 3px; }

QStatusBar { color: ${muted}; background: ${panel}; border-top: 1px solid ${border}; }
QToolTip { background: ${card}; color: ${fg}; border: 1px solid ${primary}; }
""")


def build_qss(theme_name, base_size=13):
    """根据主题名与基础字号生成整份 QSS 字符串。"""
    values = dict(THEMES[theme_name])
    b = int(base_size)
    values.update(base=b, b2=b + 2, b6=b + 6)
    return _QSS_TEMPLATE.safe_substitute(values)


def hex_to_rgb(hex_color):
    """'#rrggbb' -> (r, g, b)，便于主窗口生成半透明色。"""
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
