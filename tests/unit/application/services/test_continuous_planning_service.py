from unittest.mock import AsyncMock, Mock

import pytest

from application.blueprint.services.continuous_planning_service import (
    ContinuousPlanningService,
    _extract_outer_json_value,
    _incremental_macro_parts_trustworthy,
    _try_parse_parts_from_llm_buffer,
    get_macro_plan_progress,
)
from domain.structure.story_node import NodeType
from infrastructure.ai.prompt_keys import (
    PLANNING_ACT,
    PLANNING_MACRO_PRECISE,
    PLANNING_MACRO_REPAIR,
    PLANNING_QUICK_MACRO,
)


def _make_service() -> ContinuousPlanningService:
    return ContinuousPlanningService(
        story_node_repo=Mock(),
        chapter_element_repo=Mock(),
        llm_service=Mock(),
    )


def test_parse_llm_response_repairs_truncated_macro_plan_json():
    svc = _make_service()

    response = """```json
{
  "parts": [
    {
      "title": "第一部",
      "volumes": [
        {
          "title": "卷一",
          "acts": [
            {
              "title": "初入京城",
              "description": "主角进入京城，卷入风暴",
              "core_conflict": "必须在权斗中站稳脚跟"
            }
          ]
        }
      ]
    }
  ],
  "theme": "权谋成长"
"""

    result = svc._parse_llm_response(response)

    assert result["theme"] == "权谋成长"
    assert result["parts"][0]["volumes"][0]["acts"][0]["title"] == "初入京城"


def test_parse_llm_response_repairs_unterminated_string():
    svc = _make_service()

    response = """{
  "parts": [
    {
      "title": "第一部",
      "volumes": [
        {
          "title": "卷一",
          "acts": [
            {
              "title": "初入京城",
              "description": "主角进入京城",
              "core_conflict": "站稳脚跟"
            },
            {
              "title": "风暴将至",
              "description": "主角发现
"""

    result = svc._parse_llm_response(response)

    acts = result["parts"][0]["volumes"][0]["acts"]
    assert len(acts) == 2
    assert acts[0]["title"] == "初入京城"
    assert acts[1]["title"] == "风暴将至"


def test_parse_llm_response_repairs_missing_comma_between_fields():
    svc = _make_service()

    response = """{
  "parts": [
    {
      "title": "第一部"
      "volumes": [
        {
          "title": "卷一",
          "acts": []
        }
      ]
    }
  ],
  "theme": "权谋成长"
}"""

    result = svc._parse_llm_response(response)

    assert result["theme"] == "权谋成长"
    assert result["parts"][0]["title"] == "第一部"
    assert result["parts"][0]["volumes"][0]["title"] == "卷一"


def test_extract_outer_json_value_prefers_object_root_over_leading_array():
    text = '["noise"] {"parts": [], "theme": "x"}'

    result = _extract_outer_json_value(text)

    assert result == '{"parts": [], "theme": "x"}'


def test_incremental_macro_parts_trustworthy_rejects_single_char_titles():
    bad = [
        {
            "title": "第一部",
            "volumes": [
                {"title": "卷一", "acts": [{"title": "X", "description": ""}]},
            ],
        }
    ]
    assert _incremental_macro_parts_trustworthy(bad) is False

    ok = [
        {
            "title": "第一部",
            "volumes": [
                {"title": "卷一", "acts": [{"title": "初入山门", "description": ""}]},
            ],
        }
    ]
    assert _incremental_macro_parts_trustworthy(ok) is True


def test_try_parse_parts_from_llm_buffer_accepts_complete_valid_minimal_json():
    raw = '{"parts": [{"title": "第一部", "volumes": [{"title": "卷一", "acts": [{"title": "序幕", "description": ""}]}]}], "theme": "x"}'
    parts = _try_parse_parts_from_llm_buffer(raw)
    assert parts is not None
    assert parts[0]["title"] == "第一部"
    assert parts[0]["volumes"][0]["acts"][0]["title"] == "序幕"


