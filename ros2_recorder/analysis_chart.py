"""One metric at a time, shared header-time axis and direct plot interaction."""
from __future__ import annotations

from PyQt5.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import QWidget

COLORS = ['#177c72', '#d48036', '#5573c6', '#b55283', '#7a963d', '#418faa']
METRICS = {
    'frequency': ('频率', 'Hz', 'frequency_hz', '由 header.stamp 计算，显示各时间窗口的平均频率。'),
    'count': ('消息数', '条', 'count', '上方显示消息总数；曲线显示每个 header 时间窗口内的消息数。'),
    'maximum': ('最长间隔', 'ms', 'max_interval_ms', '相邻两条有效 header 时间的差值；曲线显示每个窗口内的最大值。'),
    'minimum': ('最小间隔', 'ms', 'min_interval_ms', '相邻两条有效 header 时间的差值；零值表示重复，负值表示时间倒退。'),
    'timestamps': ('时间戳分布', '', '', '每一行一个话题，落点来自 header.stamp；空白处表示没有对应的消息时间戳。'),
}


class MetricChart(QWidget):
    rangeChanged = pyqtSignal()
    cursorChanged = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(250, 230)
        self.setMouseTracking(True)
        self.topics = []
        self.metric = 'frequency'
        self.origin_ns = 0
        self.view = (0, 1)
        self.stamps = {}
        self.loading = False
        self.cursor = None
        self.fixed_cursor = False
        self.drag_start = None
        self.moved = False
        self.setToolTip('滚轮缩放 · 拖动平移 · 双击恢复全部范围 · 单击固定 / 解除游标')

    def bounds(self):
        valid = [t for t in self.topics if t['first_timestamp_ns'] is not None]
        if not valid:
            return 0, 1
        low = min((t['first_timestamp_ns'] - self.origin_ns) / 1e9 for t in valid)
        high = max((t['last_timestamp_ns'] - self.origin_ns) / 1e9 for t in valid)
        # Include whole buckets for count and interval plots.
        if self.metric != 'timestamps':
            low = min([low] + [t['series'][0]['seconds'] for t in valid if t['series']])
            high = max([high] + [t['series'][-1]['seconds'] + t['bucket_seconds'] for t in valid if t['series']])
        return low, max(low + .001, high)

    def set_topics(self, topics, origin_ns):
        self.topics = topics
        self.origin_ns = origin_ns or 0
        self.stamps = {}
        self.reset_view()

    def set_metric(self, metric):
        self.metric = metric
        self.rangeChanged.emit()
        self.update()

    def reset_view(self):
        self.view = self.bounds()
        self.cursor = None
        self.fixed_cursor = False
        self.rangeChanged.emit()
        self.cursorChanged.emit('')
        self.update()

    def area(self):
        legends = min(4, len(self.topics))
        top = 17 + 21 * legends + (21 if len(self.topics) > 4 else 0)
        left = 115 if self.metric == 'timestamps' else 66
        return QRectF(left, top + 20, max(1, self.width() - left - 23), max(1, self.height() - top - 75))

    def x(self, seconds):
        area = self.area()
        low, high = self.view
        return area.left() + (seconds - low) / (high - low) * area.width()

    def time_at(self, x):
        low, high = self.view
        return low + (x - self.area().left()) / self.area().width() * (high - low)

    def wheelEvent(self, event):
        if not self.topics:
            return
        low, high = self.view
        full_low, full_high = self.bounds()
        anchor = min(high, max(low, self.time_at(event.pos().x())))
        fraction = (anchor - low) / (high - low)
        width = min(full_high - full_low, max(.000001, (high - low) * (.75 if event.angleDelta().y() > 0 else 4 / 3)))
        start = max(full_low, min(full_high - width, anchor - width * fraction))
        self.view = (start, start + width)
        self.rangeChanged.emit()
        self.update()
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.area().contains(QPointF(event.pos())):
            self.drag_start = (event.pos().x(), self.view)
            self.moved = False

    def mouseMoveEvent(self, event):
        if self.drag_start:
            x, (low, high) = self.drag_start
            if abs(event.pos().x() - x) > 3:
                self.moved = True
                full_low, full_high = self.bounds()
                start = low - (event.pos().x() - x) / self.area().width() * (high - low)
                start = max(full_low, min(full_high - (high - low), start))
                self.view = (start, start + high - low)
                self.update()
            return
        if not self.fixed_cursor and self.area().contains(QPointF(event.pos())):
            self.cursor = self.time_at(event.pos().x())
            self.describe_cursor()
            self.update()

    def mouseReleaseEvent(self, event):
        if self.drag_start:
            if self.moved:
                self.rangeChanged.emit()
            else:
                self.fixed_cursor = not self.fixed_cursor
                self.cursor = self.time_at(event.pos().x())
                self.describe_cursor()
            self.drag_start = None
            self.update()

    def mouseDoubleClickEvent(self, event):
        self.drag_start = None
        self.reset_view()

    def describe_cursor(self):
        if self.cursor is None:
            return
        text = [f'相对时间 {self.cursor:.6f} s']
        field = METRICS[self.metric][2]
        for topic in self.topics[:3]:
            if self.metric == 'timestamps':
                points = self.stamps.get(topic['name'], {}).get('points', [])
                if not points:
                    continue
                target = self.origin_ns + round(self.cursor * 1e9)
                point = min(points, key=lambda p: min(abs(p['first_ns'] - target), abs(p['last_ns'] - target)))
                stamp = min((point['first_ns'], point['last_ns']), key=lambda n: abs(n - target))
                text.append(f"{topic['name']}: {stamp // 10**9}.{stamp % 10**9:09d}" +
                            (f"（该显示窗口 {point['count']} 条）" if point['count'] > 1 else ''))
            else:
                point = next((p for p in topic['series'] if p['seconds'] <= self.cursor < p['seconds'] + topic['bucket_seconds']), None)
                value = point.get(field) if point else None
                if value is not None:
                    text.append(f"{topic['name']}: {value:.3f} {METRICS[self.metric][1]}（窗口 {topic['bucket_seconds']:g} s）")
        self.cursorChanged.emit('   |   '.join(text))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor('white'))
        if not self.topics:
            painter.setPen(QColor('#82909e'))
            painter.drawText(self.rect(), Qt.AlignCenter, '勾选左侧话题，再点击“读取所选”')
            return
        area = self.area()
        for i, topic in enumerate(self.topics[:4]):
            color = QColor(COLORS[i % len(COLORS)])
            y = 23 + i * 21
            painter.setPen(QPen(color, 3))
            painter.drawLine(QPointF(area.left(), y), QPointF(area.left() + 18, y))
            painter.setPen(QColor('#344054'))
            name = painter.fontMetrics().elidedText(topic['name'], Qt.ElideMiddle, max(20, int(area.width() - 35)))
            painter.drawText(QPointF(area.left() + 27, y + 4), name)
        if len(self.topics) > 4:
            painter.drawText(QPointF(area.left(), 23 + 4 * 21), f'另有 {len(self.topics) - 4} 个话题')
        low, high = self.view
        for i in range(6):
            x = area.left() + area.width() * i / 5
            painter.setPen(QPen(QColor('#e9edf1'), 1))
            painter.drawLine(QPointF(x, area.top()), QPointF(x, area.bottom()))
            painter.setPen(QColor('#657286'))
            painter.drawText(QRectF(x - 42, area.bottom() + 6, 84, 20), Qt.AlignCenter, f'{low + (high - low) * i / 5:.3f}')
        painter.drawText(QRectF(0, self.height() - 24, self.width() - 20, 20), Qt.AlignRight, 'header 相对时间 / s')
        if self.metric == 'timestamps':
            self.paint_timestamps(painter, area)
        else:
            self.paint_values(painter, area)
        if self.cursor is not None and low <= self.cursor <= high:
            painter.setPen(QPen(QColor('#cb4259'), 1))
            painter.drawLine(QPointF(self.x(self.cursor), area.top()), QPointF(self.x(self.cursor), area.bottom()))

    def paint_timestamps(self, painter, area):
        rows = max(1, len(self.topics))
        for i, topic in enumerate(self.topics):
            y = area.top() + (i + .5) / rows * area.height()
            painter.setPen(QPen(QColor('#e5eaef'), 1))
            painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
            name = topic['name'].rstrip('/').rsplit('/', 1)[-1]
            painter.setPen(QColor('#657286'))
            painter.drawText(QRectF(2, y - 11, area.left() - 10, 22), Qt.AlignRight | Qt.AlignVCenter,
                             painter.fontMetrics().elidedText(name, Qt.ElideRight, int(area.left() - 12)))
            height = min(35, area.height() / rows * .6)
            painter.save()
            painter.setClipRect(area)
            painter.setPen(QPen(QColor(COLORS[i % len(COLORS)]), 1.4))
            for point in self.stamps.get(topic['name'], {}).get('points', []):
                for stamp in set((point['first_ns'], point['last_ns'])):
                    x = self.x((stamp - self.origin_ns) / 1e9)
                    painter.drawLine(QPointF(x, y - height / 2), QPointF(x, y + height / 2))
            painter.restore()
        if self.loading:
            painter.setPen(QColor('#657286'))
            painter.drawText(area, Qt.AlignCenter, '正在读取此时间段的 header 时间戳…')
        elif not any(data['points'] for data in self.stamps.values()):
            painter.drawText(area, Qt.AlignCenter, '此时间段没有有效的 header.stamp')

    def paint_values(self, painter, area):
        field = METRICS[self.metric][2]
        low, high = self.view
        visible = [[p for p in topic['series'] if p['seconds'] + topic['bucket_seconds'] >= low and p['seconds'] <= high]
                   for topic in self.topics]
        values = [p[field] for points in visible for p in points if p.get(field) is not None]
        minimum, maximum = min([0] + values), max([0] + values)
        padding = max((maximum - minimum) * .08, 1e-6)
        if minimum < 0:
            minimum -= padding
        maximum += padding
        for i in range(5):
            y = area.bottom() - area.height() * i / 4
            painter.setPen(QPen(QColor('#e5eaef'), 1))
            painter.drawLine(QPointF(area.left(), y), QPointF(area.right(), y))
            painter.setPen(QColor('#657286'))
            value = minimum + (maximum - minimum) * i / 4
            text = f'{value:.2f}' if abs(value) >= .01 or value == 0 else f'{value:.1e}'
            painter.drawText(QRectF(0, y - 10, area.left() - 9, 20), Qt.AlignRight | Qt.AlignVCenter, text)
        painter.drawText(QPointF(12, area.top() - 10), METRICS[self.metric][1])
        painter.save()
        painter.setClipRect(area.adjusted(-2, -2, 2, 2))
        for i, (topic, points) in enumerate(zip(self.topics, visible)):
            path = QPainterPath()
            started = False
            painter.setPen(QPen(QColor(COLORS[i % len(COLORS)]), 1.8))
            for point in points:
                value = point.get(field)
                if value is None:
                    started = False
                    continue
                x = self.x(point['seconds'])
                right = self.x(point['seconds'] + topic['bucket_seconds'])
                y = area.bottom() - (value - minimum) / (maximum - minimum) * area.height()
                if started:
                    path.lineTo(x, y)
                else:
                    path.moveTo(x, y)
                path.lineTo(right, y)
                started = True
            painter.drawPath(path)
        painter.restore()
        if not values:
            painter.setPen(QColor('#82909e'))
            painter.drawText(area, Qt.AlignCenter, '所选话题没有此指标的数据；检查 header 或消息类型')
