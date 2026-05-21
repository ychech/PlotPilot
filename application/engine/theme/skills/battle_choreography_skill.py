"""战斗编排 Skill — 为战斗场景提供招式/节奏编排的增强

适用于武侠、玄幻、仙侠、奇幻等包含战斗场景的题材。
"""

from typing import List
from application.engine.theme.theme_agent import ThemeSkill


class BattleChoreographySkill(ThemeSkill):
    """战斗编排器 — 增强战斗场景的动作描写质量"""

    @property
    def skill_key(self) -> str:
        return "battle_choreography"

    @property
    def skill_name(self) -> str:
        return "战斗编排"

    @property
    def skill_description(self) -> str:
        return "增强战斗场景的招式拆解、节奏控制和画面感，避免「他一拳打去」式的空泛描写"

    @property
    def compatible_genres(self) -> List[str]:
        return ["xuanhuan", "xianxia", "wuxia", "fantasy"]

    def on_beat_enhance(
        self,
        beat_description: str,
        beat_focus: str,
        chapter_number: int,
        outline: str,
    ) -> str:
        if beat_focus in ("action", "martial_arts", "power_reveal") or \
           any(kw in beat_description for kw in ["战斗", "对决", "交锋", "过招", "攻击"]):
            return (
                "战斗编排增强提示：\n"
                "- 按当前文风和本章进展选择镜头距离，别突然改成招式说明书。\n"
                "- 每个关键动作都要改变战局：距离、伤势、底牌、心理判断或旁人立场至少变一项。\n"
                "- 抓 2-3 个关键动作链重点写，动作链内部用连续因果，不逐招清单。\n"
                "- 旁观者反应只选一两个具体人物，反应必须推进信息或关系，不能只负责震惊。\n"
                "- 少用破折号、不是而是、除了/此外来解释战斗；让动作和后果自己说明。"
            )
        return ""

    def on_audit_enhance(
        self,
        chapter_number: int,
        chapter_content: str,
        outline: str,
    ) -> List[str]:
        checks = []
        if any(kw in outline for kw in ["战斗", "对决", "比武", "交锋", "攻击"]):
            checks.append("检查战斗场景是否有具体的招式/动作描写（不能只有结果没有过程）")
            checks.append("检查战斗节奏是否有快慢变化（避免全程高强度或全程平淡）")
        return checks
