"""Validators for unit-drama outline expansion and beat realization."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, Iterable, List, Sequence

from application.engine.dtos.expanded_outline_dto import EmotionBeatCard, UnitDramaPlan
from application.engine.dtos.validation_result import ValidationResult
from infrastructure.ai.prompt_keys import (
    BEAT_REALIZATION_VALIDATION,
    NODE_CARD_VALIDATION,
    UNIT_DRAMA_VALIDATION,
)


def _load_protocol_block(node_key: str) -> str:
    try:
        from infrastructure.ai.prompt_registry import get_prompt_registry

        registry = get_prompt_registry()
        system = registry.get_system(node_key)
        user = registry.get_user_template(node_key)
        return "\n\n".join(part for part in [system, user] if isinstance(part, str) and part.strip())
    except Exception:
        return ""


class UnitDramaValidator:
    """Validate unit-drama plans before and after prose generation."""
    protocol_block = _load_protocol_block(UNIT_DRAMA_VALIDATION)

    REQUIRED_FIELDS = (
        "protagonist_goal",
        "core_obstacle",
        "turning_point",
        "payoff",
        "cost",
        "next_hook",
    )

    def validate_plan(self, units: Sequence[UnitDramaPlan]) -> ValidationResult:
        errors: List[str] = []
        warnings: List[str] = []
        if not units:
            return ValidationResult(False, ["单元剧为空"], score=0.0)
        for unit in units:
            for field_name in self.REQUIRED_FIELDS:
                if not getattr(unit, field_name, "").strip():
                    errors.append(f"{unit.unit_id} 缺少 {field_name}")
            if unit.target_words <= 0:
                errors.append(f"{unit.unit_id} 目标字数无效")
            if unit.target_words > 8500:
                warnings.append(f"{unit.unit_id} 超过标准小单元剧容量，建议继续拆分")
        score = 1.0 - min(1.0, len(errors) * 0.2 + len(warnings) * 0.05)
        return ValidationResult(not errors, errors, warnings, score=max(0.0, score))

    def validate_completion(
        self,
        units: Sequence[UnitDramaPlan],
        realized_cards: Dict[str, Sequence[ValidationResult]],
    ) -> ValidationResult:
        errors: List[str] = []
        warnings: List[str] = []
        if not units:
            return ValidationResult(False, ["单元剧为空"], score=0.0)
        for unit in units:
            unit_results = list(realized_cards.get(unit.unit_id, []))
            if not unit_results:
                errors.append(f"{unit.unit_id} 没有任何已兑现节点")
                continue
            pass_ratio = sum(1 for r in unit_results if r.passed) / len(unit_results)
            if pass_ratio < 0.8:
                errors.append(f"{unit.unit_id} 节点兑现率不足 {pass_ratio:.0%}")
            if not any("钩子" in " ".join(r.warnings + r.errors) or r.passed for r in unit_results[-1:]):
                warnings.append(f"{unit.unit_id} 末节点缺少明确闭环证据")
        score = 1.0 - min(1.0, len(errors) * 0.25 + len(warnings) * 0.05)
        return ValidationResult(not errors, errors, warnings, score=max(0.0, score))


class NodeCardValidator:
    """Validate emotion beat cards before prose generation."""
    protocol_block = _load_protocol_block(NODE_CARD_VALIDATION)

    PASSIVE_DRIFT_MARKERS = ("氛围", "设定", "感受", "被动承受", "内心")

    def validate(self, cards: Sequence[EmotionBeatCard]) -> ValidationResult:
        errors: List[str] = []
        warnings: List[str] = []
        if not cards:
            return ValidationResult(False, ["节点卡为空"], score=0.0)

        emotion_count = 0
        info_count = 0
        previous_passive = False
        for idx, card in enumerate(cards, 1):
            if not card.active_action.strip():
                errors.append(f"节点 {idx} 缺少 active_action")
            if not card.external_feedback.strip():
                errors.append(f"节点 {idx} 缺少 external_feedback")
            if not card.emotion_gap.strip():
                warnings.append(f"节点 {idx} 缺少 emotion_gap")
            else:
                emotion_count += 1
            if card.information_delta.strip():
                info_count += 1
            else:
                warnings.append(f"节点 {idx} 缺少 information_delta")
            passive = any(marker in card.function for marker in self.PASSIVE_DRIFT_MARKERS)
            if passive and previous_passive:
                errors.append(f"节点 {idx - 1}-{idx} 连续偏被动/氛围")
            previous_passive = passive
            if card.target_words < 250:
                errors.append(f"节点 {idx} 目标字数低于 250，应合并")
            if card.target_words > 1000:
                errors.append(f"节点 {idx} 目标字数超过 1000，应拆分")

        if emotion_count / len(cards) < 0.6:
            errors.append("情绪缺口覆盖不足 60%")
        if info_count / len(cards) < 0.4:
            errors.append("信息差变化覆盖不足 40%")

        last = cards[-1]
        if not (last.mini_payoff_or_pressure.strip() or last.hook_delta.strip()):
            errors.append("最后节点缺少 payoff/cost/hook")

        score = 1.0 - min(1.0, len(errors) * 0.16 + len(warnings) * 0.03)
        return ValidationResult(not errors, errors, warnings, score=max(0.0, score))


class BeatRealizationValidator:
    """Validate generated prose against the current EmotionBeatCard."""
    protocol_block = _load_protocol_block(BEAT_REALIZATION_VALIDATION)

    PASSIVE_PATTERNS = (
        "仿佛",
        "宛如",
        "犹如",
        "说不清",
        "无法形容",
        "莫名",
    )

    PROGRESS_MARKERS = (
        "退",
        "停",
        "亮",
        "裂",
        "断",
        "松",
        "低吼",
        "让",
        "改口",
        "露出",
        "发现",
        "看见",
        "听见",
        "抓住",
        "撞",
        "撕",
        "血",
        "雷",
        "骨",
    )

    def validate(
        self,
        *,
        card: EmotionBeatCard | None,
        content: str,
        target_words: int,
        is_final_beat: bool = False,
    ) -> ValidationResult:
        text = (content or "").strip()
        if not text:
            return ValidationResult(False, ["正文为空"], score=0.0)

        errors: List[str] = []
        warnings: List[str] = []
        floor_ratio = 0.55 if is_final_beat else 0.65
        floor = max(180, int(target_words * floor_ratio))
        if len(text) < floor:
            errors.append(f"正文过短 {len(text)}/{target_words}")

        if self._looks_like_drift(text):
            errors.append("正文疑似纯氛围/纯体感/解释腔空转")

        if not self._has_progress_marker(text):
            errors.append("正文缺少可见局势变化")

        if card is not None:
            if not self._matches_action_or_domain(text, card.active_action):
                errors.append("未兑现节点卡主动动作")
            if not self._matches_action_or_domain(text, card.external_feedback):
                errors.append("未兑现节点卡外界反馈")
            if not self._matches_action_or_domain(text, card.information_delta):
                warnings.append("信息差变化兑现不明显")
            if not self._matches_action_or_domain(text, card.hook_delta):
                warnings.append("钩子变化兑现不明显")

        score = 1.0 - min(1.0, len(errors) * 0.25 + len(warnings) * 0.08)
        return ValidationResult(not errors, errors, warnings, score=max(0.0, score))

    def build_retry_hint(self, result: ValidationResult, card: EmotionBeatCard | None) -> str:
        card_lines = []
        if card is not None:
            card_lines = [
                f"主动动作：{card.active_action}",
                f"外界反馈：{card.external_feedback}",
                f"信息差变化：{card.information_delta}",
                f"小爽点/压迫点：{card.mini_payoff_or_pressure}",
                f"钩子变化：{card.hook_delta}",
                f"禁止漂移：{card.forbidden_drift}",
            ]
        return (
            "\n\n⚠️【节点兑现验收失败：原位重写】\n"
            f"失败原因：{result.summary()}\n"
            + (f"验收协议：\n{self.protocol_block}\n" if self.protocol_block else "")
            + "不要续写上一版，不要补丁式加段落；请废弃上一版，重新写完整节点。\n"
            + ("\n".join(card_lines) + "\n" if card_lines else "")
            + "正文必须让主角做出具体动作，并让外界给出可见反馈，最后留下局势变化或新钩子。"
        )

    def group_results_by_unit(
        self,
        cards: Sequence[EmotionBeatCard],
        results: Sequence[ValidationResult],
    ) -> Dict[str, List[ValidationResult]]:
        grouped: Dict[str, List[ValidationResult]] = defaultdict(list)
        for card, result in zip(cards, results):
            grouped[card.unit_id].append(result)
        return grouped

    def _looks_like_drift(self, text: str) -> bool:
        if any(pattern in text for pattern in self.PASSIVE_PATTERNS):
            return True
        paragraphs = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
        if not paragraphs:
            return True
        passive_heavy = sum(1 for p in paragraphs if self._paragraph_is_passive(p))
        return passive_heavy / len(paragraphs) > 0.6

    def _paragraph_is_passive(self, paragraph: str) -> bool:
        action_chars = "走退停抓扯看听问答笑吼咬撞撕按贴举放收"
        return not any(ch in paragraph for ch in action_chars) and len(paragraph) > 80

    def _has_progress_marker(self, text: str) -> bool:
        return any(marker in text for marker in self.PROGRESS_MARKERS)

    def _matches_action_or_domain(self, text: str, requirement: str) -> bool:
        tokens = self._keywords(requirement)
        if not tokens:
            return True
        hits = sum(1 for token in tokens if token in text)
        return hits >= max(1, min(2, len(tokens) // 3))

    def _keywords(self, text: str) -> List[str]:
        clean = re.sub(r"[，。！？；：、\s]+", " ", text or "")
        chunks = [c.strip() for c in clean.split(" ") if len(c.strip()) >= 2]
        result: List[str] = []
        for chunk in chunks:
            if len(chunk) <= 4:
                result.append(chunk)
            else:
                result.extend([chunk[:2], chunk[-2:]])
        return list(dict.fromkeys(result))[:8]
