import pytest
import json
from unittest.mock import AsyncMock, Mock

from application.world.services.auto_bible_generator import AutoBibleGenerator


def _field_summary(value: str) -> str:
    return json.loads(value)["summary"]


@pytest.mark.asyncio
async def test_generate_worldbuilding_and_style_completes_dimension_fields():
    llm = Mock()
    llm.generate = AsyncMock()
    llm.stream_generate = AsyncMock()
    svc = AutoBibleGenerator(llm_service=llm, bible_service=Mock())

    svc._call_llm_and_parse_with_retry = AsyncMock(
        return_value={
            "style": "冷峻克制",
            "worldbuilding": {
                "core_rules": {"power_system": "天赋修行"},
                "geography": {},
                "society": {},
                "culture": {},
                "daily_life": {},
            },
        }
    )
    svc._generate_style = AsyncMock(return_value="冷峻克制")
    async def _fake_generate_single_field(*args, **kwargs):
        field_key = args[3]
        if field_key == "power_system":
            return "天赋修行"
        return "补齐内容"

    svc._generate_single_field = AsyncMock(side_effect=_fake_generate_single_field)

    result = await svc._generate_worldbuilding_and_style("premise", 30)

    assert result["style"] == "冷峻克制"
    assert _field_summary(result["worldbuilding"]["core_rules"]["power_system"]) == "天赋修行"
    assert _field_summary(result["worldbuilding"]["core_rules"]["physics_rules"]) == "补齐内容"
    assert "quick_ref" in json.loads(result["worldbuilding"]["core_rules"]["power_system"])


@pytest.mark.asyncio
async def test_generate_single_dimension_uses_storage_normalization_and_completion():
    llm = Mock()
    llm.generate = AsyncMock(
        return_value=Mock(content='{"power_system":"灵气","cost_and_limitation":"额外字段"}')
    )
    llm.stream_generate = AsyncMock()
    svc = AutoBibleGenerator(llm_service=llm, bible_service=Mock())
    svc._generate_single_field = AsyncMock(return_value="补齐字段")

    result = await svc._generate_single_dimension("premise", 30, "core_rules", {})

    assert _field_summary(result["power_system"]) == "灵气"
    assert _field_summary(result["physics_rules"]) == "补齐字段"
    assert "cost_and_limitation" not in result


def test_worldbuilding_field_plan_uses_canonical_storage_fields_only():
    svc = AutoBibleGenerator(llm_service=Mock(), bible_service=Mock())

    plan = svc.get_worldbuilding_field_plan()

    assert [item["field"] for item in plan] == [
        "power_system", "physics_rules", "magic_tech",
        "terrain", "climate", "resources", "ecology",
        "politics", "economy", "class_system",
        "history", "religion", "taboos",
        "food_clothing", "language_slang", "entertainment",
    ]


def test_bible_context_summaries_are_compact():
    svc = AutoBibleGenerator(llm_service=Mock(), bible_service=Mock())
    worldbuilding = {
        "core_rules": {
            "power_system": "力量体系" * 200,
            "physics_rules": "物理规则" * 200,
        },
        "geography": {
            "terrain": "地理生态" * 200,
        },
    }
    characters = [
        {"name": f"角色{i}", "role": "配角", "description": "人物描述" * 120}
        for i in range(12)
    ]

    wb = svc._summarize_worldbuilding(worldbuilding, max_chars=500, max_item_chars=40)
    chars = svc._summarize_characters(characters, max_items=4, max_desc_chars=30, max_chars=300)

    assert len(wb) <= 500
    assert len(chars) <= 300
    assert chars.count("\n") <= 3
    assert "..." in wb


def test_story_kernel_brief_preserves_metadata_and_key_conflict_without_hard_cut():
    svc = AutoBibleGenerator(llm_service=Mock(), bible_service=Mock())
    premise = """【系统内部·叙事结构规划（勿向读者展示本段标题与标签）】
规划目标体量：约 300,000 字。

【类型：玄幻升级；世界观基调：丹武修行】

少年林渊在矿山长大，靠替人试药活命。宗门测验当天，他被污为偷丹，被逐出山门。濒死时他发现母亲留下的神鼎可以把毒性炼成修为，但每次突破都会留下丹火反噬。为了查清母亲失踪真相，他必须进入各宗争夺的遗迹。后期他会发现所谓正道丹盟正在用凡人城池养丹。"""

    brief = svc._build_story_kernel_brief(premise, limit=260)

    assert "系统内部" not in brief
    assert "规划目标体量" not in brief
    assert "类型：玄幻升级" in brief
    assert "世界观基调：丹武修行" in brief
    assert "少年林渊" in brief
    assert "神鼎" in brief
    assert "丹火反噬" in brief
    assert "母亲失踪真相" in brief
    assert not brief.endswith("...")


def test_worldbuilding_field_length_rule_is_dynamic():
    svc = AutoBibleGenerator(llm_service=Mock(), bible_service=Mock())

    power_rule = svc._worldbuilding_field_length_rule("core_rules", "power_system")
    terrain_rule = svc._worldbuilding_field_length_rule("geography", "terrain")

    assert "120-220字" in power_rule
    assert "写清体系即可" in power_rule
    assert "220-420字" in terrain_rule
    assert "允许具体展开" in terrain_rule
