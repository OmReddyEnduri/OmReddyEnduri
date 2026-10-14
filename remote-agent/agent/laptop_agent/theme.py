BG, CARD, CARD2, BORDER = "#0e1319", "#161d26", "#1c2530", "#263241"
TEXT, MUTED = "#e6edf5", "#8b9bb0"
ACCENT, OK, WARN, BAD = "#4f8cff", "#2fcf8a", "#f5a524", "#ff5c6c"

STATUS_COLORS = {
    "connected": OK, "reconnecting": WARN, "paused": WARN, "stopped": MUTED,
    "disconnected": MUTED, "auth_failed": BAD,
    "completed": OK, "running": ACCENT, "awaiting_approval": WARN, "blocked": BAD,
    "rejected": BAD, "failed": BAD, "timeout": BAD, "cancelled": MUTED, "received": MUTED,
    "classified": MUTED, "aborted": BAD,
}

QSS = f"""
QFrame#bubbleYou {{ background: {ACCENT}; border-radius: 14px; }}
QFrame#bubbleBrain {{ background: {CARD2}; border: 1px solid {BORDER}; border-radius: 14px; }}
QLabel#sysline {{ color: {MUTED}; font-size: 12px; }}
QLabel#bubbleText {{ background: transparent; }}
QComboBox {{ background: {CARD2}; border: 1px solid {BORDER}; border-radius: 8px; padding: 7px 12px; min-height: 18px; }}
QComboBox:hover {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox QAbstractItemView {{ background: {CARD}; border: 1px solid {BORDER}; selection-background-color: {CARD2}; outline: 0; padding: 4px; }}
QListWidget {{ background: #0a0f14; border: 1px solid {BORDER}; border-radius: 10px; padding: 6px; outline: 0; }}
QListWidget::item {{ padding: 11px 12px; border-radius: 8px; color: {TEXT}; margin-bottom: 2px; }}
QListWidget::item:hover {{ background: {CARD}; }}
QListWidget::item:selected {{ background: {CARD2}; color: {TEXT}; border-left: 3px solid {ACCENT}; }}
QFrame#sidebar {{ background: #0a0e13; border-right: 1px solid {BORDER}; }}
QPushButton#nav {{ text-align: left; background: transparent; border: none; padding: 11px 14px; border-radius: 8px; color: {MUTED}; font-weight: 600; }}
QPushButton#nav:hover {{ color: {TEXT}; background: {CARD}; }}
QPushButton#nav:checked {{ color: {TEXT}; background: {CARD2}; }}
QPushButton#stop {{ background: {BAD}; border-color: {BAD}; color: white; font-weight: 700; }}
QTextBrowser {{ background: #0a0f14; border: 1px solid {BORDER}; border-radius: 8px; padding: 8px; }}
QScrollArea {{ background: transparent; border: none; }}
* {{ font-family: 'Segoe UI', 'Inter', 'SF Pro Text', 'DejaVu Sans', sans-serif; font-size: 13px; color: {TEXT}; }}
QMainWindow, QDialog, QWidget#root {{ background: {BG}; }}
QLabel#title {{ font-size: 20px; font-weight: 700; }}
QLabel#muted, QLabel#caption {{ color: {MUTED}; }}
QLabel#caption {{ font-size: 11px; font-weight: 600; letter-spacing: 1px; }}
QLabel#value {{ font-family: 'Cascadia Mono', 'Consolas', 'DejaVu Sans Mono', monospace; font-size: 12px; }}
QFrame#card {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px; }}
QLabel#pill {{ border-radius: 12px; padding: 4px 12px; font-weight: 600; background: {CARD2}; }}
QPushButton {{ background: {CARD2}; border: 1px solid {BORDER}; border-radius: 8px; padding: 8px 16px; font-weight: 600; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: {MUTED}; }}
QPushButton#primary {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
QPushButton#danger {{ background: transparent; border-color: {BAD}; color: {BAD}; }}
QPushButton#danger:hover {{ background: {BAD}; color: white; }}
QPushButton#approve {{ background: {OK}; border-color: {OK}; color: #06241a; font-size: 14px; padding: 10px 20px; }}
QPushButton#reject {{ background: {BAD}; border-color: {BAD}; color: white; font-size: 14px; padding: 10px 20px; }}
QPushButton#reject:default {{ border: 2px solid white; }}
QPushButton#neutral {{ font-size: 14px; padding: 10px 20px; }}
QPushButton#seg {{ border-radius: 0; padding: 10px 22px; margin: 0; }}
QPushButton#seg:checked {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
QPushButton#segL {{ border-top-left-radius: 8px; border-bottom-left-radius: 8px; border-top-right-radius: 0; border-bottom-right-radius: 0; padding: 10px 22px; }}
QPushButton#segR {{ border-top-right-radius: 8px; border-bottom-right-radius: 8px; border-top-left-radius: 0; border-bottom-left-radius: 0; padding: 10px 22px; }}
QPushButton#segL:checked, QPushButton#segR:checked {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 12px; background: {CARD}; top: -1px; }}
QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 10px 18px; margin-right: 4px; border-bottom: 2px solid transparent; font-weight: 600; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QTableWidget {{ background: transparent; border: none; gridline-color: transparent; selection-background-color: {CARD2}; alternate-background-color: #131a22; }}
QHeaderView {{ background: {CARD}; border: none; }}
QTableCornerButton::section {{ background: {CARD}; border: none; }}
QHeaderView::section {{ background: transparent; color: {MUTED}; border: none; border-bottom: 1px solid {BORDER}; padding: 8px; font-weight: 600; text-align: left; }}
QPlainTextEdit, QLineEdit {{ background: #0a0f14; border: 1px solid {BORDER}; border-radius: 8px; padding: 8px; font-family: 'Cascadia Mono', 'Consolas', 'DejaVu Sans Mono', monospace; font-size: 12px; selection-background-color: {ACCENT}; }}
QLineEdit:focus, QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
"""
