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

from PySide6.QtCore import (QAbstractAnimation, QEasingCurve, QEvent, QPoint, QPointF, QPropertyAnimation, QRectF, Qt,
                            QTimer, QVariantAnimation)
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QGuiApplication, QLinearGradient, QPainter, QPen,
                           QPolygonF, QRadialGradient)
from PySide6.QtWidgets import (QApplication, QFrame, QGraphicsEffect, QGridLayout, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .config import UiConfig
from .orb import Bubble, Orb, ease_out, progress
from .status import Card

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
        # a window over the orb (a list of files to choose from, web results, or a card like the weather),
        # asked for by the voice thread and made by the window's: a function making it from (above,
        # on_closed), or 'close' to take it away
        self._choices_lock = threading.Lock()
        self._choices_asked = None
        self.picker = None

    def choose(self, title: str, items: list[tuple[str, str]], on_pick: Callable[[int], None]) -> bool:
        self._ask(lambda above, on_closed: _Picker(self.cfg, title, items, on_pick, above, on_closed))
        return True

    def show_web_results(self, title: str, results: list[tuple[str, str, str]], on_open: Callable[[int], None],
                         on_more: Callable[[], None]) -> bool:
        self._ask(lambda above, on_closed: _WebResults(self.cfg, title, results, on_open, on_more, above, on_closed))
        return True

    def show_card(self, card: Card) -> bool:
        self._ask(lambda above, on_closed: _Card(self.cfg, card, above, on_closed))
        return True

    def close_choices(self) -> None:
        self._ask("close")

    def _ask(self, window) -> None:
        with self._choices_lock:
            self._choices_asked = window
            # open from now, though it shows on the next frame, so Jarvis can't fall asleep in between
            self.popup_open = window != "close"

    def _show_choices(self) -> None:
        """On the window's thread, which alone may make windows: show or close the window asked for."""
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
            self.picker = asked(self.window.geometry(), self._choices_closed)

    def _choices_closed(self, picker) -> None:
        """The window was closed: picked from, Esc, the X, or a click elsewhere."""
        with self._choices_lock:
            if picker is self.picker and self._choices_asked is None:   # not already replaced by another
                self.picker, self.popup_open = None, False

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


ROW_GAP = 2
TYPING_MS = 700      # pressing 1 and then 2 within this picks the twelfth
AROUND_ROWS = 70     # height of the title and the margins around a list's rows
RISING = "#5bd68a"   # a card's trend going up; going down is the error colour
# How a popup moves (times in ms, distances in pixels): it fades in as it rises into place over the orb,
# springing a little past it, and its rows (or a card's parts) follow one after another; closing, it
# fades out as it sinks
ARRIVE_MS, RISE = 420, 18
LEAVE_MS, SINK = 180, 6
PART_MS, PART_RISE = 300, 8
PART_DELAY_MS = 70   # the first row starts this long after the popup, each of the rest PART_GAP_MS after the last
PART_GAP_MS = 40
HOVER_MS = 150       # a row lighting up under the mouse or the keys
SCROLL_MS = 220      # a list gliding to where the wheel or the keys move it
CHART_MS = 900       # a card's chart drawing its line
SPRING = QEasingCurve(QEasingCurve.OutBack)


def _lines(text: str, font: QFont, width: float, lines: int) -> str:
    """text wrapped at words into at most this many lines of this width, cut short with '…' if longer."""
    metrics, rest, out = QFontMetricsF(font), text.split(), []
    while rest and len(out) < lines - 1:
        line = rest.pop(0)
        while rest and metrics.horizontalAdvance(f"{line} {rest[0]}") <= width:
            line += " " + rest.pop(0)
        out.append(line)
    out.append(" ".join(rest))
    return "\n".join(metrics.elidedText(line, Qt.ElideRight, width) for line in out if line)


def _holding(layout) -> QWidget:
    """A plain widget holding layout, so what's in it can come in on its own as a popup appears."""
    widget = QWidget()
    widget.setLayout(layout)
    return widget


class _Reveal(QGraphicsEffect):
    """Draws a widget only as far into view as shown says, 0 to 1: faded, and up to PART_RISE lower
    down, so taking shown from 0 to 1 fades it in as it rises into place."""

    def __init__(self, widget: QWidget):
        super().__init__(widget)
        self.shown = 0.0

    def set_shown(self, shown: float) -> None:
        if shown != self.shown:
            self.shown = shown
            self.update()

    def boundingRectFor(self, rect: QRectF) -> QRectF:
        return rect.adjusted(0, 0, 0, PART_RISE)   # room to draw it below its place

    def draw(self, p: QPainter) -> None:
        if self.shown <= 0:
            return
        # (drawSource would ignore the painter's opacity and offset: a widget paints itself straight
        # onto the window)
        offset = QPoint()   # where the picture of it goes, filled in by sourcePixmap
        pixmap = self.sourcePixmap(Qt.LogicalCoordinates, offset, QGraphicsEffect.NoPad)
        p.save()
        p.setOpacity(p.opacity() * self.shown)
        p.drawPixmap(offset + QPoint(0, round(PART_RISE * (1 - self.shown))), pixmap)
        p.restore()


class _Row(QPushButton):
    """A row of a list to click: it lights up smoothly as the mouse comes over it or the keys reach it,
    and a little more while it's pressed. What it says is in labels on top."""

    def __init__(self, accent: str, height: int):
        super().__init__(objectName="row", cursor=Qt.PointingHandCursor)
        self.setFixedHeight(height)
        self.accent = accent
        self.hovered = False
        self.glow = 0.0      # how lit up it is: the opacity of the accent colour behind it
        self.fade = QVariantAnimation(self, duration=HOVER_MS, easingCurve=QEasingCurve.OutCubic)
        self.fade.valueChanged.connect(self._set_glow)
        self.pressed.connect(self._light)
        self.released.connect(self._light)

    def _light(self) -> None:
        """Fade to how lit up it should be now."""
        glow = 0.28 if self.isDown() else 0.18 if self.hovered else 0.08 if self.hasFocus() else 0.0
        self.fade.stop()
        self.fade.setStartValue(self.glow)
        self.fade.setEndValue(glow)
        self.fade.start()

    def _set_glow(self, glow: float) -> None:
        self.glow = glow
        self.update()

    def enterEvent(self, event):
        self.hovered = True
        self._light()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.hovered = False
        self._light()
        super().leaveEvent(event)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self._light()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self._light()

    def paintEvent(self, event):
        if self.glow <= 0:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(_color(self.accent, self.glow))
        p.drawRoundedRect(QRectF(self.rect()), 8, 8)
        p.end()


class _Popup(QWidget):
    """A window floating above the orb like a dropdown: a panel with a title and an X, holding what
    each kind of popup puts in self.body. Esc, the X or clicking elsewhere closes it. Unlike the orb
    it takes clicks. It rises into place as it appears, and fades away as it closes."""
    WIDTH = 440

    def __init__(self, cfg: UiConfig, title: str, on_closed: Callable[["_Popup"], None]):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.on_closed = on_closed
        self.used = False    # the mouse has been over it or a key pressed in it
        self.leaving = False   # it's closing, fading away, and takes no more clicks or keys
        self.gone = False      # it has faded away: closing it now closes it for real
        self.place = QPoint()  # where it rises to
        self.parts = []        # what comes in after it, one after another
        self.arrival = QVariantAnimation(self, startValue=0.0)   # its value is the ms since it started appearing
        self.arrival.valueChanged.connect(self._arrive)
        self.arrival.finished.connect(self._arrived)
        self.departure = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=LEAVE_MS,
                                           easingCurve=QEasingCurve.OutCubic)   # going at once, as it's closed
        self.departure.valueChanged.connect(self._leave)
        self.departure.finished.connect(self._gone)
        self.setAttribute(Qt.WA_TranslucentBackground)    # the panel's rounded corners show what's behind
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.accent = accent = cfg.colors["listening"]
        muted = _color(cfg.heard_color, 0.45).name(QColor.HexArgb)
        # fonts are set on the labels rather than in the style sheet, so eliding long names measures
        # them in the font they're drawn in
        self.fonts = {"title": _font(cfg.font, 9), "name": _font(cfg.font, 10), "detail": _font(cfg.font, 9),
                      "site": _font(cfg.font, 9), "number": _font(cfg.font, 10), "big": _font(cfg.font, 26)}
        self.text_width = self.WIDTH - 100   # room for a row's text, next to its number and the scroll bar
        self.fonts["title"].setWeight(QFont.DemiBold)
        self.fonts["number"].setWeight(QFont.Bold)
        self.fonts["big"].setWeight(QFont.Light)
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
            QLabel#number, QLabel#site, QLabel#marked {{ color: {accent}; }}
            QPushButton {{ border: none; background: transparent; border-radius: 8px; text-align: left; }}
            QPushButton#close {{ color: {cfg.heard_color}; font-size: 11pt; padding: 2px 8px; }}
            QPushButton#close:hover, QPushButton#more:hover {{ color: {cfg.reply_color}; }}
            QPushButton#more {{ color: {accent}; padding: 6px 10px 2px 10px; }}
        """)
        panel = QWidget(self, objectName="panel")
        panel.setAttribute(Qt.WA_StyledBackground)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(panel)
        self.body = QVBoxLayout(panel)
        self.body.setContentsMargins(8, 6, 6, 8)
        self.body.setSpacing(2)

        head = QHBoxLayout()
        head.setContentsMargins(8, 2, 0, 2)
        title = QFontMetricsF(self.fonts["title"]).elidedText(title, Qt.ElideRight, self.WIDTH - 80)
        head.addWidget(QLabel(title, objectName="title", font=self.fonts["title"]))
        head.addStretch()
        close = QPushButton("✕", objectName="close", cursor=Qt.PointingHandCursor)
        close.clicked.connect(self.close)
        head.addWidget(close)
        self.body.addLayout(head)

    def _link(self, text: str, action: Callable[[], None]) -> QPushButton:
        """A link for the bottom, like 'See it on Google': clicking it closes this and runs action."""
        more = QPushButton(f"{text}  ↗", objectName="more", cursor=Qt.PointingHandCursor, font=self.fonts["detail"])
        more.setFocusPolicy(Qt.NoFocus)   # so Enter in a list always opens the row that has the focus
        more.clicked.connect(lambda _=False: self._then_close(action))
        return more

    def _then_close(self, action: Callable, *args) -> None:
        if self.leaving:     # a second click while it fades away
            return
        self.close()
        try:
            action(*args)
        except Exception:
            log.exception("Opening what was clicked failed")

    def _show_above(self, above, parts: list[QWidget]) -> None:
        """Show it centred over the orb, just above where the orb shows up, and take the keys. It rises
        into place, then the parts follow one after another."""
        screen_top = QGuiApplication.primaryScreen().availableGeometry().top()
        self.setFixedWidth(self.WIDTH)
        self.adjustSize()
        self.place = QPoint(above.center().x() - self.WIDTH // 2,
                            max(screen_top + 8, above.top() - self.height() - 8))
        self.parts = parts
        for part in parts:
            part.setGraphicsEffect(_Reveal(part))
        ms = max(ARRIVE_MS, PART_DELAY_MS + (len(parts) - 1) * PART_GAP_MS + PART_MS)
        self.arrival.setDuration(ms)
        self.arrival.setEndValue(float(ms))
        self._arrive(0.0)
        self.show()
        self.raise_()
        self.activateWindow()
        self.arrival.start()

    def _arrive(self, ms: float) -> None:
        """One frame of appearing, ms into it."""
        self.setWindowOpacity(ease_out(progress(ms, 0, ARRIVE_MS / 2)))
        below = 1 - SPRING.valueForProgress(progress(ms, 0, ARRIVE_MS))   # below 0 as it springs past
        self.move(self.place + QPoint(0, round(RISE * below)))
        for i, part in enumerate(self.parts):
            part.graphicsEffect().set_shown(ease_out(progress(ms, PART_DELAY_MS + i * PART_GAP_MS, PART_MS)))

    def _arrived(self) -> None:
        for part in self.parts:
            part.setGraphicsEffect(None)   # drawn straight onto the window again, as crisp as can be

    def _leave(self, t: float) -> None:
        """One frame of leaving, t from 0 to 1: it fades out from wherever it was as it sinks."""
        opacity, y = self.left_from
        self.setWindowOpacity(opacity * (1 - t))
        self.move(self.x(), round(y + SINK * t))

    def _gone(self) -> None:
        self.gone = True
        self.close()

    def _key(self, event) -> bool:
        """Esc closes it. False for any other key."""
        self.used = True
        if self.leaving:
            return True
        if event.key() == Qt.Key_Escape:
            self.close()
            return True
        return False

    def keyPressEvent(self, event):
        if not self._key(event):
            super().keyPressEvent(event)

    def enterEvent(self, event):
        self.used = True
        super().enterEvent(event)

    def closeEvent(self, event):
        """Closing fades it away first; it's closed for real once it's gone. It counts as closed at once."""
        if self.gone:
            super().closeEvent(event)
            return
        event.ignore()
        if not self.leaving:
            self.leaving = True
            self.on_closed(self)
            self.arrival.stop()
            self.left_from = (self.windowOpacity(), self.y())
            self.departure.start()

    def changeEvent(self, event):
        """Clicking anywhere else closes it, like a dropdown, once the user has been in it with the
        mouse or the keys. Not before: the app they're typing in may take the focus straight back
        as it appears, and it would vanish before they saw it."""
        if event.type() == QEvent.ActivationChange and not self.isActiveWindow() and self.used:
            self.close()
        super().changeEvent(event)


class _Picker(_Popup):
    """A list of files to click one of: each row is a number (what 'the second one' counts), the name
    and where it is. A long list scrolls, gliding, with the wheel or the arrow keys. Clicking a row,
    Enter, or typing its number picks it."""
    ROWS = 8             # rows shown at once; the rest scroll
    ROW_HEIGHT = 46

    def __init__(self, cfg: UiConfig, title: str, items: list[tuple[str, ...]],
                 on_pick: Callable[[int], None], above, on_closed: Callable[[_Popup], None]):
        super().__init__(cfg, title, on_closed)
        self.on_pick = on_pick
        self.typed = ""      # digits typed so far, for numbers above 9
        self.typing = QTimer(self, singleShot=True, interval=TYPING_MS)
        self.typing.timeout.connect(self._pick_typed)

        holder = QWidget(objectName="rows")
        rows = QVBoxLayout(holder)
        rows.setContentsMargins(0, 0, 2, 0)
        rows.setSpacing(ROW_GAP)
        self.rows = [self._row(i, item) for i, item in enumerate(items)]
        for row in self.rows:
            rows.addWidget(row)
        self.scroll = QScrollArea(widgetResizable=True, frameShape=QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setWidget(holder)
        self.body.addWidget(self.scroll)
        below = self._below_rows()
        if below:
            self.body.addWidget(below)

        # as many rows as fit between the top of the screen and the orb, up to ROWS
        screen_top = QGuiApplication.primaryScreen().availableGeometry().top()
        around = AROUND_ROWS + (below.sizeHint().height() if below else 0)
        room = (above.top() - screen_top - around) // (self.ROW_HEIGHT + ROW_GAP)
        self.shown = max(1, min(self.ROWS, len(items), room))
        self.scroll.setFixedHeight(self.shown * (self.ROW_HEIGHT + ROW_GAP) - ROW_GAP)
        bar = self.scroll.verticalScrollBar()
        bar.setSingleStep(self.ROW_HEIGHT + ROW_GAP)   # the wheel moves whole rows
        self.glide = QPropertyAnimation(bar, b"value", self, duration=SCROLL_MS, easingCurve=QEasingCurve.OutCubic)
        self.scroll.viewport().installEventFilter(self)   # for the wheel
        self._show_above(above, self.rows[:self.shown] + ([below] if below else []))
        if self.rows:
            self.rows[0].setFocus()

    def _row(self, i: int, item: tuple[str, ...]) -> QPushButton:
        row = _Row(self.accent, self.ROW_HEIGHT)
        line = QHBoxLayout(row)
        line.setContentsMargins(10, 5, 10, 5)
        line.setSpacing(10)
        number = QLabel(str(i + 1), objectName="number", font=self.fonts["number"])
        number.setFixedWidth(20)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        texts.addStretch()   # the lines together, in the middle of the row
        for label in self._labels(*item):
            texts.addWidget(label)
        texts.addStretch()
        line.addWidget(number)
        line.addLayout(texts, 1)
        for label in row.findChildren(QLabel):   # clicks go to the row underneath
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
        row.clicked.connect(lambda _=False: self._pick(i))
        # the rows get the keys first, and the scroll area would take the arrows to scroll
        row.installEventFilter(self)
        return row

    def _labels(self, name: str, detail: str) -> list[QLabel]:
        """A file's lines: its name, then where it is and when it changed."""
        return [self._label(name, "name", Qt.ElideMiddle), self._label(detail, "detail", Qt.ElideMiddle)]

    def _label(self, text: str, kind: str, cut: Qt.TextElideMode) -> QLabel:
        """One line of a row, in the font and colour for its kind, cut short to fit."""
        label = QLabel(QFontMetricsF(self.fonts[kind]).elidedText(text, cut, self.text_width),
                       objectName=kind, font=self.fonts[kind])
        label.setToolTip(text)
        return label

    def _below_rows(self) -> QWidget | None:
        """Anything under the rows, like a link to more."""
        return None

    def _pick(self, i: int) -> None:
        self._then_close(self.on_pick, i)

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
        if super()._key(event):
            return True
        key, rows = event.key(), self.rows
        current = rows.index(self.focusWidget()) if self.focusWidget() in rows else 0
        moves = {Qt.Key_Down: 1, Qt.Key_Up: -1, Qt.Key_PageDown: self.ROWS, Qt.Key_PageUp: -self.ROWS,
                 Qt.Key_Home: -len(rows), Qt.Key_End: len(rows)}
        if key in moves and rows:
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
        step = self.ROW_HEIGHT + ROW_GAP
        top = min(i, max(self._scrolled() // step, i - self.shown + 1))
        self._glide_to(top * step)

    def _scrolled(self) -> int:
        """How far down the list is scrolled, or will be once it has glided there."""
        if self.glide.state() == QAbstractAnimation.Running:
            return self.glide.endValue()
        return self.scroll.verticalScrollBar().value()

    def _glide_to(self, value: int) -> None:
        bar = self.scroll.verticalScrollBar()
        value = min(max(value, bar.minimum()), bar.maximum())
        if value == self._scrolled():
            return
        self.glide.stop()
        self.glide.setStartValue(bar.value())
        self.glide.setEndValue(value)
        self.glide.start()

    def eventFilter(self, watched, event):
        """The rows' keys and focus, and the wheel over the list."""
        if event.type() == QEvent.FocusIn and watched in self.rows:   # a row reached with Tab scrolls into view
            self._scroll_to(self.rows.index(watched))
        elif event.type() == QEvent.KeyPress and self._key(event):
            return True
        # a wheel's notches would jump; a touchpad scrolls smoothly by itself
        elif event.type() == QEvent.Wheel and watched is self.scroll.viewport() and event.pixelDelta().isNull():
            notches = event.angleDelta().y() / 120
            lines = QApplication.wheelScrollLines() * self.scroll.verticalScrollBar().singleStep()
            self._glide_to(self._scrolled() - round(notches * lines))
            event.accept()   # or it goes on to the scroll area, which would jump there as well
            return True
        return super().eventFilter(watched, event)


class _WebResults(_Picker):
    """A web search's results, like a search page: each row is a page's title, its site and the start
    of what it says. Clicking one (or Enter, or its number) opens it in the browser, and the link below
    them opens the whole search. Otherwise it works like the list of files."""
    WIDTH = 540
    ROWS = 5
    ROW_HEIGHT = 86

    def __init__(self, cfg: UiConfig, title: str, results: list[tuple[str, str, str]],
                 on_open: Callable[[int], None], on_more: Callable[[], None], above,
                 on_closed: Callable[[_Popup], None]):
        self.on_more = on_more   # before the link below the rows is made
        super().__init__(cfg, title, results, on_open, above, on_closed)

    def _labels(self, title: str, site: str, snippet: str) -> list[QLabel]:
        text = QLabel(_lines(snippet, self.fonts["detail"], self.text_width, 2), objectName="detail",
                      font=self.fonts["detail"])
        text.setContentsMargins(0, 2, 0, 0)
        text.setToolTip(snippet)
        return [self._label(title, "name", Qt.ElideRight), self._label(site, "site", Qt.ElideMiddle), text]

    def _below_rows(self) -> QWidget:
        return self._link("See all the results on Google", self.on_more)


class _Card(_Popup):
    """Facts like the weather or a stock price (a Card): a big figure with notes beside it, then a line
    chart, a table and a link to more. There's nothing to pick: it's there to read."""
    WIDTH = 400

    def __init__(self, cfg: UiConfig, card: Card, above, on_closed: Callable[[_Popup], None]):
        super().__init__(cfg, card.title, on_closed)
        trend = {1: RISING, -1: cfg.colors["error"]}.get(card.trend)
        top = QHBoxLayout()
        top.setContentsMargins(10, 0, 10, 6)
        top.setSpacing(16)
        notes = QVBoxLayout()
        notes.setSpacing(1)
        notes.addStretch()   # the notes together, level with the middle of the big figure
        widest = 0
        for i, note in enumerate(card.notes):
            label = QLabel(note, objectName="detail" if i else "", font=self.fonts["detail" if i else "name"])
            if i == 0 and trend:
                label.setStyleSheet(f"color: {trend};")
            notes.addWidget(label)
            widest = max(widest, QFontMetricsF(label.font()).horizontalAdvance(note))
        # a long figure, like Bitcoin's price, gets smaller until it fits beside the notes
        big, room = QFont(self.fonts["big"]), self.WIDTH - 60 - widest
        while big.pointSizeF() > 14 and QFontMetricsF(big).horizontalAdvance(card.big) > room:
            big.setPointSizeF(big.pointSizeF() - 1)
        top.addWidget(QLabel(card.big, font=big))
        notes.addStretch()
        top.addLayout(notes, 1)
        parts = [_holding(top)]
        if len(card.chart) > 1:
            parts.append(_Chart(card.chart, card.baseline, trend or self.accent, cfg.heard_color))
        if card.rows:
            parts.append(_holding(self._table(card.rows, card.marked)))
        if card.link and card.on_link:
            parts.append(self._link(card.link, card.on_link))
        for part in parts:
            self.body.addWidget(part)
        self._show_above(above, parts)

    def _table(self, rows: tuple[tuple[str, ...], ...], marked: tuple[int, ...]) -> QGridLayout:
        """The rows in columns: the first, a label, dimmer; the second takes the room left over; the
        last and any after the second, numbers like '51% rain', aligned right. Marked rows are in the
        accent colour."""
        grid = QGridLayout()
        grid.setContentsMargins(10, 6, 10, 2)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(5)
        for r, row in enumerate(rows):
            for c, text in enumerate(row):
                kind = "marked" if r in marked else "detail" if c == 0 else ""
                right = c >= 2 or (c == 1 and len(row) == 2)
                grid.addWidget(QLabel(text, objectName=kind, font=self.fonts["name"]), r, c,
                               Qt.AlignRight if right else Qt.AlignLeft)
        grid.setColumnStretch(1, 1)
        return grid


class _Chart(QWidget):
    """A line through points, like a day's prices, with the area under it shaded, over a dotted line
    at the baseline, like yesterday's close. As the card appears the line draws itself from the left,
    with a dot at its tip that ends up on the last point, like the latest price."""

    def __init__(self, points: tuple[tuple[float, float], ...], baseline: float | None, color: str, muted: str):
        super().__init__()
        self.points, self.baseline, self.color, self.muted = points, baseline, color, muted
        self.setFixedHeight(76)
        self.drawn = 0.0     # how much of the line there is so far, 0 to 1
        self.drawing = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=CHART_MS,
                                         easingCurve=QEasingCurve.InOutCubic)
        self.drawing.valueChanged.connect(self._draw_to)
        self.drawing.start()   # the card shows straight after it's made

    def _draw_to(self, drawn: float) -> None:
        self.drawn = drawn
        self.update()

    def paintEvent(self, event):
        values = [v for _, v in self.points] + ([self.baseline] if self.baseline is not None else [])
        low, high = min(values), max(values)
        box = QRectF(self.rect()).adjusted(12, 6, -12, -6)

        def at(x: float, value: float) -> QPointF:
            up = (value - low) / (high - low) if high > low else 0.5
            return QPointF(box.left() + box.width() * x, box.bottom() - box.height() * up)

        line = [at(x, v) for x, v in self.points]
        # the line so far: up to its tip, which is between the points either side of it
        tip_x = line[0].x() + (line[-1].x() - line[0].x()) * self.drawn
        after = next((i for i, point in enumerate(line) if point.x() >= tip_x), len(line) - 1)
        before = line[max(after - 1, 0)]
        across = line[after].x() - before.x()
        tip = before + (line[after] - before) * ((tip_x - before.x()) / across if across > 0 else 1.0)
        line = line[:after] + [tip]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.baseline is not None:
            y = at(0, self.baseline).y()
            p.setPen(QPen(_color(self.muted, 0.6), 1, Qt.DotLine))
            p.drawLine(QPointF(box.left(), y), QPointF(box.right(), y))
        shade = QLinearGradient(0, box.top(), 0, box.bottom())
        shade.setColorAt(0, _color(self.color, 0.28))
        shade.setColorAt(1, _color(self.color, 0.0))
        p.setPen(Qt.NoPen)
        p.setBrush(shade)
        p.drawPolygon(QPolygonF(line + [QPointF(tip.x(), box.bottom()), QPointF(line[0].x(), box.bottom())]))
        p.setPen(QPen(_color(self.color), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        p.setBrush(Qt.NoBrush)
        p.drawPolyline(QPolygonF(line))
        p.setPen(Qt.NoPen)
        p.setBrush(_color(self.color, 0.25))
        p.drawEllipse(tip, 6, 6)
        p.setBrush(_color(self.color))
        p.drawEllipse(tip, 3, 3)
        p.end()


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
