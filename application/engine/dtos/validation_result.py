"""Validation result DTOs for generation planning and realization checks."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class ValidationResult:
    """Small validation result object shared by outline expansion services."""

    passed: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    score: float = 1.0

    def summary(self) -> str:
        parts = []
        if self.errors:
            parts.append("错误：" + "；".join(self.errors))
        if self.warnings:
            parts.append("警告：" + "；".join(self.warnings))
        return "；".join(parts) if parts else "通过"

