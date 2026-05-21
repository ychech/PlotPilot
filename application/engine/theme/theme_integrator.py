"""Theme/Skill 系统集成器 - 将题材能力注入写作管线

核心设计：
1. SkillOrchestrator: 编排多个 Skill 的调用
2. ThemeAwareContextBuilder: Theme 感知的上下文构建
3. BattleTrigger: 战斗场景检测和增强注入

解决问题：
- BattleChoreographySkill 已实现但从未被调用
- ThemeAgent 未集成到工作流
- 战斗触发关键词太少
"""
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from application.engine.theme.theme_agent import ThemeAgent, ThemeSkill
    from application.engine.services.context_builder import ContextBuilder, Beat

logger = logging.getLogger(__name__)


class SkillOrchestrator:
    """Skill 编排器 - 管理多个 Skill 的生命周期和调用

    负责在写作管线的各个阶段调用所有注册的 Skill，
    收集增强内容并组装到最终结果中。
    """

    def __init__(self, skills: List["ThemeSkill"] = None):
        self._skills: Dict[str, "ThemeSkill"] = {}
        if skills:
            for skill in skills:
                self.register(skill)

    def register(self, skill: "ThemeSkill") -> None:
        """注册 Skill"""
        self._skills[skill.skill_key] = skill
        logger.debug(f"[SkillOrchestrator] 已注册 Skill: {skill.skill_name}")

    def get_skill(self, skill_key: str) -> Optional["ThemeSkill"]:
        """获取 Skill"""
        return self._skills.get(skill_key)

    def invoke_context_build(
        self,
        novel_id: str,
        chapter_number: int,
        outline: str,
        existing_context: str,
    ) -> str:
        """调用所有 Skill 的上下文构建增强

        Returns:
            增强的上下文文本
        """
        parts = []
        for skill in self._skills.values():
            try:
                text = skill.on_context_build(
                    novel_id, chapter_number, outline, existing_context
                )
                if text and text.strip():
                    parts.append(f"【{skill.skill_name}】\n{text}")
            except Exception as e:
                logger.warning(
                    f"[SkillOrchestrator] Skill {skill.skill_key} 上下文增强失败: {e}"
                )
        return "\n\n".join(parts) if parts else ""

    def invoke_beat_enhance(
        self,
        beat_description: str,
        beat_focus: str,
        chapter_number: int,
        outline: str,
    ) -> str:
        """调用所有 Skill 的节拍增强

        Returns:
            增强的节拍提示
        """
        parts = []
        for skill in self._skills.values():
            try:
                text = skill.on_beat_enhance(
                    beat_description, beat_focus, chapter_number, outline
                )
                if text and text.strip():
                    parts.append(text)
            except Exception as e:
                logger.debug(
                    f"[SkillOrchestrator] Skill {skill.skill_key} 节拍增强失败: {e}"
                )
        return "\n".join(parts) if parts else ""

    def invoke_prompt_build(
        self,
        novel_id: str,
        chapter_number: int,
        outline: str,
    ) -> str:
        """调用所有 Skill 的提示词构建增强

        Returns:
            增强的提示词文本
        """
        parts = []
        for skill in self._skills.values():
            try:
                if hasattr(skill, "on_prompt_build"):
                    text = skill.on_prompt_build(novel_id, chapter_number, outline)
                    if text and text.strip():
                        parts.append(text)
            except Exception as e:
                logger.debug(
                    f"[SkillOrchestrator] Skill {skill.skill_key} 提示词增强失败: {e}"
                )
        return "\n".join(parts) if parts else ""

    def invoke_audit_enhance(
        self,
        chapter_number: int,
        chapter_content: str,
        outline: str,
    ) -> List[str]:
        """调用所有 Skill 的审计增强

        Returns:
            审计检查项列表
        """
        checks = []
        for skill in self._skills.values():
            try:
                items = skill.on_audit_enhance(chapter_number, chapter_content, outline)
                if items:
                    checks.extend(items)
            except Exception as e:
                logger.debug(
                    f"[SkillOrchestrator] Skill {skill.skill_key} 审计增强失败: {e}"
                )
        return checks


