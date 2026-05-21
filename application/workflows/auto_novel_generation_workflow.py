"""自动小说生成工作流

整合所有子项目组件，实现完整的章节生成流程。
"""
import asyncio
import logging
import re
from pathlib import Path
from typing import Tuple, Dict, Any, AsyncIterator, Optional, List, Callable, Awaitable
from application.engine.services.context_builder import ContextBuilder
from application.analyst.services.state_extractor import StateExtractor
from application.analyst.services.state_updater import StateUpdater
from application.audit.services.conflict_detection_service import ConflictDetectionService
from application.engine.services.style_constraint_builder import build_style_summary
from application.engine.dtos.generation_result import GenerationResult
from application.engine.dtos.scene_director_dto import SceneDirectorAnalysis
from application.audit.dtos.ghost_annotation import GhostAnnotation
from domain.novel.services.consistency_checker import ConsistencyChecker
from domain.novel.services.storyline_manager import StorylineManager
from domain.novel.repositories.plot_arc_repository import PlotArcRepository
from domain.bible.repositories.bible_repository import BibleRepository
from domain.novel.repositories.foreshadowing_repository import ForeshadowingRepository
from domain.novel.value_objects.consistency_report import ConsistencyReport, Severity
from domain.novel.value_objects.chapter_state import ChapterState
from domain.novel.value_objects.consistency_context import ConsistencyContext
from domain.novel.value_objects.novel_id import NovelId
from domain.ai.services.llm_service import LLMService, GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from application.ai.llm_output_sanitize import (
    normalize_prose_punctuation,
    strip_prose_control_artifacts,
    strip_reasoning_artifacts,
)
from application.ai.prose_fragment_aggregator import aggregate_inline_prose_fragments
from application.audit.services.final_draft_auditor import (
    audit_final_draft_text,
    remove_near_duplicate_paragraphs,
)
from application.workflows.beat_continuation import format_prior_draft_for_prompt
from application.workflows.prose_discipline import build_prose_discipline_block
from application.core.novel_profile_lock import build_novel_profile_lock
from application.core.premise_genre_world import parse_genre_world_from_premise
from application.engine.services.beat_coherence_enhancer import BeatCoherenceEnhancer, BeatContext
from application.engine.services.spatial_coherence import (
    DraftTopologyCommitGate,
    EscalatingBeatRetryDirector,
    StreamingSceneLeakGuard,
    initialize_micro_scene_context,
    refresh_micro_scene_context_after_beat,
)
from domain.novel.value_objects.action_transition_graph import ActionTransitionGraph
from domain.novel.value_objects.micro_scene_context import MicroSceneContext

from application.core.chapter_target_limits import clamp_chapter_target_words

logger = logging.getLogger(__name__)

_STREAM_POST_EXTRACT_TIMEOUT_SECONDS = 45.0
_STREAM_POST_MEMORY_TIMEOUT_SECONDS = 45.0


def _beats_for_sse(beats: List[Any]) -> List[Dict[str, Any]]:
    """指挥器微观节拍 → SSE / done 载荷（与前端 StreamGeneratedBeat 对齐）。"""
    out: List[Dict[str, Any]] = []
    for beat in beats or []:
        desc = (getattr(beat, "description", None) or getattr(beat, "scene_goal", None) or "").strip()
        if not desc:
            continue
        out.append(
            {
                "description": desc,
                "target_words": int(getattr(beat, "target_words", 0) or 0),
                "focus": (getattr(beat, "focus", None) or "pacing"),
                "location_id": getattr(beat, "location_id", "") or "",
            }
        )
    return out


# ─── 模板安全渲染工具 ───

class _SafeDict(dict):
    """format_map 专用字典：未匹配的变量保留为 {name} 占位符，不抛 KeyError。"""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _safe_format(template: str, variables: Dict[str, Any]) -> str:
    """安全模板渲染：缺失变量保留占位符，不抛异常。

    Args:
        template: 含 {variable} 占位符的模板字符串
        variables: 变量字典

    Returns:
        渲染后的字符串
    """
    if not template:
        return ""
    try:
        return template.format_map(_SafeDict(variables))
    except (KeyError, ValueError, IndexError):
        return template


def _read_prompt_package_template(node_key: str, filename: str) -> str:
    try:
        nodes_dir = Path(__file__).resolve().parents[2] / "infrastructure" / "ai" / "prompt_packages" / "nodes"
        return (nodes_dir / node_key / filename).read_text(encoding="utf-8")
    except Exception as exc:
        logger.debug("读取 prompt package 失败 node=%s file=%s: %s", node_key, filename, exc)
        return ""


def _looks_like_legacy_chapter_system_template(text: str) -> bool:
    return any(
        marker in (text or "")
        for marker in (
            "━━━ 输入材料",
            "爽文引擎约束（必须遵守）",
            "不要复述",
            "主角永远不能主动炫耀",
        )
    )


def _looks_like_legacy_chapter_user_template(text: str) -> bool:
    return any(
        marker in (text or "")
        for marker in (
            "必须遵守",
            "不得复述",
            "禁止停在",
        )
    )


def _render_package_prompt_text(node_key: str, variables: Dict[str, Any]) -> str:
    parts = []
    for filename in ("system.md", "user.md"):
        template = _read_prompt_package_template(node_key, filename)
        if template.strip():
            parts.append(_safe_format(template, variables).strip())
    return "\n\n".join(part for part in parts if part)


def _render_generation_prompt_text(node_key: str, variables: Dict[str, Any]) -> str:
    package_text = _render_package_prompt_text(node_key, variables)
    if package_text:
        return package_text
    return render_prompt_text(node_key, variables)


# CPMS: 主工作流提示词节点 key（与 prompt_packages 中节点 id 一致）
from infrastructure.ai.prompt_keys import (
    CHAPTER_BEAT_CONTINUATION as _CHAPTER_BEAT_CONTINUATION_NODE_KEY,
    CHAPTER_BEAT_TRANSITION_FALLBACK as _CHAPTER_BEAT_TRANSITION_FALLBACK_NODE_KEY,
    CHAPTER_CHARACTER_CANON_CONTRACT as _CHAPTER_CHARACTER_CANON_CONTRACT_NODE_KEY,
    CHAPTER_COMPLETION_CONTRACT as _CHAPTER_COMPLETION_CONTRACT_NODE_KEY,
    CHAPTER_CONTEXT_ALIGNMENT as _CHAPTER_CONTEXT_ALIGNMENT_NODE_KEY,
    CHAPTER_GENERATION_MAIN as _WORKFLOW_CHAPTER_GEN_NODE_KEY,
    CHAPTER_NAME_CANON_GUARD as _CHAPTER_NAME_CANON_GUARD_NODE_KEY,
    CHAPTER_OUTLINE_SUGGEST as _CHAPTER_OUTLINE_SUGGEST_NODE_KEY,
    CHAPTER_QUALITY_REPAIR as _CHAPTER_QUALITY_REPAIR_NODE_KEY,
    CHAPTER_REGENERATION_GUIDANCE as _CHAPTER_REGENERATION_GUIDANCE_NODE_KEY,
    CHAPTER_SHUANGWEN_DIRECTIVE as _CHAPTER_SHUANGWEN_DIRECTIVE_NODE_KEY,
)
from infrastructure.ai.prompt_utils import render_prompt, render_prompt_text

_CONTEXT_PRIORITY_ORDER = "FACT_LOCK > Bible 正典 > 故事线/里程碑 > 最近章节正文 > 向量召回 > 本章大纲措辞"
_PROSE_OUTPUT_TOKEN_CAP = 120_000
_PROSE_TOKEN_PER_CHINESE_WORD = 4.8
_PROSE_TOKEN_COMPLETION_BUFFER = 6000
_CHAPTER_CONTEXT_BUILDER_TOKEN_BUDGET = 5000

# 与 ContextBuilder.build_structured_context 映射：Layer1≈T0+T1，Layer2=T2，Layer3=T3
# 段名与语义对齐，避免「SMART RETRIEVAL」贴在近期正文等历史误标
CHAPTER_CONTEXT_LAYER2_HEADER = "RECENT CHAPTERS"  # T2 近期章节正文
CHAPTER_CONTEXT_LAYER3_HEADER = "VECTOR RECALL"  # T3 向量召回
_CONTEXT_FOCUS_HEADER = "CONTEXT FOCUS"
_CONTEXT_SECTION_LIMITS = {
    "layer1_text": 3600,
    "layer2_text": 1800,
    "layer3_text": 800,
}
_CONTEXT_FOCUS_LIMIT = 600


