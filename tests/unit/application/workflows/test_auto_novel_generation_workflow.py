"""AutoNovelGenerationWorkflow 单元测试"""
import pytest
from unittest.mock import Mock, AsyncMock
from application.workflows.auto_novel_generation_workflow import (
    AutoNovelGenerationWorkflow,
    CHAPTER_CONTEXT_LAYER2_HEADER,
    CHAPTER_CONTEXT_LAYER3_HEADER,
    assemble_chapter_bundle_context_text,
    estimate_chapter_context_tokens,
)
from application.engine.dtos.generation_result import GenerationResult
from application.engine.dtos.scene_director_dto import SceneDirectorAnalysis
from application.engine.services.context_builder import ContextBuilder
from domain.novel.services.consistency_checker import ConsistencyChecker
from domain.novel.services.storyline_manager import StorylineManager
from domain.novel.repositories.plot_arc_repository import PlotArcRepository
from domain.novel.value_objects.consistency_report import ConsistencyReport, Issue, IssueType, Severity
from domain.novel.value_objects.chapter_state import ChapterState
from domain.ai.services.llm_service import LLMService, GenerationResult as LLMResult
from domain.ai.value_objects.token_usage import TokenUsage
from domain.novel.value_objects.novel_id import NovelId


@pytest.fixture
def mock_context_builder():
    """Mock ContextBuilder"""
    builder = Mock(spec=ContextBuilder)
    builder.build_structured_context.return_value = {
        "layer1_text": "Layer 1 context",
        "layer2_text": "Layer 2 context",
        "layer3_text": "Layer 3 context",
        "token_usage": {
            "layer1": 1250,
            "layer2": 5500,
            "layer3": 2500,
            "total": 9250
        }
    }
    # 默认空节拍列表 → 走整段生成分支（与历史单测期望一致）
    builder.magnify_outline_to_beats = Mock(return_value=[])
    # 不再需要 estimate_tokens 方法
    return builder


@pytest.fixture
def mock_consistency_checker():
    """Mock ConsistencyChecker"""
    checker = Mock(spec=ConsistencyChecker)
    checker.check_all = Mock(return_value=ConsistencyReport(
        issues=[],
        warnings=[],
        suggestions=[]
    ))
    return checker


@pytest.fixture
def mock_storyline_manager():
    """Mock StorylineManager"""
    manager = Mock(spec=StorylineManager)
    manager.repository = Mock()
    manager.repository.get_by_novel_id.return_value = []
    manager.get_storyline_context.return_value = "Main storyline context"
    return manager


@pytest.fixture
def mock_plot_arc_repository():
    """Mock PlotArcRepository"""
    repo = Mock(spec=PlotArcRepository)
    return repo


async def _mock_stream_generate(*args, **kwargs):
    yield "Generated chapter content"


@pytest.fixture
def mock_llm_service():
    """Mock LLMService"""
    service = Mock(spec=LLMService)
    service.generate = AsyncMock(return_value=LLMResult(
        content="Generated chapter content",
        token_usage=TokenUsage(input_tokens=500, output_tokens=500)
    ))
    service.stream_generate = _mock_stream_generate
    return service


@pytest.fixture
def workflow(
    mock_context_builder,
    mock_consistency_checker,
    mock_storyline_manager,
    mock_plot_arc_repository,
    mock_llm_service
):
    """创建 AutoNovelGenerationWorkflow 实例"""
    return AutoNovelGenerationWorkflow(
        context_builder=mock_context_builder,
        consistency_checker=mock_consistency_checker,
        storyline_manager=mock_storyline_manager,
        plot_arc_repository=mock_plot_arc_repository,
        llm_service=mock_llm_service
    )


def test_assemble_chapter_bundle_context_text_uses_t2_t3_headers():
    payload = {
        "layer1_text": "角色锚点：林渊不能改名",
        "layer2_text": "- 已发生结果：林渊确认被监控",
        "layer3_text": "[第 3 章] 待回收伏笔仍未揭露",
    }
    s = assemble_chapter_bundle_context_text(payload)
    assert f"=== {CHAPTER_CONTEXT_LAYER2_HEADER} ===" in s
    assert f"=== {CHAPTER_CONTEXT_LAYER3_HEADER} ===" in s
    assert "林渊不能改名" in s
    assert "林渊确认被监控" in s
    assert "待回收伏笔仍未揭露" in s


