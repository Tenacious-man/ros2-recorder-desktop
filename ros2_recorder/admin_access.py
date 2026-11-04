"""Local administrator password, independent of recording profiles."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets

from .config import atomic_json


class AdminCredentials:
    ITERATIONS = 600_000

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else (
            Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
            / "ros2-recorder" / "admin.json")

    @property
    def configured(self):
        # A damaged file must never silently reopen initial password setup.
        return os.path.lexists(self.path)

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (data["version"] != 1 or data["algorithm"] != "pbkdf2-sha256"
                    or type(data["iterations"]) is not int
                    or not 100_000 <= data["iterations"] <= 2_000_000):
                raise ValueError()
            salt = bytes.fromhex(data["salt"])
            digest = bytes.fromhex(data["digest"])
            if len(salt) != 16 or len(digest) != 32:
                raise ValueError()
            return salt, digest, data["iterations"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError("管理员密码文件无法读取，请检查配置文件；为保护现有密码，未开放重新设置。") from exc

    def verify(self, password):
        salt, digest, iterations = self._read()
        if not isinstance(password, str) or len(password) > 256:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, digest)

    def _write(self, password):
        if not isinstance(password, str) or not 6 <= len(password) <= 256 or not password.strip():
            raise ValueError("密码长度需为 6～256 个字符，不能全部为空格。")
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, self.ITERATIONS)
        atomic_json(self.path, {"version": 1, "algorithm": "pbkdf2-sha256",
                               "iterations": self.ITERATIONS, "salt": salt.hex(), "digest": digest.hex()})

    def setup(self, password):
        if self.configured:
            raise ValueError("管理员密码已设置，请使用现有密码验证。")
        self._write(password)

    def change(self, current_password, new_password):
        if not self.verify(current_password):
            raise ValueError("当前密码不正确。")
        self._write(new_password)
