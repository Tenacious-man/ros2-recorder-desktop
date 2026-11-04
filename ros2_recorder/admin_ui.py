"""Hidden six-click entry and password dialogs for the native workbench."""
from __future__ import annotations

import time

from PyQt5.QtCore import QEvent, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout, QWidget,
)


class AdminEntryLabel(QLabel):
    activated = pyqtSignal()
    CLICK_INTERVAL = 1.5

    def __init__(self, text, parent=None):
        super().__init__(text, parent)
        self._clicks = 0
        self._last_click = 0.0
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, watched, event):
        # Native input first reaches QWindow, then its target QWidget. Counting
        # that first dispatch as an outside click resets every real click.
        if (event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick)
                and isinstance(watched, QWidget) and watched is not self):
            self._clicks = 0
        return super().eventFilter(watched, event)

    def _count_click(self, event):
        if event.button() != Qt.LeftButton:
            self._clicks = 0
            return
        now = time.monotonic()
        if now - self._last_click > self.CLICK_INTERVAL:
            self._clicks = 0
        self._last_click = now
        self._clicks += 1
        event.accept()
        if self._clicks == 6:
            self._clicks = 0
            self.activated.emit()

    def mousePressEvent(self, event):
        self._count_click(event)

    def mouseDoubleClickEvent(self, event):
        # Qt delivers a double-click event in place of the second press.
        self._count_click(event)


class AdminPasswordDialog(QDialog):
    def __init__(self, credentials, mode, parent=None):
        super().__init__(parent)
        self.credentials = credentials
        self.mode = mode
        self.setWindowTitle({"setup": "设置管理员密码", "unlock": "管理员验证", "change": "修改管理员密码"}[mode])
        self.setWindowModality(Qt.WindowModal)
        self.setMinimumWidth(390)
        layout = QVBoxLayout(self)
        note = QLabel("首次使用，请设置管理员密码（至少 6 个字符）。" if mode == "setup"
                      else "验证通过后显示管理员功能。" if mode == "unlock"
                      else "请输入当前密码和新密码。")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.current_password = self._password_field()
        self.new_password = self._password_field()
        self.confirm_password = self._password_field()
        if mode != "setup":
            form.addRow("密码" if mode == "unlock" else "当前密码", self.current_password)
        else:
            self.current_password.hide()
        if mode != "unlock":
            form.addRow("新密码", self.new_password)
            form.addRow("确认密码", self.confirm_password)
        else:
            self.new_password.hide()
            self.confirm_password.hide()
        layout.addLayout(form)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #b42318;")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("验证" if mode == "unlock" else "保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._submit)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.finished.connect(self._clear_passwords)
        (self.new_password if mode == "setup" else self.current_password).setFocus()

    def _password_field(self):
        field = QLineEdit(self)
        field.setEchoMode(QLineEdit.Password)
        field.setMaxLength(256)
        return field

    def _clear_passwords(self, _result):
        for field in (self.current_password, self.new_password, self.confirm_password):
            field.clear()

    def _submit(self):
        try:
            if self.mode == "unlock":
                if not self.credentials.verify(self.current_password.text()):
                    self.current_password.clear()
                    self.current_password.setFocus()
                    raise ValueError("密码不正确，请重新输入。")
            else:
                password = self.new_password.text()
                if password != self.confirm_password.text():
                    raise ValueError("两次输入的新密码不一致。")
                if self.mode == "setup":
                    self.credentials.setup(password)
                else:
                    self.credentials.change(self.current_password.text(), password)
        except (ValueError, OSError) as exc:
            self.error.setText(str(exc))
            return
        self.accept()
