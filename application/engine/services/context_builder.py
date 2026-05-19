"""上下文构建器 - 双轨融合版

核心设计：
- 使用 ContextBudgetAllocator 进行洋葱模型优先级挤压
- T0: 强制内容（伏笔、角色锚点、当前幕摘要）—— 绝不删减
- T1: 可压缩内容（图谱子网、近期幕摘要）—— 按比例压缩
- T2: 动态内容（最近章节）—— 动态水位线
- T3: 可牺牲内容（向量召回）—— 预算不足时归零

与 AutoNovelGenerationWorkflow 拼接时：Layer1≈T0+T1，Layer2 段名为 RECENT CHAPTERS（T2），
Layer3 段名为 VECTOR RECALL（T3）；见 assemble_chapter_bundle_context_text。
"""
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from application.engine.dtos.scene_director_dto import SceneDirectorInput
from application.engine.dtos.expanded_outline_dto import EmotionBeatCard, UnitDramaPlan

from application.world.services.bible_service import BibleService
from domain.bible.services.relationship_engine import RelationshipEngine
from domain.novel.services.storyline_manager import StorylineManager
from domain.novel.repositories.novel_repository import NovelRepository
from domain.novel.repositories.chapter_repository import ChapterRepository
from domain.novel.repositories.plot_arc_repository import PlotArcRepository
from domain.novel.repositories.foreshadowing_repository import ForeshadowingRepository
from domain.ai.services.vector_store import VectorStore
from domain.ai.services.embedding_service import EmbeddingService
from application.engine.services.context_budget_allocator import ContextBudgetAllocator
from application.engine.services.expanded_outline_service import ExpandedOutlineService

logger = logging.getLogger(__name__)


@dataclass
class Beat:
    """微观节拍（Beat）

    将章节大纲拆分为多个微观节拍，强制 AI 放慢节奏，增加感官细节。
    """
    description: str  # 节拍描述
    target_words: int  # 目标字数
    focus: str  # 聚焦点：sensory（感官）、dialogue（对话）、action（动作）、emotion（情绪）
    expansion_hints: List[str] = None  # 扩写维度提示（如何达到目标字数）
    scene_goal: str = ""  # 场景目标（从规划阶段继承）
    transition_from_prev: str = ""  # 🔗 从上一节拍如何过渡（对话延续/动作接续/情绪过渡/场景切换）
    location_id: str = ""  # 微观坐标（由 ATG visit_sequence 绑定；无 ATG 时为空）
    unit_id: str = ""  # 单元剧 ID（由 UnitDramaPlanner 绑定；无规划时为空）
    beat_card: Optional[EmotionBeatCard] = None  # 情绪/爽点节点卡
    unit_plan: Optional[UnitDramaPlan] = None  # 原始单元剧规划

    def __post_init__(self):
        if self.expansion_hints is None:
            self.expansion_hints = []


