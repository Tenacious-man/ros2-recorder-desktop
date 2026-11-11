"""Show decimal capacity while retaining legacy profile increments."""
from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import QDoubleSpinBox

from .units import DECIMAL_BYTES, format_capacity


class CapacitySpinBox(QDoubleSpinBox):
    binary_value_changed = pyqtSignal()

    def __init__(self, value, minimum, maximum, bytes_per_unit, unit, *, precision=0):
        super().__init__()
        self.scale = bytes_per_unit / DECIMAL_BYTES[unit]
        self.precision = precision
        self.setDecimals(2)
        self.setRange(minimum * self.scale, maximum * self.scale)
        self.setSuffix(' ' + unit)
        self.setKeyboardTracking(False)
        self.set_binary_value(value)
        self.last_binary_value = self.binary_value()
        self.valueChanged.connect(self._notify_binary_change)
        self.editingFinished.connect(self._snap_to_step)
        step = format_capacity(bytes_per_unit * 10**-precision, unit)
        self.setToolTip(f'按十进制 {unit} 显示。编辑时取最接近的可保存值，最小间隔约 {step}。')

    def binary_value(self):
        value = self.value() / self.scale
        return round(value, self.precision) if self.precision else round(value)

    def set_binary_value(self, value):
        self.setValue(value * self.scale)

    def stepBy(self, steps):
        self.set_binary_value(self.binary_value() + steps)

    def _snap_to_step(self):
        self.set_binary_value(self.binary_value())

    def _notify_binary_change(self):
        value = self.binary_value()
        if self.last_binary_value != value:
            self.last_binary_value = value
            self.binary_value_changed.emit()
