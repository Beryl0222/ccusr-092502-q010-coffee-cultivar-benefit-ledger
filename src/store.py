"""仅追加事件存储与重放。

- JSONL 落盘，进程中断后重新 load 即可继续；
- event_id 全局唯一：同一事件重送按幂等忽略，不会产生第二笔事实；
- 同一 aggregate_id 下 version 必须从 1 连续递增；
- 业务状态完全由重放事件得到，存储层不做业务裁决。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .envelope import validate_event


class EventStoreError(ValueError):
    """事件流不合法（版本断号、未知类型、信封错误）。"""


class EventStore:
    def __init__(self, path: str | os.PathLike[str], allowed_events: set[str]) -> None:
        self.path = Path(path)
        self.allowed_events = set(allowed_events)
        self._events: list[dict] = []
        self._ids: set[str] = set()
        self._versions: dict[str, int] = {}

    # ---- 恢复 ----

    def load(self) -> list[dict]:
        """从磁盘重放全部事件；返回本次新载入的事件列表。"""
        if not self.path.exists():
            return []
        loaded: list[dict] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise EventStoreError(f"第 {line_no} 行不是合法 JSON: {exc}") from exc
                self._ingest(event, check_envelope=True)
                loaded.append(event)
        return loaded

    # ---- 写入 ----

    def append(self, event: dict) -> bool:
        """校验并追加事件。

        返回 True 表示新事件已入账；False 表示同一 event_id 重送，幂等忽略。
        """
        self._ingest(event, check_envelope=True)
        self._persist(event)
        return True

    def _ingest(self, event: dict, check_envelope: bool) -> None:
        if check_envelope:
            errors = validate_event(event, self.allowed_events)
            if errors:
                raise EventStoreError("; ".join(errors))
        event_id = event["event_id"]
        if event_id in self._ids:
            # 同 id 重送：幂等，直接跳过（不重复入账、不重复持久化）
            raise DuplicateEvent(event_id)
        aggregate_id = event["aggregate_id"]
        version = event["version"]
        expected = self._versions.get(aggregate_id, 0) + 1
        if version != expected:
            raise EventStoreError(
                f"聚合 {aggregate_id} 版本应为 {expected}，收到 {version}"
            )
        self._ids.add(event_id)
        self._versions[aggregate_id] = version
        self._events.append(event)

    def _persist(self, event: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再原子追加行：崩溃也不会留下半行 JSON
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    # ---- 查询 ----

    def events(self) -> list[dict]:
        return list(self._events)

    def by_aggregate(self, aggregate_id: str) -> list[dict]:
        return [e for e in self._events if e["aggregate_id"] == aggregate_id]

    def seen(self, event_id: str) -> bool:
        return event_id in self._ids

    def version_of(self, aggregate_id: str) -> int:
        return self._versions.get(aggregate_id, 0)


class DuplicateEvent(Exception):
    """同一 event_id 重送，调用方应按幂等成功处理。"""

    def __init__(self, event_id: str) -> None:
        self.event_id = event_id
        super().__init__(f"事件已接收，幂等忽略: {event_id}")
