"""An event-driven two-axis editor for the existing voice EQ trims."""
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget


class TonePad(QWidget):
    values_changed = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._values = (0, 0)
        self.setMinimumWidth(280)
        self.setFixedHeight(140)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setAccessibleName('Tone pad')
        self.setAccessibleDescription(
            'Left and right adjust warmth. Up and down adjust clarity. '
            'Use arrow keys for one dB steps. Home or double-click resets both to zero.')
        self.setToolTip(
            'Drag right for fuller lows; left for a leaner voice.\n'
            'Drag up for clearer presence; down for a softer tone.\n'
            'Arrow keys: 1 dB steps. Home or double-click: reset tone.')

    @property
    def values(self):
        return self._values

    def set_values(self, warmth, clarity):
        values = tuple(max(-6, min(6, int(value))) for value in (warmth, clarity))
        if values != self._values:
            self._values = values
            self.update()

    def _plot_rect(self):
        return QRectF(58, 25, max(1, self.width()-116), self.height()-50)

    def _edit_values(self, warmth, clarity):
        previous = self._values
        self.set_values(warmth, clarity)
        if self._values != previous:
            self.values_changed.emit(*self._values)

    def _edit_position(self, position):
        plot = self._plot_rect()
        x = max(0, min(1, (position.x()-plot.left())/plot.width()))
        y = max(0, min(1, (position.y()-plot.top())/plot.height()))
        self._edit_values(round(12*x-6), round(6-12*y))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self._edit_position(event.position())
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._edit_position(event.position())
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._edit_values(0, 0)
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        warmth, clarity = self._values
        changes = {
            Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0),
            Qt.Key.Key_Up: (0, 1), Qt.Key.Key_Down: (0, -1),
        }
        if event.key() == Qt.Key.Key_Home:
            self._edit_values(0, 0)
        elif event.key() in changes:
            dx, dy = changes[event.key()]
            self._edit_values(warmth+dx, clarity+dy)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.4)
        plot = self._plot_rect()
        color = QLinearGradient(plot.topLeft(), plot.topRight())
        color.setColorAt(0, QColor('#26303e'))
        color.setColorAt(1, QColor('#3b3540'))
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(plot, 4, 4)
        presence = QLinearGradient(plot.topLeft(), plot.bottomLeft())
        presence.setColorAt(0, QColor(110, 151, 211, 35))
        presence.setColorAt(1, QColor(25, 27, 32, 60))
        painter.setBrush(QBrush(presence))
        painter.drawRoundedRect(plot, 4, 4)
        painter.setPen(QPen(QColor('#566173'), 1, Qt.PenStyle.DotLine))
        painter.drawLine(QPointF(plot.center().x(), plot.top()),
                         QPointF(plot.center().x(), plot.bottom()))
        painter.drawLine(QPointF(plot.left(), plot.center().y()),
                         QPointF(plot.right(), plot.center().y()))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor('#a9c6f4' if self.hasFocus() else '#495365'), 1))
        painter.drawRoundedRect(plot, 4, 4)
        painter.drawEllipse(plot.center(), 3, 3)

        font = self.font()
        font.setPointSize(8)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor('#a9b2c2'))
        align = Qt.AlignmentFlag.AlignCenter
        painter.drawText(QRectF(plot.left(), 0, plot.width(), 23), align, 'CRISPER')
        painter.drawText(QRectF(plot.left(), self.height()-23, plot.width(), 23), align, 'SOFTER')
        painter.drawText(QRectF(0, plot.center().y()-12, 53, 24), align, 'LEANER')
        painter.drawText(QRectF(plot.right()+5, plot.center().y()-12, 53, 24), align, 'FULLER')

        warmth, clarity = self._values
        point = QPointF(plot.left()+(warmth+6)*plot.width()/12,
                        plot.top()+(6-clarity)*plot.height()/12)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(130, 169, 224, 40))
        painter.drawEllipse(point, 15, 15)
        painter.setPen(QPen(QColor('#22252b'), 2))
        painter.setBrush(QColor('#c4d7f4'))
        painter.drawEllipse(point, 8, 8)