def _normalize_context_lines(text: str) -> List[str]:
    """Normalize context text and remove exact duplicate non-heading lines."""
    lines: List[str] = []
    seen: set[str] = set()
    for raw in (text or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            if lines and lines[-1] != "":
                lines.append("")
            continue
        key = re.sub(r"\s+", "", line)
        is_heading = line.lstrip().startswith(("===", "【", "第 "))
        if key in seen and not is_heading:
            continue
        if not is_heading:
            seen.add(key)
        lines.append(line)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _select_context_bullets(text: str, patterns: tuple[str, ...], *, limit: int, max_line: int = 180) -> str:
    """Extract context bullets by meaning; avoid injecting raw long passages."""
    selected: List[str] = []
    seen: set[str] = set()
    for raw in _normalize_context_lines(text):
        line = raw.strip()
        if len(line) < 6 or line.startswith("==="):
            continue
        if not any(re.search(pattern, line, re.I) for pattern in patterns):
            continue
        line = line[:max_line].rstrip()
        key = re.sub(r"\s+", "", line)
        if key in seen:
            continue
        seen.add(key)
        selected.append(f"- {line}")
        if len("\n".join(selected)) >= limit:
            break
    return "\n".join(selected)


def _build_context_brief_section(title: str, text: str, patterns: tuple[str, ...], *, limit: int) -> str:
    bullets = _select_context_bullets(text, patterns, limit=limit)
    return f"【{title}】\n{bullets}" if bullets else ""


def _extract_context_focus(payload: Dict[str, Any]) -> str:
    """Build a compact editor-facing focus block from noisy context layers."""
    combined = "\n".join(
        str(payload.get(key, "") or "")
        for key in ("layer1_text", "layer2_text", "layer3_text")
    )
    if not combined.strip():
        return ""

    focus_patterns = (
        r"FACT_LOCK|绝对事实|角色锚点|CONTEXT_BRIEF|编辑手记|主线锚点|创作契约",
        r"章末节选|本章已生成|供本章开头承接|断点续写|阶段结果|代价|倒计时|追踪|锁定",
        r"待回收伏笔|叙事备忘|DEBT|CAUSAL|已揭露线索|故事线上下文|汇流|揭露",
    )
    selected: List[str] = []
    seen: set[str] = set()
    for raw in re.split(r"\n+", combined):
        line = raw.strip()
        if len(line) < 8:
            continue
        if not any(re.search(pattern, line, re.I) for pattern in focus_patterns):
            continue
        key = re.sub(r"\s+", "", line)
        if key in seen:
            continue
        seen.add(key)
        selected.append(line[:220])
        if len("\n".join(selected)) >= _CONTEXT_FOCUS_LIMIT:
            break
    if not selected:
        return ""
    return "【本章上下文焦点】\n" + "\n".join(f"- {line}" for line in selected)


def _build_dynamic_coherence_rules(anchor) -> str:
    """基于前节拍尾部锚点动态生成连贯性建议（V9: 从约束变为参考）

    V9 改革：不再使用"必须"、"不能"等强制性语言。
    改为温和的建议，允许 AI 灵活处理节拍间过渡。

    Args:
        anchor: BeatTailAnchor 实例

    Returns:
        格式化的连贯性建议文本
    """
    # 基础规则（所有情况都适用）
    base_rules = [
        "1. 从上文最后的情节发展自然过渡",
    ]

    # 根据尾部状态追加建议（V9: 从"必须"变为"可以"）
    state_rules = {
        "对话中": [
            "2. 上文停在对白中间，可以先回应或延续该对话",
            "3. 对话中的情绪弦外之音建议承接",
            "4. 对话自然结束后再推进新情节",
        ],
        "动作中": [
            "2. 上文停在动作进行中，可以展示该动作的完成或结果",
            "3. 动作完成后写角色的反应（表情/心理/下一步）",
            "4. 也可以跳过动作结果，从后续反应开始",
        ],
        "悬念中": [
            "2. 上文留下了悬念，可以延续悬念的紧张感",
            "3. 可以暂时搁置悬念，从另一条线索开篇",
            "4. 如果合适，也可以揭晓悬念",
        ],
        "叙述中": [
            "2. 承接叙述的情绪惯性自然过渡",
            "3. 从叙述自然过渡到具体场景或对话",
            "4. 用环境细节或角色动作作为过渡桥梁",
        ],
        "场景转换": [
            "2. 新场景可以先用感官细节（画面/声音/温度）站稳脚跟",
            "3. 也可以直接进入新场景的对话/动作",
            "4. 时间跳跃是合法的文学技法",
        ],
    }

    extra = state_rules.get(anchor.tail_state, [
        "2. 保持相同的场景设置，除非节拍明确要求转换场景",
        "3. 人物情绪和状态应与上文保持一致并合理发展",
        "4. 如果上文以对话结尾，继续该对话；如果以动作结尾，展示后续",
    ])
    base_rules.extend(extra)

    # 情绪基调参考（V9: 删除"不能"措辞）
    mood_hints = {
        '紧张': "5. 情绪参考：上文紧张，紧张感通常会延续一段时间",
        '愤怒': "5. 情绪参考：上文愤怒，余怒未消是常见的",
        '悲伤': "5. 情绪参考：上文悲伤，悲伤有惯性",
        '悬疑': "5. 情绪参考：上文悬疑，保持悬念张力",
    }
    mood_hint = mood_hints.get(anchor.mood_tone)
    if mood_hint:
        base_rules.append(mood_hint)

    # 最后画面参考（V9: 不再说"必须接续"）
    if anchor.last_moment:
        base_rules.append(
            f"6. 📍 上文最后画面：……{anchor.last_moment}"
        )

    return "\n".join(base_rules)


def assemble_chapter_bundle_context_text(payload: Dict[str, Any]) -> str:
    """将结构化上下文提炼成章节可用简报，而不是注入原文剪辑。"""
    layer1_src = str(payload.get("layer1_text", "") or "")
    layer2_src = str(payload.get("layer2_text", "") or "")
    layer3_src = str(payload.get("layer3_text", "") or "")
    layer1 = "\n\n".join(
        part for part in (
            _build_context_brief_section(
                "硬约束 / 正典事实",
                layer1_src,
                (
                    r"FACT_LOCK|绝对事实|角色锚点|不能|不得|必须|死亡|身份|ID=|姓名|白名单|隐藏信息",
                    r"故事内核锁|创作契约|题材|文风|规则|主线锚点|ANCHOR",
                ),
                limit=_CONTEXT_SECTION_LIMITS["layer1_text"],
            ),
            _build_context_brief_section(
                "本章相关叙事备忘",
                layer1_src,
                (
                    r"编辑手记|CONTEXT_BRIEF|当前幕|阶段|代价|伏笔|线索|债务|因果|SCARS|DEBT|CLUES|CAUSAL",
                    r"心理|伤疤|执念|状态|关系|冲突|目标|压力",
                ),
                limit=1400,
            ),
        )
        if part
    )
    layer2 = _build_context_brief_section(
        "近期承接提要",
        layer2_src,
        (
            r"用途：|已发生结果：|未解决压力：|可接续画面：|承接点：|上一章直接承接|远期背景参考",
            r"第 \d+ 章|倒计时|锁定|追踪|监控|暴露|获得|确认|决定|失败|成功",
        ),
        limit=_CONTEXT_SECTION_LIMITS["layer2_text"],
    )
    layer3 = _build_context_brief_section(
        "远期记忆提要",
        layer3_src,
        (
            r"远期记忆|向量召回|第 \d+ 章|伏笔|线索|承诺|未揭露|关系|代价|系统|监控|追踪",
        ),
        limit=_CONTEXT_SECTION_LIMITS["layer3_text"],
    )
    focus = _extract_context_focus(payload)
    parts = []
    if focus:
        parts.append(f"=== {_CONTEXT_FOCUS_HEADER} ===\n{focus}")
    if layer1:
        parts.append(layer1)
    parts.append(f"=== {CHAPTER_CONTEXT_LAYER2_HEADER} ===\n{layer2}")
    parts.append(f"=== {CHAPTER_CONTEXT_LAYER3_HEADER} ===\n{layer3}")
    return "\n\n".join(parts).strip()


def estimate_chapter_context_tokens(context: str) -> int:
    """Estimate tokens after local context clipping; Chinese prose is close enough at ~2 chars/token."""
    cleaned = context or ""
    if not cleaned:
        return 0
    ascii_chars = sum(1 for ch in cleaned if ord(ch) < 128)
    non_ascii_chars = len(cleaned) - ascii_chars
    return max(1, int(non_ascii_chars / 2 + ascii_chars / 4))


def _consistency_report_to_dict(report: ConsistencyReport) -> Dict[str, Any]:
    """供 SSE / JSON 序列化。"""
    return {
        "issues": [
            {
                "type": issue.type.value,
                "severity": issue.severity.value,
                "description": issue.description,
                "location": issue.location,
            }
            for issue in report.issues
        ],
        "warnings": [
            {
                "type": w.type.value,
                "severity": w.severity.value,
                "description": w.description,
                "location": w.location,
            }
            for w in report.warnings
        ],
        "suggestions": list(report.suggestions),
    }


class AutoNovelGenerationWorkflow:
    """自动小说生成工作流

    整合所有组件完成完整的章节生成流程：
    1. Planning Phase: 获取故事线上下文、情节弧张力
    2. Pre-Generation: 使用 ContextBuilder 构建 35K token 上下文
    3. Generation: 调用 LLM 生成内容
    4. Post-Generation: 提取状态、检查一致性、更新状态
    5. Review Phase: 返回一致性报告
    """

    def __init__(
        self,
        context_builder: ContextBuilder,
        consistency_checker: ConsistencyChecker,
        storyline_manager: StorylineManager,
        plot_arc_repository: PlotArcRepository,
        llm_service: LLMService,
        state_extractor: Optional[StateExtractor] = None,
        state_updater: Optional[StateUpdater] = None,
        bible_repository: Optional[BibleRepository] = None,
        foreshadowing_repository: Optional[ForeshadowingRepository] = None,
        conflict_detection_service: Optional[ConflictDetectionService] = None,
        voice_fingerprint_service: Optional['VoiceFingerprintService'] = None,
        cliche_scanner: Optional['ClicheScanner'] = None,
        memory_engine: Optional['MemoryEngine'] = None,
    ):
        """初始化工作流

        Args:
            context_builder: 上下文构建器
            consistency_checker: 一致性检查器
            storyline_manager: 故事线管理器
            plot_arc_repository: 情节弧仓储
            llm_service: LLM 服务
            state_extractor: 状态提取器（可选）
            state_updater: 状态更新器（可选）
            bible_repository: Bible 仓储（用于一致性检查，可选）
            foreshadowing_repository: Foreshadowing 仓储（用于一致性检查，可选）
            conflict_detection_service: 冲突检测服务（可选）
            voice_fingerprint_service: 风格指纹服务（可选）
            cliche_scanner: 俗套扫描器（可选）
            memory_engine: V6 记忆引擎（可选，提供 FACT_LOCK / BEATS / CLUES 注入与章后回写）
        """
        self.context_builder = context_builder
        self.consistency_checker = consistency_checker
        self.storyline_manager = storyline_manager
        self.plot_arc_repository = plot_arc_repository
        self.llm_service = llm_service

        # ★ V6 记忆引擎（跨章节状态机）
        self.memory_engine = memory_engine
        if memory_engine and bible_repository:
            # 将 memory_engine 注入 context_builder 的 budget_allocator
            if hasattr(self.context_builder, 'budget_allocator'):
                self.context_builder.budget_allocator.memory_engine = memory_engine
                logger.info("✓ MemoryEngine 已注入 ContextBudgetAllocator")

        # V6 运行时上下文缓存（供 _build_prompt 使用）
        self._current_novel_id: str = ""
        self._current_chapter_number: int = 0
        self._current_character_canon_contract: str = ""
        self._current_profile_lock: str = ""
        
        # 强制初始化 StateExtractor（如果未提供）
        if state_extractor is None:
            logger.info("StateExtractor not provided, creating default instance")
            self.state_extractor = StateExtractor(llm_service=llm_service)
        else:
            self.state_extractor = state_extractor
        
        # 强制初始化 StateUpdater（如果未提供且有所需仓储）
        if state_updater is None and bible_repository and foreshadowing_repository:
            logger.info("StateUpdater not provided, creating default instance")
            from infrastructure.persistence.database.connection import get_database
            db = get_database()
            self.state_updater = StateUpdater(
                bible_repository=bible_repository,
                foreshadowing_repository=foreshadowing_repository,
                db_connection=db.get_connection()
            )
        else:
            self.state_updater = state_updater
        
        self.bible_repository = bible_repository
        self.foreshadowing_repository = foreshadowing_repository
        self.conflict_detection_service = conflict_detection_service
        self.voice_fingerprint_service = voice_fingerprint_service
        self.cliche_scanner = cliche_scanner

        # 初始化节拍连贯性增强器
        self.coherence_enhancer = BeatCoherenceEnhancer()

        # ★ Theme 集成器（延迟初始化）
        self._theme_integrator = None
        self._genre: Optional[str] = None

    def set_genre(self, genre: str) -> None:
        """设置小说题材，激活对应的 Theme Agent"""
        self._genre = genre
        self._theme_integrator = None
        self._initialize_theme()

    def _load_profile_lock_for_novel(self, novel_id: str) -> str:
        """Load the saved archive profile and activate its genre rules."""
        try:
            novel = self.context_builder.novel_repository.get_by_id(NovelId(novel_id))
            if not novel:
                return ""
            premise = str(getattr(novel, "premise", "") or "")
            genre, _world = parse_genre_world_from_premise(premise)
            if genre and genre != self._genre:
                self.set_genre(genre)
            return build_novel_profile_lock(
                title=str(getattr(novel, "title", "") or ""),
                premise=premise,
                target_chapters=int(getattr(novel, "target_chapters", 0) or 0) or None,
            )
        except Exception as exc:
            logger.debug("读取章节生成档案锁失败 novel=%s: %s", novel_id, exc)
            return ""

    def _initialize_theme(self) -> None:
        """延迟初始化 Theme 集成器"""
        if self._theme_integrator is not None:
            return

        try:
            from application.engine.theme.theme_integrator import ThemeIntegrator
            self._theme_integrator = ThemeIntegrator()
            if self._theme_integrator.initialize(self._genre):
                logger.info(f"✓ Theme 集成器已初始化，题材: {self._genre or 'default'}")
            else:
                self._theme_integrator = None
        except Exception as e:
            logger.warning(f"Theme 集成器初始化失败: {e}")
            self._theme_integrator = None

    def _maybe_action_transition_graph(
        self, scene_director: Optional[SceneDirectorAnalysis]
    ) -> Optional[ActionTransitionGraph]:
        if scene_director and scene_director.action_transition_graph:
            return scene_director.action_transition_graph.to_domain()
        return None

    def _spatial_topology_bundle_for_beat(
        self,
        beat_index: int,
        beats: List[Any],
        graph_dom: Optional[ActionTransitionGraph],
        micro_ctx: Optional[MicroSceneContext],
        scene_director: Optional[SceneDirectorAnalysis],
    ) -> Dict[str, Any]:
        roster = (
            {str(x).strip() for x in (scene_director.characters or []) if str(x).strip()}
            if scene_director
            else set()
        )
        prev_loc = (
            (beats[beat_index - 1].location_id or "").strip()
            if beat_index > 0
            else (beats[beat_index].location_id or "").strip()
        )
        curr_loc = (beats[beat_index].location_id or "").strip()
        transitioning = bool(graph_dom and beat_index > 0 and prev_loc != curr_loc)
        edge = (
            graph_dom.get_transition_path(prev_loc, curr_loc)
            if graph_dom and transitioning
            else None
        )
        atg_block = ""
        if graph_dom and curr_loc:
            atg_block = self.coherence_enhancer.build_atg_transition_directive(
                prev_loc, curr_loc, graph_dom
            )
        use_gate = graph_dom is not None and micro_ctx is not None and scene_director is not None
        guard = None
        if use_gate:
            guard = StreamingSceneLeakGuard(
                roster=roster,
                allowed_characters=set(micro_ctx.active_characters),
                transitioning=transitioning,
            )
        return {
            "roster": roster,
            "prev_loc": prev_loc,
            "curr_loc": curr_loc,
            "transitioning": transitioning,
            "edge": edge,
            "atg_block": atg_block,
            "use_gate": use_gate,
            "guard": guard,
        }

    def prepare_chapter_generation(
        self,
        novel_id: str,
        chapter_number: int,
        outline: str,
        *,
        scene_director: Optional[SceneDirectorAnalysis] = None,
        max_tokens: int = _CHAPTER_CONTEXT_BUILDER_TOKEN_BUDGET,
    ) -> Dict[str, Any]:
        """与单章 / 流式 / 托管按节拍写作同源：结构化三层上下文 + 故事线 + 张力 + 文风。

        托管守护进程与 HTTP 接口应复用此方法，避免「两套基建」。
        """
        storyline_context = self._get_storyline_context(novel_id, chapter_number)
        plot_tension = self._get_plot_tension(novel_id, chapter_number)
        payload = self.context_builder.build_structured_context(
            novel_id=novel_id,
            chapter_number=chapter_number,
            outline=outline,
            max_tokens=max_tokens,
            scene_director=scene_director,
        )
        context = assemble_chapter_bundle_context_text(payload)
        context_tokens = estimate_chapter_context_tokens(context)
        style_summary = self._get_style_summary(novel_id)
        voice_anchors = ""
        try:
            voice_anchors = self.context_builder.build_voice_anchor_system_section(novel_id)
        except Exception as e:
            logger.warning("voice_anchor section skipped: %s", e)
        character_canon_contract = self._build_character_canon_contract(novel_id, chapter_number)
        return {
            "storyline_context": storyline_context,
            "plot_tension": plot_tension,
            "context": context,
            "context_tokens": context_tokens,
            "style_summary": style_summary,
            "voice_anchors": voice_anchors,
            "character_canon_contract": character_canon_contract,
        }

    def _resolve_target_chapter_words(self, novel_id: str) -> int:
        """每章目标字数：与作品设置 target_words_per_chapter 一致（工作流 / API 单章生成）。"""
        try:
            novel = self.context_builder.novel_repository.get_by_id(NovelId(novel_id))
            if novel is not None:
                w = int(getattr(novel, "target_words_per_chapter", 2500) or 2500)
                return clamp_chapter_target_words(w)
        except Exception as e:
            logger.debug("读取 target_words_per_chapter 失败，使用默认 2500: %s", e)
        return 2500

    def _finalize_chapter_body_text(self, novel_id: str, raw: str) -> str:
        """推理块清洗 + 按书目偏好可选段内短句聚合。"""
        stripped = strip_prose_control_artifacts(strip_reasoning_artifacts(raw))
        stripped = normalize_prose_punctuation(stripped)
        stripped = remove_near_duplicate_paragraphs(stripped)
        try:
            novel = self.context_builder.novel_repository.get_by_id(NovelId(novel_id))
            if (
                novel is not None
                and getattr(novel.generation_prefs, "inline_prose_aggregation_enabled", False)
            ):
                return aggregate_inline_prose_fragments(stripped)
        except Exception as e:
            logger.debug(
                "inline_prose_aggregation 偏好读取失败，跳过聚合 novel=%s: %s",
                novel_id,
                e,
            )
        return stripped

    def _chapter_generation_config(self, target_words: int, *, temperature: float = 1.0) -> GenerationConfig:
        """给正文生成宽松兜底预算；实际篇幅由 prompt 的字数指引控制。"""
        tw = int(target_words or 2500)
        # max_tokens 只用于兜底防失控，不能作为实际控字工具。
        max_tokens = max(
            12000,
            min(_PROSE_OUTPUT_TOKEN_CAP, int(tw * _PROSE_TOKEN_PER_CHINESE_WORD) + _PROSE_TOKEN_COMPLETION_BUFFER),
        )
        return GenerationConfig(max_tokens=max_tokens, temperature=temperature)

    def _beat_generation_config(
        self,
        beat_target_words: int,
        *,
        temperature: float = 0.92,
    ) -> GenerationConfig:
        """节拍生成预算：宽松兜底，节拍长短由提示词和指挥器约束。"""
        bw = max(300, int(beat_target_words or 800))
        max_tokens = max(
            8000,
            min(_PROSE_OUTPUT_TOKEN_CAP, int(bw * _PROSE_TOKEN_PER_CHINESE_WORD) + 5000),
        )
        return GenerationConfig(max_tokens=max_tokens, temperature=temperature)

    def build_fallback_chapter_bundle(
        self,
        novel_id: str,
        chapter_number: int,
        outline: str,
        *,
        scene_director: Optional[SceneDirectorAnalysis] = None,
        max_tokens: int = _CHAPTER_CONTEXT_BUILDER_TOKEN_BUDGET,
    ) -> Dict[str, Any]:
        """prepare_chapter_generation 失败时的降级：仍用三层洋葱 + 同段名拼接；叙事/文风各步独立容错。

        供全托管等场景在「故事线/张力等」子步骤异常时保持与主路径一致的上下文形态。
        """
        payload = self.context_builder.build_structured_context(
            novel_id=novel_id,
            chapter_number=chapter_number,
            outline=outline,
            max_tokens=max_tokens,
            scene_director=scene_director,
        )
        context = assemble_chapter_bundle_context_text(payload)
        context_tokens = estimate_chapter_context_tokens(context)

        storyline_context = ""
        try:
            storyline_context = self._get_storyline_context(novel_id, chapter_number)
        except Exception as e:
            logger.warning("fallback storyline_context skipped: %s", e)

        plot_tension = ""
        try:
            plot_tension = self._get_plot_tension(novel_id, chapter_number)
        except Exception as e:
            logger.warning("fallback plot_tension skipped: %s", e)

        style_summary = ""
        try:
            style_summary = self._get_style_summary(novel_id)
        except Exception as e:
            logger.warning("fallback style_summary skipped: %s", e)

        voice_anchors = ""
        try:
            voice_anchors = self.context_builder.build_voice_anchor_system_section(novel_id)
        except Exception as e:
            logger.warning("fallback voice_anchors skipped: %s", e)
        character_canon_contract = self._build_character_canon_contract(novel_id, chapter_number)

        return {
            "storyline_context": storyline_context,
            "plot_tension": plot_tension,
            "context": context,
            "context_tokens": context_tokens,
            "style_summary": style_summary,
            "voice_anchors": voice_anchors,
            "character_canon_contract": character_canon_contract,
        }

    async def post_process_generated_chapter(
        self,
        novel_id: str,
        chapter_number: int,
        outline: str,
        content: str,
        scene_director: Optional[SceneDirectorAnalysis] = None,
        persist_updates: bool = True,
    ) -> Dict[str, Any]:
        """生成正文后的统一后处理：俗套扫描、状态提取、一致性、冲突批注、StateUpdater、MemoryEngine回写。"""
        style_warnings = self._scan_cliches(content)
        chapter_state = await self._extract_chapter_state(content, chapter_number)
        consistency_report = self._check_consistency(chapter_state, novel_id)
        ghost_annotations = self._detect_conflicts(novel_id, chapter_number, outline, scene_director)
        ghost_annotations.extend(self._detect_character_canon_drift(
            novel_id=novel_id,
            chapter_number=chapter_number,
            content=content,
        ))
        memory_delta = {}
        if persist_updates:
            memory_delta = await self._commit_generated_chapter_updates(
                novel_id=novel_id,
                chapter_number=chapter_number,
                outline=outline,
                content=content,
                chapter_state=chapter_state,
            )

        return {
            "style_warnings": style_warnings,
            "chapter_state": chapter_state,
            "consistency_report": consistency_report,
            "ghost_annotations": ghost_annotations,
            "memory_delta": memory_delta,
        }

    async def post_process_generated_chapter_stream(
        self,
        novel_id: str,
        chapter_number: int,
        outline: str,
        content: str,
        scene_director: Optional[SceneDirectorAnalysis] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """流式章节后处理：把耗时子步骤显式透出，避免前端停在笼统 post 阶段。"""
        yield {"type": "post_step", "step": "style_scan", "message": "扫描俗套句式"}
        style_warnings = self._scan_cliches(content)

        yield {"type": "post_step", "step": "state_extract", "message": "提取章节状态"}
        try:
            chapter_state = await asyncio.wait_for(
                self._extract_chapter_state(content, chapter_number),
                timeout=_STREAM_POST_EXTRACT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "StateExtractor 超时，使用空章节状态 novel=%s chapter=%s timeout=%.1fs",
                novel_id,
                chapter_number,
                _STREAM_POST_EXTRACT_TIMEOUT_SECONDS,
            )
            yield {
                "type": "post_step",
                "step": "state_extract_timeout",
                "message": "章节状态提取超时，已降级为空状态",
                "level": "warning",
            }
            chapter_state = ChapterState(
                new_characters=[],
                character_actions=[],
                relationship_changes=[],
                foreshadowing_planted=[],
                foreshadowing_resolved=[],
                events=[],
            )

        yield {"type": "post_step", "step": "consistency", "message": "检查一致性"}
        consistency_report = self._check_consistency(chapter_state, novel_id)

        yield {"type": "post_step", "step": "conflict_scan", "message": "生成冲突批注"}
        ghost_annotations = self._detect_conflicts(novel_id, chapter_number, outline, scene_director)
        ghost_annotations.extend(self._detect_character_canon_drift(
            novel_id=novel_id,
            chapter_number=chapter_number,
            content=content,
        ))

        yield {
            "type": "post_result",
            "post": {
                "style_warnings": style_warnings,
                "chapter_state": chapter_state,
                "consistency_report": consistency_report,
                "ghost_annotations": ghost_annotations,
                "memory_delta": {},
            },
        }

    async def _commit_generated_chapter_updates(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        outline: str,
        content: str,
        chapter_state: ChapterState,
    ) -> Dict[str, Any]:
        """只对最终采用的正文回写状态与记忆，避免失败草稿污染后续上下文。"""
        if self.state_updater:
            try:
                self.state_updater.update_from_chapter(novel_id, chapter_number, chapter_state)
            except Exception as e:
                logger.warning("StateUpdater 失败: %s", e)

        # ★ V6 新增：MemoryEngine 章后状态回写（LLM 驱动的增量提取）
        memory_delta = {}
        if self.memory_engine:
            try:
                memory_delta = await self.memory_engine.update_from_chapter(
                    novel_id=novel_id,
                    chapter_number=chapter_number,
                    content=content,
                    outline=outline,
                )
                if memory_delta.get("new_beats", 0) or memory_delta.get("new_clues", 0):
                    logger.info(
                        f"  🧠 MemoryEngine: +{memory_delta.get('new_beats', 0)} beats, "
                        f"+{memory_delta.get('new_clues', 0)} clues"
                    )
                if memory_delta.get("violations", 0):
                    logger.warning(
                        f"  ⚠️ MemoryEngine 检测到 {memory_delta['violations']} 个事实违反"
                    )
            except Exception as e:
                logger.warning("MemoryEngine 章后回写失败: %s", e)

        return memory_delta

    def _estimate_chinese_words(self, content: str) -> int:
        """粗略估算正文长度，中文按非空白字符计，英文按词计。"""
        if not content:
            return 0

        cjk_chars = re.findall(r"[\u4e00-\u9fff]", content)
        ascii_words = re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)?", content)
        return len(cjk_chars) + len(ascii_words)

    def _build_retrospective_micro_beats(
        self,
        content: str,
        *,
        target_words: int,
    ) -> List[Dict[str, Any]]:
        """从最终正文反推展示/回顾用 micro_beats；不参与本章正文生成。"""
        text = (content or "").strip()
        if not text:
            return []

        paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
        sentences = [
            s.strip()
            for s in re.findall(r"[^。！？!?]+[。！？!?]?", text)
            if s.strip()
        ]
        blocks = paragraphs if len(paragraphs) >= 4 else (sentences or paragraphs)
        if not blocks:
            return []

        estimated_words = self._estimate_chinese_words(text)
        desired = max(3, min(6, int(round((estimated_words or target_words or 2500) / 550))))
        desired = max(1, min(desired, len(blocks)))
        target_per_group = max(1, int(sum(len(b) for b in blocks) / desired))

        groups: List[List[str]] = []
        current: List[str] = []
        current_chars = 0
        for idx, block in enumerate(blocks):
            remaining_blocks = len(blocks) - idx
            remaining_groups = desired - len(groups)
            should_close = (
                current
                and current_chars >= target_per_group
                and remaining_blocks >= remaining_groups
            )
            if should_close:
                groups.append(current)
                current = []
                current_chars = 0
            current.append(block)
            current_chars += len(block)
        if current:
            groups.append(current)

        focus_by_position = ["setup", "development", "conflict", "turning_point", "resolution"]
        beats: List[Dict[str, Any]] = []
        for idx, group in enumerate(groups[:6]):
            segment = "\n\n".join(group).strip()
            if not segment:
                continue
            desc = self._summarize_retrospective_segment(segment)
            if not desc:
                continue
            if len(groups) == 1:
                focus = "review"
            else:
                pos = round(idx * (len(focus_by_position) - 1) / max(1, len(groups) - 1))
                focus = focus_by_position[pos]
            beats.append(
                {
                    "description": desc,
                    "target_words": self._estimate_chinese_words(segment),
                    "focus": focus,
                    "location_id": "",
                }
            )
        return beats

    def _summarize_retrospective_segment(self, segment: str, *, limit: int = 96) -> str:
        """给回顾节拍生成短描述，避免把整段正文塞进 micro_beats。"""
        compact = re.sub(r"\s+", "", segment or "")
        if not compact:
            return ""
        sentences = [
            s.strip()
            for s in re.findall(r"[^。！？!?]+[。！？!?]?", compact)
            if s.strip()
        ]
        if not sentences:
            return compact[:limit]
        first = sentences[0]
        last = sentences[-1]
        summary = first if first == last else f"{first} -> {last}"
        return summary[:limit]

    def _build_character_canon_contract(self, novel_id: str, chapter_number: int) -> str:
        """从 Bible 提炼本章角色正典锁，供生成与修稿共用。"""
        if not self.bible_repository:
            return ""
        try:
            bible = self.bible_repository.get_by_novel_id(NovelId(novel_id))
        except Exception as exc:
            logger.warning("角色正典锁构建失败 novel=%s chapter=%s: %s", novel_id, chapter_number, exc)
            return ""
        if not bible or not getattr(bible, "characters", None):
            return ""

        def _character_info_score(character: Any) -> int:
            fields = [
                "role", "description", "public_profile", "mental_state_reason",
                "verbal_tic", "idle_behavior", "core_belief", "hidden_profile",
            ]
            score = 0
            for field in fields:
                value = (getattr(character, field, "") or "").strip()
                score += min(len(value), 120)
            score += 40 * len(getattr(character, "relationships", None) or [])
            score += 40 * len(getattr(character, "moral_taboos", None) or [])
            score += 40 * len(getattr(character, "active_wounds", None) or [])
            return score

        unique_by_name: Dict[str, Any] = {}
        for character in bible.characters:
            name = (getattr(character, "name", "") or "").strip()
            if not name:
                continue
            existing = unique_by_name.get(name)
            if existing is None or _character_info_score(character) > _character_info_score(existing):
                unique_by_name[name] = character

        lines: List[str] = []
        for character in list(unique_by_name.values())[:16]:
            name = (getattr(character, "name", "") or "").strip()
            if not name:
                continue
            cid_obj = getattr(character, "character_id", None)
            cid = getattr(cid_obj, "value", None) or str(getattr(character, "id", "") or "")
            role = (getattr(character, "role", "") or "").strip()
            desc = (getattr(character, "description", "") or "").strip()
            public_profile = (getattr(character, "public_profile", "") or "").strip()
            mental_state = (getattr(character, "mental_state", "") or "").strip()
            mental_reason = (getattr(character, "mental_state_reason", "") or "").strip()
            verbal_tic = (getattr(character, "verbal_tic", "") or "").strip()
            idle_behavior = (getattr(character, "idle_behavior", "") or "").strip()
            core_belief = (getattr(character, "core_belief", "") or "").strip()
            moral_taboos = [str(x).strip() for x in (getattr(character, "moral_taboos", None) or []) if str(x).strip()]
            active_wounds = []
            for wound in getattr(character, "active_wounds", None) or []:
                if isinstance(wound, dict):
                    label = wound.get("name") or wound.get("summary") or wound.get("description") or wound.get("wound")
                    if label:
                        active_wounds.append(str(label).strip())
                elif str(wound).strip():
                    active_wounds.append(str(wound).strip())
            relationships = [str(x).strip() for x in (getattr(character, "relationships", None) or []) if str(x).strip()]
            reveal_chapter = getattr(character, "reveal_chapter", None)
            hidden_profile = (getattr(character, "hidden_profile", "") or "").strip()

            facts = [f"ID={cid}"]
            if role:
                facts.append(f"定位={role}")
            if desc:
                facts.append(f"设定={desc[:180]}")
            if public_profile:
                facts.append(f"公开信息={public_profile[:160]}")
            if mental_state and mental_state != "NORMAL":
                reason = f"（{mental_reason[:80]}）" if mental_reason else ""
                facts.append(f"当前心理={mental_state}{reason}")
            if verbal_tic:
                facts.append(f"口癖/声线={verbal_tic[:80]}")
            if idle_behavior:
                facts.append(f"习惯动作={idle_behavior[:80]}")
            if core_belief:
                facts.append(f"核心信念={core_belief[:100]}")
            if moral_taboos:
                facts.append("禁忌=" + "、".join(moral_taboos[:4]))
            if active_wounds:
                facts.append("持续伤痕=" + "、".join(active_wounds[:3]))
            if relationships:
                facts.append("关系=" + "；".join(relationships[:4])[:180])
            if hidden_profile:
                if reveal_chapter and chapter_number < int(reveal_chapter):
                    facts.append(f"隐藏信息=第{reveal_chapter}章前作为叙述层背景保留")
                else:
                    facts.append(f"隐藏信息={hidden_profile[:140]}")
            lines.append(f"- {name}：" + "；".join(facts))

        if not lines:
            return ""
        return _render_generation_prompt_text(
            _CHAPTER_CHARACTER_CANON_CONTRACT_NODE_KEY,
            {"character_canon": "\n".join(lines)},
        )

    def _detect_character_canon_drift(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        content: str,
    ) -> List[GhostAnnotation]:
        """用 Bible 做轻量正文审稿，拦截常见角色漂移。"""
        if not self.bible_repository or not content:
            return []
        try:
            bible = self.bible_repository.get_by_novel_id(NovelId(novel_id))
        except Exception as exc:
            logger.debug("角色正典审稿跳过 novel=%s chapter=%s: %s", novel_id, chapter_number, exc)
            return []
        if not bible or not getattr(bible, "characters", None):
            return []

        annotations: List[GhostAnnotation] = []
        canon_names = [(getattr(c, "name", "") or "").strip() for c in bible.characters]
        canon_names = [name for name in canon_names if name]

        named_candidates = set(re.findall(r"[\u4e00-\u9fff]{2,4}", content))
        # 只拦截高置信的“疑似新增姓名”：2-4 字中文名，且上下文没有该人名。
        common_non_names = {
            "这个", "那个", "他们", "我们", "你们", "自己", "时候", "地方", "声音", "空气", "眼神", "身影",
            "系统", "基因", "武道", "世界", "能力", "力量", "男人", "女人", "少年", "少女", "老师", "同学",
            "众人", "所有人", "下一刻", "与此同时", "话音刚落", "办公室", "训练场", "黑暗", "光芒",
        }
        for candidate in sorted(named_candidates):
            if candidate in common_non_names or candidate in canon_names:
                continue
            if any(candidate in name or name in candidate for name in canon_names):
                continue
            # 需要附近有说/问/看/走等人物动作，降低误报。
            if not re.search(rf"{re.escape(candidate)}[^。！？\n]{{0,24}}(说|问|看|走|站|笑|点头|摇头|伸手|转身)", content):
                continue
            annotations.append(GhostAnnotation(
                type="character_inconsistency",
                severity="warning",
                message=f"正文疑似引入 Bible 外具名角色「{candidate}」，请确认是否应改为既有角色或无名路人。",
                entity_name=candidate,
                expected="仅使用 Bible/大纲允许的角色",
                actual=candidate,
            ))
            if len(annotations) >= 5:
                break

        for character in bible.characters:
            name = (getattr(character, "name", "") or "").strip()
            if not name or name not in content:
                continue
            hidden_profile = (getattr(character, "hidden_profile", "") or "").strip()
            reveal_chapter = getattr(character, "reveal_chapter", None)
            if hidden_profile and reveal_chapter and chapter_number < int(reveal_chapter):
                hidden_terms = [
                    term
                    for term in re.split(r"[，。；、,;.\s]+", hidden_profile)
                    if len(term.strip()) >= 3
                ][:6]
                leaked_terms = [term for term in hidden_terms if term and term in content]
                if leaked_terms:
                    annotations.append(GhostAnnotation(
                        type="character_inconsistency",
                        severity="error",
                        message=f"「{name}」的隐藏信息疑似提前公开：{', '.join(leaked_terms[:3])}",
                        entity_id=getattr(getattr(character, "character_id", None), "value", None),
                        entity_name=name,
                        expected=f"第 {reveal_chapter} 章前隐藏信息留在叙述层背景",
                        actual=", ".join(leaked_terms[:3]),
                    ))

        return annotations

    def _get_canonical_character_names(self, novel_id: str) -> List[str]:
        if not self.bible_repository:
            return []
        try:
            bible = self.bible_repository.get_by_novel_id(NovelId(novel_id))
        except Exception:
            return []
        if not bible or not getattr(bible, "characters", None):
            return []
        names: List[str] = []
        for character in bible.characters:
            name = (getattr(character, "name", "") or "").strip()
            if name and name not in names:
                names.append(name)
        return names

    def _get_canonical_protagonist_name(self, novel_id: str) -> str:
        """尽量从 Bible/作品梗概找到正典主角名，用于清洗旧大纲占位名。"""
        if self.bible_repository:
            try:
                bible = self.bible_repository.get_by_novel_id(NovelId(novel_id))
            except Exception:
                bible = None
            if bible and getattr(bible, "characters", None):
                fallback = ""
                for character in bible.characters:
                    name = (getattr(character, "name", "") or "").strip()
                    if not name:
                        continue
                    if not fallback:
                        fallback = name
                    role = (getattr(character, "role", "") or "").strip()
                    desc = (getattr(character, "description", "") or "").strip()
                    marker = role + desc
                    if any(key in marker for key in ("主角", "男主", "女主", "主人公", "protagonist")):
                        return name
                if fallback:
                    return fallback

        try:
            novel = self.context_builder.novel_repository.get_by_id(NovelId(novel_id))
            blob = "\n".join([
                str(getattr(novel, "title", "") or ""),
                str(getattr(novel, "premise", "") or ""),
            ])
            match = re.search(r"[，,]\s*([\u4e00-\u9fff]{2,4})(?:觉醒|获得|绑定|重生|穿越|意外)", blob)
            if match:
                return match.group(1)
        except Exception:
            pass
        return ""

    def _looks_like_protagonist_alias(
        self,
        *,
        candidate: str,
        protagonist: str,
        text: str,
    ) -> bool:
        """判断大纲里的非正典姓名是否像旧主角名，而不是普通新角色。

        只做高置信归一：
        - 与正典主角同姓；
        - 正典主角名没有出现在大纲中；
        - 候选名在句首/条目开头或附近承担觉醒、获得、决定、逃亡、战斗等主角动作。
        """
        if not candidate or not protagonist or candidate == protagonist:
            return False
        if candidate[:1] != protagonist[:1]:
            return False
        if protagonist in text:
            return False

        escaped = re.escape(candidate)
        leading_name = re.search(rf"(^|[\n。！？；;：:\d、\-\s]){escaped}", text) is not None
        protagonist_actions = (
            "觉醒", "获得", "绑定", "融合", "发现", "决定", "前往", "进入", "逃离", "逃亡",
            "反杀", "击败", "突破", "修炼", "解析", "复制", "掌握", "回忆", "意识到",
        )
        nearby_action = re.search(
            rf"{escaped}[^。！？\n]{{0,60}}({'|'.join(protagonist_actions)})",
            text,
        ) is not None
        return leading_name and nearby_action

    def _normalize_outline_against_character_canon(self, novel_id: str, outline: str) -> str:
        """把上游结构树/旧稿大纲里的旧主角名统一到 Bible 正典名。"""
        text = (outline or "").strip()
        if not text:
            return text
        canon_names = self._get_canonical_character_names(novel_id)
        protagonist = self._get_canonical_protagonist_name(novel_id)
        if not protagonist:
            return text
        canon_set = set(canon_names)

        replacements: Dict[str, str] = {}
        # 泛化：同姓、非 Bible 名、承担主角动作的 2-3 字姓名，归并到正典主角。
        surname = protagonist[:1]
        for candidate in set(re.findall(rf"{re.escape(surname)}[\u4e00-\u9fff]{{1,2}}", text)):
            if candidate == protagonist or candidate in canon_set or candidate in replacements:
                continue
            if self._looks_like_protagonist_alias(
                candidate=candidate,
                protagonist=protagonist,
                text=text,
            ):
                replacements[candidate] = protagonist

        normalized = text
        for old, new in replacements.items():
            normalized = normalized.replace(old, new)
        if normalized != text:
            logger.info(
                "章节大纲角色名已按 Bible 正典归一 novel=%s replacements=%s",
                novel_id,
                replacements,
            )
        return normalized

    def _assess_chapter_completeness(
        self,
        *,
        content: str,
        target_words: int,
    ) -> List[str]:
        """检查章节是否像完整章节，而不是停在半截动作/对白。"""
        text = (content or "").strip()
        if not text:
            return ["正文为空"]
        reasons: List[str] = []
        word_count = self._estimate_chinese_words(text)
        soft_min = max(900, int((target_words or 2000) * 0.80))
        if word_count < soft_min:
            reasons.append(f"章节篇幅偏短：约 {word_count} 字，完整章节建议不低于 {soft_min} 字")

        tail = text[-360:]
        stripped_tail = tail.rstrip()
        if stripped_tail.endswith(("，", "、", "：", "；", ",", ":", ";", "——", "…")):
            reasons.append("章节结尾疑似句子未写完")
        quote_count = stripped_tail.count("“") + stripped_tail.count("”")
        if quote_count % 2 == 1 or stripped_tail.endswith(("“", "「", "『")):
            reasons.append("章节结尾疑似对白未闭合")

        dangling_patterns = [
            r"(刚要|正要|准备|还没来得及|才刚|下一刻|就在这时|忽然|突然|他抬起头|她抬起头)[^。！？]{0,60}[。！？”」』]?$",
            r"(门外|门口|身后|耳边|屏幕上|系统|提示|警报|裂隙里)[^。！？]{0,50}(响起|传来|亮起|浮现|弹出|刷新|推开|裂开)[^。！？]{0,40}[。！？”」』]?$",
            r"(?:他|她|[一-龥]{2,4})[^。！？]{0,40}(伸出手|抬手|转身|迈步|开口|张开嘴|推开门|按下)[^。！？]{0,40}[。！？”」』]?$",
        ]
        if any(re.search(pattern, stripped_tail) for pattern in dangling_patterns):
            reasons.append("章节结尾停在半截动作/突发事件上，缺少本章收束画面")

        ending_markers = ("暗下去", "关上", "合拢", "落下", "沉入", "消失", "停住", "收回", "没有回头", "只剩下", "更深了")
        has_terminal_punctuation = stripped_tail.endswith(("。", "！", "？", "”", "」", "』"))
        if word_count >= soft_min and has_terminal_punctuation and not any(m in stripped_tail[-80:] for m in ending_markers):
            # 不是硬失败，只在结尾明显是新事件/新对白时提醒。
            if re.search(r"(说道|问道|低声说|声音响起|屏幕|提示|警报|脚步声|门外)", stripped_tail[-90:]):
                reasons.append("章节末尾更像新场景开头，建议补一个本章落点")
        return reasons

    def _allows_first_person_narration(self, outline: str) -> bool:
        """只有用户/大纲明确指定时，才允许整章第一人称叙述。"""
        text = outline or ""
        return bool(re.search(r"(第一人称|一人称|我视角|主角自述|自述体|日记体)", text))

    def _strip_dialogue_for_narration_scan(self, content: str) -> str:
        """移除常见中文引号内对白，避免把角色台词里的“我”误判为叙述人称。"""
        text = content or ""
        text = re.sub(r"“[^”]{0,240}”", "", text)
        text = re.sub(r"「[^」]{0,240}」", "", text)
        text = re.sub(r"『[^』]{0,240}』", "", text)
        return text

    def _assess_narrative_person(self, *, content: str, outline: str) -> List[str]:
        """检查正文是否误用了第一人称叙述。"""
        if self._allows_first_person_narration(outline):
            return []
        narration = self._strip_dialogue_for_narration_scan(content)
        first_person_hits = re.findall(r"(?<![A-Za-z0-9])(?:我|我们|我的|我们的)(?![A-Za-z0-9])", narration)
        if len(first_person_hits) >= 3:
            return ["叙述人称疑似误用第一人称：未明确要求第一人称时，正文应使用第三人称限制视角"]
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", narration) if p.strip()]
        if paragraphs and re.match(r"^(我|我的|我们|我们的)[^。！？]{0,80}[。！？]", paragraphs[0]):
            return ["章节开头使用第一人称叙述，默认应改为第三人称限制视角"]
        return []

    def _assess_chapter_task_closure(self, *, content: str, outline: str) -> List[str]:
        """检查章节核心任务是否已经阶段性落地，而不是停在新事件刚出现。"""
        text = (content or "").strip()
        if not text:
            return []
        reasons: List[str] = []
        tail = text[-560:]

        result_pattern = (
            r"(确认|证实|完成|结束|落定|恢复|明白|看清|认出|拿到|失去|付出|代价|结果|失败|成功|"
            r"逃出|被困|翻脸|和解|成交|破裂|拒绝|答应|决定|选择|交出|保住|毁掉|暴露|揭穿|"
            r"解决|升级|退去|停下|停住|放弃|承认|签下|达成|崩塌|熄灭)"
        )
        landed_hook_pattern = (
            r"(停在|停住|没有再|不再|只剩下|落下|暗下去|合上|关上|收回|散去|定住|归于|"
            r"压低|扣住|放下|留下|退后|走远|熄灭|沉下去|安静下来)"
        )

        inciting_tail = re.search(
            r"(?:(?:新|陌生|另一|第二|第三)[^。！？]{0,40})?"
            r"(?:人|声音|脚步|提示|警报|文字|符号|影子|名字|信息|画面|物件|信号|亮光|异动|变化|轮廓)"
            r"[^。！？]{0,80}(?:出现|浮现|亮起|响起|传来|推开|走出|伸出|震动|裂开|靠近|弹出|刷新|停下)"
            r"|(?:身后|门外|耳边|屏幕|墙上|地面|水面|镜中|裂缝|阴影|光里|雾里|远处)"
            r"[^。！？]{0,80}(?:出现|浮现|亮起|响起|传来|推开|走出|伸出|震动|裂开|靠近|弹出|刷新|停下)",
            tail,
        )
        closure_markers = re.search(result_pattern, tail)
        if inciting_tail:
            before_new_event = tail[:inciting_tail.start()]
            after_new_event = tail[inciting_tail.end():]
            new_event_text = tail[inciting_tail.start():]
            has_result_before = bool(re.search(result_pattern, before_new_event))
            has_result_after = bool(re.search(result_pattern, after_new_event))
            hook_has_landing_image = bool(re.search(landed_hook_pattern, new_event_text[-160:]))
            if not has_result_after and not (has_result_before and hook_has_landing_image):
                reasons.append("章节结尾停在新事件刚出现的位置，缺少信息确认、目标成败、代价、选择、关系变化或危机升级等阶段性结果")

        final_paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
        final_paragraph = final_paragraphs[-1][-260:] if final_paragraphs else tail[-260:]
        final_dialogue_from_new_source = re.search(
            r"(门外|身后|耳边|陌生|脚步|声音|屏幕|系统|提示)[^。！？]{0,100}[：:“「][^”」]{0,120}[？。！？][”」]?$",
            final_paragraph,
        )
        final_action_starts = re.search(
            r"(刚要|正要|准备|还没来得及|下一刻|就在这时|忽然|突然)[^。！？]{0,100}[。！？”」』]?$",
            final_paragraph,
        )
        final_has_result = bool(re.search(result_pattern, final_paragraph))
        final_has_landing = bool(re.search(landed_hook_pattern, final_paragraph))
        if (final_dialogue_from_new_source or final_action_starts) and not (final_has_result or final_has_landing):
            reasons.append("章节结尾停在新事件刚出现的位置，缺少信息确认、目标成败、代价、选择、关系变化或危机升级等阶段性结果")
        return reasons

    def _build_quality_gate(
        self,
        *,
        content: str,
        outline: str,
        target_words: int,
        style_warnings: List[Any],
        consistency_report: ConsistencyReport,
        ghost_annotations: List[GhostAnnotation],
        chapter_number: int | None = None,
    ) -> Dict[str, Any]:
        """生成后验收：把现有审稿信号收敛成可执行的门禁结果。"""
        reasons: List[str] = []
        word_count = self._estimate_chinese_words(content)
        min_words = max(900, int(target_words * 0.80)) if target_words else 900
        completeness_reasons = self._assess_chapter_completeness(
            content=content,
            target_words=target_words,
        )
        narrative_person_reasons = self._assess_narrative_person(
            content=content,
            outline=outline,
        )
        task_closure_reasons = self._assess_chapter_task_closure(
            content=content,
            outline=outline,
        )
        final_draft_issues = audit_final_draft_text(
            content,
            target_words=target_words,
            strict_length=False,
            chapter_number=chapter_number,
        )
        final_draft_reasons = [issue.message for issue in final_draft_issues]
        final_draft_error_reasons = [
            issue.message for issue in final_draft_issues if issue.severity == "error"
        ]

        critical_style = [h for h in style_warnings if getattr(h, "severity", "") == "critical"]
        warning_style = [h for h in style_warnings if getattr(h, "severity", "") == "warning"]
        critical_issues = list(consistency_report.get_issues_by_severity(Severity.CRITICAL))
        important_issues = list(consistency_report.get_issues_by_severity(Severity.IMPORTANT))
        error_annotations = [ann for ann in ghost_annotations if ann.severity == "error"]
        character_drift_annotations = [
            ann for ann in ghost_annotations if ann.type == "character_inconsistency"
        ]

        if word_count < min_words:
            reasons.append(f"正文长度不足：约 {word_count} 字，目标下限 {min_words} 字")
        reasons.extend(completeness_reasons)
        reasons.extend(narrative_person_reasons)
        reasons.extend(task_closure_reasons)
        reasons.extend(final_draft_reasons)
        if critical_style:
            reasons.append(f"命中 critical 级 AI 腔/俗套 {len(critical_style)} 处")
        if len(warning_style) >= 8:
            reasons.append(f"warning 级 AI 腔/俗套偏多：{len(warning_style)} 处")
        if critical_issues:
            reasons.append(f"一致性严重问题 {len(critical_issues)} 个")
        if important_issues:
            reasons.append(f"一致性重要问题 {len(important_issues)} 个")
        if error_annotations:
            reasons.append(f"设定冲突 error 级批注 {len(error_annotations)} 个")
        if character_drift_annotations:
            reasons.append(f"角色正典漂移/疑似新增角色 {len(character_drift_annotations)} 个")

        outline_terms = [
            term.strip(" ，。！？；：,.!?;:、")
            for term in outline.replace("\n", " ").split()
            if len(term.strip(" ，。！？；：,.!?;:、")) >= 3
        ]
        missed_terms = [term for term in outline_terms[:12] if term and term not in content]
        if len(missed_terms) >= 5:
            reasons.append("正文疑似未覆盖章纲关键词：" + "、".join(missed_terms[:5]))

        return {
            "passed": not reasons,
            "reasons": reasons,
            "word_count": word_count,
            "min_words": min_words,
            "chapter_complete": not completeness_reasons,
            "completeness_reasons": completeness_reasons,
            "narrative_person_reasons": narrative_person_reasons,
            "task_closure_reasons": task_closure_reasons,
            "final_draft_reasons": final_draft_reasons,
            "critical_style_count": len(critical_style),
            "warning_style_count": len(warning_style),
            "critical_consistency_count": len(critical_issues),
            "important_consistency_count": len(important_issues),
            "error_annotation_count": len(error_annotations),
            "character_drift_count": len(character_drift_annotations),
            "auto_repair_required": bool(
                critical_style
                or critical_issues
                or important_issues
                or error_annotations
                or character_drift_annotations
                or completeness_reasons
                or narrative_person_reasons
                or task_closure_reasons
                or final_draft_error_reasons
                or any("对白过短" in reason or "极短对白" in reason for reason in final_draft_reasons)
                or word_count < max(120, int(min_words * 0.35))
            ),
            "repair_attempted": False,
            "repair_applied": False,
        }

    def _format_quality_repair_brief(
        self,
        *,
        quality_gate: Dict[str, Any],
        style_warnings: List[Any],
        consistency_report: ConsistencyReport,
        ghost_annotations: List[GhostAnnotation],
    ) -> str:
        lines = ["【未通过项】"]
        lines.extend(f"- {reason}" for reason in quality_gate.get("reasons", []))

        critical_hits = [h for h in style_warnings if getattr(h, "severity", "") == "critical"][:8]
        if critical_hits:
            lines.append("\n【需要清理的 AI 腔样例】")
            for hit in critical_hits:
                hint = getattr(hit, "replacement_hint", "") or "改为具体动作、对白或感官细节"
                lines.append(f"- {hit.pattern}: {hit.text} -> {hint}")

        issues = list(consistency_report.issues or [])[:6]
        if issues:
            lines.append("\n【一致性问题】")
            for issue in issues:
                lines.append(f"- {issue.severity.value}/{issue.type.value}: {issue.description}")

        errors = [ann for ann in ghost_annotations if ann.severity == "error"][:6]
        if errors:
            lines.append("\n【设定冲突】")
            for ann in errors:
                lines.append(f"- {ann.message}")

        character_drifts = [ann for ann in ghost_annotations if ann.type == "character_inconsistency"][:8]
        if character_drifts:
            lines.append("\n【角色正典问题】")
            for ann in character_drifts:
                expected = f"（应为：{ann.expected}）" if ann.expected else ""
                lines.append(f"- {ann.message}{expected}")

        return "\n".join(lines)

    def _get_prompt_registry_texts(self, node_key: str) -> tuple[str, str]:
        """读取 CPMS 节点文本；DB 失败时退到 prompt_packages 文件。"""
        try:
            from infrastructure.ai.prompt_registry import get_prompt_registry

            registry = get_prompt_registry()
            system = registry.get_system(node_key) or ""
            user = registry.get_user_template(node_key) or ""
            if system or user:
                return system, user
        except Exception as exc:
            logger.debug("PromptRegistry 节点读取失败 node_key=%s: %s", node_key, exc)

        package_rendered = render_prompt(node_key, {})
        return package_rendered.get("system", ""), package_rendered.get("user", "")

    def _build_context_alignment_protocol(self, context: str, outline: str) -> str:
        """生成章节主 prompt 的上下文对齐协议，文本由 CPMS 节点管理。"""
        rendered = _render_generation_prompt_text(
            _CHAPTER_CONTEXT_ALIGNMENT_NODE_KEY,
            {
                "context": context,
                "outline": outline,
                "priority_order": _CONTEXT_PRIORITY_ORDER,
            },
        ).strip()
        return rendered

    async def _repair_chapter_against_quality_gate(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        outline: str,
        original_content: str,
        bundle: Dict[str, Any],
        quality_gate: Dict[str, Any],
        style_warnings: List[Any],
        consistency_report: ConsistencyReport,
        ghost_annotations: List[GhostAnnotation],
        target_words: int,
        scene_director: Optional[SceneDirectorAnalysis],
    ) -> Optional[str]:
        """门禁失败时进行一次受约束整章修稿，继续使用同一上下文对齐。"""
        if not quality_gate.get("reasons"):
            return None

        repair_brief = self._format_quality_repair_brief(
            quality_gate=quality_gate,
            style_warnings=style_warnings,
            consistency_report=consistency_report,
            ghost_annotations=ghost_annotations,
        )
        context = bundle["context"]
        cap = min(len(context), 32000)
        character_canon_contract = (
            bundle.get("character_canon_contract")
            or self._current_character_canon_contract
            or self._build_character_canon_contract(novel_id, chapter_number)
        )
        rendered = render_prompt(
            _CHAPTER_QUALITY_REPAIR_NODE_KEY,
            {
                "context": context[:cap],
                "character_canon_contract": character_canon_contract,
                "outline": outline,
                "quality_feedback": repair_brief,
                "target_words": target_words,
                "min_words": quality_gate.get("min_words", 800),
                "original_content": original_content,
            },
        )
        system = rendered.get("system", "").strip()
        user = rendered.get("user", "").strip()
        try:
            cfg = self._chapter_generation_config(target_words, temperature=0.85)
            result = await self.llm_service.generate(Prompt(system=system, user=user), cfg)
            repaired = self._finalize_chapter_body_text(novel_id, result.content or "")
            min_len = int(len((original_content or "").strip()) * 0.92)
            if repaired.strip() and len(repaired.strip()) >= min_len:
                return repaired
            if repaired.strip():
                logger.warning(
                    "质量门禁自动修稿结果明显缩水，丢弃本次修稿 novel=%s chapter=%s: %d < %d",
                    novel_id,
                    chapter_number,
                    len(repaired.strip()),
                    min_len,
                )
        except Exception as e:
            logger.warning(
                "质量门禁自动修稿失败 novel=%s chapter=%s: %s",
                novel_id,
                chapter_number,
                e,
            )
        return None

    async def _apply_quality_gate_and_optional_repair(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        outline: str,
        content: str,
        bundle: Dict[str, Any],
        post: Dict[str, Any],
        target_words: int,
        scene_director: Optional[SceneDirectorAnalysis],
    ) -> tuple[str, Dict[str, Any], Dict[str, Any]]:
        quality_gate = self._build_quality_gate(
            content=content,
            outline=outline,
            target_words=target_words,
            style_warnings=post["style_warnings"],
            consistency_report=post["consistency_report"],
            ghost_annotations=post["ghost_annotations"],
            chapter_number=chapter_number,
        )
        if quality_gate["passed"]:
            return content, post, quality_gate
        if not quality_gate.get("auto_repair_required"):
            return content, post, quality_gate

        logger.info("质量门禁未通过，开始一次自动修稿: %s", "; ".join(quality_gate["reasons"]))
        quality_gate["repair_attempted"] = True
        repaired = await self._repair_chapter_against_quality_gate(
            novel_id=novel_id,
            chapter_number=chapter_number,
            outline=outline,
            original_content=content,
            bundle=bundle,
            quality_gate=quality_gate,
            style_warnings=post["style_warnings"],
            consistency_report=post["consistency_report"],
            ghost_annotations=post["ghost_annotations"],
            target_words=target_words,
            scene_director=scene_director,
        )
        if not repaired:
            return content, post, quality_gate

        repaired_post = await self.post_process_generated_chapter(
            novel_id, chapter_number, outline, repaired, scene_director=scene_director, persist_updates=False
        )
        repaired_gate = self._build_quality_gate(
            content=repaired,
            outline=outline,
            target_words=target_words,
            style_warnings=repaired_post["style_warnings"],
            consistency_report=repaired_post["consistency_report"],
            ghost_annotations=repaired_post["ghost_annotations"],
            chapter_number=chapter_number,
        )
        repaired_gate["repair_attempted"] = True
        repaired_gate["repair_applied"] = True
        repaired_gate["original_reasons"] = quality_gate["reasons"]
        return repaired, repaired_post, repaired_gate

    async def generate_chapter(
        self,
        novel_id: str,
        chapter_number: int,
        outline: str,
        scene_director: Optional[SceneDirectorAnalysis] = None,
        enable_beats: bool = True
    ) -> GenerationResult:
        """生成章节（完整工作流）

        Args:
            novel_id: 小说 ID
            chapter_number: 章节号
            outline: 章节大纲
            scene_director: 可选的场记分析结果，用于过滤角色和地点

        Returns:
            GenerationResult 包含内容、一致性报告、上下文和 token 数

        Raises:
            ValueError: 如果参数无效
            RuntimeError: 如果生成失败
        """
        outline = self._normalize_outline_against_character_canon(novel_id, outline)
        # 验证输入
        if chapter_number < 1:
            raise ValueError("chapter_number must be positive")
        if not outline or not outline.strip():
            raise ValueError("outline cannot be empty")

        logger.info(f"========================================")
        logger.info(f"开始生成章节: 小说={novel_id}, 章节={chapter_number}")
        logger.info(f"大纲: {outline[:100]}...")
        logger.info(f"========================================")

        # ★ V6: 缓存当前 novel_id/chapter_number 供 _build_prompt 中 MemoryEngine 使用
        self._current_novel_id = novel_id
        self._current_chapter_number = chapter_number
        self._current_profile_lock = self._load_profile_lock_for_novel(novel_id)

        logger.info("阶段 1-2: 规划 + 结构化上下文（prepare_chapter_generation）")
        bundle = self.prepare_chapter_generation(
            novel_id, chapter_number, outline, scene_director=scene_director
        )
        context = bundle["context"]
        self._current_character_canon_contract = bundle.get("character_canon_contract") or ""
        context_tokens = bundle["context_tokens"]
        logger.info(f"  ✓ 上下文已构建: {len(context)} 字符, 约 {context_tokens} tokens")

        logger.info("阶段 3: 生成 - 调用 LLM")
        target_words = self._resolve_target_chapter_words(novel_id)
        config = self._chapter_generation_config(target_words)
        
        # 如果使用节拍模式，先放大节拍
        beats = []
        if enable_beats:
            logger.info("  → 启用节拍模式，拆分大纲为微观节拍")
            beats = await self._magnify_outline_with_execution_plan(
                novel_id,
                chapter_number,
                outline,
                target_words,
                beat_sheet=None,
                scene_director=scene_director,
            )
            logger.info(f"  ✓ 已拆分为 {len(beats)} 个微观节拍（整章目标 {target_words} 字）")
        
        # 根据是否使用节拍选择不同的生成策略
        if enable_beats and beats:
            # 按节拍生成（可选 ATG 拓扑闸门 + 递增重试）
            graph_dom = self._maybe_action_transition_graph(scene_director)
            micro_ctx: Optional[MicroSceneContext] = None
            if graph_dom is not None and beats and scene_director:
                micro_ctx = initialize_micro_scene_context(
                    graph=graph_dom,
                    first_location_id=(beats[0].location_id or "").strip(),
                    roster=scene_director.characters or [],
                    pov=scene_director.pov,
                )

            content_parts: list[str] = []
            previous_context: Optional[BeatContext] = None
            commit_gate = DraftTopologyCommitGate()
            retry_director = EscalatingBeatRetryDirector()

            for i, beat in enumerate(beats):
                prior_draft = "\n\n".join(content_parts)
                beat_prompt_text = self.context_builder.build_beat_prompt(beat, i, len(beats))

                topo = self._spatial_topology_bundle_for_beat(
                    i, beats, graph_dom, micro_ctx, scene_director
                )

                coherence_instructions = ""
                if previous_context and prior_draft:
                    coherence_instructions = self.coherence_enhancer.generate_coherence_instructions(
                        previous_content=content_parts[-1] if content_parts else "",
                        current_beat_description=beat.description,
                        previous_context=previous_context,
                        beat_index=i,
                        total_beats=len(beats),
                    )

                    current_content_preview = f"将生成关于'{beat.description}'的内容"
                    issues = self.coherence_enhancer.check_coherence_between_beats(
                        content_parts[-1] if content_parts else "",
                        current_content_preview,
                        previous_context=previous_context,
                        current_context=self.coherence_enhancer.analyze_beat_context(
                            current_content_preview, beat.focus
                        ),
                    )

                    if issues:
                        issue_descriptions = [
                            f"- {issue.description}"
                            for issue in issues
                            if issue.severity in ["high", "medium"]
                        ]
                        if issue_descriptions:
                            coherence_instructions += "\n\n【连贯性修复要求】\n" + "\n".join(
                                issue_descriptions[:3]
                            )

                base_extra = (topo["atg_block"] or "") + (
                    ("\n\n" + coherence_instructions) if coherence_instructions else ""
                )

                attempt = 0
                max_attempts = 3
                beat_content = ""
                last_failure = ""
                illegal_names: tuple[str, ...] = ()

                while attempt < max_attempts:
                    escalating = ""
                    if attempt > 0:
                        escalating = retry_director.build_patch(
                            attempt,
                            failure_kind=last_failure,
                            edge=topo["edge"],
                            prev_loc=topo["prev_loc"],
                            curr_loc=topo["curr_loc"],
                            illegal_characters=illegal_names,
                        )

                    prompt = self._build_prompt(
                        context,
                        outline,
                        storyline_context=bundle["storyline_context"],
                        plot_tension=bundle["plot_tension"],
                        style_summary=bundle["style_summary"],
                        beat_prompt=beat_prompt_text + base_extra + escalating,
                        beat_index=i,
                        total_beats=len(beats),
                        beat_target_words=beat.target_words,
                        voice_anchors=bundle.get("voice_anchors") or "",
                        chapter_draft_so_far=prior_draft,
                        character_canon_contract=bundle.get("character_canon_contract") or "",
                    )

                    beat_config = self._beat_generation_config(beat.target_words)
                    llm_result = await self.llm_service.generate(prompt, beat_config)
                    beat_content = llm_result.content or ""

                    res_ok = True
                    if topo["use_gate"]:
                        gate_res = commit_gate.evaluate(
                            beat_content,
                            transitioning=topo["transitioning"],
                            edge=topo["edge"],
                            roster=topo["roster"],
                            active_characters=micro_ctx.active_characters,
                        )
                        res_ok = gate_res.ok
                        if not gate_res.ok:
                            last_failure = gate_res.failure_kind
                            illegal_names = gate_res.illegal_characters

                    if res_ok or attempt >= max_attempts - 1:
                        break
                    attempt += 1

                logger.info(f"生成节拍 {i+1}/{len(beats)}: {beat.focus} - {beat.description[:50]}...")

                if beat_content:
                    previous_context = self.coherence_enhancer.analyze_beat_context(
                        beat_content, beat.focus
                    )
                    logger.debug(
                        f"节拍 {i+1} 上下文分析: 角色={previous_context.characters}, 场景={previous_context.scene}"
                    )

                if micro_ctx is not None and graph_dom is not None and scene_director:
                    refresh_micro_scene_context_after_beat(
                        micro_ctx,
                        beat_location_id=topo["curr_loc"],
                        beat_text=beat_content,
                        graph=graph_dom,
                        roster=scene_director.characters or [],
                        character_extractor=self.coherence_enhancer.extract_character_names,
                    )

                content_parts.append(beat_content)
            
            content = self._finalize_chapter_body_text(novel_id, "\n\n".join(content_parts))
            logger.info(f"  ✓ 节拍生成完成: {len(beats)} 个节拍, {len(content)} 字符")
        else:
            # 传统单段生成
            prompt = self._build_prompt(
                context,
                outline,
                storyline_context=bundle["storyline_context"],
                plot_tension=bundle["plot_tension"],
                style_summary=bundle["style_summary"],
                voice_anchors=bundle.get("voice_anchors") or "",
                character_canon_contract=bundle.get("character_canon_contract") or "",
                chapter_target_words=target_words,
            )
            logger.info(f"  → 发送请求到 LLM (max_tokens={config.max_tokens}, temperature={config.temperature})")
            llm_result = await self.llm_service.generate(prompt, config)
            content = self._finalize_chapter_body_text(novel_id, llm_result.content or "")
            logger.info(f"  ✓ LLM 响应已接收: {len(content)} 字符")
        
        # 保存微观节拍用于后续处理
        if beats:
            bundle["micro_beats"] = [
                {
                    "description": beat.description,
                    "target_words": beat.target_words,
                    "focus": beat.focus,
                    "location_id": getattr(beat, "location_id", "") or "",
                }
                for beat in beats
            ]

        logger.info("阶段 4: 后处理（post_process_generated_chapter）")
        post = await self.post_process_generated_chapter(
            novel_id, chapter_number, outline, content, scene_director=scene_director, persist_updates=False
        )
        content, post, quality_gate = await self._apply_quality_gate_and_optional_repair(
            novel_id=novel_id,
            chapter_number=chapter_number,
            outline=outline,
            content=content,
            bundle=bundle,
            post=post,
            target_words=target_words,
            scene_director=scene_director,
        )
        post["memory_delta"] = await self._commit_generated_chapter_updates(
            novel_id=novel_id,
            chapter_number=chapter_number,
            outline=outline,
            content=content,
            chapter_state=post["chapter_state"],
        )
        display_beats = _beats_for_sse(beats)
        if not display_beats:
            display_beats = self._build_retrospective_micro_beats(
                content,
                target_words=target_words,
            )
            if display_beats:
                bundle["micro_beats"] = display_beats
        style_warnings = post["style_warnings"]
        consistency_report = post["consistency_report"]
        ghost_annotations = post["ghost_annotations"]
        if style_warnings:
            logger.info(f"  ✓ 俗套扫描: 检测到 {len(style_warnings)} 个俗套句式")
        if not quality_gate.get("passed"):
            logger.info("  ⚠ 质量门禁仍有未通过项: %s", "; ".join(quality_gate.get("reasons", [])))

        # Phase 5: Review - 返回结果
        logger.info(f"阶段 5: 完成 - 章节生成完成")
        token_count = context_tokens
        logger.info(f"  ✓ 总计: {len(content)} 字符, {token_count} tokens")
        logger.info(f"========================================")
        logger.info(f"章节生成完成: 小说={novel_id}, 章节={chapter_number}")
        logger.info(f"========================================")

        return GenerationResult(
            content=content,
            consistency_report=consistency_report,
            context_used=context,
            token_count=token_count,
            ghost_annotations=ghost_annotations,
            style_warnings=style_warnings,
            quality_gate=quality_gate,
        )

    async def _magnify_outline_with_execution_plan(
        self,
        novel_id: str,
        chapter_number: int,
        outline: str,
        target_words: int,
        *,
        beat_sheet: Optional[Any] = None,
        scene_director: Optional[SceneDirectorAnalysis] = None,
        emit_llm_delta: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> List[Any]:
        """章纲拆节拍：经 ``build_chapter_execution_plan_async``（与 DAG planning_outline_partition 同源）再投影为 Beat。"""
        from application.engine.dag.plan.outline_beat_planner import (
            build_chapter_execution_plan_async,
        )

        beat_sheet_json: Optional[Dict[str, Any]] = None
        if beat_sheet is not None and getattr(beat_sheet, "scenes", None):
            beat_sheet_json = {
                "scenes": [
                    {
                        "title": getattr(s, "title", "") or "",
                        "goal": getattr(s, "goal", "") or "",
                        "estimated_words": getattr(s, "estimated_words", None) or 600,
                        "pov_character": getattr(s, "pov_character", "") or "",
                        "location": getattr(s, "location", None),
                        "tone": getattr(s, "tone", None),
                        "transition_from_prev": getattr(s, "transition_from_prev", None),
                    }
                    for s in beat_sheet.scenes
                ]
            }

        chapter_plan = None
        try:
            chapter_plan = await build_chapter_execution_plan_async(
                outline,
                target_chapter_words=target_words,
                novel_id=novel_id,
                chapter_number=chapter_number,
                beat_sheet_json=beat_sheet_json,
                use_llm=True,
                emit_llm_delta=emit_llm_delta,
                llm_service=self.llm_service,
            )
        except Exception as e:
            logger.warning("章前执行计划（拆节拍）失败，降级：%s", e)

        use_plan = chapter_plan is not None and bool(chapter_plan.atoms)
        return self.context_builder.magnify_outline_to_beats(
            chapter_number,
            outline,
            target_chapter_words=target_words,
            chapter_execution_plan=chapter_plan if use_plan else None,
            beat_sheet=None if use_plan else beat_sheet,
            scene_director=scene_director,
        )

    async def generate_chapter_stream(
        self,
        novel_id: str,
        chapter_number: int,
        outline: str,
        scene_director: Optional[SceneDirectorAnalysis] = None,
        enable_beats: bool = True,
        regeneration_guidance: Optional[str] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """流式生成章节：阶段事件 + 正文 token 流 + 最终 done（含一致性报告）。

        事件类型：
        - phase: planning | context | outline_planning | prose | post（已无节拍时的正文亦走 prose）
        - beats_generated: beats 列表（指挥器微观节拍，供前端侧栏展示）
        - chunk: { text }（正文）
        - done: { content, consistency_report, token_count }
        - error: { message }

        Args:
            regeneration_guidance: 重写时的改进方向（可选）。非空时 AI 会在 prompt 中看到
                                   上一版本的问题描述，并被要求针对性改进。
        """
        try:
            outline = self._normalize_outline_against_character_canon(novel_id, outline)
            if chapter_number < 1:
                raise ValueError("chapter_number must be positive")
            if not outline or not outline.strip():
                raise ValueError("outline cannot be empty")

            logger.info(f"========================================")
            logger.info(f"开始流式生成章节: 小说={novel_id}, 章节={chapter_number}")
            logger.info(f"========================================")

            yield {"type": "phase", "phase": "planning"}
            yield {"type": "phase", "phase": "context"}
            logger.info("阶段 1-2: prepare_chapter_generation（规划 + 结构化上下文）")
            bundle = self.prepare_chapter_generation(
                novel_id, chapter_number, outline, scene_director=scene_director
            )
            context = bundle["context"]
            self._current_novel_id = novel_id
            self._current_chapter_number = chapter_number
            self._current_profile_lock = self._load_profile_lock_for_novel(novel_id)
            self._current_character_canon_contract = bundle.get("character_canon_contract") or ""
            context_tokens = bundle["context_tokens"]
            logger.info(f"  ✓ 上下文已构建: {len(context)} 字符, 约 {context_tokens} tokens")

            chunk_count = 0
            target_words = self._resolve_target_chapter_words(novel_id)
            config = self._chapter_generation_config(target_words)

            beats: List[Any] = []
            if enable_beats:
                yield {"type": "phase", "phase": "outline_planning"}
                logger.info("阶段 3a: 章前规划 — 节拍划分 LLM（SSE llm_chunk / outline_partition）")
                chunk_queue: asyncio.Queue[str] = asyncio.Queue()

                async def emit_outline_partition(text: str) -> None:
                    if text:
                        await chunk_queue.put(text)

                plan_task = asyncio.create_task(
                    self._magnify_outline_with_execution_plan(
                        novel_id,
                        chapter_number,
                        outline,
                        target_words,
                        beat_sheet=None,
                        scene_director=scene_director,
                        emit_llm_delta=emit_outline_partition,
                    )
                )

                while not plan_task.done():
                    try:
                        piece = await asyncio.wait_for(chunk_queue.get(), timeout=0.05)
                        yield {"type": "llm_chunk", "stage": "outline_partition", "text": piece}
                    except asyncio.TimeoutError:
                        continue

                while True:
                    try:
                        piece = chunk_queue.get_nowait()
                        yield {"type": "llm_chunk", "stage": "outline_partition", "text": piece}
                    except asyncio.QueueEmpty:
                        break

                exc = plan_task.exception()
                if exc is not None:
                    raise exc
                beats = plan_task.result()

                logger.info(f"  ✓ 已拆分为 {len(beats)} 个微观节拍（整章目标 {target_words} 字）")

                beats_payload = _beats_for_sse(beats)
                yield {"type": "beats_generated", "beats": beats_payload}

            # 根据是否使用节拍选择不同的生成策略
            if enable_beats and beats:
                yield {"type": "phase", "phase": "prose"}
                logger.info("阶段 3b: 正文撰写 — 按节拍流式生成")
                graph_dom = self._maybe_action_transition_graph(scene_director)
                micro_ctx: Optional[MicroSceneContext] = None
                if graph_dom is not None and beats and scene_director:
                    micro_ctx = initialize_micro_scene_context(
                        graph=graph_dom,
                        first_location_id=(beats[0].location_id or "").strip(),
                        roster=scene_director.characters or [],
                        pov=scene_director.pov,
                    )

                content_parts: list[str] = []
                previous_context: Optional[BeatContext] = None
                commit_gate = DraftTopologyCommitGate()
                retry_director = EscalatingBeatRetryDirector()

                for i, beat in enumerate(beats):
                    prior_draft = "\n\n".join(content_parts)
                    beat_prompt_text = self.context_builder.build_beat_prompt(beat, i, len(beats))

                    topo = self._spatial_topology_bundle_for_beat(
                        i, beats, graph_dom, micro_ctx, scene_director
                    )

                    coherence_instructions = ""
                    if previous_context and prior_draft:
                        coherence_instructions = self.coherence_enhancer.generate_coherence_instructions(
                            previous_content=content_parts[-1] if content_parts else "",
                            current_beat_description=beat.description,
                            previous_context=previous_context,
                            beat_index=i,
                            total_beats=len(beats),
                        )

                    base_extra = (topo["atg_block"] or "") + (
                        ("\n\n" + coherence_instructions) if coherence_instructions else ""
                    )

                    attempt = 0
                    max_attempts = 3
                    beat_content = ""
                    last_failure = ""
                    illegal_names: tuple[str, ...] = ()
                    last_try = ""

                    while attempt < max_attempts:
                        escalating = ""
                        if attempt > 0:
                            escalating = retry_director.build_patch(
                                attempt,
                                failure_kind=last_failure,
                                edge=topo["edge"],
                                prev_loc=topo["prev_loc"],
                                curr_loc=topo["curr_loc"],
                                illegal_characters=illegal_names,
                            )

                        prompt = self._build_prompt(
                            context,
                            outline,
                            storyline_context=bundle["storyline_context"],
                            plot_tension=bundle["plot_tension"],
                            style_summary=bundle["style_summary"],
                            beat_prompt=beat_prompt_text + base_extra + escalating,
                            beat_index=i,
                            total_beats=len(beats),
                            beat_target_words=beat.target_words,
                            voice_anchors=bundle.get("voice_anchors") or "",
                            chapter_draft_so_far=prior_draft,
                            regeneration_guidance=regeneration_guidance if i == 0 else None,
                            character_canon_contract=bundle.get("character_canon_contract") or "",
                        )

                        buffered: list[str] = []
                        beat_try = ""
                        leaked = False
                        guard = topo.get("guard")
                        beat_config = self._beat_generation_config(beat.target_words)
                        async for piece in self.llm_service.stream_generate(prompt, beat_config):
                            beat_try += piece
                            if guard:
                                hit = guard.check(beat_try)
                                if hit:
                                    leaked = True
                                    break
                            buffered.append(piece)

                        last_try = beat_try or last_try

                        if leaked:
                            attempt += 1
                            continue

                        res_ok = True
                        if topo["use_gate"]:
                            gate_res = commit_gate.evaluate(
                                beat_try,
                                transitioning=topo["transitioning"],
                                edge=topo["edge"],
                                roster=topo["roster"],
                                active_characters=micro_ctx.active_characters,
                            )
                            res_ok = gate_res.ok
                            if not gate_res.ok:
                                last_failure = gate_res.failure_kind
                                illegal_names = gate_res.illegal_characters

                        if res_ok or attempt >= max_attempts - 1:
                            beat_content = beat_try
                            for piece in buffered:
                                chunk_count += 1
                                yield {
                                    "type": "chunk",
                                    "text": piece,
                                    "beat_index": i,
                                    "beat_focus": beat.focus,
                                }
                            break

                        attempt += 1

                    if not beat_content:
                        beat_content = last_try or ""

                    logger.info(
                        f"生成节拍 {i+1}/{len(beats)}: {beat.focus} - {beat.description[:50]}..."
                    )

                    if beat_content:
                        previous_context = self.coherence_enhancer.analyze_beat_context(
                            beat_content, beat.focus
                        )

                    if micro_ctx is not None and graph_dom is not None and scene_director:
                        refresh_micro_scene_context_after_beat(
                            micro_ctx,
                            beat_location_id=topo["curr_loc"],
                            beat_text=beat_content,
                            graph=graph_dom,
                            roster=scene_director.characters or [],
                            character_extractor=self.coherence_enhancer.extract_character_names,
                        )

                    content_parts.append(beat_content)
                    yield {"type": "beat_done", "beat_index": i, "beat_content_length": len(beat_content)}
                
                content = self._finalize_chapter_body_text(novel_id, "\n\n".join(content_parts))
            else:
                yield {"type": "phase", "phase": "prose"}
                logger.info("阶段 3: 正文撰写 — 单段流式生成")
                # 传统单段生成
                prompt = self._build_prompt(
                    context,
                    outline,
                    storyline_context=bundle["storyline_context"],
                    plot_tension=bundle["plot_tension"],
                    style_summary=bundle["style_summary"],
                    voice_anchors=bundle.get("voice_anchors") or "",
                    regeneration_guidance=regeneration_guidance,
                    chapter_target_words=target_words,
                    character_canon_contract=bundle.get("character_canon_contract") or "",
                )
                
                logger.info(f"  → 发送流式请求到 LLM")
                parts: list[str] = []
                total_chars = 0
                async for piece in self.llm_service.stream_generate(prompt, config):
                    parts.append(piece)
                    chunk_count += 1
                    total_chars += len(piece)
                    # 增强事件：包含累计字数和预估 token（中文约 1.5 字/token，英文约 4 字/token）
                    estimated_tokens = int(total_chars / 1.5)  # 简化估算
                    yield {
                        "type": "chunk", 
                        "text": piece,
                        "stats": {
                            "chars": total_chars,
                            "chunks": chunk_count,
                            "estimated_tokens": estimated_tokens,
                        }
                    }

                content = self._finalize_chapter_body_text(novel_id, "".join(parts))
            logger.info(f"  ✓ LLM 流式响应完成: {chunk_count} 个块, {len(content)} 字符")

            if not content.strip():
                logger.error("  × 模型返回空内容")
                yield {"type": "error", "message": "模型返回空内容"}
                return

            yield {"type": "phase", "phase": "post"}
            logger.info("阶段 4: post_process_generated_chapter_stream")
            post: Dict[str, Any] = {}
            async for post_event in self.post_process_generated_chapter_stream(
                novel_id, chapter_number, outline, content, scene_director=scene_director
            ):
                if post_event.get("type") == "post_result":
                    candidate = post_event.get("post")
                    if isinstance(candidate, dict):
                        post = candidate
                    continue
                yield post_event
            if not post:
                raise RuntimeError("章节后处理未返回结果")
            yield {"type": "post_step", "step": "quality_gate", "message": "执行质量门禁"}
            content, post, quality_gate = await self._apply_quality_gate_and_optional_repair(
                novel_id=novel_id,
                chapter_number=chapter_number,
                outline=outline,
                content=content,
                bundle=bundle,
                post=post,
                target_words=target_words,
                scene_director=scene_director,
            )
            yield {"type": "post_step", "step": "memory_commit", "message": "回写状态与记忆"}
            try:
                post["memory_delta"] = await asyncio.wait_for(
                    self._commit_generated_chapter_updates(
                        novel_id=novel_id,
                        chapter_number=chapter_number,
                        outline=outline,
                        content=content,
                        chapter_state=post["chapter_state"],
                    ),
                    timeout=_STREAM_POST_MEMORY_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "章节状态/记忆回写超时，跳过本次回写 novel=%s chapter=%s timeout=%.1fs",
                    novel_id,
                    chapter_number,
                    _STREAM_POST_MEMORY_TIMEOUT_SECONDS,
                )
                post["memory_delta"] = {"skipped": True, "reason": "timeout"}
                yield {
                    "type": "post_step",
                    "step": "memory_commit_timeout",
                    "message": "状态/记忆回写超时，已跳过本次回写",
                    "level": "warning",
                }
            display_beats = _beats_for_sse(beats)
            if not display_beats:
                yield {"type": "post_step", "step": "beats_review", "message": "整理章节节拍"}
                display_beats = self._build_retrospective_micro_beats(
                    content,
                    target_words=target_words,
                )
                if display_beats:
                    bundle["micro_beats"] = display_beats
                    yield {"type": "beats_generated", "beats": display_beats}
            yield {"type": "quality_gate", **quality_gate}
            style_warnings = post["style_warnings"]
            consistency_report = post["consistency_report"]
            ghost_annotations = post["ghost_annotations"]
            if style_warnings:
                logger.info(f"  ✓ 俗套扫描: 检测到 {len(style_warnings)} 个俗套句式")

            token_count = context_tokens
            output_tokens = int(len(content) / 1.5)  # 预估输出 token
            total_tokens = token_count + output_tokens
            logger.info(f"========================================")
            logger.info(f"流式章节生成完成: 小说={novel_id}, 章节={chapter_number}")
            logger.info(f"  输出: {len(content)} 字符, 约 {output_tokens} tokens")
            logger.info(f"  总计: 约 {total_tokens} tokens (上下文 {token_count} + 输出 {output_tokens})")
            logger.info(f"========================================")

            yield {
                "type": "done",
                "content": content,
                "consistency_report": _consistency_report_to_dict(consistency_report),
                "token_count": token_count,
                "output_tokens": output_tokens,
                "total_tokens": total_tokens,
                "chars": len(content),
                "beats": display_beats,
                "quality_gate": quality_gate,
                "ghost_annotations": [ann.to_dict() for ann in ghost_annotations],
                "style_warnings": [
                    {
                        "pattern": hit.pattern,
                        "text": hit.text,
                        "start": hit.start,
                        "end": hit.end,
                        "severity": hit.severity,
                    }
                    for hit in style_warnings
                ],
            }
        except ValueError as e:
            logger.error(f"参数错误: {e}")
            yield {"type": "error", "message": str(e)}
        except Exception as e:
            logger.exception("流式生成章节失败")
            yield {"type": "error", "message": str(e)}

    async def suggest_outline(self, novel_id: str, chapter_number: int) -> str:
        """托管模式：用全书上下文让模型生成本章要点大纲；失败则回退为简短占位。"""
        seed = f"第{chapter_number}章：承接前情，推进主线与人物节拍；保持人设与叙事节奏一致。"
        try:
            context = self.context_builder.build_context(
                novel_id=novel_id,
                chapter_number=chapter_number,
                outline=seed,
                max_tokens=28000,
            )
            cap = min(len(context), 28000)
            rendered = render_prompt(
                _CHAPTER_OUTLINE_SUGGEST_NODE_KEY,
                {
                    "context": context[:cap],
                    "chapter_number": chapter_number,
                },
            )
            outline_prompt = Prompt(system=rendered.get("system", ""), user=rendered.get("user", ""))
            cfg = GenerationConfig(max_tokens=1024, temperature=0.7)
            out = await self.llm_service.generate(outline_prompt, cfg)
            text = strip_reasoning_artifacts((out.content or "").strip())
            if text:
                return self._normalize_outline_against_character_canon(novel_id, text)
        except Exception as e:
            logger.warning("suggest_outline failed: %s", e)
        return self._normalize_outline_against_character_canon(novel_id, seed)

    async def generate_chapter_with_review(
        self,
        novel_id: str,
        chapter_number: int,
        outline: str
    ) -> Tuple[str, ConsistencyReport]:
        """生成章节并返回一致性审查

        Args:
            novel_id: 小说 ID
            chapter_number: 章节号
            outline: 章节大纲

        Returns:
            (content, consistency_report) 元组
        """
        result = await self.generate_chapter(novel_id, chapter_number, outline)
        return result.content, result.consistency_report

    def _get_storyline_context(self, novel_id: str, chapter_number: int) -> str:
        """获取故事线上下文

        Args:
            novel_id: 小说 ID
            chapter_number: 章节号

        Returns:
            故事线上下文字符串
        """
        try:
            # 检查 storyline_manager 是否有 repository 属性
            if not hasattr(self.storyline_manager, 'repository'):
                return "Storyline context unavailable"

            # 获取所有活跃的故事线
            storylines = self.storyline_manager.repository.get_by_novel_id(NovelId(novel_id))
            active_storylines = [
                s for s in storylines
                if s.status.value == "active"
                and s.estimated_chapter_start <= chapter_number <= s.estimated_chapter_end
            ]

            if not active_storylines:
                return "No active storylines for this chapter"

            context_parts = []
            for storyline in active_storylines:
                context = self.storyline_manager.get_storyline_context(storyline.id)
                context_parts.append(context)

            return "\n\n".join(context_parts)
        except Exception as e:
            logger.warning(f"Failed to get storyline context: {e}")
            return "Storyline context unavailable"

    def _get_plot_tension(self, novel_id: str, chapter_number: int) -> str:
        """获取情节张力信息——融合预设锚点 + 前章实际张力评分，形成闭环反馈。

        Args:
            novel_id: 小说 ID
            chapter_number: 章节号

        Returns:
            情节张力描述（含预设期望 + 前章实际 + 调整指令）
        """
        parts: list[str] = []

        # 1. 预设张力（来自 PlotArc 锚点）
        try:
            plot_arc = self.plot_arc_repository.get_by_novel_id(NovelId(novel_id))
            if plot_arc and plot_arc.key_points:
                tension = plot_arc.get_expected_tension(chapter_number)
                next_point = plot_arc.get_next_plot_point(chapter_number)
                # ★ Phase 3: 使用 PlotArc 的 get_expected_tension_100 方法（含非线性插值）
                tension_100 = plot_arc.get_expected_tension_100(chapter_number)
                parts.append(f"预设期望张力等级：{tension_100}/100（{tension.display_name}）")
                if next_point:
                    parts.append(
                        f"下一个锚点：第{next_point.chapter_number}章 - {next_point.description}"
                    )
        except Exception as e:
            logger.warning(f"Failed to get plot arc tension: {e}")

        # 2. 前章实际张力评分（闭环反馈的核心）
        prev_actual_tension = None
        try:
            from interfaces.api.dependencies import get_chapter_repository
            from domain.novel.value_objects.novel_id import NovelId as NId
            chapter_repo = get_chapter_repository()
            db = chapter_repo.db if hasattr(chapter_repo, 'db') else None
            if db is not None and chapter_number > 1:
                row = db.fetch_one(
                    "SELECT tension_score, plot_tension, emotional_tension, pacing_tension "
                    "FROM chapters WHERE novel_id = ? AND number = ?",
                    (novel_id, chapter_number - 1)
                )
                if row and row['tension_score'] is not None and row['tension_score'] != -1:
                    prev_actual_tension = float(row['tension_score'])
                    parts.append(
                        f"前章（第{chapter_number - 1}章）实际张力评分：{prev_actual_tension:.0f}/100"
                        f"（情节={float(row['plot_tension'] or 0):.0f} "
                        f"情绪={float(row['emotional_tension'] or 0):.0f} "
                        f"节奏={float(row['pacing_tension'] or 0):.0f}）"
                    )
        except Exception as e:
            logger.debug(f"Failed to get prev chapter tension: {e}")

        # 3. 张力调整指令（基于预设与实际的差距）
        # ★ Phase 1: 前三章硬编码高张力指令（冷启动保护）
        if chapter_number <= 3:
            early_chapter_directives = {
                1: (
                    "🔥【首章目标】本章是读者决定是否继续阅读的关键！"
                    "综合张力目标：≥65。"
                    "第一个节拍制造强烈冲突、悬念或反转。"
                    "开篇以事件、动作或压迫切入，减少大段背景介绍、设定灌输和平淡日常。"
                    "主角在被压制或轻视后展露底牌，给读者第一次爽感冲击。"
                ),
                2: (
                    "⚡【次章加速】读者已被首章吸引，但不能松劲！"
                    "综合张力目标：≥55。"
                    "本章安排至少一次实力验证、身份暗示或新冲突爆发。"
                    "继续升温，扩大主角展现出的特殊之处引发的反响。"
                ),
                3: (
                    "⚡【三章定乾坤】前三章决定了读者是否追读！"
                    "综合张力目标：≥60。"
                    "本章安排一次真正的高潮场景，"
                    "可以是第一次正式对抗、博弈或危机中的大逆转。"
                    "读者在这里应该获得明确的'这书好看'的正反馈！"
                ),
            }
            early_directive = early_chapter_directives.get(chapter_number, "")
            if early_directive:
                parts.append(early_directive)
        elif prev_actual_tension is not None:
            if prev_actual_tension <= 30:
                parts.append(
                    "⚠ 张力修正：前章张力严重不足。本章制造至少一次核心冲突、反转或悬念，"
                    "将综合张力拉升到 55 以上。建议：引入新威胁、暴露隐藏信息、"
                    "让角色做出痛苦选择。"
                )
            elif prev_actual_tension <= 45:
                parts.append(
                    "⚡ 调整指令：前章张力偏低，读者可能正在流失。"
                    "本章应逐步升温，增加信息不对称、加深角色矛盾、"
                    "让阻碍变得更加紧迫。目标张力：50-65。"
                )
            elif prev_actual_tension >= 80:
                parts.append(
                    "📊 缓冲指令：前章已是高潮，本章应给读者喘息空间。"
                    "可以写角色消化冲击、盟友互动、新线索浮现，"
                    "但结尾要留一个钩子暗示更大的风暴即将到来。目标张力：40-55。"
                )
            elif prev_actual_tension >= 65:
                parts.append(
                    "📊 维持指令：前章张力较高，本章可以保持高压推进，"
                    "也可以适当喘息后再次攀升。避免连续高压导致读者疲劳。"
                )

        if not parts:
            return "No plot arc defined"

        return "\n".join(parts)

    def build_chapter_prompt(
        self,
        context: str,
        outline: str,
        *,
        storyline_context: str = "",
        plot_tension: str = "",
        style_summary: str = "",
        beat_prompt: Optional[str] = None,
        beat_index: Optional[int] = None,
        total_beats: Optional[int] = None,
        beat_target_words: Optional[int] = None,
        voice_anchors: str = "",
        chapter_draft_so_far: str = "",
        character_canon_contract: str = "",
    ) -> Prompt:
        """构建与 HTTP 单章 / 流式 / 托管按节拍写作一致的 Prompt（对外 API）。"""
        return self._build_prompt(
            context,
            outline,
            storyline_context=storyline_context,
            plot_tension=plot_tension,
            style_summary=style_summary,
            beat_prompt=beat_prompt,
            beat_index=beat_index,
            total_beats=total_beats,
            beat_target_words=beat_target_words,
            voice_anchors=voice_anchors,
            chapter_draft_so_far=chapter_draft_so_far,
            character_canon_contract=character_canon_contract,
        )

    def _build_prompt(
        self,
        context: str,
        outline: str,
        *,
        storyline_context: str = "",
        plot_tension: str = "",
        style_summary: str = "",
        beat_prompt: Optional[str] = None,
        beat_index: Optional[int] = None,
        total_beats: Optional[int] = None,
        beat_target_words: Optional[int] = None,
        voice_anchors: str = "",
        chapter_draft_so_far: str = "",
        regeneration_guidance: Optional[str] = None,
        chapter_target_words: Optional[int] = None,
        character_canon_contract: str = "",
    ) -> Prompt:
        """构建 LLM 提示词

        Args:
            context: 完整上下文
            outline: 章节大纲
            storyline_context: 当前章相关故事线与里程碑（Phase 1）
            plot_tension: 情节弧期望张力与下一锚点（Phase 1）
            style_summary: 风格指纹摘要（Phase 2.5）
            beat_prompt: 非空时进入「分节拍」模式（托管断点续写）
            beat_index / total_beats: 节拍序号（0-based / 总数）
            beat_target_words: 本段目标字数（分节拍时覆盖整章说明）
            voice_anchors: Bible 角色声线/小动作锚点（高优先级 System 提示）
            chapter_draft_so_far: 同章内当前节拍之前已生成的正文
            chapter_target_words: 非 beat 模式下的整章目标字数（覆盖默认硬编码值）
            character_canon_contract: Bible 角色正典锁（姓名、身份、关系、隐藏信息等硬约束）

        Returns:
            Prompt 对象
        """
        sc = (storyline_context or "").strip()
        pt = (plot_tension or "").strip()
        ss = (style_summary or "").strip()
        va = (voice_anchors or "").strip()
        cc = (character_canon_contract or self._current_character_canon_contract or "").strip()
        planning_parts: list[str] = []
        if sc and sc not in ("Storyline context unavailable",):
            planning_parts.append(f"【故事线 / 里程碑】\n{sc}")
        if pt and pt not in ("Plot tension unavailable", "No plot arc defined"):
            planning_parts.append(f"【情节节奏 / 张力控制】\n{pt}")
        if ss:
            planning_parts.append(f"【风格约束】\n{ss}")
        planning_section = ""
        if planning_parts:
            planning_section = (
                "\n".join(planning_parts)
                + "\n\n以上内容与本章大纲及后文 Bible/摘要保持一致。\n"
            )

        voice_block = ""
        if va:
            voice_block = (
                "\n【角色声线与肢体语言（Bible 锚点）】\n"
                f"{va}\n\n"
            )

        beat_mode = bool((beat_prompt or "").strip())
        prior_in_chapter = format_prior_draft_for_prompt(chapter_draft_so_far)
        # 字数控制：像小说家一样自然收束，而非粗暴截断
        if beat_target_words:
            length_rule = (
                f"【字数指引】本节拍约 {beat_target_words} 字。"
                f"用有信息的对话、动作与因果推进填到目标附近，减少同一震撼或同一情绪的反复描写；"
                f"收束用完整句，给本节拍一个清晰落点。"
            )
        elif beat_mode:
            length_rule = "按下方节拍说明控制篇幅，勿写章节标题"
        elif chapter_target_words:
            length_rule = (
                f"【章节字数指引】本章目标约 {chapter_target_words} 字。"
                f"完整覆盖下方大纲的所有要点，字数不足时优先补充对话与场景细节，减少重复情节水字；"
                f"用完整句收束，给本章一个清晰落点。"
            )
        else:
            length_rule = "章节长度：3000-4000字"
        beat_extra = ""
        if beat_mode and beat_index is not None and total_beats is not None and total_beats > 0:
            if prior_in_chapter:
                beat_extra = (
                    f"\n本章第 {beat_index + 1}/{total_beats} 段：用户消息中「本章已生成正文」为当前章已写部分，"
                    "请从其**之后**自然续写，沿用其中已发生对白与情节。\n"
                )
            else:
                beat_extra = (
                    f"\n本章第 {beat_index + 1}/{total_beats} 段：与前后节拍连贯，避免同章内重复铺垫或重复对白。\n"
                )

        # ★ V6: 从 MemoryEngine 获取 fact_lock 文本块（T0 注入）
        fact_lock = ""
        if self.memory_engine:
            try:
                # 从 context 中提取 novel_id（通过 budget_allocator 传递）
                # 这里用组合方式：FACT_LOCK + BEATS + CLUES 合并为一个文本块
                fl = self.memory_engine.build_fact_lock_section(
                    self._current_novel_id or "", self._current_chapter_number or 0
                )
                beats = self.memory_engine.get_completed_beats_section(
                    self._current_novel_id or ""
                )
                clues = self.memory_engine.get_revealed_clues_section(
                    self._current_novel_id or ""
                )
                parts = [p for p in [fl, beats, clues] if p.strip()]
                fact_lock = "\n\n".join(parts) if parts else ""
            except Exception as e:
                logger.warning(f"MemoryEngine fact_lock 构建失败: {e}")

        # ★ Theme 集成：获取系统人设和写作规则
        theme_persona = ""
        theme_rules = ""
        format_rules = ""
        battle_enhancement = ""

        if self._theme_integrator:
            try:
                theme_persona = self._theme_integrator.build_system_persona()
                theme_rules = self._theme_integrator.build_writing_rules()
                format_rules = self._theme_integrator.build_format_rules()

                # 战斗场景检测和增强
                if beat_mode and beat_prompt:
                    battle_enhancement = self._theme_integrator.build_beat_enhancement(
                        beat_prompt,
                        beat_focus="",
                        chapter_number=self._current_chapter_number or 0,
                        outline=outline,
                        style_summary=ss,
                        chapter_progress=chapter_draft_so_far,
                        beat_index=beat_index,
                        total_beats=total_beats,
                    )
            except Exception as e:
                logger.debug(f"Theme 增强构建失败: {e}")

        # ★★★ 爽文引擎: 动态 Prompt 模板方案 ★★★
        # 架构决策：不在 autopilot_daemon 中硬编码规则引擎，
        # 而是在 workflow 的 Prompt 构建层注入动态爽文约束。
        # 这样 LLM 在强约束下自行发挥爽点呈现形式，比硬编码更灵活。
        shuangwen_directive = self._build_shuangwen_directive(
            chapter_number=self._current_chapter_number or 0,
            beat_mode=beat_mode,
            beat_index=beat_index,
            total_beats=total_beats,
            beat_prompt=beat_prompt or "",
            outline=outline,
        )

        prose_discipline = build_prose_discipline_block(
            beat_mode=beat_mode,
            beat_target_words=beat_target_words,
        )

        # ⚡ 提示词集中管理说明：
        # 此模板对应 prompt_packages/nodes/chapter-generation-main（CPMS chapter-generation-main）
        # CPMS: 优先从 PromptRegistry 获取模板，不可用时使用硬编码回退
        system_template = self._get_workflow_system_template()
        user_template = self._get_workflow_user_template()

        # 使用模板渲染（兼容 CPMS 模板和硬编码回退）
        # SafeDict: 用户在提示词广场编辑模板时可能引入未知变量，
        # 需要安全降级——未匹配的变量保留为 {name} 占位符，而非抛出 KeyError
        system_vars = {
            "theme_persona": theme_persona,
            "theme_rules": theme_rules,
            "planning_section": planning_section,
            "voice_block": voice_block,
            "behavior_protocol": "",
            "character_state_lock": "",
            "nervous_habits": "",
            "allowlist_block": "",
            "context": context,
            "character_canon_contract": cc,
            "fact_lock": fact_lock,
            "shuangwen_directive": shuangwen_directive,
            "prose_discipline": prose_discipline,
            "length_rule": length_rule,
            "beat_extra": beat_extra,
            "format_rules": format_rules,
        }
        system_message = _safe_format(system_template, system_vars)

        theme_block = "\n".join(
            block.strip()
            for block in (theme_persona, theme_rules, format_rules)
            if block and block.strip()
        )
        if theme_block and not any(
            marker in system_message
            for marker in ("作家风格", "题材专项规则", "题材格式", "题材禁忌")
        ):
            system_message = system_message.rstrip() + "\n\n" + theme_block

        # 旧版 CPMS 模板可能未含 {prose_discipline} 占位符：仍注入行文质量块，避免升级后长期不生效
        if "行文目标：反八股 / 控水分" not in system_message:
            system_message = system_message.rstrip() + "\n\n" + prose_discipline

        if shuangwen_directive.strip() and "爽文引擎写作目标" not in system_message:
            system_message = system_message.rstrip() + "\n\n" + shuangwen_directive

        if "人名硬约束" not in system_message:
            system_message = system_message.rstrip() + "\n\n" + _render_generation_prompt_text(
                _CHAPTER_NAME_CANON_GUARD_NODE_KEY,
                {},
            )

        if self._current_profile_lock and "故事内核锁" not in system_message:
            system_message = system_message.rstrip() + "\n\n" + self._current_profile_lock

        if cc and "角色正典锁" not in system_message:
            system_message = system_message.rstrip() + "\n\n" + cc

        if "上下文对齐协议" not in system_message:
            system_message = system_message.rstrip() + "\n\n" + self._build_context_alignment_protocol(context, outline)

        beat_section_intro = "见下方【节拍】补充；只写当前节拍正文并自然承接同章已生成内容。" if beat_mode else "非分节拍生成；按本章大纲写完整章节正文。"
        user_message = _safe_format(
            user_template,
            {"outline": outline, "beat_section": beat_section_intro},
        )
        user_message = re.sub(r"结尾别收干净。.*?让读者非看下一章不可。?", "", user_message)
        user_message = re.sub(r"对话说到一半|一句话说到一半停了", "用未解决问题或新代价作为钩子", user_message)
        system_message = re.sub(r"结尾别收干净。.*?让读者非看下一章不可。?", "", system_message)
        system_message = re.sub(r"对话说到一半|一句话说到一半停了", "用未解决问题或新代价作为钩子", system_message)
        completion_contract = _render_generation_prompt_text(_CHAPTER_COMPLETION_CONTRACT_NODE_KEY, {})
        if "完整章节收束" not in system_message and "结尾要有落点" not in user_message:
            system_message = system_message.rstrip() + "\n\n" + completion_contract
        if "完整对白" not in user_message:
            user_message = user_message.rstrip() + "\n\n" + completion_contract

        if beat_mode and prior_in_chapter:
            # V2：基于锚点的动态连贯性要求
            try:
                from application.workflows.beat_continuation import extract_beat_tail_anchor
                anchor = extract_beat_tail_anchor(prior_in_chapter)
                coherence_rules = _build_dynamic_coherence_rules(anchor)
            except Exception:
                coherence_rules = _render_generation_prompt_text(
                    _CHAPTER_BEAT_TRANSITION_FALLBACK_NODE_KEY,
                    {"beat_index": beat_index or 0, "total_beats": total_beats or 1},
                )

            user_message += "\n\n" + _render_generation_prompt_text(
                _CHAPTER_BEAT_CONTINUATION_NODE_KEY,
                {
                    "prior_in_chapter": prior_in_chapter,
                    "coherence_rules": coherence_rules,
                },
            )

        if beat_mode:
            bi = beat_index if beat_index is not None else 0
            tb = total_beats if total_beats is not None else 1
            beat_tail = (
                "本段只写该节拍对应正文，紧接上文已写正文之后继续，衔接自然。"
                if prior_in_chapter
                else "本段只写该节拍对应正文，与全章其它节拍情节连贯。"
            )

            # 节拍间过渡指导（V2：基于前节拍尾部锚点的精确衔接指令）
            transition_guide = ""
            if prior_in_chapter and bi > 0:
                try:
                    from application.workflows.beat_continuation import (
                        extract_beat_tail_anchor,
                        build_beat_transition_directive,
                    )
                    anchor = extract_beat_tail_anchor(prior_in_chapter)
                    next_beat_desc = (beat_prompt or "").strip()[:80] if beat_prompt else ""
                    transition_guide = "\n\n" + build_beat_transition_directive(
                        anchor, bi, tb, next_beat_desc,
                    )
                except Exception as e:
                    logger.debug(f"节拍衔接锚点提取失败，降级通用过渡: {e}")
                    transition_guide = "\n\n" + _render_generation_prompt_text(
                        _CHAPTER_BEAT_TRANSITION_FALLBACK_NODE_KEY,
                        {"beat_index": bi, "total_beats": tb},
                    )

            # ★ 战斗场景增强
            battle_hint = ""
            if battle_enhancement:
                battle_hint = f"\n\n{battle_enhancement}"

            user_message += f"""

【节拍 {bi + 1}/{tb}】
{(beat_prompt or '').strip()}

{beat_tail}{transition_guide}{battle_hint}"""

        # 重写指导注入：告知 AI 这是重写任务，并提供改进方向
        if regeneration_guidance and regeneration_guidance.strip():
            user_message += "\n\n" + _render_generation_prompt_text(
                _CHAPTER_REGENERATION_GUIDANCE_NODE_KEY,
                {"regeneration_guidance": regeneration_guidance.strip()},
            )

        user_message += "\n\n开始撰写："

        return Prompt(system=system_message, user=user_message)

    # ─── CPMS 模板获取辅助方法 ───

    def _get_workflow_system_template(self) -> str:
        """获取主工作流 system 模板（DB 优先，失败时读 prompt_packages）。

        设计决策：
        - 主工作流的 system prompt 包含大量动态变量（theme_persona, fact_lock 等），
          不适合直接用 Registry.render() 一步渲染，而是获取模板后由 _build_prompt 手动 format。
        - 如果 PromptRegistry 中注册了 chapter-generation-main 节点，
          用户可在提示词广场直接编辑此模板并实时生效。

        Returns:
            system prompt 模板字符串（含 {variable} 占位符）
        """
        try:
            from infrastructure.ai.prompt_registry import get_prompt_registry
            registry = get_prompt_registry()
            system = registry.get_system(_WORKFLOW_CHAPTER_GEN_NODE_KEY)
            if system:
                if _looks_like_legacy_chapter_system_template(system):
                    package_system = _read_prompt_package_template(_WORKFLOW_CHAPTER_GEN_NODE_KEY, "system.md")
                    if package_system:
                        logger.debug(
                            "CPMS: Registry 主模板疑似旧版，使用 prompt_packages system (node_key=%s)",
                            _WORKFLOW_CHAPTER_GEN_NODE_KEY,
                        )
                        return package_system
                logger.debug(
                    "CPMS: 使用 Registry 模板 (node_key=%s)", _WORKFLOW_CHAPTER_GEN_NODE_KEY
                )
                return system
        except Exception as exc:
            logger.debug(
                "PromptRegistry 不可用 (node_key=%s): %s", _WORKFLOW_CHAPTER_GEN_NODE_KEY, exc
            )

        package_system = _read_prompt_package_template(_WORKFLOW_CHAPTER_GEN_NODE_KEY, "system.md")
        if package_system:
            logger.debug("CPMS: 使用 prompt_packages system 模板")
            return package_system
        rendered = render_prompt(_WORKFLOW_CHAPTER_GEN_NODE_KEY, {})
        return rendered.get("system", "")

    def _get_workflow_user_template(self) -> str:
        """获取主工作流 user 模板（DB 优先，失败时读 prompt_packages）。

        同 _get_workflow_system_template 的设计决策：
        - 获取模板文本，后续由 _build_prompt 根据节拍模式追加更多段落。

        Returns:
            user prompt 模板字符串（含 {variable} 占位符）
        """
        try:
            from infrastructure.ai.prompt_registry import get_prompt_registry
            registry = get_prompt_registry()
            user_template = registry.get_user_template(_WORKFLOW_CHAPTER_GEN_NODE_KEY)
            if user_template:
                if _looks_like_legacy_chapter_user_template(user_template):
                    package_user = _read_prompt_package_template(_WORKFLOW_CHAPTER_GEN_NODE_KEY, "user.md")
                    if package_user:
                        logger.debug(
                            "CPMS: Registry user_template 疑似旧版，使用 prompt_packages user (node_key=%s)",
                            _WORKFLOW_CHAPTER_GEN_NODE_KEY,
                        )
                        return package_user
                logger.debug(
                    "CPMS: 使用 Registry user_template (node_key=%s)", _WORKFLOW_CHAPTER_GEN_NODE_KEY
                )
                return user_template
        except Exception as exc:
            logger.debug(
                "PromptRegistry 不可用 (node_key=%s): %s", _WORKFLOW_CHAPTER_GEN_NODE_KEY, exc
            )

        package_user = _read_prompt_package_template(_WORKFLOW_CHAPTER_GEN_NODE_KEY, "user.md")
        if package_user:
            logger.debug("CPMS: 使用 prompt_packages user_template")
            return package_user
        rendered = render_prompt(_WORKFLOW_CHAPTER_GEN_NODE_KEY, {})
        return rendered.get("user", "")

    async def _extract_chapter_state(self, content: str, chapter_number: int) -> ChapterState:
        """从生成的内容中提取章节状态

        Args:
            content: 生成的章节内容
            chapter_number: 章节号

        Returns:
            ChapterState 对象
        """
        # 如果有 StateExtractor，使用它提取状态
        if self.state_extractor:
            try:
                logger.info(f"Extracting chapter state using StateExtractor for chapter {chapter_number}")
                return await self.state_extractor.extract_chapter_state(content)
            except Exception as e:
                logger.warning(f"StateExtractor failed: {e}, returning empty state")

        # 降级：返回空状态
        return ChapterState(
            new_characters=[],
            character_actions=[],
            relationship_changes=[],
            foreshadowing_planted=[],
            foreshadowing_resolved=[],
            events=[]
        )

    # ──────────────────────────────────────────────────────────
    # ★★★ 爽文引擎: 动态 Prompt 模板方案 ★★★
    # ──────────────────────────────────────────────────────────

    def _build_shuangwen_directive(
        self,
        chapter_number: int,
        beat_mode: bool,
        beat_index: Optional[int],
        total_beats: Optional[int],
        beat_prompt: str,
        outline: str,
    ) -> str:
        """★★★ 爽文引擎: 动态构建爽文约束指令

        架构决策核心实现：
        - 不在 autopilot_daemon 中硬编码规则引擎
        - 而是在 workflow 的 Prompt 构建层注入动态爽文约束
        - LLM 在强约束下自行发挥爽点呈现形式
        - 约束来自：章节位置、节拍类型、大纲关键词匹配

        Args:
            chapter_number: 当前章节号
            beat_mode: 是否为节拍模式
            beat_index: 节拍索引
            total_beats: 总节拍数
            beat_prompt: 节拍 Prompt
            outline: 章节大纲

        Returns:
            爽文引擎约束指令文本（注入 system_message）
        """
        parts: list[str] = []

        # ── 1. 前三章强力指令（冷启动保护）──
        if 1 <= chapter_number <= 3:
            early_directive = self._get_shuangwen_early_directive(chapter_number)
            if early_directive:
                parts.append(early_directive)

        # ── 2. 节拍级爽点检测与指令注入 ──
        if beat_mode and beat_prompt:
            beat_directive = self._detect_and_build_beat_directive(
                beat_prompt, outline
            )
            if beat_directive:
                parts.append(beat_directive)

        # ── 3. 节拍位置约束（基于 STEP 阶跃） ──
        if beat_mode and beat_index is not None and total_beats and total_beats > 0:
            position_hint = self._build_beat_position_hint(
                beat_index, total_beats, chapter_number
            )
            if position_hint:
                parts.append(position_hint)

        # ── 4. 通用爽文节奏约束（所有章节） ──
        general = self._get_shuangwen_general_rules()
        if general:
            parts.append(general)

        if parts:
            return _render_generation_prompt_text(
                _CHAPTER_SHUANGWEN_DIRECTIVE_NODE_KEY,
                {"directives": "\n\n".join(parts)},
            )

        return ""

    def _detect_and_build_beat_directive(self, beat_prompt: str, outline: str) -> str:
        """检测节拍/大纲中的爽点关键词，返回对应的约束指令"""
        combined_text = f"{beat_prompt} {outline}"
        patterns = self._get_shuangwen_patterns()

        best_match = None
        best_score = 0

        for pattern_config in patterns.values():
            score = sum(1 for kw in pattern_config["keywords"] if kw in combined_text)
            if score > best_score:
                best_score = score
                best_match = pattern_config

        if best_match and best_score >= 1:
            return best_match["directive"]

        return ""

    def _get_shuangwen_extra(self, key: str, default: Any) -> Any:
        try:
            from infrastructure.ai.prompt_seed.loader import NODES_DIR
            import json

            extras_path = NODES_DIR / _CHAPTER_SHUANGWEN_DIRECTIVE_NODE_KEY / "extras.json"
            data = json.loads(extras_path.read_text(encoding="utf-8"))
            if key in data:
                return data.get(key, default)
        except Exception as exc:
            logger.debug("读取爽文 extras 文件失败 key=%s: %s", key, exc)

        try:
            from infrastructure.ai.prompt_registry import get_prompt_registry
            value = get_prompt_registry().get_field(_CHAPTER_SHUANGWEN_DIRECTIVE_NODE_KEY, key, None)
            if value:
                return value
        except Exception as exc:
            logger.debug("读取爽文 Registry 片段失败 key=%s: %s", key, exc)
        return default

    def _get_shuangwen_patterns(self) -> Dict[str, Dict[str, Any]]:
        value = self._get_shuangwen_extra("_beat_patterns", {})
        return value if isinstance(value, dict) else {}

    def _get_shuangwen_early_directive(self, chapter_number: int) -> str:
        value = self._get_shuangwen_extra("_early_chapter_directives", {})
        if not isinstance(value, dict):
            return ""
        return str(value.get(str(chapter_number)) or value.get(chapter_number) or "")

    def _get_shuangwen_general_rules(self) -> str:
        return str(self._get_shuangwen_extra("_general_rules", "") or "")

    def _build_beat_position_hint(
        self, beat_index: int, total_beats: int, chapter_number: int
    ) -> str:
        """根据节拍在章节中的位置，返回节奏约束"""
        progress = beat_index / max(total_beats, 1)
        hints = self._get_shuangwen_extra("_position_hints", {})
        if not isinstance(hints, dict):
            return ""

        if progress < 0.2:
            return str(hints.get("opening", ""))
        elif progress < 0.5:
            return str(hints.get("build", ""))
        elif progress < 0.8:
            return str(hints.get("payoff", ""))
        else:
            return str(hints.get("close", ""))

    def _check_consistency(
        self,
        chapter_state: ChapterState,
        novel_id: str
    ) -> ConsistencyReport:
        """检查章节一致性

        Args:
            chapter_state: 章节状态
            novel_id: 小说 ID

        Returns:
            ConsistencyReport
        """
        from domain.bible.entities.bible import Bible
        from domain.bible.entities.character_registry import CharacterRegistry
        from domain.novel.entities.foreshadowing_registry import ForeshadowingRegistry
        from domain.novel.entities.plot_arc import PlotArc
        from domain.novel.value_objects.event_timeline import EventTimeline
        from domain.bible.value_objects.relationship_graph import RelationshipGraph

        novel_id_obj = NovelId(novel_id)

        try:
            # 尝试从仓储加载真实数据
            if self.bible_repository:
                bible = self.bible_repository.get_by_novel_id(novel_id_obj)
                logger.debug(f"Loaded real Bible for consistency check: {bible is not None}")
            else:
                bible = None

            if self.foreshadowing_repository:
                foreshadowing_registry = self.foreshadowing_repository.get_by_novel_id(novel_id_obj)
                logger.debug(f"Loaded real ForeshadowingRegistry for consistency check: {foreshadowing_registry is not None}")
            else:
                foreshadowing_registry = None

            context = ConsistencyContext(
                bible=bible or Bible(id="temp", novel_id=novel_id_obj),
                character_registry=CharacterRegistry(id="temp", novel_id=novel_id),
                foreshadowing_registry=foreshadowing_registry or ForeshadowingRegistry(id="temp", novel_id=novel_id_obj),
                plot_arc=PlotArc(id="temp", novel_id=novel_id_obj),
                event_timeline=EventTimeline(),
                relationship_graph=RelationshipGraph()
            )

            return self.consistency_checker.check_all(chapter_state, context)
        except Exception as e:
            logger.warning(f"Consistency check failed: {e}")
            return ConsistencyReport(issues=[], warnings=[], suggestions=[])

    def _detect_conflicts(
        self,
        novel_id: str,
        chapter_number: int,
        outline: str,
        scene_director: Optional[SceneDirectorAnalysis] = None
    ) -> List[GhostAnnotation]:
        """检测冲突并生成幽灵批注

        Args:
            novel_id: 小说 ID
            chapter_number: 章节号
            outline: 章节大纲
            scene_director: 场记分析结果（可选）

        Returns:
            GhostAnnotation 列表
        """
        # 如果没有冲突检测服务，返回空列表
        if not self.conflict_detection_service:
            logger.debug("ConflictDetectionService not available, skipping conflict detection")
            return []

        try:
            # 构造 name_to_entity_id 映射（从 Bible 获取）
            name_to_entity_id = self._build_name_to_entity_id_mapping(novel_id)

            # 获取实体状态（从 Bible 或 NarrativeEntityStateService）
            entity_states = self._get_entity_states(novel_id, chapter_number, name_to_entity_id)

            # 调用冲突检测服务
            annotations = self.conflict_detection_service.detect(
                outline=outline,
                entity_states=entity_states,
                name_to_entity_id=name_to_entity_id,
                scene_director=scene_director
            )

            return annotations

        except Exception as e:
            logger.warning(f"Conflict detection failed: {e}", exc_info=True)
            return []

    def _build_name_to_entity_id_mapping(self, novel_id: str) -> Dict[str, str]:
        """构造实体名称到 ID 的映射

        Args:
            novel_id: 小说 ID

        Returns:
            {name: entity_id} 字典
        """
        name_to_id = {}

        try:
            if not self.bible_repository:
                return name_to_id

            novel_id_obj = NovelId(novel_id)
            bible = self.bible_repository.get_by_novel_id(novel_id_obj)

            if not bible:
                return name_to_id

            # 从 Bible 中提取角色名称和 ID
            for character in bible.characters:
                name_to_id[character.name] = character.id

            # 从 Bible 中提取地点名称和 ID
            for location in bible.locations:
                name_to_id[location.name] = location.id

        except Exception as e:
            logger.warning(f"Failed to build name_to_entity_id mapping: {e}")

        return name_to_id

    def _get_entity_states(
        self,
        novel_id: str,
        chapter_number: int,
        name_to_entity_id: Dict[str, str]
    ) -> Dict[str, Dict]:
        """获取实体状态

        Args:
            novel_id: 小说 ID
            chapter_number: 章节号
            name_to_entity_id: 实体名称到 ID 的映射

        Returns:
            {entity_id: {attribute: value}} 字典
        """
        entity_states = {}

        try:
            if not self.bible_repository:
                return entity_states

            novel_id_obj = NovelId(novel_id)
            bible = self.bible_repository.get_by_novel_id(novel_id_obj)

            if not bible:
                return entity_states

            # 从 Bible 中提取角色状态（简化版本，使用静态属性）
            for character in bible.characters:
                state = {}

                # 提取角色属性
                if hasattr(character, 'attributes') and character.attributes:
                    state.update(character.attributes)

                # 提取角色描述中的关键信息（简化版本）
                if hasattr(character, 'description') and character.description:
                    desc = character.description.lower()
                    # 检测魔法类型
                    if '火系' in desc or '火魔法' in desc:
                        state['magic_type'] = '火系'
                    elif '水系' in desc or '水魔法' in desc:
                        state['magic_type'] = '水系'
                    elif '冰系' in desc or '冰魔法' in desc:
                        state['magic_type'] = '冰系'
                    elif '雷系' in desc or '雷魔法' in desc:
                        state['magic_type'] = '雷系'
                    elif '风系' in desc or '风魔法' in desc:
                        state['magic_type'] = '风系'

                if state:
                    entity_states[character.id] = state

        except Exception as e:
            logger.warning(f"Failed to get entity states: {e}")

        return entity_states

    def _get_style_summary(self, novel_id: str) -> str:
        """获取风格指纹摘要

        Args:
            novel_id: 小说 ID

        Returns:
            风格指纹摘要字符串，如果不可用则返回空字符串
        """
        if not self.voice_fingerprint_service:
            return ""

        try:
            # 获取指纹数据
            fingerprint = self.voice_fingerprint_service.fingerprint_repo.get_by_novel(
                novel_id, pov_character_id=None
            )
            if not fingerprint:
                return ""

            # 构建摘要
            summary = build_style_summary(fingerprint)
            return summary

        except Exception as e:
            logger.warning(f"Failed to get style summary: {e}")
            return ""

    def _scan_cliches(self, content: str) -> List['ClicheHit']:
        """扫描俗套句式

        Args:
            content: 生成的内容

        Returns:
            俗套句式列表，如果扫描器不可用则返回空列表
        """
        if not self.cliche_scanner:
            return []

        try:
            return self.cliche_scanner.scan_cliches(content)
        except Exception as e:
            logger.warning(f"Failed to scan cliches: {e}")
            return []
