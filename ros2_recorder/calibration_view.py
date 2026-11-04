"""Native, bounded 3-D point view for manual calibration (no external window)."""
from __future__ import annotations

import math

import numpy as np
from PyQt5.QtCore import QPointF, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QImage, QPainter, QPen
from PyQt5.QtWidgets import QWidget


COLORS = {"reference": QColor("#e9eef4"), "input": QColor("#ff6569")}
NAMES = {"reference": "基准点云", "input": "待标定点云"}


class CalibrationView(QWidget):
    pointPicked = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("calibrationView")
        self.setMinimumSize(320, 260)
        self.setFocusPolicy(Qt.StrongFocus)
        self.clouds = {}
        self.matrices = {role: np.eye(4) for role in COLORS}
        self.visible_layers = {role: True for role in COLORS}
        self.center = np.zeros(3)
        self.yaw, self.pitch, self.distance = -.9, .65, 35.
        self.point_size = 2
        self._press = self._previous = None
        self._dragged = False
        self._auto_fit = True
        self.selection = None

    def set_cloud(self, role, cloud):
        if cloud is None:
            self.clouds.pop(role, None)
        else:
            self.clouds[role] = cloud
        if self.selection and self.selection["role"] == role:
            self.selection = None
            self.pointPicked.emit(None)
        if self._auto_fit:
            self.fit_clouds(manual=False)
        self.update()

    def set_matrix(self, role, matrix):
        self.matrices[role] = np.asarray(matrix, dtype=float).copy()
        if self.selection and self.selection["role"] == role:
            self._emit_selection(role, self.selection["sample_index"])
        self.update()

    def transformed_points(self, role):
        matrix = self.matrices[role]
        # Avoid BLAS thread scheduling for these small, frequent Nx3 operations.
        return np.einsum("ij,kj->ik", self.clouds[role]["xyz"], matrix[:3, :3]) + matrix[:3, 3]

    def fit_clouds(self, *, manual=True):
        arrays = [self.transformed_points(role) for role in self.clouds
                  if self.visible_layers[role] and len(self.clouds[role]["xyz"])]
        if arrays:
            values = np.concatenate(arrays)
            lower, upper = np.percentile(values, [1, 99], axis=0)
            self.center = (lower + upper) / 2
            self.distance = max(.5, float(np.linalg.norm(upper - lower)) * 1.4)
        if manual:
            self._auto_fit = False
        self.update()

    def reset_camera(self):
        self.yaw, self.pitch = -.9, .65
        self.fit_clouds()

    def _basis(self):
        cy, sy, cp, sp = math.cos(self.yaw), math.sin(self.yaw), math.cos(self.pitch), math.sin(self.pitch)
        forward = np.array([-cp*cy, -cp*sy, -sp])
        right = np.array([-sy, cy, 0.])
        up = np.cross(right, forward)
        return right, up, forward

    def _project(self, points):
        right, up, forward = self._basis()
        relative = np.asarray(points) - self.center
        depth = self.distance + np.einsum("ij,j->i", relative, forward)
        focal = max(1., min(self.width(), self.height()) * .95)
        scale = focal / np.maximum(depth, .001)
        xy = np.column_stack([self.width()/2 + np.einsum("ij,j->i", relative, right)*scale,
                              self.height()/2 - np.einsum("ij,j->i", relative, up)*scale])
        return xy, depth

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#202a35"))
        self._draw_grid(painter)
        pixels = np.zeros((self.height(), self.width()), dtype=np.uint32)
        for role in ("reference", "input"):
            if not self.visible_layers[role] or role not in self.clouds:
                continue
            points = self.transformed_points(role)
            xy, depth = self._project(points)
            mask = (depth > .01) & np.isfinite(xy).all(axis=1) & (xy[:, 0] >= 0) & (xy[:, 0] <= self.width()) & (xy[:, 1] >= 0) & (xy[:, 1] <= self.height())
            indexes = np.flatnonzero(mask)
            visible = xy[indexes]
            # Rasterize the small point squares in batches. Constructing and
            # stroking tens of thousands of QPointF objects stalls mouse input.
            coordinates = visible.astype(np.int32)
            for dx in range(self.point_size):
                for dy in range(self.point_size):
                    x = coordinates[:, 0] + dx - self.point_size // 2
                    y = coordinates[:, 1] + dy - self.point_size // 2
                    inside = (x >= 0) & (x < self.width()) & (y >= 0) & (y < self.height())
                    pixels[y[inside], x[inside]] = COLORS[role].rgba()
        image = QImage(pixels.data, self.width(), self.height(), pixels.strides[0], QImage.Format_ARGB32)
        painter.drawImage(0, 0, image)
        if self.selection and self.selection["role"] in self.clouds:
            role, index = self.selection["role"], self.selection["sample_index"]
            point = self.transformed_points(role)[index:index+1]
            screen, depth = self._project(point)
            if depth[0] > .01:
                painter.setPen(QPen(QColor("#ffc35a"), 2))
                painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(QPointF(*screen[0]), 7, 7)
        painter.setPen(QColor("#b8c5d4"))
        painter.drawText(14, 24, "白：基准点云    红：待标定点云")
        painter.drawText(14, self.height()-14, "左键旋转 · 中/右键平移 · 滚轮缩放 · 单击选点 · 双击适配")
        if not any(len(value["xyz"]) for value in self.clouds.values()):
            painter.setPen(QColor("#c4cfdb"))
            painter.drawText(self.rect().adjusted(24, 40, -24, -40), Qt.AlignCenter | Qt.TextWordWrap,
                             "等待点云\n选中点云话题后自动订阅\n可在外部播放 bag，或直接连接传感器驱动")
        painter.end()

    def _draw_grid(self, painter):
        spacing = 10 ** math.floor(math.log10(max(.01, self.distance / 12)))
        extent = spacing * 15
        origin = np.round(self.center[:2] / spacing) * spacing
        segments = []
        for value in range(-15, 16):
            offset = value * spacing
            segments.extend([[[origin[0]+offset, origin[1]-extent, 0], [origin[0]+offset, origin[1]+extent, 0]],
                             [[origin[0]-extent, origin[1]+offset, 0], [origin[0]+extent, origin[1]+offset, 0]]])
        painter.setPen(QPen(QColor("#344250"), 1))
        for segment in segments:
            screen, depth = self._project(segment)
            if (depth > .01).all():
                painter.drawLine(QPointF(*screen[0]), QPointF(*screen[1]))
        for end, color, name in (([spacing*3, 0, 0], "#ee7373", "X"), ([0, spacing*3, 0], "#79d199", "Y"), ([0, 0, spacing*3], "#75b5ff", "Z")):
            screen, depth = self._project([[0, 0, 0], end])
            if (depth > .01).all():
                painter.setPen(QPen(QColor(color), 2))
                painter.drawLine(QPointF(*screen[0]), QPointF(*screen[1]))
                painter.drawText(QPointF(*screen[1]), name)

    def mousePressEvent(self, event):
        if event.button() in (Qt.LeftButton, Qt.MiddleButton, Qt.RightButton):
            self._press = self._previous = event.pos()
            self._dragged = False
            self._auto_fit = False
            event.accept()

    def mouseMoveEvent(self, event):
        if self._previous is None:
            return
        delta = event.pos() - self._previous
        self._previous = event.pos()
        self._dragged = self._dragged or (event.pos() - self._press).manhattanLength() > 4
        if event.buttons() & Qt.LeftButton:
            self.yaw -= delta.x() * .007
            self.pitch = max(-1.55, min(1.55, self.pitch + delta.y() * .007))
        elif event.buttons() & (Qt.MiddleButton | Qt.RightButton):
            right, up, _ = self._basis()
            scale = self.distance / max(1., min(self.width(), self.height()) * .95)
            self.center += (-right * delta.x() + up * delta.y()) * scale
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._press is not None and not self._dragged:
            self.pick_point(event.pos())
        self._press = self._previous = None

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.fit_clouds()

    def wheelEvent(self, event):
        self.distance = max(.03, min(1e7, self.distance * 1.15 ** (-event.angleDelta().y()/120.)))
        self._auto_fit = False
        self.update()
        event.accept()

    def pick_point(self, position):
        # Recompute from current transforms/camera, rather than stale paint data.
        best = None
        for role in self.clouds:
            if not self.visible_layers[role]:
                continue
            xy, depth = self._project(self.transformed_points(role))
            distance = np.sum((xy - [position.x(), position.y()]) ** 2, axis=1)
            candidates = np.flatnonzero((distance <= 64) & (depth > .01))
            if len(candidates):
                index = min(candidates, key=lambda i: (distance[i], depth[i]))
                score = (distance[index], depth[index])
                if best is None or score < best[0]:
                    best = (score, role, int(index))
        if best is None:
            self.selection = None
            self.pointPicked.emit(None)
        else:
            self._emit_selection(best[1], best[2])
        self.update()

    def _emit_selection(self, role, index):
        cloud = self.clouds[role]
        record = cloud["fields"][index]
        attributes = {name: record[name].tolist() for name in record.dtype.names}
        self.selection = {"role": role, "sample_index": index, "point_index": int(cloud["indexes"][index]),
                          "raw_xyz": cloud["xyz"][index].tolist(), "xyz": self.transformed_points(role)[index].tolist(),
                          "attributes": attributes, "frame": cloud["frame"], "stamp_ns": cloud["stamp_ns"]}
        self.pointPicked.emit(self.selection)