class ContextBuilder:
    """上下文构建器（双轨融合版）
    
    智能组装章节生成所需的上下文，使用洋葱模型优先级挤压。
    """

    def __init__(
        self,
        bible_service: BibleService,
        storyline_manager: StorylineManager,
        relationship_engine: RelationshipEngine,
        vector_store: VectorStore,
        novel_repository: NovelRepository,
        chapter_repository: ChapterRepository,
        plot_arc_repository: Optional[PlotArcRepository] = None,
        embedding_service: Optional[EmbeddingService] = None,
        foreshadowing_repository: Optional[ForeshadowingRepository] = None,
        story_node_repository=None,
        bible_repository=None,
        chapter_element_repository=None,
        triple_repository=None,
        # feed-forward 三件套（V8+）：接入后 T0/T1 槽位才会有内容
        causal_edge_repository=None,
        character_state_repository=None,
        narrative_debt_repository=None,
        # 故事线 + 汇流点（供预算分配器使用）
        storyline_repository=None,
        confluence_point_repository=None,
        worldbuilding_repository=None,
    ):
        self.bible_service = bible_service
        self.storyline_manager = storyline_manager
        self.relationship_engine = relationship_engine
        self.vector_store = vector_store
        self.novel_repository = novel_repository
        self.chapter_repository = chapter_repository
        self.plot_arc_repository = plot_arc_repository
        self.embedding_service = embedding_service
        self.foreshadowing_repository = foreshadowing_repository
        self.story_node_repository = story_node_repository
        self.bible_repository = bible_repository
        self.chapter_element_repository = chapter_element_repository
        self.triple_repository = triple_repository
        self.storyline_repository = storyline_repository
        self.confluence_point_repository = confluence_point_repository
        self.worldbuilding_repository = worldbuilding_repository

        # ContextAssembler：提供 ANCHOR / SCARS / DEBT_DUE / CAUSAL_CHAINS 槽位
        context_assembler = None
        try:
            from application.engine.services.context_assembler import ContextAssembler
            context_assembler = ContextAssembler(
                causal_edge_repo=causal_edge_repository,
                character_state_repo=character_state_repository,
                debt_repo=narrative_debt_repository,
                foreshadowing_repo=foreshadowing_repository,
                chapter_repo=chapter_repository,
                bible_repo=bible_repository,
                story_node_repo=story_node_repository,
                novel_repository=novel_repository,
                storyline_repo=storyline_repository,
            )
        except Exception as _e:
            logger.warning("ContextAssembler 初始化失败: %s", _e)

        # MemoryEngine：提供 FACT_LOCK / COMPLETED_BEATS / REVEALED_CLUES 槽位
        memory_engine = None
        if bible_repository:
            try:
                from application.engine.services.memory_engine import MemoryEngine
                from infrastructure.persistence.database.connection import get_database
                memory_engine = MemoryEngine(
                    llm_service=None,
                    bible_repository=bible_repository,
                    db_connection=get_database(),
                )
            except Exception as _e:
                logger.warning("MemoryEngine 初始化失败: %s", _e)

        # 预算分配器（核心组件）
        self.budget_allocator = ContextBudgetAllocator(
            foreshadowing_repository=foreshadowing_repository,
            chapter_repository=chapter_repository,
            bible_repository=bible_repository,
            story_node_repository=story_node_repository,
            chapter_element_repository=chapter_element_repository,
            triple_repository=triple_repository,
            vector_store=vector_store,
            embedding_service=embedding_service,
            context_assembler=context_assembler,
            memory_engine=memory_engine,
            storyline_repository=storyline_repository,
            confluence_point_repository=confluence_point_repository,
            worldbuilding_repository=worldbuilding_repository,
        )
        self.expanded_outline_service = ExpandedOutlineService()

    def estimate_tokens(self, text: str) -> int:
        """估算文本 token 数（委托 ContextBudgetAllocator）。"""
        return self.budget_allocator.estimate_tokens(text)

    def build_voice_anchor_system_section(self, novel_id: str) -> str:
        """Bible 角色声线/小动作锚点"""
        return self.bible_service.build_character_voice_anchor_section(novel_id)

    def build_context(
        self,
        novel_id: str,
        chapter_number: int,
        outline: str,
        max_tokens: int = 35000,
        scene_director: SceneDirectorInput = None,
    ) -> str:
        """构建上下文（使用预算分配器）
        
        Args:
            novel_id: 小说 ID
            chapter_number: 章节号
            outline: 章节大纲
            max_tokens: 最大 token 数
            scene_director: 场记（模型或 dict；allocator 内统一为 dict）
        
        Returns:
            组装好的上下文字符串
        """
        allocation = self.budget_allocator.allocate(
            novel_id=novel_id,
            chapter_number=chapter_number,
            outline=outline,
            total_budget=max_tokens,
            scene_director=scene_director,
        )
        
        return allocation.get_final_context()

    def build_structured_context(
        self,
        novel_id: str,
        chapter_number: int,
        outline: str,
        max_tokens: int = 35000,
        scene_director: SceneDirectorInput = None,
    ) -> Dict[str, Any]:
        """构建结构化上下文，返回详细信息
        
        Returns:
            {
                "layer1_text": "核心上下文（T0+T1）",
                "layer2_text": "最近章节（T2）",
                "layer3_text": "向量召回（T3）",
                "token_usage": {
                    "layer1": int,
                    "layer2": int,
                    "layer3": int,
                    "total": int,
                },
            }
        """
        allocation = self.budget_allocator.allocate(
            novel_id=novel_id,
            chapter_number=chapter_number,
            outline=outline,
            total_budget=max_tokens,
            scene_director=scene_director,
        )
        
        # 从 BudgetAllocation 中提取三层内容
        layer1_parts = []
        layer2_parts = []
        layer3_parts = []
        
        layer1_tokens = 0
        layer2_tokens = 0
        layer3_tokens = 0
        
        for name, slot in allocation.slots.items():
            if not slot.content.strip():
                continue
            
            if slot.tier.value in ["t0_critical", "t1_compressible"]:
                layer1_parts.append(f"=== {slot.name.upper()} ===\n{slot.content}")
                layer1_tokens += slot.tokens
            elif slot.tier.value == "t2_dynamic":
                layer2_parts.append(f"=== {slot.name.upper()} ===\n{slot.content}")
                layer2_tokens += slot.tokens
            elif slot.tier.value == "t3_sacrificial":
                layer3_parts.append(f"=== {slot.name.upper()} ===\n{slot.content}")
                layer3_tokens += slot.tokens
        
        return {
            "layer1_text": "\n\n".join(layer1_parts),
            "layer2_text": "\n\n".join(layer2_parts),
            "layer3_text": "\n\n".join(layer3_parts),
            "token_usage": {
                "layer1": layer1_tokens,
                "layer2": layer2_tokens,
                "layer3": layer3_tokens,
                "total": allocation.used_tokens,
            },
        }

    # 扩写维度提示（根据节拍类型动态注入）
    EXPANSION_HINTS = {
        "action": [
            "动作必须改变局势：位置、资源、伤势、身份暴露或主动权至少一项变化",
            "写清动作前的目标、动作中的阻碍、动作后的结果",
            "旁观者反应必须落成实际变化：让路、收手、改口、交易、欠人情",
            "战斗或追逐只保留会影响胜负判断的环境细节",
        ],
        "dialogue": [
            "每轮对白必须推进信息、关系、误判、交易或威胁之一",
            "对白后要有局势变化，不能只停在情绪表达",
            "用打断、回避、反问或条件交换制造潜台词",
            "减少声线标签，用话语内容和动作后果体现态度",
        ],
        "sensory": [
            "只保留会帮助主角判断危险或机会的感官细节",
            "感官信息必须立刻影响下一步行动",
            "不要连续堆颜色、温度、气味和抽象压迫感",
            "把环境写成阻碍、遮蔽、线索或资源，而不是背景板",
        ],
        "emotion": [
            "情绪必须来自目标受阻、资源不足、误判或代价逼近",
            "用选择前后的动作变化表现情绪，不写长段自我解释",
            "回忆只能补当前选择的动机，不能单独扩成长背景",
            "情绪段结尾必须落到决定、行动、关系变化或新风险",
        ],
        "suspense": [
            "悬念必须绑定人、物、地点、倒计时、代价或未完成动作",
            "不要用虚神秘词拖延答案，至少给出一个可追踪事实",
            "悬念推进要逼主角试探、拒绝、交易、逃跑或反击",
            "章尾钩子必须接出下一目标或新阻碍",
        ],
        "hook": [
            "前300字交代人物、地点、事件、主角当前目标",
            "前500字出现明确阻碍或倒计时",
            "开篇世界观只保留目标资源、风险和压力源",
            "用主角正在做的事展示特质，不写设定说明书",
        ],
        "character_intro": [
            "人物出场必须带目标、利益或阻碍，不能只做外貌介绍",
            "用一次选择或一句有信息量的对白建立记忆点",
            "关系暗示要影响当场行动或资源分配",
            "避免新增无关有名角色",
        ],
    }

    # 节拍数量上限：短章不宜过碎，长章也不应无限拆分
    MAX_BEATS = 9
    LONG_CHAPTER_MAX_BEATS = 24
    # 节拍最低目标字数：用于合并/兜底，不是强制平均值
    MIN_BEAT_WORDS = 300
    SHORT_CHAPTER_MAX_BEATS = 6
    MID_CHAPTER_MAX_BEATS = 7

    def magnify_outline_to_beats(
        self,
        chapter_number: int,
        outline: str,
        target_chapter_words: int = 2500,
        beat_sheet: Optional[Any] = None,
        scene_director: Optional[Any] = None,
        novel_id: str = "",
    ) -> List[Beat]:
        """节拍放大器：将章节大纲拆分为微观节拍

        核心策略（选项 C：动态弹性扩写与前置预估）：
        1. 优先使用规划阶段的 BeatSheet（含 estimated_words）
        2. 无 BeatSheet 时回退到关键词识别 + 25% 均分
        3. 根据 focus 类型注入扩写维度提示（expansion_hints）
        4. 不再强制 75% 缩减，相信规划阶段的预估
        5. 拍数上限 MAX_BEATS；每拍目标字数 < MIN_BEAT_WORDS 时合并相邻拍
        """
        # === 路径 A：有规划阶段的 BeatSheet ===
        uses_expanded_outline = False
        if beat_sheet is not None and hasattr(beat_sheet, 'scenes') and beat_sheet.scenes:
            beats = self._build_beats_from_beat_sheet(beat_sheet, outline, target_chapter_words)
        else:
            # === 路径 B：无 BeatSheet，先扩成单元剧 + 情绪节点卡，再兼容为 Beat ===
            beats = self._build_beats_from_expanded_outline(chapter_number, outline, target_chapter_words, novel_id=novel_id)
            uses_expanded_outline = True

        if not uses_expanded_outline:
            beats = self._expand_beats_to_functional_arc(beats, target_chapter_words, outline)
        beats = self._cap_and_merge_beats(beats, target_chapter_words, outline)
        self._bind_atg_locations_if_present(beats, scene_director)
        return beats

    def _resolve_target_beat_count(self, target_chapter_words: int, outline: str = "") -> int:
        """按章节字数与章纲复杂度动态估算节拍数。"""
        text = (outline or "").strip()
        segment_count = len(self._segment_user_outline(text)) if text else 0

        # 章纲很短时，宁可保留 4 个完整场景拍，也不要拆成 6-7 个碎功能拍。
        # 碎拍会诱导模型每拍只交付一句“功能完成”，最终形成短段拼贴。
        if target_chapter_words <= 3200 and len(text) < 180 and 2 <= segment_count <= 4:
            return segment_count

        if target_chapter_words <= 1800:
            base = 4
        elif target_chapter_words <= 2400:
            base = 5
        elif target_chapter_words <= 3200:
            base = 6
        elif target_chapter_words <= 4200:
            base = 7
        elif target_chapter_words <= 8500:
            base = 8
        elif target_chapter_words <= 15000:
            # 10000-15000 字不应压成一个超长单元剧，应允许两个单元剧的节点进入正文。
            base = 16
        else:
            # 超长章节按 6000-7500 字一个单元剧继续扩容，拍数上限也必须随目标字数放大。
            # 否则 30000/50000 字章节会被重新压回 24 拍，单节点又变成超长灌水容器。
            dynamic_long_cap = max(self.LONG_CHAPTER_MAX_BEATS, (target_chapter_words + 749) // 750)
            base = max(16, dynamic_long_cap)

        complexity_bonus = 0
        if len(text) >= 260:
            complexity_bonus += 1
        if segment_count >= 4:
            complexity_bonus += 1
        if target_chapter_words > 15000:
            complexity_bonus += max(0, segment_count - 4)

        if target_chapter_words <= 8500:
            max_beats = self.MAX_BEATS
        elif target_chapter_words <= 15000:
            max_beats = self.LONG_CHAPTER_MAX_BEATS
        else:
            max_beats = max(self.LONG_CHAPTER_MAX_BEATS, (target_chapter_words + 599) // 600)
        return max(4, min(max_beats, base + complexity_bonus))

    def _resolve_min_beat_words(self, target_chapter_words: int) -> int:
        """根据章节字数动态计算单拍最低目标字数。"""
        if target_chapter_words <= 2200:
            return 300
        if target_chapter_words <= 3500:
            return 350
        if target_chapter_words <= 5000:
            return 400
        return 450

    def _expand_beats_to_functional_arc(
        self,
        beats: List[Beat],
        target_chapter_words: int,
        outline: str = "",
    ) -> List[Beat]:
        """当章纲或 BeatSheet 过粗时，拆成可完成的功能节点。"""
        if not beats:
            return beats

        desired_count = self._resolve_target_beat_count(target_chapter_words, outline)
        if len(beats) >= max(4, desired_count - 1):
            return beats

        source_text = "\n".join(
            p for p in [
                (outline or "").strip(),
                "\n".join(b.description for b in beats if b.description),
            ]
            if p
        ).strip()
        if not source_text:
            source_text = beats[0].description

        functions = [
            ("hook", "起：明确主角当前目标、地点、必须现在行动的理由，并在前300字内给出读者抓手。"),
            ("suspense", "承：让阻碍出现或升级，写清失败后果、资源压力、误判或关系压力。"),
            ("action", "进：主角主动尝试改变局面，必须有具体动作、试探、交易、逃跑、救人或反击。"),
            ("dialogue", "转：通过对白、对峙、发现或选择制造转折，让信息差或关系发生变化。"),
            ("action", "兑：兑现一个小爽点或阶段结果，写清收获、代价、身份/资源/主动权变化。"),
            ("suspense", "钩：收束本章故事单元，留下具体下一目标、新阻碍、倒计时、物件或未完成动作。"),
        ]
        if desired_count > len(functions):
            functions.insert(4, ("action", "压：让对手、环境或规则追加压力，迫使主角付出更明确代价。"))
            functions.insert(5, ("dialogue", "评：让他人反应落成实际变化，给主角新的筹码或风险。"))

        selected = functions[:desired_count]
        base_words = max(1, target_chapter_words // len(selected))
        expanded: List[Beat] = []
        for idx, (focus, duty) in enumerate(selected):
            words = base_words
            if idx == len(selected) - 1:
                words = target_chapter_words - base_words * (len(selected) - 1)
            expanded.append(
                Beat(
                    description=(
                        f"【功能节点·必须完成】{duty}\n"
                        f"【章纲来源】{source_text}\n"
                        "要求：只写本节点承担的进展，不能跳过后续节点，也不能重复已完成节点。"
                    ),
                    target_words=max(self._resolve_min_beat_words(target_chapter_words), words),
                    focus=focus,
                    expansion_hints=self._generate_expansion_hints(focus, words),
                )
            )

        logger.info(
            "节拍功能弧扩展：原 %d 拍 -> %d 拍，目标字数=%d",
            len(beats),
            len(expanded),
            target_chapter_words,
        )
        return expanded

    def _bind_atg_locations_if_present(self, beats: List[Beat], scene_director: Optional[Any]) -> None:
        """若场记携带 ATG，将 visit_sequence 映射到各节拍。"""
        if not beats or scene_director is None:
            return
        graph_payload = getattr(scene_director, "action_transition_graph", None)
        if graph_payload is None:
            return
        try:
            from application.engine.services.spatial_coherence import assign_visit_locations_to_beats
        except ImportError:
            return
        seq = list(graph_payload.visit_sequence or [])
        if not seq:
            entry_first = [n.location_id for n in graph_payload.nodes if getattr(n, "is_entry_point", False)]
            seen = set(entry_first)
            tail = [n.location_id for n in graph_payload.nodes if n.location_id and n.location_id not in seen]
            seq = entry_first + tail
        if not seq:
            seq = [n.location_id for n in graph_payload.nodes if getattr(n, "location_id", "").strip()]
        assign_visit_locations_to_beats(beats, seq)

    def _cap_and_merge_beats(self, beats: List[Beat], target_chapter_words: int, outline: str = "") -> List[Beat]:
        """控制节拍数量与最低字数。

        策略：
        1. 若 len(beats) > MAX_BEATS，按均分合并使总数降到 MAX_BEATS。
        2. 若某拍 target_words 过低，与相邻拍合并，避免碎拍灌水。
        3. 合并后重新均摊 target_words 使总字数维持接近 target_chapter_words。
        """
        if not beats:
            return beats

        desired_count = self._resolve_target_beat_count(target_chapter_words, outline)
        has_node_cards = any(getattr(b, "beat_card", None) is not None for b in beats)
        if has_node_cards:
            # 节点卡已经完成单元剧/情绪节点规划，不再用旧的“目标拍数”把它们压扁。
            desired_count = max(desired_count, len(beats))
        min_beat_words = self._resolve_min_beat_words(target_chapter_words)

        # 步骤 1：超过目标拍数时按组合并
        while len(beats) > desired_count:
            # 找到相邻两拍中 target_words 之和最小的组合，合并掉一拍
            min_sum = None
            merge_idx = 0
            for i in range(len(beats) - 1):
                s = beats[i].target_words + beats[i + 1].target_words
                if min_sum is None or s < min_sum:
                    min_sum = s
                    merge_idx = i
            beats = self._merge_two_beats(beats, merge_idx)

        # 步骤 2：每拍过碎时合并
        changed = True
        while changed and len(beats) > 1:
            changed = False
            for i, b in enumerate(beats):
                if b.target_words < min_beat_words:
                    # 与前一拍或后一拍合并（优先后一拍）
                    merge_idx = i if i < len(beats) - 1 else i - 1
                    beats = self._merge_two_beats(beats, merge_idx)
                    changed = True
                    break

        # 步骤 3：重新均摊 target_words（等比缩放保持各拍权重）
        total_assigned = sum(b.target_words for b in beats)
        if total_assigned > 0 and abs(total_assigned - target_chapter_words) > 200:
            ratio = target_chapter_words / total_assigned
            for b in beats:
                b.target_words = max(min_beat_words, int(b.target_words * ratio))

        # 步骤 4：短章的节拍不要过碎，确保最后一拍仍有收束空间。
        # 长章会由多个单元剧承载收束，不能再要求最后一个节点吃掉整章 18%。
        if len(beats) > 1 and target_chapter_words <= 4200:
            tail_floor = max(min_beat_words, int(target_chapter_words * 0.18))
            if beats[-1].target_words < tail_floor and len(beats) > 2:
                beats = self._merge_two_beats(beats, len(beats) - 2)

        if beats:
            assigned = sum(b.target_words for b in beats)
            delta = target_chapter_words - assigned
            if delta > 0:
                beats[-1].target_words += delta
            elif delta < 0 and beats[-1].target_words + delta >= min_beat_words:
                beats[-1].target_words += delta

        final_total = sum(b.target_words for b in beats)
        target_delta = final_total - target_chapter_words
        logger.info(
            "节拍整形：目标 %d 拍 -> 实际 %d 拍（低字数拍已合并），各拍字数=%s，总目标=%d，偏差=%+d",
            desired_count,
            len(beats),
            [b.target_words for b in beats],
            final_total,
            target_delta,
        )
        return beats

    def _merge_two_beats(self, beats: List[Beat], idx: int) -> List[Beat]:
        """将 beats[idx] 与 beats[idx+1] 合并为一拍。"""
        a, b = beats[idx], beats[idx + 1]
        merged_card = self._merge_beat_cards(getattr(a, "beat_card", None), getattr(b, "beat_card", None))
        merged = Beat(
            description=f"{a.description} / {b.description}",
            target_words=a.target_words + b.target_words,
            focus=a.focus,  # 保留前拍的 focus 类型
            expansion_hints=list(dict.fromkeys(a.expansion_hints + b.expansion_hints))[:4],
            scene_goal=f"{a.scene_goal or ''} {b.scene_goal or ''}".strip(),
            transition_from_prev=a.transition_from_prev or '',
            location_id=(a.location_id or b.location_id or "").strip(),
            unit_id=(a.unit_id or b.unit_id or "").strip(),
            beat_card=merged_card,
            unit_plan=a.unit_plan or b.unit_plan,
        )
        return beats[:idx] + [merged] + beats[idx + 2:]

    def _merge_beat_cards(
        self,
        first: Optional[EmotionBeatCard],
        second: Optional[EmotionBeatCard],
    ) -> Optional[EmotionBeatCard]:
        """Merge node cards when beat shaping combines adjacent beats."""
        if first is None:
            return second
        if second is None:
            return first
        merged = EmotionBeatCard(
            beat_id=f"{first.beat_id}+{second.beat_id}",
            unit_id=first.unit_id or second.unit_id,
            title=f"{first.title} / {second.title}",
            function=f"{first.function}；{second.function}",
            target_words=first.target_words + second.target_words,
            focus=first.focus or second.focus,
            emotion_gap=f"{first.emotion_gap}；{second.emotion_gap}",
            protagonist_goal=first.protagonist_goal or second.protagonist_goal,
            obstacle_or_misbelief=f"{first.obstacle_or_misbelief}；{second.obstacle_or_misbelief}",
            active_action=f"{first.active_action}；随后{second.active_action}",
            external_feedback=f"{first.external_feedback}；随后{second.external_feedback}",
            information_delta=f"{first.information_delta}；{second.information_delta}",
            mini_payoff_or_pressure=f"{first.mini_payoff_or_pressure}；{second.mini_payoff_or_pressure}",
            hook_delta=second.hook_delta or first.hook_delta,
            sensory_anchor=f"{first.sensory_anchor}；{second.sensory_anchor}",
            forbidden_drift=f"{first.forbidden_drift}；{second.forbidden_drift}",
            acceptance_criteria=list(dict.fromkeys(first.acceptance_criteria + second.acceptance_criteria)),
            source_outline=f"{first.source_outline}；{second.source_outline}",
        )
        return merged

    def _build_beats_from_beat_sheet(
        self,
        beat_sheet: Any,
        outline: str,
        target_chapter_words: int,
    ) -> List[Beat]:
        """从 BeatSheet 构建 Beat 列表（使用规划阶段的预估字数）"""
        beats = []
        scenes = beat_sheet.scenes

        for i, scene in enumerate(scenes):
            # 从 Scene 提取信息
            estimated_words = getattr(scene, 'estimated_words', 600)
            goal = getattr(scene, 'goal', '')
            title = getattr(scene, 'title', '')
            tone = getattr(scene, 'tone', '')

            # 根据 goal/标题 推断 focus 类型
            focus = self._infer_focus_from_scene(scene, outline)

            # 生成扩写维度提示
            expansion_hints = self._generate_expansion_hints(focus, estimated_words)

            beat = Beat(
                description=f"{title}：{goal}" if goal else title,
                target_words=estimated_words,
                focus=focus,
                expansion_hints=expansion_hints,
                scene_goal=goal,
                transition_from_prev=getattr(scene, 'transition_from_prev', '') or '',
            )
            beats.append(beat)

        # 验证总字数
        total_estimated = sum(b.target_words for b in beats)
        logger.info(
            f"节拍放大器（BeatSheet）：{len(beats)} 个场景，"
            f"预估总字数 {total_estimated} 字（目标 {target_chapter_words} 字）"
        )

        # 如果总字数差距过大，发出警告但不强制调整
        if total_estimated < target_chapter_words * 0.7:
            logger.warning(
                f"规划阶段预估字数 {total_estimated} 低于目标 {target_chapter_words} 的 70%，"
                f"将在章节完成时弹性处理"
            )

        return beats

    def _build_beats_from_expanded_outline(
        self,
        chapter_number: int,
        outline: str,
        target_chapter_words: int,
        *,
        novel_id: str = "",
    ) -> List[Beat]:
        """Build beats from unit-drama plans and emotion node cards.

        This is the default no-BeatSheet path. It keeps compatibility with the
        existing Beat contract while making each beat carry a concrete mini arc:
        emotion gap, protagonist action, external feedback, information delta,
        payoff/pressure, and hook movement.
        """
        raw = (outline or "").strip()
        if not raw:
            return self._build_beats_from_outline(chapter_number, outline, target_chapter_words)

        try:
            plan = self.expanded_outline_service.expand(
                novel_id=novel_id,
                chapter_number=chapter_number,
                outline=raw,
                target_words=target_chapter_words,
            )
        except Exception as exc:
            logger.error("单元剧章纲扩写失败，拒绝回退旧功能标签节拍：%s", exc)
            raise

        units_by_id = {unit.unit_id: unit for unit in plan.units}
        beats: List[Beat] = []
        for idx, card in enumerate(plan.beat_cards):
            unit = units_by_id.get(card.unit_id)
            focus = card.focus or self._infer_focus_from_outline(card.source_outline)
            beat = Beat(
                description=self._format_beat_card_description(card, unit),
                target_words=card.target_words,
                focus=focus,
                expansion_hints=self._node_card_expansion_hints(card, focus),
                scene_goal=card.protagonist_goal,
                transition_from_prev=self._transition_from_previous_card(plan.beat_cards, idx),
                unit_id=card.unit_id,
                beat_card=card,
                unit_plan=unit,
            )
            beats.append(beat)

        logger.info(
            "单元剧章纲扩写：%d 个单元剧 -> %d 张情绪节点卡，目标字数=%d，节点字数=%s",
            len(plan.units),
            len(beats),
            target_chapter_words,
            [b.target_words for b in beats],
        )
        return beats

    def _format_beat_card_description(
        self,
        card: EmotionBeatCard,
        unit: Optional[UnitDramaPlan],
    ) -> str:
        return (
            f"{card.to_prompt_block(unit)}\n\n"
            "【正文执行要求】\n"
            "只写本节点，不提前跳到后续节点。必须兑现节点卡的主动动作、外界反馈、信息差变化和钩子变化；"
            "不能只完成“入局/压迫/转折”等功能标签。"
        )

    def _node_card_expansion_hints(self, card: EmotionBeatCard, focus: str) -> List[str]:
        hints = [
            f"主动动作必须写成可见动作：{card.active_action}",
            f"动作后必须出现外界反馈：{card.external_feedback}",
            f"信息差变化必须落地：{card.information_delta}",
            f"本节点禁止漂移：{card.forbidden_drift}",
        ]
        base = self._generate_expansion_hints(focus, card.target_words)
        return list(dict.fromkeys(hints + base))[:6]

    def _transition_from_previous_card(self, cards: List[EmotionBeatCard], idx: int) -> str:
        if idx <= 0:
            return ""
        prev = cards[idx - 1]
        curr = cards[idx]
        return (
            f"承接上一节点的“{prev.hook_delta or prev.mini_payoff_or_pressure}”，"
            f"开场直接推进到“{curr.active_action}”。"
        )

    def _segment_user_outline(self, outline: str) -> List[str]:
        """将用户章纲拆成多条，供「章纲优先」节拍；支持编号列表、项目符号、空行段、单段按句切分。"""
        text = (outline or "").strip()
        if not text:
            return []
        if re.search(r"(?m)^\s*\d+[\.、．\)]", text):
            parts = re.split(r"\n(?=\s*\d+[\.、．\)]\s)", text)
            segs = [p.strip() for p in parts if p.strip()]
            if len(segs) >= 2:
                return segs
        if re.search(r"(?m)^\s*[-*•]\s+\S", text):
            parts = re.split(r"\n(?=\s*[-*•]\s)", text)
            segs = [p.strip() for p in parts if p.strip()]
            if len(segs) >= 2:
                return segs
        paras = [p.strip() for p in re.split(r"\n\s*\n+", text) if p.strip()]
        if len(paras) >= 2:
            return paras
        if len(text) >= 20:
            # 中文句间无空格，不依赖 \s+ ——直接按句末标点切分
            sents = [
                s.strip()
                for s in re.split(r"(?<=[。！？；])", text)
                if len(s.strip()) > 8
            ]
            if len(sents) >= 2:
                return sents
        if len(text) >= 400:
            n = min(self.MAX_BEATS, max(2, (len(text) + 499) // 500))
            approx = max(1, len(text) // n)
            segs: List[str] = []
            idx = 0
            for k in range(n):
                if k == n - 1:
                    chunk = text[idx:].strip()
                else:
                    end = min(len(text), idx + approx)
                    brk = end
                    for j in range(end, min(len(text), end + 80)):
                        if text[j] in "。！？；":
                            brk = j + 1
                            break
                    chunk = text[idx:brk].strip()
                    idx = brk
                if chunk:
                    segs.append(chunk)
            if len(segs) >= 2:
                return segs
        return [text]

    def _build_beats_from_outline_segments(
        self,
        segments: List[str],
        target_chapter_words: int,
    ) -> List[Beat]:
        """按用户章纲条文生成节拍（每段必须落实，字数按段长比例分配）。"""
        clean = [s.strip() for s in segments if s and s.strip()]
        if not clean:
            return []
        total_w = sum(max(1, len(s)) for s in clean)
        beats: List[Beat] = []
        for seg in clean:
            w = max(1, int(target_chapter_words * max(1, len(seg)) / total_w))
            focus = self._infer_focus_from_outline(seg)
            beats.append(
                Beat(
                    description=(
                        "【章纲节选·须落实】以下要点必须写入正文（可合理扩写，不得跳过核心因果；"
                        "人物姓名须与 Bible 一致）：\n"
                        + seg
                    ),
                    target_words=w,
                    focus=focus,
                    expansion_hints=self._generate_expansion_hints(focus, w),
                )
            )
        return beats

    def _build_beats_from_outline(
        self,
        chapter_number: int,
        outline: str,
        target_chapter_words: int,
    ) -> List[Beat]:
        """无 BeatSheet 时，从大纲关键词推断节拍（回退逻辑）"""
        raw = (outline or "").strip()
        segments = self._segment_user_outline(raw)
        outline_chars = len(raw)
        if len(segments) >= 2 or (len(segments) == 1 and outline_chars >= 15):
            beats = self._build_beats_from_outline_segments(segments, target_chapter_words)
            logger.info(
                "节拍放大器（章纲优先）：用户大纲拆为 %d 个节拍，章纲约 %d 字，整章目标 %d 字",
                len(beats),
                outline_chars,
                target_chapter_words,
            )
            return beats

        beats = []
        base_beat_words = max(self.MIN_BEAT_WORDS, int(target_chapter_words * 0.25))
        if target_chapter_words <= 2200:
            base_beat_words = max(300, int(target_chapter_words * 0.2))

        # 开篇黄金法则前三章特殊拦截
        if chapter_number == 1:
            beats = [
                Beat(
                    description="开篇黄金法则：展现核心冲突，介绍主角出场，建立情感冲击（前300字内必须抓住读者）",
                    target_words=int(base_beat_words * 1.2),
                    focus="hook",
                    expansion_hints=self._generate_expansion_hints("hook", int(base_beat_words * 1.2)),
                ),
                Beat(
                    description="剧情引入及人物初步互动：展现主角特质并暗示即将发生的事件",
                    target_words=int(base_beat_words * 1.5),
                    focus="dialogue",
                    expansion_hints=self._generate_expansion_hints("dialogue", int(base_beat_words * 1.5)),
                ),
                Beat(
                    description="世界观或当前场景细节：通过具体行动展现，不用抽象叙述",
                    target_words=int(base_beat_words * 1.3),
                    focus="action",
                    expansion_hints=self._generate_expansion_hints("action", int(base_beat_words * 1.3)),
                ),
                Beat(
                    description="埋下后续剧情伏笔或抛出首个悬念：铺垫第二章",
                    target_words=int(base_beat_words * 1.0),
                    focus="suspense",
                    expansion_hints=self._generate_expansion_hints("suspense", int(base_beat_words * 1.0)),
                ),
            ]
        elif chapter_number == 2:
            beats = [
                Beat(
                    description="承接首章悬念：深化关键人物关系，展现性格差异",
                    target_words=int(base_beat_words * 1.3),
                    focus="dialogue",
                    expansion_hints=self._generate_expansion_hints("dialogue", int(base_beat_words * 1.3)),
                ),
                Beat(
                    description="推进主要情节线：引入新的次要冲突或阻碍",
                    target_words=int(base_beat_words * 1.8),
                    focus="action",
                    expansion_hints=self._generate_expansion_hints("action", int(base_beat_words * 1.8)),
                ),
                Beat(
                    description="情绪细节及内心活动：展示人物面对变故的真实反映",
                    target_words=int(base_beat_words * 1.0),
                    focus="suspense",
                    expansion_hints=self._generate_expansion_hints("suspense", int(base_beat_words * 1.0)),
                ),
                Beat(
                    description="为第三章冲突高潮做气氛铺垫",
                    target_words=int(base_beat_words * 0.8),
                    focus="suspense",
                    expansion_hints=self._generate_expansion_hints("suspense", int(base_beat_words * 0.8)),
                ),
            ]
        elif chapter_number == 3:
            beats = [
                Beat(
                    description="前三章的剧情小结或高潮前奏：紧张气氛描写",
                    target_words=int(base_beat_words * 1.0),
                    focus="action",
                    expansion_hints=self._generate_expansion_hints("action", int(base_beat_words * 1.0)),
                ),
                Beat(
                    description="冲突爆发/悬念高潮：激烈的动作或对峙",
                    target_words=int(base_beat_words * 2.0),
                    focus="action",
                    expansion_hints=self._generate_expansion_hints("action", int(base_beat_words * 2.0)),
                ),
                Beat(
                    description="暴露深层问题或引出更高层面人物背景",
                    target_words=int(base_beat_words * 1.3),
                    focus="emotion",
                    expansion_hints=self._generate_expansion_hints("emotion", int(base_beat_words * 1.3)),
                ),
                Beat(
                    description="建立长线悬念结局：为整卷后续发展铺设巨大好奇心",
                    target_words=int(base_beat_words * 0.7),
                    focus="suspense",
                    expansion_hints=self._generate_expansion_hints("suspense", int(base_beat_words * 0.7)),
                ),
            ]
        # 根据大纲关键词推断
        elif "争吵" in outline or "冲突" in outline or "质问" in outline:
            beats = self._build_conflict_beats(base_beat_words)
        elif "战斗" in outline or "打斗" in outline or "对决" in outline:
            beats = self._build_battle_beats(base_beat_words)
        elif "发现" in outline or "真相" in outline or "揭露" in outline:
            beats = self._build_revelation_beats(base_beat_words)
        else:
            beats = self._build_default_beats(base_beat_words)

        logger.info(
            f"节拍放大器（回退）：将大纲拆分为 {len(beats)} 个节拍，"
            f"目标 {sum(b.target_words for b in beats)} 字"
        )
        return beats

    def _infer_focus_from_scene(self, scene: Any, outline: str) -> str:
        """从 Scene 推断 focus 类型"""
        goal = getattr(scene, 'goal', '') or ''
        title = getattr(scene, 'title', '') or ''
        combined = f"{title} {goal}".lower()

        # 关键词匹配
        if any(kw in combined for kw in ["战斗", "打斗", "对决", "攻击", "招式"]):
            return "action"
        if any(kw in combined for kw in ["对话", "争吵", "谈判", "质问", "对峙"]):
            return "dialogue"
        if any(kw in combined for kw in ["悬念", "谜团", "发现", "真相", "揭露"]):
            return "suspense"
        if any(kw in combined for kw in ["情绪", "内心", "回忆", "痛苦", "挣扎"]):
            return "emotion"
        if any(kw in combined for kw in ["环境", "场景", "氛围", "感官"]):
            return "sensory"

        # 默认根据大纲推断
        return self._infer_focus_from_outline(outline)

    def _infer_focus_from_outline(self, outline: str) -> str:
        """从大纲推断 focus 类型"""
        combined = outline.lower()
        if any(kw in combined for kw in ["战斗", "打斗", "对决"]):
            return "action"
        if any(kw in combined for kw in ["争吵", "对话", "谈判"]):
            return "dialogue"
        if any(kw in combined for kw in ["发现", "真相", "悬念"]):
            return "suspense"
        if any(kw in combined for kw in ["情绪", "内心", "回忆"]):
            return "emotion"
        return "action"

    def _generate_expansion_hints(self, focus: str, target_words: int) -> List[str]:
        """根据 focus 类型和目标字数生成扩写维度提示"""
        base_hints = self.EXPANSION_HINTS.get(focus, [])

        # 根据目标字数调整提示数量
        if target_words >= 1000:
            # 高字数节拍：给出更多扩写方向
            return base_hints[:4]
        elif target_words >= 600:
            # 中等字数：给出 2-3 个方向
            return base_hints[:3]
        else:
            # 低字数节拍：只需 1-2 个方向
            return base_hints[:2]

    def _build_conflict_beats(self, base_beat_words: int) -> List[Beat]:
        """构建冲突场景的节拍"""
        return [
            Beat(
                description="冲突前压：主角目标被卡住，对手或规则给出明确后果",
                target_words=int(base_beat_words * 0.9),
                focus="suspense",
                expansion_hints=self._generate_expansion_hints("suspense", int(base_beat_words * 0.9)),
            ),
            Beat(
                description="冲突爆发：主角的质问、对方的反应、情绪的升级",
                target_words=int(base_beat_words * 1.4),
                focus="dialogue",
                expansion_hints=self._generate_expansion_hints("dialogue", int(base_beat_words * 1.4)),
            ),
            Beat(
                description="选择转折：主角在压力下做出决定，带来代价或新风险",
                target_words=int(base_beat_words * 1.2),
                focus="action",
                expansion_hints=self._generate_expansion_hints("action", int(base_beat_words * 1.2)),
            ),
            Beat(
                description="冲突结果：决裂、离开、或暂时妥协（不要轻易和好）",
                target_words=int(base_beat_words * 0.9),
                focus="action",
                expansion_hints=self._generate_expansion_hints("action", int(base_beat_words * 0.9)),
            ),
        ]

    def _build_battle_beats(self, base_beat_words: int) -> List[Beat]:
        """构建战斗场景的节拍"""
        return [
            Beat(
                description="战前目标：主角必须赢下或逃出这一局，写清失败后果和战场限制",
                target_words=int(base_beat_words * 0.7),
                focus="suspense",
                expansion_hints=self._generate_expansion_hints("suspense", int(base_beat_words * 0.7)),
            ),
            Beat(
                description="第一回合：试探性攻击、展示能力、观察弱点",
                target_words=int(base_beat_words * 1.0),
                focus="action",
                expansion_hints=self._generate_expansion_hints("action", int(base_beat_words * 1.0)),
            ),
            Beat(
                description="战斗升级：全力以赴、招式碰撞、环境破坏",
                target_words=int(base_beat_words * 1.2),
                focus="action",
                expansion_hints=self._generate_expansion_hints("action", int(base_beat_words * 1.2)),
            ),
            Beat(
                description="转折点：意外发生、底牌揭露、受伤或资源损失，迫使主角改策略",
                target_words=int(base_beat_words * 0.9),
                focus="action",
                expansion_hints=self._generate_expansion_hints("action", int(base_beat_words * 0.9)),
            ),
            Beat(
                description="战斗结束：胜负揭晓、战后状态、后续影响",
                target_words=int(base_beat_words * 0.6),
                focus="action",
                expansion_hints=self._generate_expansion_hints("action", int(base_beat_words * 0.6)),
            ),
        ]

    def _build_revelation_beats(self, base_beat_words: int) -> List[Beat]:
        """构建真相揭露场景的节拍"""
        return [
            Beat(
                description="线索逼近：主角用已有事实试探或验证，不能只回忆和心理推演",
                target_words=int(base_beat_words * 1.2),
                focus="suspense",
                expansion_hints=self._generate_expansion_hints("suspense", int(base_beat_words * 1.2)),
            ),
            Beat(
                description="真相揭露：关键证据出现、震惊的反应、世界观崩塌",
                target_words=int(base_beat_words * 1.8),
                focus="dialogue",
                expansion_hints=self._generate_expansion_hints("dialogue", int(base_beat_words * 1.8)),
            ),
            Beat(
                description="结果落地：真相改变资源、关系、身份或下一步目标",
                target_words=int(base_beat_words * 1.3),
                focus="action",
                expansion_hints=self._generate_expansion_hints("action", int(base_beat_words * 1.3)),
            ),
        ]

    def _build_default_beats(self, base_beat_words: int) -> List[Beat]:
        """构建默认「起承转合」四节拍"""
        return [
            Beat(
                description="起：交代场景与人物状态，抛出本章要处理的具体麻烦或悬念（可小但须清晰）。",
                target_words=base_beat_words,
                focus="hook",
                expansion_hints=self._generate_expansion_hints("hook", base_beat_words),
            ),
            Beat(
                description="承：阻碍升级或对手施压，人物关系或信息出现新变化。",
                target_words=base_beat_words,
                focus="dialogue",
                expansion_hints=self._generate_expansion_hints("dialogue", base_beat_words),
            ),
            Beat(
                description="转：主角做出选择、亮出底牌或发现盲点，情节出现可感知的转折。",
                target_words=base_beat_words,
                focus="action",
                expansion_hints=self._generate_expansion_hints("action", base_beat_words),
            ),
            Beat(
                description="合：阶段性结果落地，同时抛出下一章钩子（勿提前剧透全书谜底）。",
                target_words=base_beat_words,
                focus="suspense",
                expansion_hints=self._generate_expansion_hints("suspense", base_beat_words),
            ),
        ]

    # 节拍聚焦指令：CPMS 节点 beat-focus-instructions（prompt_packages）
    # 通过 PromptRegistry 统一读取，不再在此硬编码
    from infrastructure.ai.prompt_keys import BEAT_FOCUS_INSTRUCTIONS as _BEAT_PROMPT_ID

    def build_beat_prompt(self, beat: Beat, beat_index: int, total_beats: int) -> str:
        """构建单个节拍的生成提示（指令从 CPMS beat-focus-instructions 读取）"""
        from infrastructure.ai.prompt_registry import get_prompt_registry

        registry = get_prompt_registry()

        # 聚焦指令字典
        focus_instructions = registry.get_directives_dict(self._BEAT_PROMPT_ID, "_focus_instructions")
        instruction = focus_instructions.get(beat.focus, "")

        # 行动后果锚点轮转
        action_rotation = registry.get_list_field(self._BEAT_PROMPT_ID, "_sensory_rotation")
        if not action_rotation:
            action_rotation = [
                "本节拍至少一处行动后果锚点：位置变化、资源得失、身份暴露或关系变化。",
                "本节拍至少一处选择后果锚点：主角做出具体判断，并立刻产生代价或收益。",
                "本节拍至少一处信息差锚点：读者知道了新事实，主角也必须因此调整。",
                "本节拍至少一处局势锚点：对手、同伴或规则的态度发生可见变化。",
            ]
        anchor_line = action_rotation[beat_index % len(action_rotation)]

        # 叙事义务
        obligations = registry.get_field(self._BEAT_PROMPT_ID, "_obligations", {})
        if isinstance(obligations, dict):
            obligation = obligations.get(beat.focus, obligations.get("default", "叙事义务：推进情节或深化人物。"))
        else:
            obligation = "叙事义务：推进情节或深化人物。"

        # 扩写维度提示（核心改进：告诉 LLM 怎么凑够字数）
        expansion_block = ""
        if beat.expansion_hints:
            hints_text = "\n".join(f"- {hint}" for hint in beat.expansion_hints)
            expansion_block = f"\n\n【字数扩充方向】（请参考以下方向展开细节）\n{hints_text}"

        node_card_block = ""
        card = getattr(beat, "beat_card", None)
        if card is not None:
            node_card_block = (
                "\n\n【节点卡兑现要求】\n"
                f"- 必须写出主动动作：{card.active_action}\n"
                f"- 必须写出动作后的外界反馈：{card.external_feedback}\n"
                f"- 必须写出信息差变化：{card.information_delta}\n"
                f"- 必须推进钩子变化：{card.hook_delta}\n"
                f"- 禁止漂移：{card.forbidden_drift}\n"
                "- 不要把这些字段解释给读者看，要把它们写成正文里的动作、反应、发现和选择。"
            )

        # 使用 PromptRegistry 渲染 user 模板
        rendered = registry.render(
            self._BEAT_PROMPT_ID,
            variables={
                "beat_index": beat_index + 1,
                "total_beats": total_beats,
                "target_words": beat.target_words,
                "focus": beat.focus,
                "instruction": instruction,
                "description": beat.description,
                "anchor_line": anchor_line,
                "obligation": obligation,
            },
        )
        prompt = (rendered.user if rendered else "") or ""

        # 注入扩写维度
        if expansion_block:
            # 在 "密度与可检查要求" 之后插入
            prompt = prompt.replace(
                "\n\n⚠️ 篇幅控制",
                f"{expansion_block}{node_card_block}\n\n⚠️ 篇幅控制"
            )
        elif node_card_block:
            prompt = prompt.replace(
                "\n\n⚠️ 篇幅控制",
                f"{node_card_block}\n\n⚠️ 篇幅控制"
            )

        # 🔗 V2：注入节拍间过渡方式
        if beat_index > 0 and hasattr(beat, 'transition_from_prev') and beat.transition_from_prev:
            transition_block = (
                f"\n\n🔗【本节拍过渡方式】{beat.transition_from_prev}\n"
                f"→ 你的第一句话必须遵循此过渡方式与前节拍衔接"
            )
            prompt = transition_block + prompt

        # 🔗 V2：第一个节拍特殊处理——如果有前章桥段，强调章首衔接
        if beat_index == 0:
            prompt = "\n📌 这是本章第一个节拍——你的开头就是读者翻页后看到的第一段。必须与前章结尾自然衔接，不能像新故事一样重新开始。\n" + prompt

        # 最后一个节拍特殊处理：强调收尾（双重保障——conductor 也会注入更详细的收尾指令）
        if beat_index == total_beats - 1:
            prompt += "\n\n📌 这是本章最后一个节拍！必须：\n" \
                      "1. 给出完整的章节收尾——故事告一段落，读者能感知到「这一章讲完了」\n" \
                      "2. 可以抛出下一章的悬念钩子，但不要强行总结全章\n" \
                      "3. 用有画面感的方式结束——最后一个画面留在读者脑海中\n" \
                      "4. 绝对不能留下悬而未决的对话或行动"

        return prompt
