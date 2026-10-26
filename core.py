"""与 AstrBot / bilibili-api 无关的核心逻辑：校验、待确认存储、限频。

放在独立模块里，单元测试无需安装 AstrBot。
"""

from __future__ import annotations

import json
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable

BVID_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

ACTION_COMMENT = "comment"
ACTION_DYNAMIC = "dynamic"


def valid_bvid(bvid: str) -> bool:
    return isinstance(bvid, str) and bool(BVID_RE.match(bvid.strip()))


def clean_text(text: str) -> str:
    """去掉控制字符和首尾空白。"""
    return _CONTROL_RE.sub("", text or "").strip()


def truncate(text: str, limit: int) -> str:
    text = clean_text(text)
    return text if len(text) <= limit else text[:limit] + "…"


def redact(text: str, secrets_: list[str]) -> str:
    """把已知的 Cookie 值从文本中抹掉（日志、回复、异常信息都应过一遍）。"""
    for s in secrets_:
        if s and len(s) >= 4:
            text = text.replace(s, "***")
    return text


def normalize_ids(ids) -> set[str]:
    """WebUI 里白名单可能填成数字或字符串，统一成字符串。"""
    return {str(i).strip() for i in (ids or []) if str(i).strip()}


class RateLimiter:
    """滑动窗口限频，可选落盘，避免重启后清零。"""

    def __init__(
        self,
        max_per_hour: int,
        min_interval: int,
        path: Path | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.max_per_hour = max_per_hour
        self.min_interval = min_interval
        self.path = path
        self.clock = clock
        self._stamps: deque[float] = deque()
        self._load()

    def _load(self) -> None:
        if not self.path:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._stamps = deque(float(x) for x in data)
        except (OSError, ValueError, TypeError):
            self._stamps = deque()

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(list(self._stamps)), encoding="utf-8")
        except OSError:
            pass

    def _prune(self, now: float) -> None:
        while self._stamps and now - self._stamps[0] >= 3600:
            self._stamps.popleft()

    def check(self) -> str | None:
        """返回 None 表示允许，否则返回中文原因。"""
        now = self.clock()
        self._prune(now)
        if self._stamps and now - self._stamps[-1] < self.min_interval:
            wait = int(self.min_interval - (now - self._stamps[-1])) + 1
            return f"发布过于频繁，请 {wait} 秒后再试。"
        if len(self._stamps) >= self.max_per_hour:
            wait = int(3600 - (now - self._stamps[0])) // 60 + 1
            return f"已达到每小时 {self.max_per_hour} 次的发布上限，约 {wait} 分钟后恢复。"
        return None

    def record(self) -> None:
        now = self.clock()
        self._prune(now)
        self._stamps.append(now)
        self._save()


@dataclass
class Pending:
    action: str
    params: dict
    sender: str
    origin: str
    expires_at: float
    code: str = field(default_factory=lambda: secrets.token_hex(2))


class TakeStatus(Enum):
    OK = "ok"
    NONE = "none"
    EXPIRED = "expired"
    NOT_OWNER = "not_owner"
    BAD_CODE = "bad_code"


class PendingStore:
    """待确认请求：origin -> sender -> Pending。每人每会话最多一条。"""

    def __init__(self, ttl: int, clock: Callable[[], float] = time.time):
        self.ttl = ttl
        self.clock = clock
        self._items: dict[str, dict[str, Pending]] = {}

    def create(self, action: str, params: dict, sender: str, origin: str) -> Pending:
        p = Pending(action, params, sender, origin, self.clock() + self.ttl)
        self._items.setdefault(origin, {})[sender] = p
        return p

    def peek(self, origin: str, sender: str) -> Pending | None:
        return self._items.get(origin, {}).get(sender)

    def _drop(self, origin: str, sender: str) -> None:
        bucket = self._items.get(origin)
        if bucket:
            bucket.pop(sender, None)
            if not bucket:
                self._items.pop(origin, None)

    def take(
        self, origin: str, sender: str, code: str | None
    ) -> tuple[TakeStatus, Pending | None]:
        """取出并移除待确认请求（先移除再执行，防止重复确认）。

        校验码错误时不移除，允许重试；过期则移除。
        """
        mine = self.peek(origin, sender)
        if mine is None:
            if self._items.get(origin):
                return TakeStatus.NOT_OWNER, None
            return TakeStatus.NONE, None
        if self.clock() > mine.expires_at:
            self._drop(origin, sender)
            return TakeStatus.EXPIRED, None
        if (code or "").strip().lower() != mine.code:
            return TakeStatus.BAD_CODE, mine
        self._drop(origin, sender)
        return TakeStatus.OK, mine

    def cancel(self, origin: str, sender: str) -> bool:
        existed = self.peek(origin, sender) is not None
        self._drop(origin, sender)
        return existed


def check_publish_allowed(
    *,
    sender: str,
    allowed_ids,
    read_only: bool,
    text: str,
    max_len: int,
    limiter: RateLimiter,
) -> str | None:
    """发布前的统一校验，返回 None 表示通过，否则返回中文拒绝原因。"""
    if read_only:
        return "插件处于只读模式，已禁止所有发布操作。"
    if str(sender).strip() not in normalize_ids(allowed_ids):
        return "无权限：你不在发布白名单内。"
    text = clean_text(text)
    if not text:
        return "内容不能为空。"
    if len(text) > max_len:
        return f"内容过长（{len(text)} 字），上限 {max_len} 字。"
    return limiter.check()
