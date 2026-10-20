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

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QGuiApplication, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QApplication, QWidget

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
    def __init__(self, cfg: UiConfig):
        super().__init__(cfg)
        self.app = QApplication.instance() or QApplication([])
        self.window = _Window(self)
        self.drawn = "starting"              # the state of the frame the window paints
        self.fonts = (_font(cfg.font, cfg.heard_font_size), _font(cfg.font, cfg.reply_font_size))
        self._layout_key, self._layout = None, None
        self.bubble = Bubble()

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
