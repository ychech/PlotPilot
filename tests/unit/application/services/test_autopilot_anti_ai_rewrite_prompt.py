from types import SimpleNamespace
from unittest.mock import Mock

from application.engine.services.autopilot_daemon import AutopilotDaemon


def test_anti_ai_rewrite_prompt_accepts_user_only_fragments(monkeypatch):
    """Anti-AI 片段节点允许空 system，不应中断结构性重写。"""

    class FakeRegistry:
        def render(self, node_key, variables=None):
            return SimpleNamespace(system="", user=f"{node_key} user fragment")

        def render_to_prompt(self, node_key, variables=None):  # pragma: no cover
            raise AssertionError("fragment nodes must not use render_to_prompt")

    monkeypatch.setattr(
        "infrastructure.ai.prompt_registry.get_prompt_registry",
        lambda: FakeRegistry(),
    )

    daemon = AutopilotDaemon(
        novel_repository=Mock(),
        llm_service=Mock(),
        context_builder=Mock(),
        background_task_service=Mock(),
        planning_service=Mock(),
        story_node_repo=Mock(),
        chapter_repository=Mock(),
    )
    novel = SimpleNamespace(novel_id=SimpleNamespace(value="novel-1"))
    chapter = SimpleNamespace(
        number=1,
        outline="主角被困，必须脱身。",
        pov_character="云泽",
    )
    anti_report = SimpleNamespace(
        metrics=SimpleNamespace(
            overall_assessment="严重",
            top_patterns=["破折号", "结构空转"],
        )
    )

    prompt = daemon._build_anti_ai_rewrite_prompt(
        novel,
        chapter,
        "他被绑在树上。",
        anti_report,
    )

    assert prompt.system.strip()
    assert "章节结构性重写编辑" in prompt.system
    assert "anti-ai-behavior-protocol user fragment" in prompt.user
    assert "anti-ai-character-state-lock user fragment" in prompt.user


def test_anti_ai_rewrite_skips_single_low_risk_style_category():
    daemon = AutopilotDaemon(
        novel_repository=Mock(),
        llm_service=Mock(),
        context_builder=Mock(),
        background_task_service=Mock(),
        planning_service=Mock(),
        story_node_repo=Mock(),
        chapter_repository=Mock(),
    )
    report = SimpleNamespace(
        metrics=SimpleNamespace(
            overall_assessment="严重",
            severity_score=63.2,
            critical_hits=0,
            category_distribution={"句式": 8},
        )
    )

    assert daemon._should_attempt_anti_ai_rewrite(report) is False


def test_anti_ai_rewrite_runs_for_high_score_with_serious_categories():
    daemon = AutopilotDaemon(
        novel_repository=Mock(),
        llm_service=Mock(),
        context_builder=Mock(),
        background_task_service=Mock(),
        planning_service=Mock(),
        story_node_repo=Mock(),
        chapter_repository=Mock(),
    )
    report = SimpleNamespace(
        metrics=SimpleNamespace(
            overall_assessment="严重",
            severity_score=72.0,
            critical_hits=0,
            category_distribution={"情绪": 4, "句式": 3},
        )
    )

    assert daemon._should_attempt_anti_ai_rewrite(report) is True


def test_short_beat_output_requires_in_place_rewrite():
    daemon = AutopilotDaemon(
        novel_repository=Mock(),
        llm_service=Mock(),
        context_builder=Mock(),
        background_task_service=Mock(),
        planning_service=Mock(),
        story_node_repo=Mock(),
        chapter_repository=Mock(),
    )

    assert daemon._is_beat_too_short("云泽停住。", 357, is_final_beat=False) is True
    assert daemon._is_beat_too_short("云泽停住。" * 80, 357, is_final_beat=False) is False