def test_quick_macro_prompt_renders_with_cpms_node_variables(monkeypatch):
    captured = {}

    def fake_render_prompt(node_key, variables, fallback_system="", fallback_user=""):
        captured["node_key"] = node_key
        captured["variables"] = variables
        return {
            "system": (
                "目标 {target_chapters} 章，推荐约 {total_recommended_acts} 幕，"
                "{recommended_parts} 部。{acts_output_instruction}"
            ).format_map(variables),
            "user": "{worldview_context}".format_map(variables),
        }

    monkeypatch.setattr(
        "application.blueprint.services.continuous_planning_service.render_prompt",
        fake_render_prompt,
    )

    svc = _make_service()
    bible_context = {
        "worldview": "- 基因武道: 资源会改变阶层",
        "characters": [{"name": "林渊", "role": "主角", "description": "谨慎求生"}],
        "locations": [{"name": "裂隙训练场", "description": "高压试炼场"}],
    }

    prompt = svc._build_quick_macro_prompt(bible_context, 80)

    assert "推荐约" in prompt.system
    assert "目标 80 章" in prompt.system
    assert captured["node_key"] == PLANNING_QUICK_MACRO
    assert captured["variables"]["recommended_parts"] == 1
    assert captured["variables"]["target_chapters"] == 80
    assert "worldview_context" not in prompt.user
    assert "基因武道" in prompt.user
    assert "林渊" in prompt.user
    assert "裂隙训练场" in prompt.user


def test_precise_macro_and_repair_prompts_render_with_cpms_nodes(monkeypatch):
    calls = []

    def fake_render_prompt(node_key, variables, fallback_system="", fallback_user=""):
        calls.append((node_key, variables))
        return {"system": f"SYS {node_key}", "user": str(variables)}

    monkeypatch.setattr(
        "application.blueprint.services.continuous_planning_service.render_prompt",
        fake_render_prompt,
    )

    svc = _make_service()
    skeleton = svc._build_precise_structure_skeleton(
        24,
        {"parts": 1, "volumes_per_part": 1, "acts_per_volume": 2},
    )
    bible_context = {
        "worldview": "- 基因武道: 资源会改变阶层",
        "characters": [{"name": "林渊", "role": "主角", "description": "谨慎求生"}],
    }

    precise = svc._build_precise_macro_prompt(
        bible_context,
        24,
        {"parts": 1, "volumes_per_part": 1, "acts_per_volume": 2},
        skeleton,
    )
    repair = svc._build_precise_repair_prompt(
        bible_context,
        24,
        {"parts": 1, "volumes_per_part": 1, "acts_per_volume": 2},
        [{"node_id": "A1_1_1", "title": "第一幕", "description": "开局", "missing_fields": ["narrative_goal"]}],
    )

    assert precise.system == f"SYS {PLANNING_MACRO_PRECISE}"
    assert repair.system == f"SYS {PLANNING_MACRO_REPAIR}"
    assert calls[0][0] == PLANNING_MACRO_PRECISE
    assert "skeleton_block" in calls[0][1]
    assert "A1_1_1" in calls[0][1]["skeleton_block"]
    assert calls[1][0] == PLANNING_MACRO_REPAIR
    assert "incomplete_acts_block" in calls[1][1]
    assert "narrative_goal" in calls[1][1]["incomplete_acts_block"]


def test_act_planning_prompt_renders_with_cpms_node(monkeypatch):
    captured = {}

    def fake_render_prompt(node_key, variables, fallback_system="", fallback_user=""):
        captured["node_key"] = node_key
        captured["variables"] = variables
        return {"system": "ACT SYS", "user": variables["context"]}

    monkeypatch.setattr(
        "application.blueprint.services.continuous_planning_service.render_prompt",
        fake_render_prompt,
    )

    svc = _make_service()
    act = Mock()
    act.title = "裂隙初启"
    act.description = "主角进入训练场"
    act.node_type = NodeType.ACT
    prompt = svc._build_act_planning_prompt(
        act,
        {
            "characters": [{"id": "char-1", "name": "林渊"}],
            "locations": [{"id": "loc-1", "name": "裂隙训练场"}],
        },
        previous_summary="前一幕结束在门外警报响起。",
        chapter_count=5,
    )

    assert prompt.system == "ACT SYS"
    assert captured["node_key"] == PLANNING_ACT
    assert captured["variables"]["chapter_count"] == 5
    assert "裂隙初启" in captured["variables"]["context"]
    assert "林渊" in captured["variables"]["context"]


