"""Read-only, bounded PointCloud2 preview decoding with original attributes."""
from __future__ import annotations

import numpy as np

FORMATS = {1: "i1", 2: "u1", 3: "i2", 4: "u2", 5: "i4", 6: "u4", 7: "f4", 8: "f8"}
MAX_PREVIEW_POINTS = 20_000


def cloud_records(message):
    width, height, step, row_step = message.width, message.height, message.point_step, message.row_step
    data = message.data
    if step <= 0 or row_step < width * step or len(data) < row_step * height:
        raise ValueError("PointCloud2 数据长度或步长无效。")
    names, formats, offsets = [], [], []
    endian = ">" if message.is_bigendian else "<"
    for item in message.fields:
        if not item.name or item.name in names or item.datatype not in FORMATS or item.count < 1:
            raise ValueError("PointCloud2 字段定义无效。")
        dtype = np.dtype(endian + FORMATS[item.datatype])
        if item.offset < 0 or item.offset + dtype.itemsize * item.count > step:
            raise ValueError("PointCloud2 字段超出点步长。")
        names.append(item.name)
        formats.append(dtype if item.count == 1 else (dtype, (item.count,)))
        offsets.append(item.offset)
    dtype = np.dtype({"names": names, "formats": formats, "offsets": offsets, "itemsize": step})
    for axis in ("x", "y", "z"):
        if axis not in names or dtype[axis].kind != "f" or dtype[axis].shape:
            raise ValueError("需要标量 float32/float64 的 x、y、z 字段。")
    return np.ndarray((height, width), dtype=dtype, buffer=data, strides=(row_step, step))


def preview_cloud(message, limit=MAX_PREVIEW_POINTS):
    records = cloud_records(message)
    total = message.width * message.height
    if limit < 1:
        raise ValueError("预览点数必须大于零。")
    # Index the organized array without first flattening/copying the entire cloud.
    indexes = np.linspace(0, total - 1, min(total, limit), dtype=np.int64) if total else np.empty(0, dtype=np.int64)
    selected = records[indexes // message.width, indexes % message.width] if total else np.empty(0, dtype=records.dtype)
    xyz = np.column_stack([selected[axis] for axis in ("x", "y", "z")]).astype(float)
    finite = np.isfinite(xyz).all(axis=1)
    # Preview attributes need field values, not the source record's alignment
    # gaps. Pack named fields to avoid carrying uninitialized copy padding.
    packed = np.empty(int(finite.sum()), dtype=[(name, records.dtype[name]) for name in records.dtype.names])
    for name in records.dtype.names:
        packed[name] = selected[name][finite]
    return {"xyz": xyz[finite], "fields": packed, "indexes": indexes[finite],
            "total": total, "frame": message.header.frame_id,
            "stamp_ns": int(message.header.stamp.sec) * 1_000_000_000 + int(message.header.stamp.nanosec)}
