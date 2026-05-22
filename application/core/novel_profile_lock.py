"""小说档案锁 — 生成 LLM 提示词中不可变的内核约束块。

多模块共用（auto_bible_generator / auto_knowledge_generator / continuous_planning_service /
AutoNovelGenerationWorkflow），统一生成格式，避免各处硬编码。
"""

import re
from typing import Optional

from infrastructure.ai.prompt_keys import NOVEL_PROFILE_LOCK
from infrastructure.ai.prompt_utils import render_prompt_text


def _extract_bracket_meta(premise: str) -> dict[str, str]:
    """Extract lightweight metadata from Chinese bracket blocks.

    ``premise`` may start with an internal length-planning block before the
    user-facing genre block, so scan all early bracket blocks and pick known
    metadata keys instead of only inspecting the first one.
    """
    text = premise or ""

    meta: dict[str, str] = {}
    for match in re.finditer(r"【([^】]+)】", text):
        block = match.group(1)
        for part in re.split(r"[;；]", block):
            if not part.strip():
                continue
            if ":" in part:
                key, value = part.split(":", 1)
            elif "：" in part:
                key, value = part.split("：", 1)
            else:
                continue
            key = key.strip()
            if key in {"类型", "题材", "赛道", "世界观基调", "基调"}:
                meta[key] = value.strip()
        if meta.get("类型") or meta.get("题材") or meta.get("赛道"):
            if meta.get("世界观基调") or meta.get("基调"):
                break
    return meta


def build_story_kernel(*, premise: str = "", title: str = "") -> dict[str, str]:
    """从创建页输入提炼本地兜底用的故事内核。

    这是轻量、无 LLM 的标题兜底输入结构，避免路由直接依赖长梗概原文。
    """
    premise = (premise or "").strip()
    title = (title or "").strip()
    compact = " ".join((premise or title).split())
    return {
        "title": title,
        "premise": compact,
    }


def suggest_title_from_kernel(kernel: dict[str, str] | str) -> str:
    """根据故事内核生成一个短标题兜底。

    仅在 LLM 起名失败时使用；目标是返回可保存的短标题，而不是高质量商业命名。
    """
    if isinstance(kernel, str):
        premise = kernel.strip()
        title = ""
    else:
        title = (kernel.get("title") or "").strip()
        premise = (kernel.get("premise") or "").strip()

    if title and len(title) <= 12:
        return title

    text = premise or title
    if not text:
        return "未命名小说"

    candidates = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,8}", text)
    stopwords = {"一个", "这是", "讲述", "故事", "主角", "世界", "获得", "开始", "发现"}
    for candidate in candidates:
        if candidate not in stopwords:
            return candidate[:8]
    return text[:8] or "未命名小说"


def build_novel_profile_lock(
    *,
    title: str = "",
    premise: str = "",
    target_chapters: Optional[int] = None,
) -> str:
    """构建「故事内核锁」文本块，注入 LLM 系统提示词以确保跨章一致性。

    Args:
        title: 书名
        premise: 故事梗概/核心创意
        target_chapters: 目标章数（可选）

    Returns:
        Markdown 格式的内核锁字符串；缺少关键信息时返回空字符串。
    """
    title = (title or "").strip()
    premise = (premise or "").strip()

    if not premise and not title:
        return ""

    genre = ""
    world_tone = ""
    if premise:
        meta = _extract_bracket_meta(premise)
        genre = meta.get("类型") or meta.get("题材") or meta.get("赛道") or ""
        world_tone = meta.get("世界观基调") or meta.get("基调") or ""

    display_premise = premise[:800] + ("..." if len(premise) > 800 else "")
    chapter_count = int(target_chapters or 0)
    rendered = render_prompt_text(
        NOVEL_PROFILE_LOCK,
        {
            "title": title,
            "genre": genre,
            "world_tone": world_tone,
            "premise": display_premise,
            "target_words": f"{chapter_count * 2000:,}" if chapter_count else "",
            "target_chapters": str(chapter_count) if chapter_count else "",
            "chapter_words": "2000" if chapter_count else "",
        },
    )
    return "\n".join(line for line in rendered.splitlines() if line.strip() and not line.rstrip().endswith("："))
