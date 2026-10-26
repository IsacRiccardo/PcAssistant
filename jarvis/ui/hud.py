"""
Jarvis overlay drawn with Qt: a floating animated orb that fades in above the taskbar when Jarvis
wakes up and fades away when it goes back to sleep. No window frame, no taskbar button, clicks pass
straight through it and it never takes keyboard focus.
Qt draws with smooth (anti-aliased) edges on a truly see-through window, so the orb can have a soft
glow. How it moves lives in ui.orb; this only draws it.
"""
import logging
import math
import signal
import threading
from collections.abc import Callable

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QGuiApplication, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from .config import UiConfig
from .orb import Bubble, Orb

CAPTION_FLAGS = Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap
CAPTION_SIDES = 24      # space left and right of the caption text
PILL = (44, 26)         # size of the small bubble the caption grows out of

log = logging.getLogger("jarvis")


def _color(hex_color: str, alpha: float = 1.0) -> QColor:
    c = QColor(hex_color)
    c.setAlphaF(alpha)
    return c


def _font(family: str, size: int) -> QFont:
    font = QFont(family)
    font.setPointSizeF(size)
    return font


class Hud(Orb):
    can_choose = True   # a search may find many files: they go in a list to click one of

    def __init__(self, cfg: UiConfig):
        super().__init__(cfg)
        self.app = QApplication.instance() or QApplication([])
        self.window = _Window(self)
        self.drawn = "starting"              # the state of the frame the window paints
        self.fonts = (_font(cfg.font, cfg.heard_font_size), _font(cfg.font, cfg.reply_font_size))
        self._layout_key, self._layout = None, None
        self.bubble = Bubble()
        # a list to choose from, asked for by the voice thread and shown by the window's: (title, items,
        # on_pick), or 'close' to take it away
        self._choices_lock = threading.Lock()
        self._choices_asked = None
        self.picker = None

    def choose(self, title: str, items: list[tuple[str, str]], on_pick: Callable[[int], None]) -> bool:
        with self._choices_lock:
            self._choices_asked = (title, items, on_pick)
            self.choosing = True
        return True

    def close_choices(self) -> None:
        with self._choices_lock:
            self._choices_asked = "close"
            self.choosing = False

    def _show_choices(self) -> None:
        """On the window's thread, which alone may make windows: show or close the list asked for."""
        with self._choices_lock:
            asked, self._choices_asked = self._choices_asked, None
        if asked is None:
            return
        old, self.picker = self.picker, None   # so closing it below doesn't count as the new one closing
        if old:
            try:
                old.close()
            except RuntimeError:   # already closed and gone, by a click
                pass
        if asked != "close":
            self.picker = _Picker(self.cfg, *asked, above=self.window.geometry(), on_closed=self._choices_closed)

    def _choices_closed(self, picker) -> None:
        """The list was closed: picked from, Esc, the X, or a click elsewhere."""
        with self._choices_lock:
            if picker is self.picker and self._choices_asked is None:   # not already replaced by another
                self.picker, self.choosing = None, False

    def run(self, work) -> None:
        """Run work() on a background thread and animate the overlay until Ctrl+C."""
        def guarded():
            try:
                work()
            except Exception as e:
                log.exception("Voice loop crashed")   # shows on the terminal too
                self.reply, self.state = str(e), "error"

        threading.Thread(target=guarded, daemon=True).start()
        signal.signal(signal.SIGINT, lambda *_: self.app.quit())   # handled between frames
        timer = QTimer(timerType=Qt.PreciseTimer, interval=1000 // self.cfg.fps)
        timer.timeout.connect(self._tick)
        timer.start()
        self.window.show()
        self.app.exec()

    def _tick(self):
        if self.closing:
            self.app.quit()
            return
        self._show_choices()
        alpha = self.alpha
        self.drawn = self.step()
        if self.alpha != alpha:
            self.window.setWindowOpacity(self.alpha)
        if self.alpha == 0:
            self.bubble.reset()              # so the caption grows out of its pill again next time
            return
        cfg, cx, top = self.cfg, self.cfg.width / 2, self.caption_y
        panel, _ = self._caption_layout(self.drawn)
        # going to sleep, the caption shrinks back into its pill while the orb leaves
        target = (panel.left(), panel.top(), panel.right(), panel.bottom()) if panel and self.shown else None
        pill = (cx - PILL[0] / 2, top - 10, cx + PILL[0] / 2, top - 10 + PILL[1])
        self.bubble.step(target, pill, self.caption(self.drawn), 1 / cfg.fps, cfg.fade_seconds, cfg.bounce)
        self.window.update()                 # repaint on the next pass of the event loop

    def paint(self, p: QPainter):
        """Draw the frame step() worked out: the caption, then the orb from the back to the front."""
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        self._paint_caption(p, self.drawn)
        center = QPointF(self.cfg.width / 2, self.cfg.orb_y)
        p.translate(center)                  # the whole orb springs in and sinks away around its centre
        p.scale(self.scale, self.scale)
        p.translate(-center)
        self._paint_glow(p, center)
        self._paint_sound_ring(p, center)
        self._paint_rings(p, center)
        self._paint_core(p, center)

    def _paint_glow(self, p: QPainter, center: QPointF):
        """Soft light behind the core: brighter as it gets louder, and flaring up as the orb arrives."""
        color, level, bloom = self.color, self.level, self.bloom
        radius = self.core_radius * (2.6 + 0.8 * bloom)
        glow = QRadialGradient(center, radius)
        glow.setColorAt(0.0, _color(color, min(1.0, (0.30 + 0.30 * level) * (1 + 1.2 * bloom))))
        glow.setColorAt(0.45, _color(color, min(1.0, (0.12 + 0.12 * level) * (1 + 1.2 * bloom))))
        glow.setColorAt(1.0, _color(color, 0.0))
        p.setPen(Qt.NoPen)
        p.setBrush(glow)
        p.drawEllipse(center, radius, radius)

    def _paint_sound_ring(self, p: QPainter, center: QPointF):
        """A thin circle round the core, and the bars outside it that move with the sound."""
        core_r, color = self.core_radius, self.color
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(_color(color, 0.8 * self.parts["bars"]), 1))
        p.drawEllipse(center, core_r + 5, core_r + 5)
        p.setPen(QPen(_color(color), 3, Qt.SolidLine, Qt.RoundCap))
        inner = core_r + 11
        for angle, length in self.bars:
            direction = QPointF(math.cos(angle), math.sin(angle))
            p.drawLine(center + direction * inner, center + direction * (inner + length))

    def _paint_rings(self, p: QPainter, center: QPointF):
        """The spinning arcs. Qt's angles are in 1/16 degree, counter-clockwise from 3 o'clock."""
        for ring in self.cfg.rings:
            length = round(self.ring_length(ring.length) * 16)
            if not length:
                continue
            p.setPen(QPen(_color(self.color), ring.width, Qt.SolidLine, Qt.FlatCap))
            box = QRectF(center.x() - ring.radius, center.y() - ring.radius, 2 * ring.radius, 2 * ring.radius)
            p.drawArc(box, round(self.ring_start(ring.start, ring.speed) * 16), length)

    def _paint_core(self, p: QPainter, center: QPointF):
        p.setPen(QPen(_color(self.color), 2))
        p.setBrush(_color(self.core_color))
        p.drawEllipse(center, self.core_radius, self.core_radius)

    def _caption_layout(self, state: str):
        """Where the caption lines and their bubble go; worked out again only when the text changes."""
        key = (state, self.you, self.reply)
        if key != self._layout_key:
            cfg, y = self.cfg, self.caption_y
            width = cfg.width - 2 * CAPTION_SIDES
            lines = []
            for i, (text, font, color) in enumerate(zip(
                    self.caption(state), self.fonts,
                    (cfg.heard_color, cfg.colors["error"] if state == "error" else cfg.reply_color))):
                if not text:
                    continue
                box = QFontMetricsF(font).boundingRect(QRectF(CAPTION_SIDES, y, width, 10_000), CAPTION_FLAGS, text)
                lines.append((i, QRectF(CAPTION_SIDES, y, width, box.height()), box, text, font, color))
                y = box.bottom() + 4
            panel = None
            if lines:
                panel = lines[0][2]
                for line in lines[1:]:
                    panel = panel.united(line[2])
                panel = panel.adjusted(-14, -10, 14, 10)
            self._layout_key, self._layout = key, (panel, lines)
        return self._layout

    def _paint_caption(self, p: QPainter, state: str):
        """The bubble as it springs to fit, and each line as it rises into place."""
        bubble = self.bubble
        if bubble.rect is None or bubble.alpha < 0.01:
            return
        left, top, right, bottom = bubble.rect
        panel = QRectF(left, top, max(right - left, 0), max(bottom - top, 0))
        radius = min(14, panel.height() / 2, panel.width() / 2)   # a pill while it's small
        p.setPen(Qt.NoPen)
        p.setBrush(_color(self.cfg.panel_color, bubble.alpha))
        p.drawRoundedRect(panel, radius, radius)
        for i, rect, _, text, font, color in self._caption_layout(state)[1]:
            alpha, below = bubble.line(i)
            if alpha <= 0:
                continue
            p.setFont(font)
            p.setPen(_color(color, alpha * bubble.alpha ** 3))   # text goes before its bubble does
            p.drawText(rect.translated(0, below), CAPTION_FLAGS, text)


