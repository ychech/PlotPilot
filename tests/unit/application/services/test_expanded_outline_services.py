from types import SimpleNamespace
from unittest.mock import Mock

from application.engine.dtos.expanded_outline_dto import EmotionBeatCard
from application.engine.services.beat_budget_allocator import BeatBudgetAllocator
from application.engine.services.expanded_outline_service import ExpandedOutlineService
from application.engine.services.expanded_outline_trace_store import ExpandedOutlineTraceStore
from application.engine.services.expanded_outline_validators import (
    BeatRealizationValidator,
    NodeCardValidator,
)
from application.engine.services.autopilot_daemon import AutopilotDaemon


YUNZE_OUTLINE = (
    "云泽被三叔公以血饲雷兽之名扔进化形雷兽森林。"
    "雷瘴前夕，无数雷兽潜伏，云泽经脉枯竭，沦为饵食。"
    "狼形雷兽利爪撕裂肩胛。"
    "云泽血脉中沉睡的先天雷骨苏醒。"
)


def _card(**overrides):
    data = dict(
        beat_id="u1-b1",
        unit_id="u1",
        title="血饵处境",
        function="建立目标、危险和读者情绪缺口",
        target_words=500,
        focus="hook",
        emotion_gap="读者想看云泽不是等死废物",
        protagonist_goal="先活下来",
        obstacle_or_misbelief="三叔公把他当血饵",
        active_action="停止乱挣，检查死结、树皮、地面痕迹和危险靠近方向",
        external_feedback="兽群没有立刻扑杀，反而因他的血出现迟疑",
        information_delta="献祭不是简单灭口，主角的血另有用途",
        mini_payoff_or_pressure="主角从纯被动转为能判断局势的人",
        hook_delta="这场献祭不只是喂兽",
        sensory_anchor="绳索勒痛、血味、兽群位置",
        forbidden_drift="禁止纯氛围、纯感官、纯被动挨打",
        acceptance_criteria=[],
        source_outline="云泽被绑成血饵。",
    )
    data.update(overrides)
    return EmotionBeatCard(**data)


def test_expanded_outline_service_runs_full_planning_validation_budget_flow():
    service = ExpandedOutlineService()

    plan = service.expand(
        chapter_number=1,
        outline=YUNZE_OUTLINE,
        target_words=2500,
    )

    assert len(plan.units) == 1
    assert len(plan.beat_cards) == 4
    assert sum(card.target_words for card in plan.beat_cards) == 2500
    assert all(card.active_action for card in plan.beat_cards)
    assert all(card.external_feedback for card in plan.beat_cards)


def test_expanded_outline_service_persists_plan_trace(tmp_path):
    service = ExpandedOutlineService(
        trace_store=ExpandedOutlineTraceStore(base_dir=tmp_path),
    )

    plan = service.expand(
        novel_id="novel-trace",
        chapter_number=1,
        outline=YUNZE_OUTLINE,
        target_words=2500,
    )

    assert plan.trace_path
    trace_file = tmp_path / plan.trace_path.split("/")[-1]
    assert trace_file.exists()
    assert "expanded_outline_plan" in trace_file.read_text(encoding="utf-8")


def test_beat_budget_allocator_weights_payoff_above_setup():
    service = ExpandedOutlineService()
    plan = service.expand(
        chapter_number=1,
        outline=YUNZE_OUTLINE,
        target_words=7500,
    )

    setup = plan.beat_cards[0]
    payoff = next(card for card in plan.beat_cards if "兑现反常发现" in card.function)
    assert payoff.target_words > setup.target_words


def test_beat_budget_allocator_splits_nodes_until_unit_capacity_fits():
    unit = SimpleNamespace(unit_id="u1", target_words=3200)
    cards = [
        _card(beat_id="u1-b1", focus="hook", target_words=1600),
        _card(beat_id="u1-b2", focus="suspense", target_words=1600),
    ]

    shaped = BeatBudgetAllocator().apply_to_cards([unit], cards)

    assert len(shaped) >= 4
    assert sum(card.target_words for card in shaped) == 3200
    assert all(card.target_words <= BeatBudgetAllocator.MAX_NORMAL_NODE_WORDS for card in shaped)


def test_expanded_outline_service_scales_long_chapter_into_more_units_and_nodes():
    service = ExpandedOutlineService()

    plan = service.expand(
        chapter_number=1,
        outline=YUNZE_OUTLINE,
        target_words=30000,
    )

    assert len(plan.units) == 4
    assert len(plan.beat_cards) >= 32
    assert sum(card.target_words for card in plan.beat_cards) == 30000
    assert max(card.target_words for card in plan.beat_cards) <= BeatBudgetAllocator.MAX_COMPLEX_NODE_WORDS


def test_node_card_validator_rejects_missing_action_and_feedback():
    result = NodeCardValidator().validate([
        _card(active_action="", external_feedback=""),
    ])

    assert result.passed is False
    assert "active_action" in result.summary()
    assert "external_feedback" in result.summary()


def test_beat_realization_validator_rejects_empty_ai_style_drift():
    card = _card()
    content = "夜色仿佛无边无际，压迫感莫名涌来。他感到一种无法形容的寒意。"

    result = BeatRealizationValidator().validate(
        card=card,
        content=content,
        target_words=500,
    )

    assert result.passed is False
    assert "正文过短" in result.summary()
    assert "空转" in result.summary()


def test_autopilot_beat_realization_uses_node_card_contract():
    daemon = AutopilotDaemon(
        novel_repository=Mock(),
        llm_service=Mock(),
        context_builder=Mock(),
        background_task_service=Mock(),
        planning_service=Mock(),
        story_node_repo=Mock(),
        chapter_repository=Mock(),
    )
    beat = SimpleNamespace(beat_card=_card())

    bad = daemon._validate_beat_realization(
        beat,
        "雷声很远，像一种说不清的压迫。",
        500,
        is_final_beat=False,
    )
    good = daemon._validate_beat_realization(
        beat,
        (
            "云泽停止乱挣，先用肩背抵住树干，低头去看麻绳勒进皮肉的方向。"
            "他摸到树皮下有一道旧雷纹，故意让伤口的血贴上去。"
            "围在暗处的雷兽没有扑杀，反而齐齐停住，领头那头低吼着退了半步。"
            "他这才意识到，三叔公所谓血饲雷兽，喂的不是命，而是他血里某种东西。"
            "这个判断让他没有再把力气浪费在死结上，而是把手腕慢慢转到树干阴面，"
            "用绳索摩擦过的血继续压住那道雷纹。雷纹第二次亮起时，兽群退得更明显，"
            "几双兽瞳从贪婪变成迟疑。云泽咬住牙，借这一息空隙把膝盖顶进树根缝里，"
            "整个人往侧面一拧，终于把勒进肉里的绳结扯松半寸。"
        ),
        300,
        is_final_beat=False,
    )

    assert bad.passed is False
    assert good.passed is True