class BattleTrigger:
    """战斗触发器 - 检测并注入战斗增强

    扩展战斗触发关键词，解决"没有打斗场面"的问题。
    """

    # 扩展的战斗触发关键词（覆盖常见战斗场景）
    BATTLE_KEYWORDS = [
        # 直接战斗词
        "战斗", "对决", "交锋", "过招", "比武", "擂台", "决斗",
        "厮杀", "激战", "搏斗", "火拼", "对峙", "混战", "乱战",
        # 动作词
        "攻击", "防守", "反击", "突袭", "围攻", "强攻", "猛攻",
        "杀", "斩", "刺", "砍", "劈", "轰", "打", "击",
        # 武器/招式词
        "招式", "功法", "武技", "必杀技", "绝招", "秘技",
        "剑法", "刀法", "拳法", "掌法", "腿法",
        # 修炼词
        "修为压制", "境界碾压", "以弱胜强", "逆袭", "越级挑战",
        # 情绪/状态词
        "爆发", "觉醒", "突破", "拼命", "绝境", "生死",
        "怒", "愤", "杀气", "战意",
        # 结果词
        "胜负", "输赢", "败", "胜", "死战", "不死不休",
    ]

    # 冲突升级关键词（情绪推到高潮，需要接战斗）
    CONFLICT_ESCALATION_KEYWORDS = [
        "冲突升级", "矛盾激化", "剑拔弩张", "一触即发",
        "针锋相对", "势不两立", "水火不容", "仇恨",
        "报仇", "复仇", "雪恨", "清算", "算账",
        "忍无可忍", "不再退让", "最后通牒",
    ]

    @classmethod
    def detect_battle_scene(cls, outline: str, beat_description: str = "") -> bool:
        """检测是否为战斗场景

        Args:
            outline: 章节大纲
            beat_description: 节拍描述

        Returns:
            True 表示检测到战斗场景
        """
        combined = f"{outline} {beat_description}".lower()
        return any(kw in combined for kw in cls.BATTLE_KEYWORDS)

    @classmethod
    def detect_conflict_escalation(cls, outline: str, beat_description: str = "") -> bool:
        """检测是否为冲突升级场景（需要接战斗）

        Args:
            outline: 章节大纲
            beat_description: 节拍描述

        Returns:
            True 表示检测到冲突升级
        """
        combined = f"{outline} {beat_description}".lower()
        return any(kw in combined for kw in cls.CONFLICT_ESCALATION_KEYWORDS)

    @classmethod
    def get_battle_enhancement_prompt(
        cls,
        beat_focus: str = "",
        *,
        style_summary: str = "",
        chapter_progress: str = "",
        beat_index: Optional[int] = None,
        total_beats: Optional[int] = None,
    ) -> str:
        """获取战斗增强提示词

        Args:
            beat_focus: 节拍聚焦点

        Returns:
            战斗增强提示文本
        """
        phase = "unknown"
        if beat_index is not None and total_beats:
            ratio = (beat_index + 1) / max(total_beats, 1)
            if ratio <= 0.34:
                phase = "setup"
            elif ratio >= 0.78:
                phase = "landing"
            else:
                phase = "turn"

        phase_rule = {
            "setup": "本段偏开局：先写双方站位、旧伤/体力/底牌限制和第一次试探，不急着直接秒杀。",
            "turn": "本段偏中段：必须出现战局变化，例如破招、换招、代价显现、旁人介入或主角判断修正。",
            "landing": "本段偏收束：必须写出本轮战斗结果或阶段性代价，可以留下一章钩子，但钩子要落在结果之后。",
        }.get(phase, "按当前节拍功能决定战斗进展，不要把战斗写成可替换模板。")
        style_rule = (
            f"文风对齐：{style_summary[:220]}。打斗句长、用词和镜头距离要贴合这份文风。"
            if style_summary.strip()
            else "文风对齐：沿用本章既有叙述腔调，不要突然改成游戏技能播报或说明书。"
        )
        progress_rule = (
            f"进展承接：本章已写到「{chapter_progress[-260:]}」。战斗必须从这个状态继续，别重置站位、伤势、情绪或已亮出的底牌。"
            if chapter_progress.strip()
            else "进展承接：根据本章大纲和当前节拍推进战斗，不要脱离章节任务另开一场无关打斗。"
        )
        focus_rule = {
            "power_reveal": "焦点是实力显露：写清底牌如何改变局势，反应要具体到一两个人，不要群体震惊排比。",
            "martial_arts": "焦点是招式交锋：抓两三个关键动作链，写出拆招和身体代价，不逐招流水账。",
            "action": "焦点是行动推进：每个动作都要改变距离、伤势、信息或胜负判断。",
            "emotion": "焦点是情绪爆发：情绪落在手上、步伐、呼吸和选择里，不用长内心独白解释。",
        }.get(beat_focus, "焦点由当前节拍决定：战斗描写必须服务剧情进展。")

        return (
            "【动态打斗指导】\n"
            f"- {style_rule}\n"
            f"- {progress_rule}\n"
            f"- {phase_rule}\n"
            f"- {focus_rule}\n"
            "- 写法：用「意图→动作→受阻/碰撞→后果」推进。每轮至少带来一个新后果：受伤、失位、暴露底牌、关系变化、线索确认或危机升级。\n"
            "- 禁止：不要套固定七步模板，不要逐招清单，不要靠「破折号」「不是…而是…」「除了/此外」来解释打斗，不要只写围观者震惊。"
        )

    @classmethod
    def should_inject_battle_skill(
        cls,
        outline: str,
        beat_description: str = "",
        beat_focus: str = "",
    ) -> bool:
        """判断是否需要注入战斗 Skill

        条件：
        1. 直接检测到战斗关键词
        2. 或检测到冲突升级 + beat_focus 是 action/emotion
        """
        if cls.detect_battle_scene(outline, beat_description):
            return True

        # 冲突升级场景，如果聚焦点是动作或情绪，则注入战斗
        if cls.detect_conflict_escalation(outline, beat_description):
            if beat_focus in ("action", "emotion", "suspense"):
                return True

        return False


