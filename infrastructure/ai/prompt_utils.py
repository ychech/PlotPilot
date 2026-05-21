"""prompt_utils — CPMS prompt fetch utility functions.

Design:
- All business code uses get_prompt_system() / get_prompt_user_template()
- Primary source: PromptRegistry (DB-driven)
- Fallback: hardcoded defaults passed by caller
- Graceful degradation: system never crashes if Registry is unavailable

Migration pattern (3 steps):
  Step 1: Register fallback constants
  Step 2: Business code uses get_prompt_system(key)
  Step 3: Add prompts to DB (seed JSON or manual entry in Prompt Plaza)

Usage:
    from infrastructure.ai.prompt_utils import get_prompt_system

    system = get_prompt_system("bible-all", fallback=BIBLE_ALL_SYSTEM)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def get_prompt_system(node_key: str, fallback: str = "") -> str:
    """Get system prompt for a node (CPMS unified entry).

    Args:
        node_key: Prompt node key
        fallback: Fallback text when Registry is unavailable

    Returns:
        system prompt text
    """
    try:
        from infrastructure.ai.prompt_registry import get_prompt_registry
        registry = get_prompt_registry()
        system = registry.get_system(node_key)
        if system:
            logger.info("━━━ Prompt[%s] ━━━ (system) %d chars", node_key, len(system))
            return system
        logger.warning("━━━ Prompt[%s] ━━━ (system) MISSING, fallback %d chars", node_key, len(fallback))
    except Exception as exc:
        logger.warning("━━━ Prompt[%s] ━━━ (system) ERROR (%s), fallback %d chars", node_key, exc, len(fallback))

    package_rendered = _render_from_package_node(node_key, {})
    if package_rendered and package_rendered.get("system"):
        return package_rendered["system"]
    return fallback


def get_prompt_user_template(node_key: str, fallback: str = "") -> str:
    """Get user_template for a node.

    Args:
        node_key: Prompt node key
        fallback: Fallback text

    Returns:
        user_template text
    """
    try:
        from infrastructure.ai.prompt_registry import get_prompt_registry
        registry = get_prompt_registry()
        user_template = registry.get_user_template(node_key)
        if user_template:
            logger.info("━━━ Prompt[%s] ━━━ (user)   %d chars", node_key, len(user_template))
            return user_template
    except Exception as exc:
        logger.debug("PromptRegistry unavailable (node_key=%s): %s", node_key, exc)

    package_rendered = _render_from_package_node(node_key, {})
    if package_rendered and package_rendered.get("user"):
        return package_rendered["user"]
    return fallback


def render_prompt(
    node_key: str,
    variables: Optional[Dict[str, Any]] = None,
    fallback_system: str = "",
    fallback_user: str = "",
) -> Optional[Dict[str, str]]:
    """Render prompt and return {"system": ..., "user": ...}.

    CPMS unified render entry with graceful degradation.
    """
    try:
        from infrastructure.ai.prompt_registry import get_prompt_registry
        registry = get_prompt_registry()
        result = registry.render(node_key, variables)
        if result and (result.system or result.user):
            return {
                "system": result.system or fallback_system,
                "user": result.user or fallback_user,
            }
    except Exception as exc:
        logger.debug("PromptRegistry render failed (node_key=%s): %s", node_key, exc)

    package_rendered = _render_from_package_node(node_key, variables or {})
    if package_rendered and (package_rendered["system"] or package_rendered["user"]):
        return package_rendered

    # Fallback: simple format_map rendering
    var_map = variables or {}
    system = _simple_render(fallback_system, var_map)
    user = _simple_render(fallback_user, var_map)
    return {"system": system, "user": user}


def render_prompt_text(node_key: str, variables: Optional[Dict[str, Any]] = None) -> str:
    """Render a text-only prompt node and return system+user content."""
    rendered = render_prompt(node_key, variables)
    if not rendered:
        return ""
    return "\n\n".join(
        part.strip() for part in [rendered.get("system", ""), rendered.get("user", "")] if part.strip()
    )


def _render_from_package_node(node_key: str, variables: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Render directly from prompt_packages/nodes as a non-Python fallback."""
    try:
        root = Path(__file__).resolve().parent / "prompt_packages" / "nodes" / node_key
        if not root.is_dir():
            return None
        system = (root / "system.md").read_text(encoding="utf-8") if (root / "system.md").is_file() else ""
        user = (root / "user.md").read_text(encoding="utf-8") if (root / "user.md").is_file() else ""
        return {
            "system": _simple_render(system, variables),
            "user": _simple_render(user, variables),
        }
    except Exception as exc:
        logger.debug("Package prompt render failed (node_key=%s): %s", node_key, exc)
        return None


def _simple_render(template: str, variables: Dict[str, Any]) -> str:
    """Simple template rendering (fallback mode)."""
    if not template or not variables:
        return template

    class SafeDict(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    try:
        return template.format_map(SafeDict(variables))
    except (KeyError, ValueError, IndexError):
        return template
