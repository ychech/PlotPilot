from application.engine.services.autopilot_daemon import AutopilotDaemon
from domain.structure.story_node import NodeType, StoryNode


def test_fallback_act_chapter_plan_extracts_bounded_chapters():
    daemon = AutopilotDaemon(
        novel_repository=None,
        llm_service=None,
        context_builder=None,
        background_task_service=None,
        planning_service=None,
        story_node_repo=None,
        chapter_repository=None,
    )
    act = StoryNode(
        id="act-n1-1",
        novel_id="n1",
        node_type=NodeType.ACT,
        number=1,
        title="第1幕：灰瞳之下",
        description=(
            "[标签：重生觉醒/系统附身/监控囚笼]"
            "林渊在黑潮后的病房苏醒，发现自己被赵镜全天候监控。"
            "复健时他察觉海水黏度异常。"
            "刺探监控边界时遭遇惩戒。"
            "最终获得基因武道系统。"
        ),
        order_index=1,
    )

    chapters = daemon._build_fallback_act_chapter_plan(
        target_act=act,
        requested_count=99,
        act_number=1,
    )

    assert len(chapters) == 12
    assert chapters[0]["title"] == "第1章：林渊在黑潮后的病房苏"
    assert "赵镜全天候监控" in chapters[1]["outline"]
    assert "local_fallback" == chapters[0]["metadata"]["planning_source"]
    assert chapters[-1]["title"] == "第12章：钩子"


def test_fallback_act_chapter_plan_uses_minimum_when_count_invalid():
    daemon = AutopilotDaemon(
        novel_repository=None,
        llm_service=None,
        context_builder=None,
        background_task_service=None,
        planning_service=None,
        story_node_repo=None,
        chapter_repository=None,
    )
    act = StoryNode(
        id="act-n1-2",
        novel_id="n1",
        node_type=NodeType.ACT,
        number=2,
        title="第二幕",
        description="",
        order_index=2,
    )

    chapters = daemon._build_fallback_act_chapter_plan(
        target_act=act,
        requested_count="bad",
        act_number=2,
    )

    assert len(chapters) == 3
    assert chapters[0]["outline"].startswith("围绕“第二幕”推进")


def test_basic_chapter_ending_rejects_dangling_action_tail():
    daemon = AutopilotDaemon(
        novel_repository=None,
        llm_service=None,
        context_builder=None,
        background_task_service=None,
        planning_service=None,
        story_node_repo=None,
        chapter_repository=None,
    )

    reasons = daemon._assess_basic_chapter_ending("医生没有再说话。\n\n林渊抬起头")

    assert "章节结尾疑似句子未写完" in reasons
    assert "章节结尾停在动作或发现刚开始的位置" in reasons


def test_basic_chapter_ending_accepts_closed_sentence():
    daemon = AutopilotDaemon(
        novel_repository=None,
        llm_service=None,
        context_builder=None,
        background_task_service=None,
        planning_service=None,
        story_node_repo=None,
        chapter_repository=None,
    )

    assert daemon._assess_basic_chapter_ending("林渊抬起头，看见门缝里的水雾重新凝成一线。") == []
