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
                "core_rules": {"power_system": "天赋修行", "extra_key": "扩展信息"},
                "geography": {},
                "society": {},
                "culture": {},
                "daily_life": {},
            },
        }
    )
    svc._generate_single_field = AsyncMock(side_effect=lambda *args, **kwargs: "补齐内容")

    result = await svc._generate_worldbuilding_and_style("premise", 30)

    assert result["style"] == "冷峻克制"
    assert result["worldbuilding"]["core_rules"]["power_system"] == "天赋修行"
    assert result["worldbuilding"]["core_rules"]["physics_rules"] == "补齐内容"
    assert "extra_key" not in result["worldbuilding"]["core_rules"]


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
