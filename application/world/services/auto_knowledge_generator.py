"""自动 Knowledge 生成器 - 从小说 Bible 生成初始知识图谱"""
import logging
from typing import Dict, Any
from domain.ai.services.llm_service import LLMService, GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from application.ai.knowledge_llm_contract import (
    build_initial_knowledge_system_prompt,
    parse_initial_knowledge_llm_response,
    to_knowledge_service_update_dict,
)
from application.core.novel_profile_lock import build_novel_profile_lock
from application.world.services.knowledge_service import KnowledgeService

logger = logging.getLogger(__name__)


class AutoKnowledgeGenerator:
    """自动 Knowledge 生成器

    根据小说标题和 Bible 内容，使用 LLM 生成：
    - premise_lock（核心梗概）
    - 初始知识三元组（facts）
    """

    def __init__(self, llm_service: LLMService, knowledge_service: KnowledgeService):
        self.llm_service = llm_service
        self.knowledge_service = knowledge_service

    async def generate_and_save(
        self,
        novel_id: str,
        title: str,
        bible_summary: str = "",
        premise: str = "",
    ) -> Dict[str, Any]:
        """生成并保存初始 Knowledge

        Args:
            novel_id: 小说 ID
            title: 小说标题
            bible_summary: Bible 摘要（可选，提升生成质量）

        Returns:
            生成的 Knowledge 数据
        """
        logger.info(f"AutoKnowledgeGenerator: generating knowledge for novel '{title}' ({novel_id})")

        profile_lock = self._resolve_profile_lock(
            novel_id=novel_id,
            title=title,
            premise=premise,
        )
        knowledge_data = await self._generate_knowledge_data(title, bible_summary, profile_lock)
        if profile_lock and not (knowledge_data.get("premise_lock") or "").strip():
            knowledge_data["premise_lock"] = profile_lock

        self._save_to_knowledge(novel_id, knowledge_data)

        logger.info(
            f"Knowledge generated for {novel_id}: "
            f"facts={len(knowledge_data.get('facts', []))}"
        )
        return knowledge_data

    def _resolve_profile_lock(self, *, novel_id: str, title: str = "", premise: str = "") -> str:
        try:
            from infrastructure.persistence.database.connection import get_database

            row = get_database().fetch_one(
                "SELECT title, premise, target_chapters FROM novels WHERE id = ?",
                (novel_id,),
            )
            if row:
                return build_novel_profile_lock(
                    title=row.get("title") or title or "",
                    premise=row.get("premise") or premise or title,
                    target_chapters=int(row.get("target_chapters") or 0) or None,
                )
        except Exception as exc:
            logger.debug("读取 Knowledge 档案锁失败，使用调用参数: %s", exc)

        return build_novel_profile_lock(title=title, premise=premise or title)

    async def _generate_knowledge_data(
        self,
        title: str,
        bible_summary: str,
        profile_lock: str = "",
    ) -> Dict[str, Any]:
        """使用 LLM 生成 Knowledge 数据（CPMS 统一入口）"""

        context_parts = []
        if profile_lock.strip():
            context_parts.append(f"**故事内核锁：**\n{profile_lock}")
        if bible_summary.strip():
            context_parts.append(f"**小说设定摘要：**\n{bible_summary}")
        context_section = "\n\n" + "\n\n".join(context_parts) if context_parts else ""

        # CPMS render
        from infrastructure.ai.prompt_keys import KNOWLEDGE_INITIAL
        from infrastructure.ai.prompt_registry import get_prompt_registry

        registry = get_prompt_registry()
        variables = {
            "title": title,
            "bible_summary": bible_summary or "",
            "profile_lock": profile_lock or "",
        }
        prompt = registry.render_to_prompt(KNOWLEDGE_INITIAL, variables)

        if not prompt:
            # 降级：使用契约中的系统提示词
            system_prompt = build_initial_knowledge_system_prompt()
            user_prompt = f"小说标题：《{title}》{context_section}"
            prompt = Prompt(system=system_prompt, user=user_prompt)
        elif profile_lock and "故事内核锁" not in (prompt.system + prompt.user):
            prompt = Prompt(
                system=prompt.system,
                user=(prompt.user or "").rstrip() + "\n\n" + profile_lock,
            )

        config = GenerationConfig(max_tokens=2048, temperature=0.4)

        result = await self.llm_service.generate(prompt, config)

        payload, errors = parse_initial_knowledge_llm_response(result.content)
        if payload is None:
            logger.warning(
                "AutoKnowledgeGenerator: LLM 输出未通过契约校验: %s",
                "; ".join(errors) if errors else "unknown",
            )
            return {
                "version": 1,
                "premise_lock": "",
                "chapters": [],
                "facts": [],
            }

        return to_knowledge_service_update_dict(payload)

    def _save_to_knowledge(self, novel_id: str, knowledge_data: Dict[str, Any]) -> None:
        """保存到 Knowledge（兼容带 version/chapters 的完整 update 字典）。"""
        premise_lock = knowledge_data.get("premise_lock", "")
        facts_data = knowledge_data.get("facts", [])

        data = {
            "version": knowledge_data.get("version", 1),
            "premise_lock": premise_lock,
            "chapters": knowledge_data.get("chapters", []),
            "facts": [
                {
                    "id": f.get("id", f"fact-{i+1:03d}"),
                    "subject": f.get("subject", ""),
                    "predicate": f.get("predicate", ""),
                    "object": f.get("object", ""),
                    "chapter_id": f.get("chapter_id"),
                    "note": f.get("note", "") or "",
                    "entity_type": f.get("entity_type"),
                    "importance": f.get("importance"),
                    "location_type": f.get("location_type"),
                    "description": f.get("description"),
                    "first_appearance": f.get("first_appearance"),
                    "related_chapters": f.get("related_chapters", []),
                    "tags": f.get("tags", []),
                    "attributes": f.get("attributes", {}),
                    "confidence": f.get("confidence"),
                    "source_type": f.get("source_type", "ai_generated"),
                    "subject_entity_id": f.get("subject_entity_id"),
                    "object_entity_id": f.get("object_entity_id"),
                }
                for i, f in enumerate(facts_data)
            ],
        }

        self.knowledge_service.update_knowledge(novel_id, data)
        logger.debug(f"Saved knowledge for {novel_id}: premise_lock={bool(premise_lock)}, facts={len(facts_data)}")