PICKER_WIDTH = 440
PICKER_TEXT = PICKER_WIDTH - 100   # room for a row's text, next to its number and the scroll bar
PICKER_ROWS = 8      # rows shown at once; the rest scroll
ROW_HEIGHT = 46
ROW_GAP = 2
TYPING_MS = 700      # pressing 1 and then 2 within this picks the twelfth


class _Picker(QWidget):
    """A list of files to click one of, floating above the orb like a dropdown: each row is a number
    (what 'the second one' counts), the name and where it is. A long list scrolls, with the wheel or
    the arrow keys. Clicking a row, Enter, or typing its number picks it; Esc, the X or clicking
    elsewhere closes the list. Unlike the orb it takes clicks."""

    def __init__(self, cfg: UiConfig, title: str, items: list[tuple[str, str]],
                 on_pick: Callable[[int], None], above, on_closed: Callable[["_Picker"], None]):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.on_pick, self.on_closed = on_pick, on_closed
        self.used = False    # the mouse has been over it or a key pressed in it
        self.typed = ""      # digits typed so far, for numbers above 9
        self.typing = QTimer(self, singleShot=True, interval=TYPING_MS)
        self.typing.timeout.connect(self._pick_typed)
        self.setAttribute(Qt.WA_TranslucentBackground)    # the panel's rounded corners show what's behind
        self.setAttribute(Qt.WA_DeleteOnClose)
        accent, muted = cfg.colors["listening"], _color(cfg.heard_color, 0.45).name(QColor.HexArgb)
        # fonts are set on the labels rather than in the style sheet, so eliding long names measures
        # them in the font they're drawn in
        self.fonts = {"title": _font(cfg.font, 9), "name": _font(cfg.font, 10), "detail": _font(cfg.font, 9),
                      "number": _font(cfg.font, 10)}
        self.fonts["title"].setWeight(QFont.DemiBold)
        self.fonts["number"].setWeight(QFont.Bold)
        self.setStyleSheet(f"""
            #panel {{ background: {cfg.panel_color}; border: 1px solid {_color(cfg.heard_color, 0.35).name(QColor.HexArgb)};
                      border-radius: 12px; }}
            QScrollArea, #qt_scrollarea_viewport, #rows {{ background: transparent; border: none; }}
            QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px 0 2px 2px; }}
            QScrollBar::handle:vertical {{ background: {muted}; border-radius: 3px; min-height: 24px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
            QLabel {{ color: {cfg.reply_color}; background: transparent; }}
            QLabel#title, QLabel#detail {{ color: {cfg.heard_color}; }}
            QLabel#number {{ color: {accent}; }}
            QPushButton {{ border: none; background: transparent; border-radius: 8px; text-align: left; }}
            QPushButton#row:focus {{ background: {_color(accent, 0.08).name(QColor.HexArgb)}; outline: none; }}
            QPushButton#row:hover {{ background: {_color(accent, 0.18).name(QColor.HexArgb)}; }}
            QPushButton#close {{ color: {cfg.heard_color}; font-size: 11pt; padding: 2px 8px; }}
            QPushButton#close:hover {{ color: {cfg.reply_color}; }}
        """)
        panel = QWidget(self, objectName="panel")
        panel.setAttribute(Qt.WA_StyledBackground)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(panel)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 6, 6, 8)
        layout.setSpacing(2)

        head = QHBoxLayout()
        head.setContentsMargins(8, 2, 0, 2)
        head.addWidget(QLabel(title, objectName="title", font=self.fonts["title"]))
        head.addStretch()
        close = QPushButton("✕", objectName="close", cursor=Qt.PointingHandCursor)
        close.clicked.connect(self.close)
        head.addWidget(close)
        layout.addLayout(head)

        holder = QWidget(objectName="rows")
        rows = QVBoxLayout(holder)
        rows.setContentsMargins(0, 0, 2, 0)
        rows.setSpacing(ROW_GAP)
        self.rows = [self._row(i, name, detail) for i, (name, detail) in enumerate(items)]
        for row in self.rows:
            rows.addWidget(row)
        self.scroll = QScrollArea(widgetResizable=True, frameShape=QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setWidget(holder)
        layout.addWidget(self.scroll)

        # as many rows as fit between the top of the screen and the orb, up to PICKER_ROWS
        screen_top = QGuiApplication.primaryScreen().availableGeometry().top()
        room = (above.top() - screen_top - 70) // (ROW_HEIGHT + ROW_GAP)   # 70: the title and margins
        self.shown = max(1, min(PICKER_ROWS, len(items), room))
        self.scroll.setFixedHeight(self.shown * (ROW_HEIGHT + ROW_GAP) - ROW_GAP)
        self.scroll.verticalScrollBar().setSingleStep(ROW_HEIGHT + ROW_GAP)   # the wheel moves whole rows
        self.setFixedWidth(PICKER_WIDTH)
        self.adjustSize()
        # centred over the orb, just above where it shows up
        self.move(above.center().x() - PICKER_WIDTH // 2, max(screen_top + 8, above.top() - self.height() - 8))
        self.show()
        self.raise_()
        self.activateWindow()
        if self.rows:
            self.rows[0].setFocus()

    def _row(self, i: int, name: str, detail: str) -> QPushButton:
        row = QPushButton(objectName="row", cursor=Qt.PointingHandCursor)
        row.setFixedHeight(ROW_HEIGHT)
        line = QHBoxLayout(row)
        line.setContentsMargins(10, 5, 10, 5)
        line.setSpacing(10)
        number = QLabel(str(i + 1), objectName="number", font=self.fonts["number"])
        number.setFixedWidth(20)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        for text, kind in ((name, "name"), (detail, "detail")):
            label = QLabel(objectName=kind, font=self.fonts[kind])
            label.setText(QFontMetricsF(self.fonts[kind]).elidedText(text, Qt.ElideMiddle, PICKER_TEXT))
            label.setToolTip(text)
            texts.addWidget(label)
        line.addWidget(number)
        line.addLayout(texts, 1)
        for label in row.findChildren(QLabel):   # clicks go to the row underneath
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
        row.clicked.connect(lambda _=False: self._pick(i))
        # the rows get the keys first, and the scroll area would take the arrows to scroll
        row.installEventFilter(self)
        return row

    def _pick(self, i: int) -> None:
        self.close()
        try:
            self.on_pick(i)
        except Exception:
            log.exception("Picking from the list failed")

    def _type(self, digit: str) -> None:
        """A number key: picks that row, or waits a moment for a second digit if there are that many."""
        self.typed += digit
        if int(self.typed) * 10 > len(self.rows):   # no row has a longer number starting like this
            self._pick_typed()
        else:
            self.typing.start()

    def _pick_typed(self) -> None:
        self.typing.stop()
        n, self.typed = int(self.typed or 0), ""
        if 1 <= n <= len(self.rows):
            self._pick(n - 1)

    def _key(self, event) -> bool:
        """Esc, Enter, a number, or moving through the rows. False for any other key."""
        self.used = True
        key, rows = event.key(), self.rows
        current = rows.index(self.focusWidget()) if self.focusWidget() in rows else 0
        moves = {Qt.Key_Down: 1, Qt.Key_Up: -1, Qt.Key_PageDown: PICKER_ROWS, Qt.Key_PageUp: -PICKER_ROWS,
                 Qt.Key_Home: -len(rows), Qt.Key_End: len(rows)}
        if key == Qt.Key_Escape:
            self.close()
        elif key in moves and rows:
            row = rows[min(max(current + moves[key], 0), len(rows) - 1)]
            row.setFocus()
            self._scroll_to(rows.index(row))
        elif key in (Qt.Key_Return, Qt.Key_Enter) and rows:
            self._pick(current)
        elif Qt.Key_0 <= key <= Qt.Key_9:
            self._type(chr(key))
        else:
            return False
        return True

    def _scroll_to(self, i: int) -> None:
        """Scroll just enough for row i to show, by whole rows, so none is cut off at the edges."""
        step, bar = ROW_HEIGHT + ROW_GAP, self.scroll.verticalScrollBar()
        top = min(i, max(bar.value() // step, i - self.shown + 1))
        bar.setValue(top * step)

    def eventFilter(self, row, event):
        if event.type() == QEvent.FocusIn:   # a row reached with Tab scrolls into view
            self._scroll_to(self.rows.index(row))
        elif event.type() == QEvent.KeyPress and self._key(event):
            return True
        return super().eventFilter(row, event)

    def keyPressEvent(self, event):
        if not self._key(event):
            super().keyPressEvent(event)

    def enterEvent(self, event):
        self.used = True
        super().enterEvent(event)

    def closeEvent(self, event):
        self.on_closed(self)
        super().closeEvent(event)

    def changeEvent(self, event):
        """Clicking anywhere else closes it, like a dropdown, once the user has been in it with the
        mouse or the keys. Not before: the app they're typing in may take the focus straight back
        as the list appears, and it would vanish before they saw it."""
        if event.type() == QEvent.ActivationChange and not self.isActiveWindow() and self.used:
            self.close()
        super().changeEvent(event)


class _Window(QWidget):
    def __init__(self, hud: Hud):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.hud = hud
        cfg = hud.cfg
        self.setAttribute(Qt.WA_TranslucentBackground)    # see-through wherever nothing is drawn
        self.setAttribute(Qt.WA_ShowWithoutActivating)    # never takes focus from the app you're in
        self.setFixedSize(cfg.width, cfg.height)
        area = QGuiApplication.primaryScreen().availableGeometry()   # centred, just above the taskbar
        self.move(area.x() + (area.width() - cfg.width) // 2, area.bottom() + 1 - cfg.height - cfg.margin)
        self.setWindowOpacity(0.0)

    def paintEvent(self, event):
        p = QPainter(self)
        self.hud.paint(p)
        p.end()