class TestGenerateChapter:
    """测试 generate_chapter 方法"""

    @pytest.mark.asyncio
    async def test_generate_chapter_success(self, workflow, mock_context_builder, mock_llm_service):
        """测试成功生成章节"""
        mock_context_builder.magnify_outline_to_beats.return_value = []
        result = await workflow.generate_chapter(
            novel_id="novel-1",
            chapter_number=1,
            outline="Chapter 1 outline"
        )

        # 验证返回结果
        assert isinstance(result, GenerationResult)
        assert result.content == "Generated chapter content"
        assert result.token_count == estimate_chapter_context_tokens(result.context_used)
        assert f"=== {CHAPTER_CONTEXT_LAYER2_HEADER} ===" in result.context_used
        assert f"=== {CHAPTER_CONTEXT_LAYER2_HEADER} ===" in result.context_used
        assert f"=== {CHAPTER_CONTEXT_LAYER3_HEADER} ===" in result.context_used
        assert isinstance(result.consistency_report, ConsistencyReport)

        # 验证调用顺序
        mock_context_builder.build_structured_context.assert_called_once_with(
            novel_id="novel-1",
            chapter_number=1,
            outline="Chapter 1 outline",
            max_tokens=5000,
            scene_director=None
        )
        # 验证 LLM 被调用：至少一次用于生成章节，可能还有一次用于状态提取
        assert mock_llm_service.generate.call_count >= 1

    @pytest.mark.asyncio
    async def test_generate_chapter_with_scene_director(self, workflow, mock_context_builder, mock_llm_service):
        """测试使用 scene_director 参数生成章节"""
        mock_context_builder.magnify_outline_to_beats.return_value = []
        scene_director = SceneDirectorAnalysis(
            characters=["Alice", "Bob"],
            locations=["Room A"],
            action_types=["dialogue", "action"],
            trigger_keywords=["conflict"],
            emotional_state="tense",
            pov="Alice"
        )

        result = await workflow.generate_chapter(
            novel_id="novel-1",
            chapter_number=1,
            outline="Chapter 1 outline",
            scene_director=scene_director
        )

        # 验证返回结果
        assert isinstance(result, GenerationResult)
        assert result.content == "Generated chapter content"

        # 验证 build_structured_context 被调用时传入了 scene_director
        mock_context_builder.build_structured_context.assert_called_once_with(
            novel_id="novel-1",
            chapter_number=1,
            outline="Chapter 1 outline",
            max_tokens=5000,
            scene_director=scene_director
        )

    @pytest.mark.asyncio
    async def test_generate_chapter_invalid_chapter_number(self, workflow):
        """测试无效的章节号"""
        with pytest.raises(ValueError, match="chapter_number must be positive"):
            await workflow.generate_chapter(
                novel_id="novel-1",
                chapter_number=0,
                outline="Chapter outline"
            )

    @pytest.mark.asyncio
    async def test_generate_chapter_empty_outline(self, workflow):
        """测试空大纲"""
        with pytest.raises(ValueError, match="outline cannot be empty"):
            await workflow.generate_chapter(
                novel_id="novel-1",
                chapter_number=1,
                outline=""
            )


class TestGenerateChapterWithReview:
    """测试 generate_chapter_with_review 方法"""

    @pytest.mark.asyncio
    async def test_generate_with_review_success(self, workflow):
        """测试带审查的生成成功"""
        content, report = await workflow.generate_chapter_with_review(
            novel_id="novel-1",
            chapter_number=1,
            outline="Chapter 1 outline"
        )

        assert content == "Generated chapter content"
        assert isinstance(report, ConsistencyReport)
        assert not report.has_critical_issues()


class TestSuggestOutline:
    """测试 suggest_outline"""

    @pytest.mark.asyncio
    async def test_suggest_outline_returns_llm_text(self, workflow, mock_context_builder, mock_llm_service):
        mock_context_builder.build_context = Mock(return_value="Mock context")
        mock_llm_service.generate = AsyncMock(
            return_value=LLMResult(
                content="1. 开场\n2. 转折",
                token_usage=TokenUsage(input_tokens=10, output_tokens=20),
            )
        )
        text = await workflow.suggest_outline("novel-1", 3)
        assert "开场" in text
        mock_llm_service.generate.assert_called_once()


class TestGenerateChapterStream:
    """测试 generate_chapter_stream 流式事件"""

    @pytest.mark.asyncio
    async def test_stream_emits_phases_chunk_and_done(self, workflow):
        workflow.context_builder.magnify_outline_to_beats.return_value = []
        events = []
        async for e in workflow.generate_chapter_stream("novel-1", 1, "Chapter outline"):
            events.append(e)
        types = [x["type"] for x in events]
        assert "phase" in types
        assert "chunk" in types
        assert events[-1]["type"] == "done"
        assert events[-1]["content"] == "Generated chapter content"
        expected_context = assemble_chapter_bundle_context_text(workflow.context_builder.build_structured_context.return_value)
        assert events[-1]["token_count"] == estimate_chapter_context_tokens(expected_context)

    def test_chapter_generation_config_prioritizes_output_budget(self, workflow):
        cfg = workflow._chapter_generation_config(10_000)

        assert cfg.max_tokens >= 30_000

    @pytest.mark.asyncio
    async def test_generate_chapter_uses_beats_by_default(
        self,
        workflow,
        mock_context_builder,
        mock_llm_service,
    ):
        from application.engine.services.context_builder import Beat

        mock_context_builder.magnify_outline_to_beats.return_value = [
            Beat(description="开场冲突", target_words=900, focus="dialogue"),
            Beat(description="阶段结果", target_words=900, focus="action"),
        ]
        mock_context_builder.build_beat_prompt.side_effect = lambda beat, i, total: beat.description

        result = await workflow.generate_chapter("novel-1", 1, "Chapter outline")

        assert result.content == "Generated chapter content"
        assert mock_context_builder.magnify_outline_to_beats.called
        assert mock_llm_service.generate.call_count >= 2

    @pytest.mark.asyncio
    async def test_stream_emits_retrospective_beats_when_beats_disabled(self, workflow):
        events = []
        async for e in workflow.generate_chapter_stream(
            "novel-1",
            1,
            "Chapter outline",
            enable_beats=False,
        ):
            events.append(e)

        beats_events = [e for e in events if e["type"] == "beats_generated"]
        assert beats_events
        assert beats_events[-1]["beats"][0]["description"]
        assert events[-1]["type"] == "done"
        assert events[-1]["beats"] == beats_events[-1]["beats"]


