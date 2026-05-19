"""Deterministic unit-drama planner for outline expansion.

This service turns a sparse chapter outline into executable node cards before
LLM prose generation. It deliberately has no LLM dependency: the first layer of
protection against "empty beats" should be stable, fast, and testable.
"""
from __future__ import annotations

import math
import re
from typing import List, Sequence

from application.engine.dtos.expanded_outline_dto import (
    EmotionBeatCard,
    ExpandedOutlinePlan,
    UnitDramaPlan,
)


class UnitDramaPlanner:
    """Plan unit dramas and emotion beat cards from a raw chapter outline."""

    MIN_NODE_WORDS = 300
    MAX_NODE_WORDS = 1000

    def build_plan(
        self,
        *,
        chapter_number: int,
        outline: str,
        target_words: int,
        genre: str | None = None,
    ) -> ExpandedOutlinePlan:
        source_events = self.segment_outline(outline)
        units = self.plan_units(
            outline=outline,
            target_words=target_words,
            chapter_number=chapter_number,
            genre=genre,
            source_events=source_events,
        )
        beat_cards = self.build_emotion_beat_cards(units=units, source_events=source_events)
        return ExpandedOutlinePlan(units=units, beat_cards=beat_cards)

    def segment_outline(self, outline: str) -> List[str]:
        text = (outline or "").strip()
        if not text:
            return []
        if re.search(r"(?m)^\s*\d+[\.、．\)]", text):
            parts = re.split(r"\n(?=\s*\d+[\.、．\)]\s)", text)
            return [p.strip() for p in parts if p.strip()]
        if re.search(r"(?m)^\s*[-*•]\s+\S", text):
            parts = re.split(r"\n(?=\s*[-*•]\s)", text)
            return [p.strip() for p in parts if p.strip()]
        paras = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
        if len(paras) >= 2:
            return paras
        sents = [
            s.strip()
            for s in re.split(r"(?<=[。！？；])", text)
            if len(s.strip()) > 4
        ]
        return sents or [text]

    def plan_units(
        self,
        *,
        outline: str,
        target_words: int,
        chapter_number: int,
        genre: str | None = None,
        source_events: Sequence[str] | None = None,
    ) -> List[UnitDramaPlan]:
        events = list(source_events or self.segment_outline(outline))
        unit_count = self.resolve_unit_count(target_words=target_words, event_count=len(events))
        unit_budgets = self._allocate_unit_words(target_words, unit_count)
        event_groups = self._split_events_for_units(events or [outline], unit_count)

        units: List[UnitDramaPlan] = []
        for idx in range(unit_count):
            unit_events = event_groups[idx] if idx < len(event_groups) else events
            source = "".join(unit_events).strip() or (outline or "").strip()
            phase = self._phase_name(idx, unit_count)
            protagonist_goal = self._goal_from_event(source, idx, unit_count)
            obstacle = self._obstacle_from_event(source, idx, unit_count)
            payoff = self._payoff_from_event(source, idx, unit_count)
            next_hook = self._hook_from_event(source, idx, unit_count)
            units.append(
                UnitDramaPlan(
                    unit_id=f"u{idx + 1}",
                    title=f"{phase}：{self._compact_title(source, fallback='章节单元')}",
                    unit_theme=self._unit_theme(source, genre),
                    target_words=unit_budgets[idx],
                    protagonist_goal=protagonist_goal,
                    goal_reward=self._goal_reward(source),
                    core_obstacle=obstacle,
                    obstacle_owner_goal=self._obstacle_owner_goal(source),
                    pressure_line=self._pressure_line(source, idx, unit_count),
                    turning_point=self._turning_point_from_event(source, idx, unit_count),
                    payoff=payoff,
                    cost=self._cost_from_event(source),
                    next_hook=next_hook,
                    expected_emotion_curve=self._emotion_curve(idx, unit_count),
                    source_outline=source,
                )
            )
        return units

    def resolve_unit_count(self, *, target_words: int, event_count: int = 0) -> int:
        """Resolve story-unit count from web-novel pacing and event density."""
        if target_words <= 8500:
            return 1
        if target_words <= 15000:
            return 2
        # 超长章节继续增加单元剧，避免单个单元硬撑 15000+ 字。
        return max(2, math.ceil(target_words / 7500))

    def build_emotion_beat_cards(
        self,
        *,
        units: Sequence[UnitDramaPlan],
        source_events: Sequence[str] | None = None,
    ) -> List[EmotionBeatCard]:
        cards: List[EmotionBeatCard] = []
        events = list(source_events or [])
        event_cursor = 0
        for unit in units:
            node_count = self.resolve_node_count(unit.target_words, event_count=len(events))
            node_budgets = self._allocate_node_words(unit.target_words, node_count)
            templates = self._templates_for_node_count(node_count)
            for idx in range(node_count):
                event = events[event_cursor % len(events)] if events else unit.source_outline
                if events and idx < len(events):
                    event_cursor += 1
                template = templates[idx]
                cards.append(
                    self._build_card(
                        unit=unit,
                        event=event,
                        node_index=idx,
                        node_count=node_count,
                        target_words=node_budgets[idx],
                        template=template,
                    )
                )
        return cards

    def resolve_node_count(self, target_words: int, event_count: int = 0) -> int:
        if target_words <= 3200:
            base = 4 if target_words <= 2600 else 5
        elif target_words <= 5000:
            base = 6
        elif target_words <= 8500:
            base = 8
        else:
            base = max(8, math.ceil(target_words / 850))
        if event_count >= 6 and target_words >= 3500:
            base += 1
        return max(4, min(10, base))

    def _build_card(
        self,
        *,
        unit: UnitDramaPlan,
        event: str,
        node_index: int,
        node_count: int,
        target_words: int,
        template: dict,
    ) -> EmotionBeatCard:
        compact_event = self._strip_bullet(event)
        title = f"{template['title']}：{self._compact_title(compact_event, fallback=unit.title)}"
        active_action = self._active_action(compact_event, template["action"], node_index, node_count)
        feedback = self._external_feedback(compact_event, template["feedback"], node_index, node_count)
        info_delta = self._information_delta(compact_event, template["info"], node_index, node_count)
        payoff = self._mini_payoff(compact_event, unit, template["payoff"], node_index, node_count)
        hook = self._hook_delta(compact_event, unit, template["hook"], node_index, node_count)

        return EmotionBeatCard(
            beat_id=f"{unit.unit_id}-b{node_index + 1}",
            unit_id=unit.unit_id,
            title=title,
            function=template["function"],
            target_words=target_words,
            focus=template["focus"],
            emotion_gap=self._emotion_gap(compact_event, template["emotion"]),
            protagonist_goal=unit.protagonist_goal,
            obstacle_or_misbelief=self._obstacle_or_misbelief(compact_event, unit),
            active_action=active_action,
            external_feedback=feedback,
            information_delta=info_delta,
            mini_payoff_or_pressure=payoff,
            hook_delta=hook,
            sensory_anchor=self._sensory_anchor(compact_event),
            forbidden_drift=self._forbidden_drift(template["focus"]),
            acceptance_criteria=[
                "正文必须兑现主动动作，且动作之后立刻出现外界反馈",
                "至少出现一次信息差或钩子变化，不能只写感受和氛围",
                "本节点结束时，局势、资源、身份、伤势、关系或目标至少一项发生变化",
            ],
            source_outline=compact_event,
        )

    def _templates_for_node_count(self, node_count: int) -> List[dict]:
        base = [
            {
                "title": "入局",
                "function": "建立目标、危险和读者情绪缺口",
                "focus": "hook",
                "emotion": "读者需要立刻知道主角不是在空等，而是在危险中寻找主动权",
                "action": "先停止本能乱动，观察限制、敌意、倒计时和可用资源",
                "feedback": "环境、对手或规则给出第一个反常反馈",
                "info": "读者获得当前局势的第一条可追踪事实",
                "payoff": "主角从纯被动转为能判断局势的人",
                "hook": "当前危险背后还有不合常理之处",
            },
            {
                "title": "试探",
                "function": "让阻碍显形，并制造第一次信息差",
                "focus": "suspense",
                "emotion": "读者想看主角在压迫里找出规则漏洞",
                "action": "用最小代价做一次试探、拖延、换位、询问或假动作",
                "feedback": "阻碍方的反应暴露其真正忌惮或目标",
                "info": "主角和读者至少一方意识到事情不只是表面危机",
                "payoff": "危险没有消失，但主角摸到一条可用线索",
                "hook": "线索指向更大的规则或秘密",
            },
            {
                "title": "压迫",
                "function": "追加代价，迫使主角从判断走向行动",
                "focus": "action",
                "emotion": "读者需要看到压力变具体，而不是停留在气氛",
                "action": "主动抢一个位置、资源、话语权或生机窗口",
                "feedback": "行动造成可见后果，伤势、距离、关系或资源发生变化",
                "info": "误判被局部纠正，新的风险同时出现",
                "payoff": "主角付出代价换到短暂主动权",
                "hook": "主动权很脆弱，下一步必须更冒险",
            },
            {
                "title": "转折",
                "function": "兑现反常发现或能力触发，形成小爽点",
                "focus": "action",
                "emotion": "读者期待主角抓住异常，把猎物身份扭成变数",
                "action": "把前面得到的线索用于一次冒险选择",
                "feedback": "对手、环境或旁观者的态度出现明显变化",
                "info": "读者看到主角身上或局势中真正反常的证据",
                "payoff": "完成一次从被压制到反制的阶段变化",
                "hook": "反常证据的来源尚未解释",
            },
            {
                "title": "反打",
                "function": "放大爽点并让代价落地",
                "focus": "dialogue",
                "emotion": "读者想看反应、惊惧、让步或身份错位被兑现",
                "action": "借新筹码逼对方后退、改口、露怯或做出交换",
                "feedback": "外部角色或规则承认局势已经改变",
                "info": "对手知道的秘密露出边角，主角仍未完全掌握",
                "payoff": "主角拿到资源、空间、身份优势或保命机会",
                "hook": "赢下这一口气后，更大的追索即将到来",
            },
            {
                "title": "收束",
                "function": "阶段结果落地，接出下一目标或新阻碍",
                "focus": "suspense",
                "emotion": "读者要感到这一段有结果，同时想翻到下一段",
                "action": "主角基于结果做出下一步选择，而不是原地感叹",
                "feedback": "现场留下可见的收获、代价、痕迹或追兵信号",
                "info": "本单元答案落地一部分，同时留下更具体的新问题",
                "payoff": "本单元危机阶段性解除或被转化",
                "hook": "下一目标、新敌意、倒计时、物件或未完成动作明确出现",
            },
        ]
        if node_count <= len(base):
            return base[: node_count - 1] + [base[-1]]
        extra = [
            {
                "title": "误判",
                "function": "让主角原判断受挫，避免线性顺滑",
                "focus": "suspense",
                "emotion": "读者需要看到聪明试探也会付代价",
                "action": "沿着已有判断推进一步，却撞上隐藏条件",
                "feedback": "隐藏条件改变行动后果",
                "info": "原先线索被重新解释",
                "payoff": "失败提供更准确的新判断",
                "hook": "真正的规则还差最后一块",
            },
            {
                "title": "换筹",
                "function": "通过选择交换资源和风险",
                "focus": "dialogue",
                "emotion": "读者想看主角把劣势换成筹码",
                "action": "拿现有线索或代价与对方、环境规则进行交换",
                "feedback": "对方或规则被迫给出回应",
                "info": "交换暴露新利益关系",
                "payoff": "主角得到下一步行动条件",
                "hook": "交换带来后续债务或暴露风险",
            },
            {
                "title": "逼近",
                "function": "把单元剧推向最终兑现前的临界点",
                "focus": "action",
                "emotion": "读者期待最后一击或最后确认",
                "action": "主动逼近危险核心，验证最后一个判断",
                "feedback": "危险核心给出不能回头的反馈",
                "info": "关键事实浮出水面",
                "payoff": "主角获得兑现条件",
                "hook": "兑现会带来无法撤销的代价",
            },
            {
                "title": "余波",
                "function": "把结果转化为关系、资源或追杀压力",
                "focus": "emotion",
                "emotion": "读者需要看到胜利不是空奖章，而会改变后续处境",
                "action": "主角清点代价并处理现场遗留问题",
                "feedback": "关系、环境或对手阵营因结果发生反应",
                "info": "余波暴露更高层的关注",
                "payoff": "阶段性成果变成后续筹码",
                "hook": "成果也成为新危险的定位信号",
            },
        ]
        middle = (base[:-1] + extra)[: node_count - 1]
        return middle + [base[-1]]

    def _allocate_unit_words(self, target_words: int, unit_count: int) -> List[int]:
        base = max(1, target_words // unit_count)
        budgets = [base for _ in range(unit_count)]
        budgets[-1] += target_words - sum(budgets)
        return budgets

    def _allocate_node_words(self, target_words: int, node_count: int) -> List[int]:
        weights = [1.05] + [1.0 for _ in range(max(0, node_count - 2))] + [1.15]
        total = sum(weights)
        budgets = [max(self.MIN_NODE_WORDS, min(self.MAX_NODE_WORDS, int(target_words * w / total))) for w in weights]
        delta = target_words - sum(budgets)
        budgets[-1] = max(self.MIN_NODE_WORDS, budgets[-1] + delta)
        return budgets

    def _split_events_for_units(self, events: Sequence[str], unit_count: int) -> List[List[str]]:
        clean = [e for e in events if e and e.strip()]
        if not clean:
            return [[] for _ in range(unit_count)]
        groups: List[List[str]] = []
        for idx in range(unit_count):
            start = int(idx * len(clean) / unit_count)
            end = int((idx + 1) * len(clean) / unit_count)
            groups.append(clean[start:end] or [clean[min(idx, len(clean) - 1)]])
        return groups

    def _phase_name(self, idx: int, unit_count: int) -> str:
        if unit_count == 1:
            return "本章单元"
        return "上半单元" if idx == 0 else ("下半单元" if idx == unit_count - 1 else f"第{idx + 1}单元")

    def _compact_title(self, text: str, fallback: str) -> str:
        clean = self._strip_bullet(text)
        clean = re.sub(r"[，。！？；：:\s]+", " ", clean).strip()
        return clean[:18] or fallback

    def _strip_bullet(self, text: str) -> str:
        return re.sub(r"^\s*(?:\d+[\.、．\)]|[-*•])\s*", "", (text or "").strip())

    def _unit_theme(self, source: str, genre: str | None) -> str:
        if any(k in source for k in ["雷", "兽", "血", "骨", "献祭"]):
            return "从被当成祭品到发现自身反常价值"
        if any(k in source for k in ["战", "杀", "追", "逃"]):
            return "在强压追杀中夺回阶段主动权"
        if any(k in source for k in ["真相", "发现", "秘密"]):
            return "用线索逼近真相并承担代价"
        return "主角在当前麻烦中通过选择换取新筹码"

    def _goal_from_event(self, source: str, idx: int, unit_count: int) -> str:
        if any(k in source for k in ["困", "绑", "扔进", "沦为", "饵"]):
            return "先活下来，弄清谁把自己推入死局以及死局的规则"
        if any(k in source for k in ["追", "逃"]):
            return "摆脱追击并保住关键线索或人"
        if any(k in source for k in ["发现", "真相", "秘密"]):
            return "验证线索，确认真相会带来什么后果"
        return "把眼前被动局面转成可行动的下一步"

    def _goal_reward(self, source: str) -> str:
        if any(k in source for k in ["血", "骨", "觉醒", "苏醒"]):
            return "获得活命机会，并第一次确认自身隐藏价值"
        return "得到空间、线索、资源或下一步选择权"

    def _obstacle_from_event(self, source: str, idx: int, unit_count: int) -> str:
        if any(k in source for k in ["绑", "绳", "死结"]):
            return "身体受限，错误挣扎会让处境更坏"
        if any(k in source for k in ["兽", "狼", "利爪"]):
            return "强敌近身，规则不明，任何误判都会立刻见血"
        if any(k in source for k in ["瘴", "倒计时"]):
            return "环境倒计时逼近，主角没有充足试错时间"
        return "信息不足、资源不足，对手或环境掌握主动"

    def _obstacle_owner_goal(self, source: str) -> str:
        if any(k in source for k in ["三叔", "叔公", "献祭", "血饲"]):
            return "献祭者想把主角作为代价交给更大的规则或怪物"
        if any(k in source for k in ["兽", "狼"]):
            return "危险存在并非单纯捕食，可能在确认主角身上的异常"
        return "阻碍方要维持既有优势，逼主角按它的规则行动"

    def _pressure_line(self, source: str, idx: int, unit_count: int) -> str:
        if any(k in source for k in ["雷瘴", "瘴"]):
            return "雷瘴逼近，留给主角观察、试探和反击的时间越来越少"
        if any(k in source for k in ["血", "伤", "撕裂"]):
            return "伤口和血味让危险持续升级，拖得越久越容易失控"
        return "对手、环境和时间同时压缩主角可选择空间"

    def _turning_point_from_event(self, source: str, idx: int, unit_count: int) -> str:
        if any(k in source for k in ["骨", "觉醒", "苏醒"]):
            return "主角的血脉或身体反应打破原本的猎物身份"
        if any(k in source for k in ["发现", "真相"]):
            return "一次验证让原本的判断被重写"
        return "主角用代价换来的试探结果改变局势解释"

    def _payoff_from_event(self, source: str, idx: int, unit_count: int) -> str:
        if any(k in source for k in ["骨", "觉醒", "苏醒"]):
            return "隐藏力量或身份线索初次显形，危险短暂迟疑"
        return "主角拿到阶段性筹码，不再只是承受后果"

    def _cost_from_event(self, source: str) -> str:
        if any(k in source for k in ["伤", "血", "撕裂"]):
            return "伤势、血味或暴露风险加重，后续行动更难隐藏"
        return "新筹码伴随新风险，主角需要立刻处理余波"

    def _hook_from_event(self, source: str, idx: int, unit_count: int) -> str:
        if any(k in source for k in ["骨", "觉醒", "苏醒"]):
            return "献祭者想喂给怪物的，可能不是废物，而是某种禁忌血脉"
        if idx < unit_count - 1:
            return "上一阶段得到的线索指向下一场更具体的危机"
        return "阶段结果接出下一目标、新敌意或更高层规则"

    def _emotion_curve(self, idx: int, unit_count: int) -> List[str]:
        return ["被压迫", "试探", "误判受挫", "抓住反常", "短暂反制", "新危机牵引"]

    def _emotion_gap(self, event: str, template_emotion: str) -> str:
        if any(k in event for k in ["废物", "枯竭", "沦为", "饵"]):
            return "读者想看主角不是只能等死的废物，而是能从死局里摸到规则"
        if any(k in event for k in ["撕裂", "利爪", "血"]):
            return "读者期待疼痛和危险换来有效反击，而不是只写受苦"
        return template_emotion

    def _obstacle_or_misbelief(self, event: str, unit: UnitDramaPlan) -> str:
        if any(k in event for k in ["雷兽", "兽", "狼"]):
            return "主角起初以为危险只是捕食，但对方反应可能另有目的"
        return unit.core_obstacle

    def _active_action(self, event: str, fallback: str, node_index: int, node_count: int) -> str:
        if any(k in event for k in ["绑", "绳", "树"]):
            return "停止乱挣，检查死结、树皮、地面痕迹和危险靠近方向"
        if any(k in event for k in ["雷兽", "兽", "狼"]):
            return "根据雷兽动作判断捕猎逻辑，并主动试探它为什么围而不杀"
        if any(k in event for k in ["血", "伤", "撕裂"]):
            return "利用伤口、血味或疼痛制造一次有风险的验证"
        if any(k in event for k in ["骨", "觉醒", "苏醒"]):
            return "抓住身体反常的瞬间反向借力，争取脱身空间"
        return fallback

    def _external_feedback(self, event: str, fallback: str, node_index: int, node_count: int) -> str:
        if any(k in event for k in ["雷兽", "兽", "狼"]):
            return "兽群没有按正常捕猎方式扑杀，反而因主角的血或动作出现迟疑"
        if any(k in event for k in ["骨", "觉醒", "苏醒"]):
            return "雷意、兽群或周围规则出现肉眼可见的反常回应"
        if any(k in event for k in ["绑", "绳"]):
            return "束缚没有松开，但主角确认蛮力无效，必须改用判断和环境"
        return fallback

    def _information_delta(self, event: str, fallback: str, node_index: int, node_count: int) -> str:
        if any(k in event for k in ["血饲", "献祭"]):
            return "献祭不是简单灭口，主角的血对雷兽或规则另有用途"
        if any(k in event for k in ["骨", "觉醒", "苏醒"]):
            return "读者确认主角身体里存在未被本人理解的隐藏异常"
        return fallback

    def _mini_payoff(self, event: str, unit: UnitDramaPlan, fallback: str, node_index: int, node_count: int) -> str:
        if node_index == node_count - 1:
            return unit.payoff
        if any(k in event for k in ["骨", "觉醒", "苏醒"]):
            return "猎物身份被打破，危险存在第一次因主角而退让或迟疑"
        return fallback

    def _hook_delta(self, event: str, unit: UnitDramaPlan, fallback: str, node_index: int, node_count: int) -> str:
        if node_index == node_count - 1:
            return unit.next_hook
        if any(k in event for k in ["雷兽", "血", "骨"]):
            return "雷兽的反应暗示主角的血和这片森林有旧规则关联"
        return fallback

    def _sensory_anchor(self, event: str) -> str:
        if any(k in event for k in ["雷", "瘴", "兽", "血", "骨"]):
            return "只写能帮助判断危险的雷声、血味、爪痕、绳索勒痛或兽群位置"
        return "只保留能改变行动判断的声音、距离、触感或物件位置"

    def _forbidden_drift(self, focus: str) -> str:
        if focus == "suspense":
            return "禁止只用神秘词拖延答案，必须给一个可追踪事实"
        if focus == "emotion":
            return "禁止长段内心解释，情绪必须落到选择或动作后果"
        if focus == "dialogue":
            return "禁止空对白和重复威胁，每轮回应必须改变信息、关系或筹码"
        return "禁止纯氛围、纯感官、纯被动挨打；每段都要推进目标或风险"
