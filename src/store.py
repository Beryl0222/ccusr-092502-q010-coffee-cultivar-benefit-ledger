"""只增不改的事件存储。

存储本身不做业务判断，只保证三件事：

1. 事件信封合法（复用 ``envelope.validate_event``）；
2. 同一聚合的 ``version`` 严格加一；
3. ``event_id`` 与 ``idempotency_key`` 全局唯一。

持久化为每行一个 JSON 事件的 JSONL 文件，写入后 flush 并 fsync；
进程中断后重新加载、顺序重放即可恢复全部事实。
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from typing import Any

from .envelope import validate_event


class ConcurrencyError(RuntimeError):
    """聚合版本与存储当前版本不一致。"""


class DuplicateEventError(RuntimeError):
    """event_id 或 idempotency_key 冲突。"""


class EventStore:
    """内存事件日志，可选镜像到 append-only JSONL 文件。"""

    def __init__(self, path: str | os.PathLike[str] | None = None,
                 allowed_events: Iterable[str] | None = None) -> None:
        self._path = os.fspath(path) if path is not None else None
        self._events: list[dict[str, Any]] = []
        self._versions: dict[str, int] = {}
        self._event_ids: set[str] = set()
        self._idem: dict[str, str] = {}
        self._allowed = set(allowed_events or ())
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        if self._path is not None and os.path.exists(self._path):
            self._load_from_disk()

    # ---- 订阅与重放 -------------------------------------------------
    def subscribe(self, listener: Callable[[dict[str, Any]], None]) -> None:
        """注册投影回调；历史事件与新事件都会投递给它。"""
        self._listeners.append(listener)

    def _emit(self, event: dict[str, Any]) -> None:
        for listener in self._listeners:
            listener(event)

    def _load_from_disk(self) -> None:
        assert self._path is not None
        with open(self._path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                event = json.loads(line)
                # 磁盘内容是已接收事实：直接纳管并投影，不再跑业务校验。
                self._index(event)
                self._events.append(event)
                self._emit(event)

    # ---- 写入 -------------------------------------------------------
    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        """校验并追加一条事件。"""
        errors = validate_event(event, self._allowed) if self._allowed else []
        if errors:
            raise ValueError("; ".join(errors))
        aggregate_id = event["aggregate_id"]
        expected = self._versions.get(aggregate_id, 0) + 1
        if event["version"] != expected:
            raise ConcurrencyError(
                f"聚合 {aggregate_id} 版本应为 {expected}，收到 {event['version']}"
            )
        if event["event_id"] in self._event_ids:
            raise DuplicateEventError(f"event_id 已存在: {event['event_id']}")
        key = event.get("idempotency_key")
        if key is not None and key in self._idem:
            raise DuplicateEventError(f"idempotency_key 已存在: {key}")

        self._index(event)
        self._events.append(event)
        if self._path is not None:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        self._emit(event)
        return event

    def _index(self, event: dict[str, Any]) -> None:
        aggregate_id = event["aggregate_id"]
        self._versions[aggregate_id] = event["version"]
        self._event_ids.add(event["event_id"])
        key = event.get("idempotency_key")
        if key is not None:
            self._idem[key] = event["event_id"]

    # ---- 查询 -------------------------------------------------------
    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def version_of(self, aggregate_id: str) -> int:
        return self._versions.get(aggregate_id, 0)

    def has_event(self, event_id: str) -> bool:
        return event_id in self._event_ids

    def event_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        event_id = self._idem.get(key)
        if event_id is None:
            return None
        return next(e for e in self._events if e["event_id"] == event_id)