class TestExtractChapterState:
    """测试 _extract_chapter_state 方法"""

    @pytest.mark.asyncio
    async def test_extract_chapter_state_from_content(self, workflow):
        """测试从内容中提取章节状态"""
        content = "Chapter content with character actions"

        state = await workflow._extract_chapter_state(content, chapter_number=1)

        assert isinstance(state, ChapterState)
        # 基本实现应该返回空列表
        assert isinstance(state.new_characters, list)
        assert isinstance(state.character_actions, list)
        assert isinstance(state.relationship_changes, list)


class TestBuildPrompt:
    """测试 _build_prompt 方法"""

    def test_build_prompt_with_context(self, workflow):
        """测试构建提示词"""
        prompt = workflow._build_prompt(
            context="Full context",
            outline="Chapter outline"
        )

        assert "Full context" in prompt.system
        assert "Chapter outline" in prompt.user
        assert "行文目标" in prompt.system

    def test_build_prompt_includes_storyline_and_tension(self, workflow):
        """故事线与情节张力应进入 system，供模型遵守"""
        prompt = workflow._build_prompt(
            context="CTX",
            outline="OL",
            storyline_context="主线：本章需触及 X",
            plot_tension="Expected tension: HIGH",
        )
        assert "主线" in prompt.system
        assert "HIGH" in prompt.system
        assert "CTX" in prompt.system

