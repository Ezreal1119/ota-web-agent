from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from .errors import AuditError, OTAError

_SENSITIVE = re.compile(r"password|passwd|secret|token|cookie|authorization|private.?key", re.I)


class AuditLog:
    """Append-only JSONL with process locking and fsync; audit failure blocks execution."""

    def __init__(self, directory: Path, secrets: tuple[str, ...] = ()):
        self.directory = Path(directory)
        self.secrets = secrets

    def scrub(self, value):
        if isinstance(value, dict):
            return {
                str(k): "[REDACTED]" if _SENSITIVE.search(str(k)) else self.scrub(v) for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.scrub(v) for v in value]
        if isinstance(value, str):
            for secret in sorted(self.secrets, key=len, reverse=True):
                value = value.replace(secret, "[REDACTED]")
            value = re.sub(r"(?i)(;jsessionid=)[^?\s\"'<>]+", r"\1[REDACTED]", value)
            return value
        if value is None or isinstance(value, (int, float, bool)):
            return value
        return self.scrub(str(value))

    def event(
        self, action: str, status: str, *, operation_id: str | None = None, actor: str = "sdk", **details
    ):
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        record = self.scrub(
            {
                "time": now.isoformat(),
                "operation_id": operation_id or uuid4().hex,
                "actor": actor,
                "action": action,
                "status": status,
                "details": details,
            }
        )
        payload = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode()
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = self.directory / f"audit-{now:%Y-%m-%d}.jsonl"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "ab", buffering=0) as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                os.fchmod(stream.fileno(), 0o600)
                offset = 0
                while offset < len(payload):
                    written = stream.write(payload[offset:])
                    if not written:
                        raise OSError("Short audit write")
                    offset += written
                os.fsync(stream.fileno())
        except OSError as exc:
            raise AuditError(
                "审计日志无法落盘，操作已停止；若之前已有写请求，请先读取状态核实结果。"
            ) from exc

    @contextmanager
    def operation(self, action: str, actor: str = "sdk", **details):
        operation_id = uuid4().hex
        self.event(action, "started", operation_id=operation_id, actor=actor, **details)
        result = {}
        try:
            yield result
        except Exception as exc:
            self.event(
                action,
                "failed",
                operation_id=operation_id,
                actor=actor,
                error_code=exc.code if isinstance(exc, OTAError) else type(exc).__name__,
                error=str(exc) if isinstance(exc, OTAError) else "内部错误",
                result=result,
                **details,
            )
            raise
        else:
            self.event(action, "succeeded", operation_id=operation_id, actor=actor, **details, result=result)
