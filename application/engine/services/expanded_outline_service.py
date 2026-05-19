"""Expanded outline orchestration service."""
from __future__ import annotations

import logging

from application.engine.dtos.expanded_outline_dto import ExpandedOutlinePlan
from application.engine.services.beat_budget_allocator import BeatBudgetAllocator
from application.engine.services.expanded_outline_validators import (
    NodeCardValidator,
    UnitDramaValidator,
)
from application.engine.services.unit_drama_planner import UnitDramaPlanner
from application.engine.services.expanded_outline_trace_store import ExpandedOutlineTraceStore

logger = logging.getLogger(__name__)


class ExpandedOutlineService:
    """Orchestrate unit-drama planning, node-card expansion, and validation."""

    MAX_REPAIR_ATTEMPTS = 2

    def __init__(
        self,
        planner: UnitDramaPlanner | None = None,
        budget_allocator: BeatBudgetAllocator | None = None,
        unit_validator: UnitDramaValidator | None = None,
        node_validator: NodeCardValidator | None = None,
        trace_store: ExpandedOutlineTraceStore | None = None,
    ):
        self.planner = planner or UnitDramaPlanner()
        self.budget_allocator = budget_allocator or BeatBudgetAllocator()
        self.unit_validator = unit_validator or UnitDramaValidator()
        self.node_validator = node_validator or NodeCardValidator()
        self.trace_store = trace_store or ExpandedOutlineTraceStore()

    def expand(
        self,
        *,
        novel_id: str = "",
        chapter_number: int,
        outline: str,
        target_words: int,
        genre: str | None = None,
    ) -> ExpandedOutlinePlan:
        source_events = self.planner.segment_outline(outline)
        warnings: list[str] = []
        repair_attempts = 0
        last_unit_validation = None
        last_node_validation = None
        for attempt in range(self.MAX_REPAIR_ATTEMPTS + 1):
            units = self.planner.plan_units(
                outline=outline,
                target_words=target_words,
                chapter_number=chapter_number,
                genre=genre,
                source_events=source_events,
            )
            unit_validation = self.unit_validator.validate_plan(units)
            last_unit_validation = unit_validation
            if not unit_validation.passed:
                warnings.append(f"单元剧规划验收失败 attempt={attempt + 1}: {unit_validation.summary()}")
                source_events = self._expand_source_events_for_repair(source_events, outline)
                repair_attempts += 1
                continue

            cards = self.planner.build_emotion_beat_cards(
                units=units,
                source_events=source_events,
            )
            cards = self.budget_allocator.apply_to_cards(units, cards)
            node_validation = self.node_validator.validate(cards)
            last_node_validation = node_validation
            if node_validation.passed:
                break
            warnings.append(f"节点卡验收失败 attempt={attempt + 1}: {node_validation.summary()}")
            cards = self._repair_cards(cards)
            cards = self.budget_allocator.apply_to_cards(units, cards)
            node_validation = self.node_validator.validate(cards)
            last_node_validation = node_validation
            repair_attempts += 1
            if node_validation.passed:
                break
            source_events = self._expand_source_events_for_repair(source_events, outline)
        else:  # pragma: no cover - defensive; for-loop always enters
            units = []
            cards = []

        if last_unit_validation is not None and not last_unit_validation.passed:
            raise ValueError(f"单元剧规划验收失败：{last_unit_validation.summary()}")
        if last_node_validation is not None and not last_node_validation.passed:
            raise ValueError(f"节点卡验收失败：{last_node_validation.summary()}")

        plan = ExpandedOutlinePlan(
            units=list(units),
            beat_cards=list(cards),
            validation_warnings=warnings,
            repair_attempts=repair_attempts,
        )
        trace_path = None
        if novel_id:
            trace_path = self.trace_store.record_plan(
                novel_id=novel_id,
                chapter_number=chapter_number,
                outline=outline,
                target_words=target_words,
                plan=plan,
            )
            if trace_path:
                plan.trace_path = str(trace_path)

        logger.info(
            "扩写章纲验收通过：units=%d cards=%d unit_score=%.2f node_score=%.2f",
            len(units),
            len(cards),
            unit_validation.score,
            node_validation.score,
        )
        return plan

    def _expand_source_events_for_repair(self, source_events: list[str], outline: str) -> list[str]:
        events = [event for event in source_events if event and event.strip()]
        if len(events) >= 4:
            return events
        text = (outline or "").strip()
        if not text:
            return events
        midpoint = max(1, len(text) // 2)
        repaired = [text[:midpoint].strip(), text[midpoint:].strip()]
        return [event for event in (events + repaired) if event]

    def _repair_cards(self, cards):
        repaired = []
        for idx, card in enumerate(cards):
            if not card.active_action.strip():
                card.active_action = "让主角做一次具体试探、换位、抢夺、谈判或反击"
            if not card.external_feedback.strip():
                card.external_feedback = "动作之后，对手、环境、关系或规则给出可见反馈"
            if not card.emotion_gap.strip():
                card.emotion_gap = "读者需要看到主角从被动处境里争取主动"
            if not card.information_delta.strip():
                card.information_delta = "本节点必须让主角或读者获得一个新事实"
            if idx == len(cards) - 1 and not (card.mini_payoff_or_pressure.strip() or card.hook_delta.strip()):
                card.hook_delta = "留下下一目标、新阻碍、倒计时、物件或未完成动作"
            repaired.append(card)
        return repaired
