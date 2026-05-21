"""llm_output_sanitize 单元测试。"""
from application.ai.llm_output_sanitize import (
    normalize_prose_punctuation,
    strip_prose_control_artifacts,
    strip_reasoning_artifacts,
)


def test_strip_redacted_reasoning_block():
    raw = (
        "<redacted_reasoning>内部推理</redacted_reasoning>"
        "第一章正文开始。"
    )
    assert strip_reasoning_artifacts(raw) == "第一章正文开始。"


def test_strip_think_tags():
    raw = "<think>step1</think>可见正文"
    assert strip_reasoning_artifacts(raw) == "可见正文"


def test_strip_think_tags_with_pipe():
    raw = "<think|>step1</think|>可见正文"
    assert strip_reasoning_artifacts(raw) == "可见正文"


def test_strip_thinking_tags():
    raw = "<thinking>plan</thinking>后文"
    assert strip_reasoning_artifacts(raw) == "后文"


def test_strip_bracket_thinking_case_insensitive():
    raw = "[thinking]x[/thinking]Y"
    assert strip_reasoning_artifacts(raw) == "Y"


def test_empty_and_no_tags():
    assert strip_reasoning_artifacts("") == ""
    assert strip_reasoning_artifacts("纯正文") == "纯正文"


def test_strip_status_panel_line_from_prose():
    raw = (
        "SYNAPSE-00 / STATUS: ACTIVE / HOST: NULL / EXTERNAL SYNC: LOST\n"
        "林渊睁开眼，听见灯管在头顶轻轻发抖。"
    )
    assert strip_prose_control_artifacts(raw) == "林渊睁开眼，听见灯管在头顶轻轻发抖。"


def test_strip_partial_status_fragment_from_streaming_text():
    raw = "SYNAPSE-00\n林渊睁开眼，听见灯管在头顶轻轻发抖。"
    assert strip_prose_control_artifacts(raw) == "林渊睁开眼，听见灯管在头顶轻轻发抖。"


def test_strip_scene_label_prefix_but_keep_sentence():
    raw = "画面一：林渊睁开眼，铁床的冷意贴着后背。\n镜头2：门外传来脚步声。"
    assert strip_prose_control_artifacts(raw) == "林渊睁开眼，铁床的冷意贴着后背。\n门外传来脚步声。"


def test_keep_legitimate_scene_wording():
    raw = "画面一片白。林渊眨了眨眼。"
    assert strip_prose_control_artifacts(raw) == raw


def test_normalize_prose_punctuation_replaces_dash():
    raw = "医生停了一下——那不是犹豫。门外的灯亮起——。"
    assert normalize_prose_punctuation(raw) == "医生停了一下，那不是犹豫。门外的灯亮起。"
