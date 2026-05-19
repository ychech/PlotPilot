"""Expanded outline DTOs for unit-drama driven beat planning."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class UnitDramaPlan:
    """A chapter-level story unit that owns a coherent mini arc."""

    unit_id: str
    title: str
    unit_theme: str
    target_words: int
    protagonist_goal: str
    goal_reward: str
    core_obstacle: str
    obstacle_owner_goal: str
    pressure_line: str
    turning_point: str
    payoff: str
    cost: str
    next_hook: str
    expected_emotion_curve: List[str] = field(default_factory=list)
    source_outline: str = ""

    def to_prompt_block(self) -> str:
        curve = " -> ".join(x for x in self.expected_emotion_curve if x)
        return "\n".join(
            line
            for line in [
                f"【单元剧】{self.title}",
                f"单元主题：{self.unit_theme}",
                f"主角目标：{self.protagonist_goal}",
                f"目标奖励：{self.goal_reward}",
                f"核心阻碍：{self.core_obstacle}",
                f"阻碍方目标：{self.obstacle_owner_goal}",
                f"压力线：{self.pressure_line}",
                f"转折点：{self.turning_point}",
                f"兑现：{self.payoff}",
                f"代价：{self.cost}",
                f"下一钩子：{self.next_hook}",
                f"情绪曲线：{curve}" if curve else "",
            ]
            if line
        )


@dataclass
class EmotionBeatCard:
    """Smallest prose-generation contract for a 300-1000 word beat."""

    beat_id: str
    unit_id: str
    title: str
    function: str
    target_words: int
    focus: str
    emotion_gap: str
    protagonist_goal: str
    obstacle_or_misbelief: str
    active_action: str
    external_feedback: str
    information_delta: str
    mini_payoff_or_pressure: str
    hook_delta: str
    sensory_anchor: str
    forbidden_drift: str
    acceptance_criteria: List[str] = field(default_factory=list)
    source_outline: str = ""

    def to_prompt_block(self, unit: UnitDramaPlan | None = None) -> str:
        acceptance = "\n".join(f"- {item}" for item in self.acceptance_criteria if item)
        parts = [
            "【情绪/爽点节点卡】",
            f"节点：{self.title}",
            f"节点功能：{self.function}",
            f"目标字数：约 {self.target_words} 字",
        ]
        if unit:
            parts.append(unit.to_prompt_block())
        parts.extend(
            [
                f"章纲来源：{self.source_outline}",
                f"情绪缺口：{self.emotion_gap}",
                f"主角目标：{self.protagonist_goal}",
                f"阻碍/误判：{self.obstacle_or_misbelief}",
                f"主动动作：{self.active_action}",
                f"外界反馈：{self.external_feedback}",
                f"信息差变化：{self.information_delta}",
                f"小爽点/压迫点：{self.mini_payoff_or_pressure}",
                f"钩子变化：{self.hook_delta}",
                f"感官锚点：{self.sensory_anchor}",
                f"禁止漂移：{self.forbidden_drift}",
            ]
        )
        if acceptance:
            parts.append(f"验收标准：\n{acceptance}")
        return "\n".join(p for p in parts if p)


@dataclass
class ExpandedOutlinePlan:
    """Full deterministic expansion result."""

    units: List[UnitDramaPlan]
    beat_cards: List[EmotionBeatCard]
    trace_path: str = ""
    validation_warnings: List[str] = field(default_factory=list)
    repair_attempts: int = 0
