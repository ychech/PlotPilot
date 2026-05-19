import pytest
from unittest.mock import AsyncMock, Mock

from application.world.services.auto_bible_generator import AutoBibleGenerator


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
    async def _fake_generate_single_field(*args, **kwargs):
        field_key = args[3]
        if field_key == "power_system":
            return "天赋修行"
        return "补齐内容"

    svc._generate_single_field = AsyncMock(side_effect=_fake_generate_single_field)

    result = await svc._generate_worldbuilding_and_style("premise", 30)

    assert result["style"] == "冷峻克制"
    assert result["worldbuilding"]["core_rules"]["power_system"] == "天赋修行"
    assert result["worldbuilding"]["core_rules"]["physics_rules"] == "补齐内容"


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

    assert result["power_system"] == "灵气"
    assert result["physics_rules"] == "补齐字段"
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