class TestConflictDetectionIntegration:
    """测试冲突检测集成"""

    @pytest.mark.asyncio
    async def test_generate_chapter_includes_ghost_annotations(
        self,
        mock_context_builder,
        mock_consistency_checker,
        mock_storyline_manager,
        mock_plot_arc_repository,
        mock_llm_service
    ):
        """测试生成章节时包含幽灵批注"""
        from application.services.conflict_detection_service import ConflictDetectionService
        from application.dtos.ghost_annotation import GhostAnnotation
        from domain.bible.repositories.bible_repository import BibleRepository
        from domain.bible.entities.bible import Bible
        from domain.bible.entities.character import Character
        from domain.novel.value_objects.novel_id import NovelId

        # Mock ConflictDetectionService
        mock_conflict_service = Mock(spec=ConflictDetectionService)
        mock_conflict_service.detect.return_value = [
            GhostAnnotation(
                type="setting_conflict",
                severity="warning",
                message="设定库中李明为 [水系]，此处使用了 [火系]",
                entity_id="char-001",
                entity_name="李明",
                expected="水系",
                actual="火系"
            )
        ]

        # Mock BibleRepository
        mock_bible_repo = Mock(spec=BibleRepository)
        mock_bible = Mock(spec=Bible)
        mock_bible.characters = [
            Mock(spec=Character, id="char-001", name="李明", description="水系法师", attributes={})
        ]
        mock_bible.locations = []
        mock_bible_repo.get_by_novel_id.return_value = mock_bible

        # 创建带冲突检测的工作流
        workflow = AutoNovelGenerationWorkflow(
            context_builder=mock_context_builder,
            consistency_checker=mock_consistency_checker,
            storyline_manager=mock_storyline_manager,
            plot_arc_repository=mock_plot_arc_repository,
            llm_service=mock_llm_service,
            conflict_detection_service=mock_conflict_service,
            bible_repository=mock_bible_repo
        )

        result = await workflow.generate_chapter(
            novel_id="novel-1",
            chapter_number=1,
            outline="李明释放火球术攻击敌人"
        )

        # 验证返回结果包含批注
        assert isinstance(result, GenerationResult)
        assert len(result.ghost_annotations) == 1
        assert result.ghost_annotations[0].type == "setting_conflict"
        assert result.ghost_annotations[0].severity == "warning"
        assert "李明" in result.ghost_annotations[0].message
        assert "水系" in result.ghost_annotations[0].message
        assert "火系" in result.ghost_annotations[0].message

        # 验证冲突检测服务被调用；质量门禁触发修稿时会对最终稿再审一次
        assert mock_conflict_service.detect.call_count >= 1

    @pytest.mark.asyncio
    async def test_generate_chapter_no_annotations_when_no_conflicts(
        self,
        mock_context_builder,
        mock_consistency_checker,
        mock_storyline_manager,
        mock_plot_arc_repository,
        mock_llm_service
    ):
        """测试无冲突时返回空批注列表"""
        from application.services.conflict_detection_service import ConflictDetectionService

        # Mock ConflictDetectionService 返回空列表
        mock_conflict_service = Mock(spec=ConflictDetectionService)
        mock_conflict_service.detect.return_value = []

        workflow = AutoNovelGenerationWorkflow(
            context_builder=mock_context_builder,
            consistency_checker=mock_consistency_checker,
            storyline_manager=mock_storyline_manager,
            plot_arc_repository=mock_plot_arc_repository,
            llm_service=mock_llm_service,
            conflict_detection_service=mock_conflict_service
        )

        result = await workflow.generate_chapter(
            novel_id="novel-1",
            chapter_number=1,
            outline="李明与王总对话"
        )

        # 验证返回空批注列表
        assert isinstance(result, GenerationResult)
        assert len(result.ghost_annotations) == 0

    @pytest.mark.asyncio
    async def test_generate_chapter_without_conflict_service(
        self,
        workflow
    ):
        """测试没有冲突检测服务时不报错"""
        # workflow fixture 默认没有 conflict_detection_service
        result = await workflow.generate_chapter(
            novel_id="novel-1",
            chapter_number=1,
            outline="Chapter outline"
        )

        # 验证不报错，返回空批注列表
        assert isinstance(result, GenerationResult)
        assert len(result.ghost_annotations) == 0

    @pytest.mark.asyncio
    async def test_generate_chapter_stream_includes_ghost_annotations(
        self,
        mock_context_builder,
        mock_consistency_checker,
        mock_storyline_manager,
        mock_plot_arc_repository,
        mock_llm_service
    ):
        """测试流式生成时包含幽灵批注"""
        from application.services.conflict_detection_service import ConflictDetectionService
        from application.dtos.ghost_annotation import GhostAnnotation

        # Mock ConflictDetectionService
        mock_conflict_service = Mock(spec=ConflictDetectionService)
        mock_conflict_service.detect.return_value = [
            GhostAnnotation(
                type="setting_conflict",
                severity="warning",
                message="测试批注",
                entity_id="char-001",
                entity_name="测试角色"
            )
        ]

        workflow = AutoNovelGenerationWorkflow(
            context_builder=mock_context_builder,
            consistency_checker=mock_consistency_checker,
            storyline_manager=mock_storyline_manager,
            plot_arc_repository=mock_plot_arc_repository,
            llm_service=mock_llm_service,
            conflict_detection_service=mock_conflict_service
        )

        events = []
        async for event in workflow.generate_chapter_stream(
            novel_id="novel-1",
            chapter_number=1,
            outline="测试大纲"
        ):
            events.append(event)

        # 验证最后的 done 事件包含批注
        done_event = events[-1]
        assert done_event["type"] == "done"
        assert "ghost_annotations" in done_event
        assert len(done_event["ghost_annotations"]) == 1
        assert done_event["ghost_annotations"][0]["type"] == "setting_conflict"
        assert done_event["ghost_annotations"][0]["message"] == "测试批注"


