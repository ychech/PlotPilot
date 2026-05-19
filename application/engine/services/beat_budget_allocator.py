"""Dynamic word-budget allocator for unit dramas and emotion beat cards."""
from __future__ import annotations

from dataclasses import replace
from typing import List, Sequence

from application.engine.dtos.expanded_outline_dto import EmotionBeatCard, UnitDramaPlan


class BeatBudgetAllocator:
    """Allocate words by story-unit and node function instead of flat averaging."""

    MIN_NORMAL_NODE_WORDS = 300
    MAX_NORMAL_NODE_WORDS = 900
    MAX_COMPLEX_NODE_WORDS = 1000

    FUNCTION_WEIGHTS = {
        "建立目标、危险和读者情绪缺口": 0.85,
        "让阻碍显形，并制造第一次信息差": 1.0,
        "追加代价，迫使主角从判断走向行动": 1.15,
        "兑现反常发现或能力触发，形成小爽点": 1.3,
        "放大爽点并让代价落地": 1.2,
        "阶段结果落地，接出下一目标或新阻碍": 0.95,
        "让主角原判断受挫，避免线性顺滑": 1.0,
        "通过选择交换资源和风险": 1.05,
        "把单元剧推向最终兑现前的临界点": 1.2,
        "把结果转化为关系、资源或追杀压力": 0.9,
    }

    HIGH_CAP_FOCUSES = {"action", "dialogue"}

    def allocate_unit_words(self, target_words: int, units: Sequence[UnitDramaPlan]) -> List[int]:
        if not units:
            return []
        base = max(1, target_words // len(units))
        budgets = [base for _ in units]
        budgets[-1] += target_words - sum(budgets)
        return budgets

    def allocate_node_words(
        self,
        unit: UnitDramaPlan,
        cards: Sequence[EmotionBeatCard],
    ) -> List[int]:
        if not cards:
            return []
        weights = [self._weight_for_card(card, idx, len(cards)) for idx, card in enumerate(cards)]
        total_weight = sum(weights) or 1.0
        budgets: List[int] = []
        for card, weight in zip(cards, weights):
            raw = int(unit.target_words * weight / total_weight)
            cap = self.MAX_COMPLEX_NODE_WORDS if card.focus in self.HIGH_CAP_FOCUSES else self.MAX_NORMAL_NODE_WORDS
            budgets.append(max(self.MIN_NORMAL_NODE_WORDS, min(cap, raw)))

        # Redistribute the cap/min residual without creating too-large ordinary nodes.
        delta = unit.target_words - sum(budgets)
        guard = 0
        while delta != 0 and guard < 1000:
            guard += 1
            changed = False
            if delta > 0:
                for idx, card in enumerate(cards):
                    cap = self.MAX_COMPLEX_NODE_WORDS if card.focus in self.HIGH_CAP_FOCUSES else self.MAX_NORMAL_NODE_WORDS
                    room = cap - budgets[idx]
                    if room <= 0:
                        continue
                    inc = min(room, delta)
                    budgets[idx] += inc
                    delta -= inc
                    changed = True
                    if delta <= 0:
                        break
            else:
                for idx in range(len(budgets) - 1, -1, -1):
                    room = budgets[idx] - self.MIN_NORMAL_NODE_WORDS
                    if room <= 0:
                        continue
                    dec = min(room, -delta)
                    budgets[idx] -= dec
                    delta += dec
                    changed = True
                    if delta >= 0:
                        break
            if not changed:
                budgets[-1] += delta
                delta = 0

        return budgets

    def apply_to_cards(
        self,
        units: Sequence[UnitDramaPlan],
        cards: Sequence[EmotionBeatCard],
    ) -> List[EmotionBeatCard]:
        cards_by_unit = {unit.unit_id: [] for unit in units}
        for card in cards:
            cards_by_unit.setdefault(card.unit_id, []).append(card)
        shaped: List[EmotionBeatCard] = []
        for unit in units:
            unit_cards = self._shape_node_count(cards_by_unit.get(unit.unit_id, []), unit.target_words)
            budgets = self.allocate_node_words(unit, unit_cards)
            for card, budget in zip(unit_cards, budgets):
                card.target_words = budget
            shaped.extend(unit_cards)
        return shaped

    def _shape_node_count(
        self,
        cards: Sequence[EmotionBeatCard],
        target_words: int | None = None,
    ) -> List[EmotionBeatCard]:
        shaped = [replace(card) for card in cards]
        changed = True
        while changed and len(shaped) > 1:
            changed = False
            for idx, card in enumerate(shaped):
                if card.target_words < 250:
                    merge_idx = idx if idx < len(shaped) - 1 else idx - 1
                    shaped = self._merge_cards(shaped, merge_idx)
                    changed = True
                    break

        if target_words:
            guard = 0
            while shaped and self._total_capacity(shaped) < target_words and guard < 100:
                guard += 1
                split_idx = max(
                    range(len(shaped)),
                    key=lambda i: shaped[i].target_words,
                )
                card = shaped[split_idx]
                cap = self.MAX_COMPLEX_NODE_WORDS if card.focus in self.HIGH_CAP_FOCUSES else self.MAX_NORMAL_NODE_WORDS
                shaped = shaped[:split_idx] + self._split_card(card, cap) + shaped[split_idx + 1:]
        else:
            idx = 0
            while idx < len(shaped):
                card = shaped[idx]
                cap = self.MAX_COMPLEX_NODE_WORDS if card.focus in self.HIGH_CAP_FOCUSES else self.MAX_NORMAL_NODE_WORDS
                if card.target_words > cap:
                    replacement = self._split_card(card, cap)
                    shaped = shaped[:idx] + replacement + shaped[idx + 1:]
                    idx += len(replacement)
                    continue
                idx += 1
        return shaped

    def _merge_cards(self, cards: List[EmotionBeatCard], idx: int) -> List[EmotionBeatCard]:
        first, second = cards[idx], cards[idx + 1]
        merged = EmotionBeatCard(
            beat_id=f"{first.beat_id}+{second.beat_id}",
            unit_id=first.unit_id or second.unit_id,
            title=f"{first.title} / {second.title}",
            function=f"{first.function}；{second.function}",
            target_words=first.target_words + second.target_words,
            focus=first.focus or second.focus,
            emotion_gap=f"{first.emotion_gap}；{second.emotion_gap}",
            protagonist_goal=first.protagonist_goal or second.protagonist_goal,
            obstacle_or_misbelief=f"{first.obstacle_or_misbelief}；{second.obstacle_or_misbelief}",
            active_action=f"{first.active_action}；随后{second.active_action}",
            external_feedback=f"{first.external_feedback}；随后{second.external_feedback}",
            information_delta=f"{first.information_delta}；{second.information_delta}",
            mini_payoff_or_pressure=f"{first.mini_payoff_or_pressure}；{second.mini_payoff_or_pressure}",
            hook_delta=second.hook_delta or first.hook_delta,
            sensory_anchor=f"{first.sensory_anchor}；{second.sensory_anchor}",
            forbidden_drift=f"{first.forbidden_drift}；{second.forbidden_drift}",
            acceptance_criteria=list(dict.fromkeys(first.acceptance_criteria + second.acceptance_criteria)),
            source_outline=f"{first.source_outline}；{second.source_outline}",
        )
        return cards[:idx] + [merged] + cards[idx + 2:]

    def _split_card(self, card: EmotionBeatCard, cap: int) -> List[EmotionBeatCard]:
        half = max(self.MIN_NORMAL_NODE_WORDS, card.target_words // 2)
        first_words = min(cap, half)
        second_words = max(self.MIN_NORMAL_NODE_WORDS, card.target_words - first_words)
        first = replace(
            card,
            beat_id=f"{card.beat_id}a",
            title=f"{card.title}·动作",
            target_words=first_words,
            function=f"{card.function}（动作建立）",
            hook_delta="动作后留下下一步反馈窗口",
        )
        second = replace(
            card,
            beat_id=f"{card.beat_id}b",
            title=f"{card.title}·反馈",
            target_words=second_words,
            function=f"{card.function}（反馈兑现）",
            active_action=f"承接上一节点动作结果，继续推进：{card.active_action}",
        )
        if second_words > cap:
            return [first] + self._split_card(second, cap)
        return [first, second]

    def _total_capacity(self, cards: Sequence[EmotionBeatCard]) -> int:
        total = 0
        for card in cards:
            total += self.MAX_COMPLEX_NODE_WORDS if card.focus in self.HIGH_CAP_FOCUSES else self.MAX_NORMAL_NODE_WORDS
        return total

    def _weight_for_card(self, card: EmotionBeatCard, index: int, total: int) -> float:
        weight = self.FUNCTION_WEIGHTS.get(card.function, 1.0)
        if card.focus == "action":
            weight += 0.1
        if index == total - 1:
            weight += 0.1
        return weight
