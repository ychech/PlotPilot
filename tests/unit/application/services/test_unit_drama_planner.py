from application.engine.services.unit_drama_planner import UnitDramaPlanner
from application.engine.services.expanded_outline_validators import NodeCardValidator


YUNZE_OUTLINE = (
    "云泽被三叔公以“血饲雷兽”之名扔进化形雷兽森林。"
    "雷瘴前夕，无数雷兽潜伏，云泽经脉枯竭，沦为饵食。"
    "在狼形雷兽利爪撕裂肩胛的瞬间，"
    "云泽血脉中沉睡的先天雷骨苏醒。"
)


def test_short_chapter_expands_to_emotion_node_cards():
    planner = UnitDramaPlanner()

    plan = planner.build_plan(
        chapter_number=1,
        outline=YUNZE_OUTLINE,
        target_words=2500,
    )

    assert len(plan.units) == 1
    assert len(plan.beat_cards) == 4
    assert sum(card.target_words for card in plan.beat_cards) == 2500
    assert all(card.active_action for card in plan.beat_cards)
    assert all(card.external_feedback for card in plan.beat_cards)
    assert all(card.information_delta for card in plan.beat_cards)
    assert any("先天雷骨" in card.source_outline or "雷骨" in card.hook_delta for card in plan.beat_cards)
    assert NodeCardValidator().validate(plan.beat_cards).passed is True


def test_standard_unit_drama_uses_one_complete_unit():
    planner = UnitDramaPlanner()

    plan = planner.build_plan(
        chapter_number=1,
        outline=YUNZE_OUTLINE,
        target_words=7500,
    )

    assert len(plan.units) == 1
    assert len(plan.beat_cards) == 8
    assert sum(card.target_words for card in plan.beat_cards) == 7500
    assert plan.units[0].payoff
    assert plan.beat_cards[-1].hook_delta


def test_long_chapter_splits_into_two_unit_dramas():
    planner = UnitDramaPlanner()

    plan = planner.build_plan(
        chapter_number=1,
        outline=YUNZE_OUTLINE,
        target_words=15000,
    )

    assert len(plan.units) == 2
    assert len(plan.beat_cards) == 16
    assert {card.unit_id for card in plan.beat_cards} == {"u1", "u2"}
    assert sum(unit.target_words for unit in plan.units) == 15000
    assert sum(card.target_words for card in plan.beat_cards) == 15000