class TestStyleIntegration:
    """测试风格指纹和俗套扫描集成"""

    @pytest.mark.asyncio
    async def test_generate_chapter_includes_style_warnings(
        self,
        mock_context_builder,
        mock_consistency_checker,
        mock_storyline_manager,
        mock_plot_arc_repository,
        mock_llm_service
    ):
        """测试生成章节时包含风格警告"""
        from application.services.cliche_scanner import ClicheScanner, ClicheHit

        # Mock ClicheScanner
        mock_scanner = Mock(spec=ClicheScanner)
        mock_scanner.scan_cliches.return_value = [
            ClicheHit(
                pattern="熊熊系列",
                text="熊熊烈火",
                start=10,
                end=14,
                severity="warning"
            ),
            ClicheHit(
                pattern="眼神闪过系列",
                text="眼中闪过一丝",
                start=50,
                end=57,
                severity="warning"
            )
        ]

        # 创建带俗套扫描的工作流
        workflow = AutoNovelGenerationWorkflow(
            context_builder=mock_context_builder,
            consistency_checker=mock_consistency_checker,
            storyline_manager=mock_storyline_manager,
            plot_arc_repository=mock_plot_arc_repository,
            llm_service=mock_llm_service,
            cliche_scanner=mock_scanner
        )

        result = await workflow.generate_chapter(
            novel_id="novel-1",
            chapter_number=1,
            outline="测试大纲"
        )

        # 验证返回结果包含风格警告
        assert isinstance(result, GenerationResult)
        assert len(result.style_warnings) == 2
        assert result.style_warnings[0].pattern == "熊熊系列"
        assert result.style_warnings[0].text == "熊熊烈火"
        assert result.style_warnings[1].pattern == "眼神闪过系列"

        # 验证扫描器被调用；质量门禁触发修稿时会对最终稿再扫一次
        mock_scanner.scan_cliches.assert_any_call("Generated chapter content")

    @pytest.mark.asyncio
    async def test_generate_chapter_injects_fingerprint_summary(
        self,
        mock_context_builder,
        mock_consistency_checker,
        mock_storyline_manager,
        mock_plot_arc_repository,
        mock_llm_service
    ):
        """测试生成章节时注入风格指纹摘要"""
        from application.services.voice_fingerprint_service import VoiceFingerprintService
        from domain.novel.repositories.voice_fingerprint_repository import VoiceFingerprintRepository

        # Mock VoiceFingerprintService
        mock_fingerprint_repo = Mock(spec=VoiceFingerprintRepository)
        mock_fingerprint_repo.get_by_novel.return_value = {
            "metrics": {
                "adjective_density": 0.052,
                "avg_sentence_length": 18.5,
                "sentence_count": 100
            },
            "sample_count": 10
        }

        mock_fingerprint_service = Mock(spec=VoiceFingerprintService)
        mock_fingerprint_service.fingerprint_repo = mock_fingerprint_repo

        # 创建带风格指纹的工作流
        workflow = AutoNovelGenerationWorkflow(
            context_builder=mock_context_builder,
            consistency_checker=mock_consistency_checker,
            storyline_manager=mock_storyline_manager,
            plot_arc_repository=mock_plot_arc_repository,
            llm_service=mock_llm_service,
            voice_fingerprint_service=mock_fingerprint_service
        )

        result = await workflow.generate_chapter(
            novel_id="novel-1",
            chapter_number=1,
            outline="测试大纲"
        )

        # 验证 LLM 被调用
        assert mock_llm_service.generate.called

        # 获取传递给 LLM 的 prompt（兼容位置参数或关键字 prompt=）
        prompt = None
        for call in mock_llm_service.generate.call_args_list:
            kw = call.kwargs or {}
            p = kw.get("prompt")
            if p is None and call.args:
                p = call.args[0]
            if p is not None and ("形容词密度" in (p.system or "") or "平均句长" in (p.system or "")):
                prompt = p
                break
        assert prompt is not None

        # 验证 prompt 包含风格指纹摘要
        assert "形容词密度" in prompt.system or "平均句长" in prompt.system

        # 验证指纹仓储被调用
        mock_fingerprint_repo.get_by_novel.assert_called_once_with("novel-1", pov_character_id=None)

    @pytest.mark.asyncio
    async def test_generate_chapter_without_style_services(
        self,
        workflow
    ):
        """测试没有风格服务时不报错"""
        # workflow fixture 默认没有 voice_fingerprint_service 和 cliche_scanner
        result = await workflow.generate_chapter(
            novel_id="novel-1",
            chapter_number=1,
            outline="测试大纲"
        )

        # 验证不报错，返回空风格警告列表
        assert isinstance(result, GenerationResult)
        assert len(result.style_warnings) == 0

    @pytest.mark.asyncio
    async def test_generate_chapter_stream_includes_style_warnings(
        self,
        mock_context_builder,
        mock_consistency_checker,
        mock_storyline_manager,
        mock_plot_arc_repository,
        mock_llm_service
    ):
        """测试流式生成时包含风格警告"""
        from application.services.cliche_scanner import ClicheScanner, ClicheHit

        # Mock ClicheScanner
        mock_scanner = Mock(spec=ClicheScanner)
        mock_scanner.scan_cliches.return_value = [
            ClicheHit(
                pattern="熊熊系列",
                text="熊熊烈火",
                start=10,
                end=14,
                severity="warning"
            )
        ]

        workflow = AutoNovelGenerationWorkflow(
            context_builder=mock_context_builder,
            consistency_checker=mock_consistency_checker,
            storyline_manager=mock_storyline_manager,
            plot_arc_repository=mock_plot_arc_repository,
            llm_service=mock_llm_service,
            cliche_scanner=mock_scanner
        )

        events = []
        async for event in workflow.generate_chapter_stream(
            novel_id="novel-1",
            chapter_number=1,
            outline="测试大纲"
        ):
            events.append(event)

        # 验证最后的 done 事件包含风格警告
        done_event = events[-1]
        assert done_event["type"] == "done"
        assert "style_warnings" in done_event
        assert len(done_event["style_warnings"]) == 1
        assert done_event["style_warnings"][0]["pattern"] == "熊熊系列"
        assert done_event["style_warnings"][0]["text"] == "熊熊烈火"


