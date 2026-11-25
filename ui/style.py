# ---------- GUI（PyQt6：QSS 实现完美深色/浅色双主题） ----------

DARK_BG = "#202020"


DARK_PANEL = "#303030"


DARK_FG = "#E0E0E0"


LIGHT_BG = "#EDEDED"


LIGHT_PANEL = "#F3F3F3"


LIGHT_FG = "#1F1F1F"


ACCENT = "#0078D4"


def scrollbar_qss(dark):
    """滚动条样式：透明轨道 + 细胶囊滑块，不画槽/边框（视觉上只有滑块本体）。
    单独导出：日志控件带局部样式表，必须在同一张表里再给滚动条一条规则——
    否则它会退回系统默认的「带框凹槽」，界面里就多出一个包围框。"""
    if dark:
        return """
        QScrollBar:vertical { border: 0px; background: transparent; width: 8px; margin: 2px 1px; }
        QScrollBar::handle:vertical { background: #555555; min-height: 24px; border: 0px; border-radius: 4px; }
        QScrollBar::handle:vertical:hover { background: #777777; }
        QScrollBar::handle:vertical:pressed { background: #8A8A8A; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { border: 0px; background: transparent; height: 0px; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        QScrollBar::up-arrow:vertical, QScrollBar::down-arrow:vertical { background: transparent; width: 0px; height: 0px; }
        """
    return """
        QScrollBar:vertical { border: 0px; background: transparent; width: 8px; margin: 2px 1px; }
        QScrollBar::handle:vertical { background: #C4C4C4; min-height: 24px; border: 0px; border-radius: 4px; }
        QScrollBar::handle:vertical:hover { background: #A8A8A8; }
        QScrollBar::handle:vertical:pressed { background: #909090; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { border: 0px; background: transparent; height: 0px; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        QScrollBar::up-arrow:vertical, QScrollBar::down-arrow:vertical { background: transparent; width: 0px; height: 0px; }
        """


def _build_stylesheet(dark):
    if dark:
        bg, panel, fg, border = DARK_BG, DARK_PANEL, DARK_FG, "#404040"
        hover, pressed = "#3a3a3a", "#505050"
        scroll = "#505050"
        tip_bg, tip_fg = "#303030", "#E0E0E0"
    else:
        bg, panel, fg, border = LIGHT_BG, LIGHT_PANEL, LIGHT_FG, "#cccccc"
        hover, pressed = "#e8e8e8", "#dcdcdc"
        scroll = "#bbbbbb"
        tip_bg, tip_fg = "#F0F0F0", "#333333"
    scrollbar = scrollbar_qss(dark)
    return f"""
    QMainWindow, QWidget {{ background-color: {bg}; color: {fg}; }}
    QLabel {{ background: transparent; color: {fg}; }}
    QLineEdit, QComboBox {{ background-color: {panel}; color: {fg};
                border: 1px solid {border}; border-radius: 4px; padding: 4px 6px; }}
    QComboBox QAbstractItemView {{ background-color: {panel}; color: {fg};
                selection-background-color: {ACCENT}; }}
    QPushButton {{ background-color: {panel}; color: {fg};
                  border: 1px solid {border}; border-radius: 4px; padding: 5px 12px;
                  text-align: center; outline: none; }}
    QPushButton:hover {{ background-color: {hover}; }}
    QPushButton:pressed {{ background-color: {pressed}; }}
    QPushButton:focus {{ outline: none; }}
    QPushButton:disabled {{ color: #999999; background-color: {bg}; }}
    #accentButton {{ background-color: {ACCENT}; color: #ffffff;
                    border: none; font-weight: bold; outline: none; }}
    #accentButton:hover {{ background-color: #1a88e0; }}
    #accentButton:focus {{ outline: none; }}
    #linkButton {{ background: transparent; border: 1px solid {border};
                  color: {ACCENT}; text-align: left; outline: none;
                  padding: 4px 10px; border-radius: 4px; }}
    #linkButton:hover {{ color: #1a88e0; border-color: #1a88e0;
                        background: rgba(0,120,212,0.06); }}
    #linkButton:focus {{ outline: none; }}
    QMenu {{ background-color: {panel}; color: {fg};
            border: 1px solid {border}; border-radius: 6px; padding: 6px; }}
    QMenu::item {{ background: transparent; color: {fg};
                   padding: 6px 10px; border-radius: 4px; }}
    QMenu::item:selected {{ background-color: {ACCENT}; color: #ffffff; }}
    QMenu::separator {{ height: 1px; background: {border}; margin: 4px 8px; }}
    QGroupBox {{ border: 1px solid {border};
                border-radius: 6px; margin-top: 12px; padding-top: 8px; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {fg}; background-color: {bg}; }}
    QRadioButton {{ background: transparent; color: {fg}; spacing: 6px; outline: none; }}
    QRadioButton:focus {{ outline: none; }}
    QRadioButton::indicator {{ width: 16px; height: 16px; }}
    QCheckBox {{ background: transparent; color: {fg}; spacing: 6px; outline: none; }}
    QCheckBox:focus {{ outline: none; }}
    QSpinBox {{ background-color: {panel}; color: {fg};
               border: 1px solid {border}; border-radius: 4px; padding: 4px 6px; outline: none; }}
    QSpinBox:focus {{ outline: none; }}
    QSpinBox:disabled {{ background-color: {bg}; color: #999999; border-color: transparent; outline: none; }}
    QSpinBox::up-button, QSpinBox::down-button {{ width: 0px; border: none; background: transparent; }}
    QTreeWidget {{ background-color: {panel}; color: {fg};
                  border: 1px solid {border}; border-radius: 4px; outline: none; }}
    QTreeWidget::item {{ outline: none; }}
    QTreeWidget::branch {{ outline: none; }}
    QHeaderView::section {{ background-color: {panel}; color: {fg}; border: none; }}
    QSplitter {{ background: transparent; border: none; }}
    QSplitter::handle {{ background-color: transparent; border: none; }}
    QSplitter::handle:vertical {{ height: 1px; }}
    QProgressBar {{ background-color: {panel}; color: {fg};
                   border: 1px solid {border}; border-radius: 4px; text-align: center; }}
    QProgressBar::chunk {{ background-color: {ACCENT}; border-radius: 3px; }}
    QTextEdit {{ background-color: {bg}; color: {fg};
                border: 1px solid {border}; border-radius: 4px;
                font-family: Consolas, "Microsoft YaHei", sans-serif; }}
    {scrollbar}
    QLabel#statusLabel {{ background-color: {bg}; color: {fg}; border: none; padding: 2px 0px; }}
    QToolTip {{ background-color: {tip_bg}; color: {tip_fg};
                border: none; border-radius: 4px; padding: 6px 8px; }}
    """


def _translate_menu(menu):
    """将标准编辑菜单的英文项（含 & 标记和 \t 后的快捷键提示）就地替换为中文，保留图标与快捷键。"""
    tr = {
        "undo": "撤销", "redo": "重做", "cut": "剪切", "copy": "复制",
        "paste": "粘贴", "delete": "删除", "select all": "全选",
    }
    for act in menu.actions():
        parts = act.text().split("\t")
        key = parts[0].replace("&", "").strip().lower()
        if key in tr:
            shortcut = parts[1] if len(parts) > 1 else ""
            act.setText(tr[key] + ("\t" + shortcut if shortcut else ""))
