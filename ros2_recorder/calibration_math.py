"""Manual extrinsics compatible with lidar_calibration_panel's INI convention.

The original tool uses Rz(yaw)*Ry(pitch)*Rx(roll), delta*base, meters in
the UI and centimeters in INI translations. Keep these conventions explicit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import tempfile

import numpy as np


def transform_matrix(roll=0., pitch=0., yaw=0., x=0., y=0., z=0.):
    values = np.asarray([roll, pitch, yaw, x, y, z], dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("外参必须为有限数值。")
    r, p, y_angle = np.deg2rad(values[:3])
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y_angle), math.sin(y_angle)
    matrix = np.eye(4)
    matrix[:3, :3] = [[cy*cp, cy*sp*sr-sy*cr, cy*sp*cr+sy*sr],
                     [sy*cp, sy*sp*sr+cy*cr, sy*sp*cr-cy*sr], [-sp, cp*sr, cp*cr]]
    matrix[:3, 3] = values[3:]
    return matrix


def validate_matrix(matrix):
    value = np.asarray(matrix, dtype=float)
    if value.shape != (4, 4) or not np.isfinite(value).all():
        raise ValueError("外参必须为有限的 4×4 矩阵。")
    rotation = value[:3, :3]
    if (not np.allclose(value[3], [0, 0, 0, 1], atol=1e-6)
            or not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-3)
            or not math.isclose(float(np.linalg.det(rotation)), 1., abs_tol=1e-3)):
        raise ValueError("外参不是有效刚体变换，请检查旋转矩阵及最后一行。")
    return value.copy()


def matrix_parameters(matrix):
    value = validate_matrix(matrix)
    r = value[:3, :3]
    pitch = math.atan2(-r[2, 0], math.hypot(r[0, 0], r[1, 0]))
    if math.cos(pitch) > 1e-6:
        roll, yaw = math.atan2(r[2, 1], r[2, 2]), math.atan2(r[1, 0], r[0, 0])
    else:
        roll, yaw = math.atan2(-r[1, 2], r[1, 1]), 0.
    return [math.degrees(roll), math.degrees(pitch), math.degrees(yaw), *value[:3, 3]]


@dataclass
class CalibrationState:
    base: np.ndarray = field(default_factory=lambda: np.eye(4))
    adjustment: list = field(default_factory=lambda: [0.] * 6)

    @property
    def matrix(self):
        return transform_matrix(*self.adjustment) @ self.base

    def load(self, matrix, *, reference=False):
        value = validate_matrix(matrix)
        # Match the original panel: input edits are deltas on the loaded base;
        # reference edits expose the loaded absolute pose directly.
        self.base = np.eye(4) if reference else value
        self.adjustment = matrix_parameters(value) if reference else [0.] * 6


def load_ini(path):
    sections = {}
    section = None
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line in ("Rotate", "Translate", "transformation"):
            section = line
            sections[section] = []
        elif section:
            try:
                sections[section].append([float(item) for item in line.split()])
            except ValueError as exc:
                raise ValueError("INI 矩阵包含无效数值。") from exc
            expected_rows = {"Rotate": 3, "Translate": 1, "transformation": 4}[section]
            if len(sections[section]) == expected_rows:
                # Existing sensor INIs can append unrelated key/value settings.
                section = None
    result = np.eye(4)
    if "transformation" in sections:
        rows = sections["transformation"]
        if len(rows) != 4 or any(len(row) != 4 for row in rows):
            raise ValueError("INI transformation 需要完整的 4×4 矩阵。")
        result = np.asarray(rows, dtype=float)
        result[:3, 3] /= 100.
    else:
        rotation, translation = sections.get("Rotate", []), sections.get("Translate", [])
        if len(rotation) != 3 or any(len(row) != 3 for row in rotation) or len(translation) != 1 or len(translation[0]) != 3:
            raise ValueError("INI 需要 Rotate（3×3）和 Translate（厘米），或 transformation（4×4）。")
        result[:3, :3] = rotation
        result[:3, 3] = np.asarray(translation[0]) / 100.
    return validate_matrix(result)


def save_ini(path, matrix):
    value = validate_matrix(matrix)
    rows = ["Rotate", *[" ".join(f"{v:.12g}" for v in row) for row in value[:3, :3]],
            "", "Translate", " ".join(f"{v * 100.:.12g}" for v in value[:3, 3]), "", "transformation"]
    centimeters = value.copy()
    centimeters[:3, 3] *= 100.
    rows.extend(" ".join(f"{v:.12g}" for v in row) for row in centimeters)
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write("\n".join(rows) + "\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