class TestQualityGate:
    """测试生成后质量门禁与上下文对齐注入"""

    @pytest.mark.asyncio
    async def test_generate_chapter_repairs_when_quality_gate_fails(
        self,
        mock_context_builder,
        mock_consistency_checker,
        mock_storyline_manager,
        mock_plot_arc_repository,
    ):
        from application.audit.services.cliche_scanner import ClicheHit

        llm = Mock(spec=LLMService)
        llm.generate = AsyncMock(side_effect=[
            LLMResult(content="短文 眼中闪过一丝", token_usage=TokenUsage(1, 1)),
            LLMResult(content="修订后的章节正文。" * 500, token_usage=TokenUsage(1, 1)),
        ])

        scanner = Mock()
        scanner.scan_cliches.side_effect = [
            [ClicheHit(pattern="眼神闪过系列", text="眼中闪过一丝", start=3, end=10, severity="critical")],
            [],
            [],
        ]

        workflow = AutoNovelGenerationWorkflow(
            context_builder=mock_context_builder,
            consistency_checker=mock_consistency_checker,
            storyline_manager=mock_storyline_manager,
            plot_arc_repository=mock_plot_arc_repository,
            llm_service=llm,
            cliche_scanner=scanner,
        )
        workflow.state_extractor = None

        result = await workflow.generate_chapter("novel-1", 1, "测试大纲")

        assert result.content.startswith("修订后的章节正文")
        assert result.quality_gate["repair_attempted"] is True
        assert result.quality_gate["repair_applied"] is True
        assert llm.generate.call_count >= 2

    @pytest.mark.asyncio
    async def test_stream_emits_quality_gate_event(
        self,
        workflow,
    ):
        events = []
        async for event in workflow.generate_chapter_stream("novel-1", 1, "测试大纲"):
            events.append(event)

        assert any(ev["type"] == "quality_gate" for ev in events)
        done = events[-1]
        assert done["type"] == "done"
        assert "quality_gate" in done

    def test_prompt_includes_context_alignment_protocol(self, workflow):
        prompt = workflow._build_prompt(
            "Layer 1\n\n=== RECENT CHAPTERS ===\nrecent\n\n=== VECTOR RECALL ===\nrecall",
            "测试大纲",
            chapter_target_words=2500,
        )

        assert "上下文对齐协议" in prompt.system
        assert "FACT_LOCK > Bible 正典" in prompt.system

    def test_assembled_context_is_deduped_and_focused(self):
        noisy_line = "重复事实：林渊左臂有追踪光环"
        payload = {
            "layer1_text": (
                "=== 🔒绝对事实边界(FACT_LOCK) ===\n"
                "角色锚点：林渊不能改名\n"
                f"{noisy_line}\n{noisy_line}\n"
                + "设定说明" * 4000
            ),
            "layer2_text": (
                "【章末节选，供本章开头承接】\n"
                "倒计时继续跳动，赵镜锁定了林渊的位置。\n"
                + "上一章正文" * 3000
            ),
            "layer3_text": (
                "【相关上下文（向量召回）】\n"
                "第3章：待回收伏笔仍未揭露。\n"
                + "远期片段" * 2000
            ),
            "token_usage": {"total": 30000, "layer1": 10000, "layer2": 10000, "layer3": 10000},
        }

        context = assemble_chapter_bundle_context_text(payload)

        assert "=== CONTEXT FOCUS ===" in context
        assert "角色锚点：林渊不能改名" in context
        assert "章末节选" in context
        assert context.count(noisy_line) <= 2
        assert f"{noisy_line}\n{noisy_line}" not in context
        assert "=== RECENT CHAPTERS ===" in context
        assert "=== VECTOR RECALL ===" in context
        assert "上一章正文" * 20 not in context
        assert "远期片段" * 20 not in context
        assert len(context) < 9000

    def test_assembled_context_extracts_brief_instead_of_raw_passages(self):
        payload = {
            "layer1_text": "=== 角色锚点 ===\n角色锚点：林渊不能改名\n无关设定" * 500,
            "layer2_text": (
                "【最近章节承接简报】\n"
                "第 1 章：灰瞳苏醒\n"
                "- 用途：上一章直接承接\n"
                "- 已发生结果：林渊确认自己被赵镜监控\n"
                "- 未解决压力：倒计时继续跳动，赵镜锁定了林渊的位置\n"
                "- 可接续画面：病房灯光发冷\n"
                + "正文原文" * 1000
            ),
            "layer3_text": (
                "【远期记忆提要（向量召回已简化）】\n"
                "[第 3 章] 待回收伏笔仍未揭露；赵镜的追踪权限来自协会\n"
                + "召回原文" * 1000
            ),
            "token_usage": {"total": 20000},
        }

        context = assemble_chapter_bundle_context_text(payload)

        assert "林渊确认自己被赵镜监控" in context
        assert "倒计时继续跳动" in context
        assert "待回收伏笔仍未揭露" in context
        assert "正文原文" * 20 not in context
        assert "召回原文" * 20 not in context

    def test_context_alignment_protocol_has_fallback_priority(self, workflow):
        protocol = workflow._build_context_alignment_protocol("context", "outline")

        assert "上下文对齐协议" in protocol
        assert "FACT_LOCK > Bible 正典" in protocol

    def test_prompt_does_not_encourage_dangling_ending(self, workflow):
        prompt = workflow._build_prompt("CTX", "测试大纲", chapter_target_words=2000)

        combined = prompt.system + "\n" + prompt.user
        assert "完整章节收束" in combined or "结尾要有落点" in combined
        assert "说到一半停了" not in combined
        assert "完整对白" in combined
        assert "亲历者" not in combined
        assert "第三人称限制视角" in combined

    def test_prompt_includes_saved_profile_lock(self, workflow):
        workflow._current_profile_lock = (
            "【故事内核锁（最高优先级）】\n"
            "题材/赛道：玄幻\n"
            "故事内核/梗概承诺：少年偶获神鼎，从此丹武双修，横扫八荒，成就无上神帝。"
        )

        prompt = workflow._build_prompt("CTX", "测试大纲", chapter_target_words=2000)

        assert "故事内核锁" in prompt.system
        assert "少年偶获神鼎" in prompt.system
        assert "丹武双修" in prompt.system

    def test_prompt_includes_style_and_theme_constraints(self, workflow):
        theme = Mock()
        theme.build_system_persona.return_value = "【作家风格】高武废土叙述者"
        theme.build_writing_rules.return_value = "【题材专项规则】力量体系要有压迫感"
        theme.build_format_rules.return_value = "【题材格式】第三人称限制视角"
        workflow._theme_integrator = theme

        prompt = workflow._build_prompt(
            "CTX",
            "测试大纲",
            style_summary="平均句长偏短，动作密度高。",
            voice_anchors="林渊：说话简短，紧张时按住左腕。",
            chapter_target_words=2500,
        )

        assert "【风格约束】" in prompt.system
        assert "平均句长偏短" in prompt.system
        assert "【角色声线与肢体语言" in prompt.system
        assert "林渊：说话简短" in prompt.system
        assert "【作家风格】高武废土叙述者" in prompt.system
        assert "【题材专项规则】力量体系要有压迫感" in prompt.system
        assert "{behavior_protocol}" not in prompt.system

    def test_main_prompt_uses_user_template_and_has_no_unknown_placeholders(self, workflow):
        prompt = workflow._build_prompt(
            "CTX",
            "林渊进入训练场，发现赵镜留下的追踪标记。",
            style_summary="文风冷峻克制，动作密度高。",
            chapter_target_words=2500,
        )

        combined = prompt.system + "\n" + prompt.user
        assert "【本章大纲 outline】" in prompt.user
        assert "【当前节拍 beat_section】" in prompt.user
        assert "非分节拍生成" in prompt.user
        assert "林渊进入训练场" in prompt.user
        assert "【风格约束】" in prompt.system
        assert "文风冷峻克制" in prompt.system
        assert "{style_convention}" not in combined
        assert "{chapter_outline}" not in combined
        assert "{outline}" not in combined
        assert "{beat_section}" not in combined


