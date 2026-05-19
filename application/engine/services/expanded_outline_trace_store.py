"""Trace persistence for expanded outline plans and validation results."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from application.paths import DATA_DIR

logger = logging.getLogger(__name__)


class ExpandedOutlineTraceStore:
    """Persist expanded-outline planning traces as JSON artifacts."""

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or (DATA_DIR / "traces" / "expanded_outline")
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def record_plan(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        outline: str,
        target_words: int,
        plan: Any,
    ) -> Path | None:
        payload = {
            "kind": "expanded_outline_plan",
            "novel_id": novel_id,
            "chapter_number": chapter_number,
            "target_words": target_words,
            "outline": outline,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "plan": self._to_jsonable(plan),
        }
        return self._write(novel_id, chapter_number, "plan", payload)

    def record_validation(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        beat_index: int | None,
        stage: str,
        result: Any,
        extra: dict[str, Any] | None = None,
    ) -> Path | None:
        payload = {
            "kind": "expanded_outline_validation",
            "novel_id": novel_id,
            "chapter_number": chapter_number,
            "beat_index": beat_index,
            "stage": stage,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "result": self._to_jsonable(result),
            "extra": self._to_jsonable(extra or {}),
        }
        suffix = f"{stage}_beat{beat_index + 1}" if beat_index is not None else stage
        return self._write(novel_id, chapter_number, suffix, payload)

    def list_traces(
        self,
        *,
        novel_id: str,
        chapter_number: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        safe_novel_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in novel_id)
        pattern = f"{safe_novel_id}_ch{chapter_number}_*.json" if chapter_number is not None else f"{safe_novel_id}_ch*.json"
        paths = sorted(self.base_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        rows: list[dict[str, Any]] = []
        for path in paths[: max(1, limit)]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                payload["_path"] = str(path)
                rows.append(payload)
            except Exception as exc:
                logger.debug("扩写章纲 trace 读取失败: %s", exc)
        return rows

    def _write(self, novel_id: str, chapter_number: int, suffix: str, payload: dict[str, Any]) -> Path | None:
        try:
            safe_novel_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in novel_id)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            path = self.base_dir / f"{safe_novel_id}_ch{chapter_number}_{suffix}_{stamp}.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            return path
        except Exception as exc:
            logger.debug("扩写章纲 trace 写入失败: %s", exc)
            return None

    def _to_jsonable(self, value: Any) -> Any:
        if is_dataclass(value):
            return asdict(value)
        if isinstance(value, dict):
            return {str(k): self._to_jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._to_jsonable(v) for v in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if hasattr(value, "__dict__"):
            return self._to_jsonable(vars(value))
        return str(value)
