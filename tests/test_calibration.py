from array import array
import math
import struct
from types import SimpleNamespace

import numpy as np
import pytest

from ros2_recorder.calibration_cloud import preview_cloud
from ros2_recorder.calibration_math import CalibrationState, load_ini, matrix_parameters, save_ini, transform_matrix


def make_cloud(*, bigendian=False):
    endian = ">" if bigendian else "<"
    data = bytearray([0xa5] * 160)
    points = [(1., 2., 3.), (-1., 0., 4.), (5., 6., 7.), (float("nan"), 9., 10.)]
    for index, point in enumerate(points):
        offset = (index // 2) * 80 + (index % 2) * 32
        struct.pack_into(endian + "fffHHdff", data, offset, *point, 100 + index, index, 123.456 + index, 1.5, 2.5)
    definitions = [("x", 0, 7, 1), ("y", 4, 7, 1), ("z", 8, 7, 1),
                   ("intensity", 12, 4, 1), ("ring", 14, 4, 1), ("time", 16, 8, 1), ("vector", 24, 7, 2)]
    return SimpleNamespace(width=2, height=2, point_step=32, row_step=80, is_bigendian=bigendian,
        data=array("B", data), fields=[SimpleNamespace(name=name, offset=offset, datatype=kind, count=count)
                                      for name, offset, kind, count in definitions],
        header=SimpleNamespace(frame_id="raw_lidar", stamp=SimpleNamespace(sec=123, nanosec=456)), is_dense=False)


def test_known_axis_rotation_and_left_multiply():
    matrix = transform_matrix(yaw=90, x=2)
    np.testing.assert_allclose(matrix @ [1, 0, 0, 1], [2, 1, 0, 1], atol=1e-14)
    state = CalibrationState()
    state.load(transform_matrix(x=1))
    state.adjustment[2] = 90
    np.testing.assert_allclose(state.matrix[:3, 3], [0, 1, 0], atol=1e-14)
    np.testing.assert_allclose(state.matrix @ [1, 0, 0, 1], [0, 2, 0, 1], atol=1e-14)


@pytest.mark.parametrize("pitch", [-90, -20, 0, 45, 90])
def test_reference_euler_and_ini_roundtrip(tmp_path, pitch):
    matrix = transform_matrix(roll=31, pitch=pitch, yaw=-64, x=1.25, y=-2.5, z=.004)
    np.testing.assert_allclose(transform_matrix(*matrix_parameters(matrix)), matrix, atol=1e-10)
    path = tmp_path / "calibration.ini"
    save_ini(path, matrix)
    content = path.read_text()
    assert "125 -250 0.4" in content
    loaded = load_ini(path)
    np.testing.assert_allclose(loaded, matrix, atol=1e-10)
    input_state, reference = CalibrationState(), CalibrationState()
    input_state.load(loaded)
    reference.load(loaded, reference=True)
    assert input_state.adjustment == [0.] * 6
    np.testing.assert_allclose(reference.base, np.eye(4))
    np.testing.assert_allclose(reference.matrix, loaded, atol=1e-10)


def test_legacy_rotate_translate_and_invalid_ini(tmp_path):
    path = tmp_path / "old.ini"
    path.write_text("Rotate\n0 -1 0\n1 0 0\n0 0 1\n\nTranslate\n125 -250 0.4\n\ndisplay 1\ncompensation 1\n")
    np.testing.assert_allclose(load_ini(path), transform_matrix(yaw=90, x=1.25, y=-2.5, z=.004), atol=1e-14)
    for text in ("Rotate\n1 0 0\n", "transformation\n1 0 0 0\n0 1 0 0\n0 0 1 0\n",
                 "Rotate\n2 0 0\n0 1 0\n0 0 1\nTranslate\n0 0 0\n",
                 "Rotate\n1 0 0\n0 1 0\n0 0 1\nTranslate\nnan 0 0\n"):
        path.write_text(text)
        with pytest.raises(ValueError):
            load_ini(path)


@pytest.mark.parametrize("bigendian", [False, True])
def test_preview_preserves_source_fields_time_and_padded_rows(bigendian):
    message = make_cloud(bigendian=bigendian)
    original = bytes(message.data)
    preview = preview_cloud(message)
    assert preview["total"] == 4 and preview["indexes"].tolist() == [0, 1, 2]
    assert preview["stamp_ns"] == 123000000456
    assert preview["fields"]["vector"].tolist() == [[1.5, 2.5]] * 3
    np.testing.assert_allclose(preview["xyz"], [[1, 2, 3], [-1, 0, 4], [5, 6, 7]])
    assert preview["fields"]["intensity"].tolist() == [100, 101, 102]
    assert preview["fields"]["ring"].tolist() == [0, 1, 2]
    np.testing.assert_allclose(preview["fields"]["time"], [123.456, 124.456, 125.456])
    assert bytes(message.data) == original and message.header.frame_id == "raw_lidar"
    assert message.header.stamp.sec == 123 and message.header.stamp.nanosec == 456
    np.testing.assert_array_equal(preview_cloud(message)["xyz"], preview["xyz"])
    assert len(preview_cloud(message, limit=2)["xyz"]) <= 2


def test_cloud_rejects_malformed_fields_and_sizes():
    message = make_cloud()
    message.row_step = 1
    with pytest.raises(ValueError):
        preview_cloud(message)
    message = make_cloud()
    message.fields[0].datatype = 6
    with pytest.raises(ValueError):
        preview_cloud(message)


def test_large_cloud_preview_is_bounded():
    message = make_cloud()
    message.width, message.height, message.row_step = 100_000, 1, 3_200_000
    message.data = array("B", bytes(3_200_000))
    result = preview_cloud(message)
    assert len(result["xyz"]) == 20_000
    assert result["indexes"][0] == 0 and result["indexes"][-1] == 99_999