class TestCharacterCanonGuard:
    """测试角色正典锁与角色漂移门禁"""

    def _bible_with_character(self):
        from domain.bible.entities.bible import Bible
        from domain.bible.entities.character import Character
        from domain.bible.value_objects.character_id import CharacterId

        bible = Bible(id="bible-1", novel_id=NovelId("novel-1"))
        bible.add_character(Character(
            id=CharacterId("char-linyuan"),
            name="林渊",
            description="觉醒基因武道系统的主角，行事谨慎，擅长掠夺万物基因强化自身。",
            role="主角",
            mental_state="戒备",
            verbal_tic="说话简短，常先确认代价",
            idle_behavior="紧张时会按住左腕",
            core_belief="力量必须服务于活下去",
            moral_taboos=["不滥杀无辜"],
            hidden_profile="系统真正来源于旧纪元禁区",
            reveal_chapter=20,
        ))
        return bible

    def test_character_canon_contract_enters_prompt(self, workflow):
        repo = Mock()
        repo.get_by_novel_id.return_value = self._bible_with_character()
        workflow.bible_repository = repo
        workflow._current_character_canon_contract = workflow._build_character_canon_contract("novel-1", 3)

        prompt = workflow._build_prompt("CTX", "林渊进入训练场", chapter_target_words=2500)

        assert "角色正典锁" in prompt.system
        assert "林渊" in prompt.system
        assert "第20章" in prompt.system
        assert "角色公开信息按剧情时点推进" in prompt.system

    def test_character_canon_drift_flags_new_named_character(self, workflow):
        repo = Mock()
        repo.get_by_novel_id.return_value = self._bible_with_character()
        workflow.bible_repository = repo

        annotations = workflow._detect_character_canon_drift(
            novel_id="novel-1",
            chapter_number=3,
            content="林渊按住左腕。赵明站在门口看着他，说这里不欢迎外人。",
        )

        assert annotations
        assert annotations[0].type == "character_inconsistency"
        assert "赵明" in annotations[0].message

    def test_outline_old_protagonist_alias_normalizes_to_bible_name_without_name_specific_rule(self, workflow):
        repo = Mock()
        repo.get_by_novel_id.return_value = self._bible_with_character()
        workflow.bible_repository = repo

        outline = workflow._normalize_outline_against_character_canon(
            "novel-1",
            "林舟在裂隙边缘觉醒基因共鸣能力，并决定进入禁区。",
        )

        assert "林舟" not in outline
        assert "林渊" in outline

    def test_outline_does_not_replace_non_protagonist_same_surname_character(self, workflow):
        repo = Mock()
        repo.get_by_novel_id.return_value = self._bible_with_character()
        workflow.bible_repository = repo

        outline = workflow._normalize_outline_against_character_canon(
            "novel-1",
            "林渊进入训练场。林舟站在门口看着他，说这里不欢迎外人。",
        )

        assert "林渊进入训练场" in outline
        assert "林舟站在门口" in outline

    def test_incomplete_chapter_fails_completeness_gate(self, workflow):
        reasons = workflow._assess_chapter_completeness(
            content="林渊按住左腕，屏幕上的红光忽然亮起。他刚要开口",
            target_words=2000,
        )

        assert reasons
        assert any("未写完" in reason or "半截" in reason or "篇幅偏短" in reason for reason in reasons)

    def test_first_person_narration_fails_by_default(self, workflow):
        reasons = workflow._assess_narrative_person(
            content=(
                "我站在祠堂末尾，掌心全是汗。\n\n"
                "司空烈走到我面前，针尖贴住我的脖颈。\n\n"
                "我听见骨头里传来细碎的断裂声。"
            ),
            outline="林渊在家族觉醒仪式上被陷害，坠入血沼洲深渊。",
        )

        assert reasons
        assert "第一人称" in reasons[0]

    def test_first_person_allowed_when_outline_requires_it(self, workflow):
        reasons = workflow._assess_narrative_person(
            content="我站在祠堂末尾，掌心全是汗。我听见有人喊我的名字。",
            outline="本章采用第一人称，写林渊在家族觉醒仪式上被陷害。",
        )

        assert reasons == []

    def test_chapter_task_closure_rejects_new_event_dangling_tail(self, workflow):
        reasons = workflow._assess_chapter_task_closure(
            content=(
                "主角和谈判对象僵持了一整夜，桌上的冷茶换了三次。"
                "他终于把合同推回去，拒绝交出最后一份证据。"
                "门外突然传来脚步声，一个陌生人推开门走出。"
            ),
            outline="主角在谈判中拒绝交易，并发现幕后还有第三方介入。",
        )

        assert reasons
        assert any("阶段性结果" in reason for reason in reasons)

    def test_chapter_task_closure_rejects_system_prompt_as_only_ending(self, workflow):
        reasons = workflow._assess_chapter_task_closure(
            content=(
                "林渊冲过废弃训练场，终于把追兵甩进地下通道。"
                "他按住左腕，还没来得及确认伤口，系统提示忽然亮起。"
                "【检测到未知基因源，是否立刻吞噬？】"
            ),
            outline="林渊摆脱追兵，并在废弃训练场发现未知基因源。",
        )

        assert reasons
        assert any("阶段性结果" in reason for reason in reasons)

    def test_chapter_completeness_rejects_action_that_just_starts_at_tail(self, workflow):
        reasons = workflow._assess_chapter_completeness(
            content=(
                "林渊把旧钥匙压进掌心，终于确认禁区入口就在祠堂后墙。"
                "石门后传来脚步声，他抬手推开门。"
            ),
            target_words=200,
        )

        assert reasons
        assert any("半截动作" in reason or "突发事件" in reason for reason in reasons)

    def test_chapter_task_closure_accepts_result_before_hook(self, workflow):
        reasons = workflow._assess_chapter_task_closure(
            content=(
                "主角和谈判对象僵持了一整夜，桌上的冷茶换了三次。"
                "他终于把合同推回去，拒绝交出最后一份证据，交易当场破裂。"
                "门外突然传来脚步声，一个陌生人停在门口，没有再往里走。"
            ),
            outline="主角在谈判中拒绝交易，并发现幕后还有第三方介入。",
        )

        assert reasons == []

    def test_quality_gate_rejects_duplicate_paragraphs(self, workflow):
        gate = workflow._build_quality_gate(
            content=(
                "楼道里的灯又坏了两盏。\n"
                "周虎停止抛卡扣。\n"
                "小灰烬，我不为难你。\n"
                "周虎停止抛卡扣。\n"
                "小灰烬，我不为难你。\n"
            ),
            outline="周虎堵住主角，逼他交出防尘网。",
            target_words=200,
            style_warnings=[],
            consistency_report=ConsistencyReport(issues=[], warnings=[], suggestions=[]),
            ghost_annotations=[],
        )

        assert gate["passed"] is False
        assert gate["auto_repair_required"] is True
        assert any("重复段落" in reason for reason in gate["reasons"])