class ThemeAwarePromptBuilder:
    """Theme 感知的提示词构建器

    将 ThemeAgent 的能力注入到提示词构建过程中。
    """

    def __init__(self, theme_agent: Optional["ThemeAgent"] = None):
        self._theme_agent = theme_agent
        self._orchestrator: Optional[SkillOrchestrator] = None

        if theme_agent:
            self._orchestrator = SkillOrchestrator(theme_agent.get_skills())

    def set_theme_agent(self, agent: "ThemeAgent") -> None:
        """设置 Theme Agent"""
        self._theme_agent = agent
        self._orchestrator = SkillOrchestrator(agent.get_skills())

    def build_system_persona(self, genre: str = None) -> str:
        """构建系统人设

        Args:
            genre: 题材类型

        Returns:
            系统人设文本
        """
        if not self._theme_agent:
            return ""

        try:
            persona = self._theme_agent.get_effective_system_persona()
            if persona and persona.strip():
                return f"\n【作家风格】\n{persona}\n"
        except Exception as e:
            logger.debug(f"[ThemeIntegrator] 获取系统人设失败: {e}")

        return ""

    def build_writing_rules(self, genre: str = None) -> str:
        """构建写作规则

        Args:
            genre: 题材类型

        Returns:
            写作规则文本
        """
        if not self._theme_agent:
            return ""

        try:
            rules = self._theme_agent.get_effective_writing_rules()
            if rules:
                rules_text = "\n".join(f"- {r}" for r in rules)
                if rules_text.strip():
                    return f"\n【题材专项规则】\n{rules_text}\n"
        except Exception as e:
            logger.debug(f"[ThemeIntegrator] 获取写作规则失败: {e}")

        return ""

    def build_beat_enhancement(
        self,
        beat_description: str,
        beat_focus: str,
        chapter_number: int,
        outline: str,
        style_summary: str = "",
        chapter_progress: str = "",
        beat_index: Optional[int] = None,
        total_beats: Optional[int] = None,
    ) -> str:
        """构建节拍增强提示

        Args:
            beat_description: 节拍描述
            beat_focus: 节拍聚焦点
            chapter_number: 章节号
            outline: 章节大纲

        Returns:
            增强提示文本
        """
        parts = []

        # 1. 检测是否需要战斗增强
        if BattleTrigger.should_inject_battle_skill(outline, beat_description, beat_focus):
            parts.append(
                BattleTrigger.get_battle_enhancement_prompt(
                    beat_focus,
                    style_summary=style_summary,
                    chapter_progress=chapter_progress,
                    beat_index=beat_index,
                    total_beats=total_beats,
                )
            )

        # 2. 调用 Skill 编排器
        if self._orchestrator:
            skill_enhance = self._orchestrator.invoke_beat_enhance(
                beat_description, beat_focus, chapter_number, outline
            )
            if skill_enhance:
                parts.append(skill_enhance)

        return "\n\n".join(parts) if parts else ""

    def build_format_rules(self) -> str:
        """构建格式规则（分段、对话等）

        Returns:
            格式规则文本
        """
        return """【分段与段落法则（必须遵守）】
1. 段落是意义的集合，不是句子的分行。属于同一视觉焦点、同一动作链或同一心理转折的句子，必须合并在同一段落里，用句号衔接而非换行切分
2. 段落长度应有呼吸感：铺陈/叙事段落2-5句（约80-200字），高潮暴击可用1句独段（仅限冲突引爆/重大揭露/情绪转折的瞬间），全章独段比例不得超过15%
3. 连续短对话（3句以内的一问一答）应合并为一段对话流，避免对话变成打字机式的一行一句
4. 场景转换时空一行分隔；同一场景内的动作、观察和心理应聚合在同一段落里
5. 适配手机阅读：段落不宜过长（不超过250字），但绝不要为"留白"而把有机整体拆成碎片——留白来自段落间的空行，不是来自段落内部的强制换行"""