@pytest.mark.asyncio
async def test_generate_macro_plan_precise_mode_repairs_missing_act_fields_and_rebalances_chapters():
    responses = [
        """{
          "node_updates": [
            {"node_id": "P1", "title": "寒门燃灯", "description": "寒门少年被卷入京师风暴"},
            {"node_id": "V1_1", "title": "初入京城", "description": "立足与试探"},
            {
              "node_id": "A1_1_1",
              "title": "雪夜叩门",
              "description": "主角深夜入京，撞见命案",
              "estimated_chapters": 7,
              "plot_points": ["进京", "撞见命案"],
              "setup_for": ["A1_1_2"],
              "payoff_from": []
            },
            {
              "node_id": "A1_1_2",
              "title": "朝堂余烬",
              "description": "主角被迫接触权贵",
              "estimated_chapters": 9,
              "narrative_goal": "建立主角与朝堂的冲突面",
              "plot_points": ["见权贵", "受逼迫"],
              "key_characters": ["主角-棋子"],
              "key_locations": ["都察院-压力源"],
              "emotional_arc": "紧张→压抑",
              "setup_for": ["A1_1_3"],
              "payoff_from": ["A1_1_1"]
            }
          ]
        }""",
        """{
          "node_updates": [
            {
              "node_id": "A1_1_1",
              "narrative_goal": "把主角拖入主线阴谋",
              "key_characters": ["主角-闯入者"],
              "key_locations": ["京城-漩涡入口"],
              "emotional_arc": "戒备→惊惧"
            }
          ]
        }""",
    ]
    response_iter = iter(responses)

    async def fake_stream_generate(*args, **kwargs):
        yield next(response_iter)

    llm_service = Mock()
    llm_service.stream_generate = fake_stream_generate
    svc = ContinuousPlanningService(
        story_node_repo=Mock(),
        chapter_element_repo=Mock(),
        llm_service=llm_service,
    )
    svc._get_bible_context = Mock(return_value={})

    result = await svc.generate_macro_plan(
        novel_id="novel-1",
        target_chapters=100,
        structure_preference={"parts": 1, "volumes_per_part": 5, "acts_per_volume": 5},
    )

    parts = result["structure"]
    assert len(parts) == 1
    assert len(parts[0]["volumes"]) == 5
    assert all(len(volume["acts"]) == 5 for volume in parts[0]["volumes"])

    first_volume = parts[0]["volumes"][0]
    assert parts[0]["title"] == "寒门燃灯"
    assert first_volume["title"] == "初入京城"
    assert first_volume["acts"][0]["title"] == "雪夜叩门"
    assert first_volume["acts"][1]["title"] == "朝堂余烬"
    assert first_volume["acts"][2]["title"] == "第3幕"
    assert first_volume["acts"][0]["narrative_goal"] == "把主角拖入主线阴谋"
    assert first_volume["acts"][0]["key_characters"] == ["主角-闯入者"]
    assert first_volume["acts"][0]["key_locations"] == ["京城-漩涡入口"]
    assert first_volume["acts"][0]["emotional_arc"] == "戒备→惊惧"

    all_acts = [act for volume in parts[0]["volumes"] for act in volume["acts"]]
    assert sum(act["estimated_chapters"] for act in all_acts) == 100

    progress = get_macro_plan_progress("novel-1")
    assert progress["status"] == "completed"
    assert progress["current"] == 5
    assert progress["total"] == 5
