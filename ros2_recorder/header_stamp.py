"""Read header time with ROS' native decoder, without decoding bulk arrays.

For CDR1 messages whose *declared first field* is std_msgs/Header, the first
serialized member is builtin_interfaces/Time. Decode that 12-byte encapsulated
prefix with rclpy rather than allocating the complete Image/Path/PointCloud2.
Other schemas/encodings retain the native full-message decoder.

This is a narrow adapter over rclpy/rosidl, not a general CDR implementation.
See ros2/rclpy serialization.py and ros2/common_interfaces std_msgs/Header.msg.
"""
from __future__ import annotations


class HeaderStampReader:
    def __init__(self, message_class):
        from builtin_interfaces.msg import Time
        from rclpy.serialization import deserialize_message
        sample = message_class()
        if not hasattr(getattr(sample, 'header', None), 'stamp'):
            raise ValueError('消息不含 header.stamp。')
        self.message_class = message_class
        self.time_class = Time
        self.deserialize = deserialize_message
        fields = list(message_class.get_fields_and_field_types())
        slots = getattr(message_class, 'SLOT_TYPES', ())
        first = slots[0] if slots else None
        self.prefix_supported = (bool(fields) and fields[0] == 'header' and
                                 tuple(getattr(first, 'namespaces', ())) == ('std_msgs', 'msg') and
                                 getattr(first, 'name', None) == 'Header')
        self.prefix_count = 0
        self.full_count = 0

    def read(self, payload):
        # CDR1 only. XCDR2/parameter-list encodings may have delimiter headers;
        # never assume the same member offset for those encodings.
        if self.prefix_supported and len(payload) >= 12 and payload[:2] in (b'\x00\x00', b'\x00\x01'):
            self.prefix_count += 1
            return self.deserialize(payload[:12], self.time_class)
        self.full_count += 1
        return self.deserialize(payload, self.message_class).header.stamp


def stamp_ns(stamp):
    value = int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)
    if not 0 <= stamp.nanosec < 1_000_000_000 or value == 0:
        raise ValueError('header 时间戳为零或无效。')
    return value