class ThemeIntegrator:
    """Theme 集成器 - 统一管理 Theme 相关的集成逻辑

    使用方式：
        integrator = ThemeIntegrator()
        integrator.initialize(genre="xuanhuan")

        # 构建提示词时
        system_persona = integrator.build_system_persona()
        writing_rules = integrator.build_writing_rules()
        beat_enhance = integrator.build_beat_enhancement(...)
    """

    def __init__(self):
        self._theme_agent: Optional["ThemeAgent"] = None
        self._prompt_builder: ThemeAwarePromptBuilder = ThemeAwarePromptBuilder()
        self._initialized = False

    def initialize(self, genre: str = None) -> bool:
        """初始化 Theme Agent

        Args:
            genre: 题材类型

        Returns:
            True 表示初始化成功
        """
        if self._initialized:
            return True

        try:
            from application.engine.theme.theme_registry import ThemeAgentRegistry

            registry = ThemeAgentRegistry()
            registry.auto_discover()

            self._theme_agent = registry.get_or_default(genre)
            if self._theme_agent:
                self._prompt_builder.set_theme_agent(self._theme_agent)
                logger.info(
                    f"[ThemeIntegrator] 已初始化 Theme Agent: "
                    f"{self._theme_agent.__class__.__name__}"
                )

            self._initialized = True
            return True

        except Exception as e:
            logger.warning(f"[ThemeIntegrator] 初始化失败: {e}")
            return False

    def get_theme_agent(self) -> Optional["ThemeAgent"]:
        """获取 Theme Agent"""
        return self._theme_agent

    def get_prompt_builder(self) -> ThemeAwarePromptBuilder:
        """获取提示词构建器"""
        return self._prompt_builder

    def build_system_persona(self) -> str:
        """构建系统人设"""
        return self._prompt_builder.build_system_persona()

    def build_writing_rules(self) -> str:
        """构建写作规则"""
        return self._prompt_builder.build_writing_rules()

    def build_beat_enhancement(
        self,
        beat_description: str,
        beat_focus: str,
        chapter_number: int,
        outline: str,
        style_summary: str = "",
        chapter_progress: str = "",
        beat_index: Optional[int] = None,
        total_beats: Optional[int] = None,
    ) -> str:
        """构建节拍增强"""
        return self._prompt_builder.build_beat_enhancement(
            beat_description,
            beat_focus,
            chapter_number,
            outline,
            style_summary=style_summary,
            chapter_progress=chapter_progress,
            beat_index=beat_index,
            total_beats=total_beats,
        )

    def build_format_rules(self) -> str:
        """构建格式规则"""
        return self._prompt_builder.build_format_rules()

    def get_beat_templates(self, chapter_number: int, outline: str) -> List[Dict]:
        """获取节拍模板

        Args:
            chapter_number: 章节号
            outline: 章节大纲

        Returns:
            节拍模板列表
        """
        if not self._theme_agent:
            return []

        try:
            templates = self._theme_agent.get_beat_templates(chapter_number, outline)
            if templates:
                # 转换为字典列表
                return [
                    {
                        "description": t.description,
                        "target_words": t.target_words,
                        "focus": t.focus,
                        "expansion_hints": t.expansion_hints or [],
                    }
                    for t in templates.beats
                ]
        except Exception as e:
            logger.debug(f"[ThemeIntegrator] 获取节拍模板失败: {e}")

        return []


# 全局 Theme 集成器实例
_theme_integrator: Optional[ThemeIntegrator] = None


def get_theme_integrator() -> ThemeIntegrator:
    """获取全局 Theme 集成器"""
    global _theme_integrator
    if _theme_integrator is None:
        _theme_integrator = ThemeIntegrator()
    return _theme_integrator


def initialize_theme(genre: str = None) -> ThemeIntegrator:
    """初始化全局 Theme 集成器

    Args:
        genre: 题材类型

    Returns:
        ThemeIntegrator 实例
    """
    integrator = get_theme_integrator()
    integrator.initialize(genre)
    return integrator
