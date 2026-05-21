"""从模型输出中剥离不应展示给用户的推理/思维链片段。

部分「thinking」或带 reasoning 通道的模型会把链式思考混在正文中；结构化 JSON 管线
已有部分清洗，小说正文生成路径此前未统一处理，导致用户可见正文被污染。

🔥 覆盖的模型及变体：
- DeepSeek-R1: <redacted_reasoning>...</redacted_reasoning>
- DeepSeek-R1: <think>...</think> (可能带 | 变体 <think|>)
- DeepSeek-V3: <thinking>...</thinking>
- QwQ:
- Gemini thinking: <thinking>...</thinking>
- 部分模型: [thinking]...[/thinking]
- 🔥 新增：DeepSeek 聊天模型偶尔在正文前添加「我来分析一下...」等前言
- 🔥 新增：某些模型输出 <!-- reason -->...<!-- /reason --> XML 注释
- 🔥 新增：某些模型输出 **thinking**... 等 Markdown 格式思考链
"""
from __future__ import annotations

import re

# 使用 \\x3c / \\x3e 表示尖括号，避免部分工具链误解析 XML 状字面量。
_REDACTED_BLOCK = re.compile(
    br"\x3credacted_reasoning\x3e.*?\x3c/redacted_reasoning\x3e".decode(),
    re.DOTALL,
)
_THINK_BLOCK = re.compile(
    br"\x3cthink\x7c?\x3e.*?\x3c\x2fthink\x7c?\x3e".decode(),
    re.DOTALL,
)
_THINKING_BLOCK = re.compile(
    br"\x3cthinking\x3e.*?\x3c\x2fthinking\x3e".decode(),
    re.DOTALL,
)
_BRACKET_THINKING = re.compile(
    r"\[thinking\].*?\[/thinking\]",
    re.DOTALL | re.IGNORECASE,
)
# 🔥 新增：XML 注释形式 <!-- reason -->...<!-- /reason -->
_REASON_COMMENT = re.compile(
    r"<!--\s*reason\s*-->.*?<!--\s*/reason\s*-->",
    re.DOTALL | re.IGNORECASE,
)
# 🔥 新增：Markdown 加粗思考 **thinking**...\*\*end thinking\*\*
_MD_THINKING = re.compile(
    r"\*\*thinking\*\*.*?\*\*end\s+thinking\*\*",
    re.DOTALL | re.IGNORECASE,
)
# 🔥 新增：部分模型在正文前加「分析：...」或「Analysis: ...」后跟换行
#          但这太容易误匹配，只处理明确以换行结束的前言
_PREFACE_ANALYSIS = re.compile(
    r"^(?:我来分析一下|分析[：:]|Analysis[：:]).*?\n(?=[^\n])",
    re.DOTALL | re.IGNORECASE,
)
_ALL_CAPS_STATUS_PANEL = re.compile(
    r"^\s*(?:[A-Z][A-Z0-9_-]{2,}\s*/\s*)?"
    r"(?:[A-Z][A-Z0-9_ -]{2,}\s*:\s*[^/]+)"
    r"(?:\s*/\s*[A-Z][A-Z0-9_ -]{2,}\s*:\s*[^/]+)+\s*$"
)
_ALL_CAPS_STATUS_FIELD = re.compile(
    r"^\s*(?:SYNAPSE-\d+|STATUS|HOST|EXTERNAL SYNC|SYSTEM|SYNC|MEMORY|OBJECTIVE|MISSION|ERROR|WARNING)"
    r"\s*(?::|/).*$",
    re.IGNORECASE,
)
_ALL_CAPS_STATUS_FRAGMENT = re.compile(
    r"^\s*(?:SYNAPSE(?:-\d*)?|STATUS|HOST|EXTERNAL(?:\s+SYNC)?|SYSTEM|SYNC|MEMORY|OBJECTIVE|MISSION|ERROR|WARNING)"
    r"(?:\s*[/:-]?\s*[A-Z0-9_ -]*)?\s*$",
    re.IGNORECASE,
)
_SCENE_LABEL_PREFIX = re.compile(
    r"^\s*(?:画面|镜头|分镜|场景)\s*[一二三四五六七八九十百千万\d]*\s*[：:]\s*(.*)$"
)
_SCENE_LABEL_ONLY = re.compile(
    r"^\s*(?:画面|镜头|分镜|场景)\s*[一二三四五六七八九十百千万\d]+\s*$"
)
_BRACKET_SCENE_LABEL_PREFIX = re.compile(
    r"^\s*【\s*(?:画面|镜头|分镜|场景)[^】]{0,20}】\s*(.*)$"
)


def strip_and_aggregate_prose_fragments(
    raw: str,
    *,
    short_line_max_chars: int = 52,
) -> str:
    """先去推理块，再在段落内连片短行换行（见 prose_fragment_aggregator）。"""
    from application.ai.prose_fragment_aggregator import aggregate_inline_prose_fragments

    return aggregate_inline_prose_fragments(
        strip_prose_control_artifacts(strip_reasoning_artifacts(raw)),
        short_line_max_chars=short_line_max_chars,
    )


def strip_reasoning_artifacts(raw: str) -> str:
    """移除常见推理块标签与围栏，保留其余文本顺序不变。

    覆盖：DeepSeek/Qwen 系 redacted_reasoning、think/thinking 标签及方括号变体，
    以及 XML 注释、Markdown 格式等变体。
    """
    if not raw:
        return ""

    s = raw
    s = _REDACTED_BLOCK.sub("", s)
    s = _THINK_BLOCK.sub("", s)
    s = _THINKING_BLOCK.sub("", s)
    s = _BRACKET_THINKING.sub("", s)
    s = _REASON_COMMENT.sub("", s)
    s = _MD_THINKING.sub("", s)
    s = _PREFACE_ANALYSIS.sub("", s)
    return s


def strip_prose_control_artifacts(raw: str) -> str:
    """移除正文里泄漏的状态面板、分镜标签和镜头编号。

    保留合法叙述，比如「画面一片白」；只处理明确的标签形态：
    「画面一：」「镜头1：」「SYNAPSE-00 / STATUS: ...」。
    """
    if not raw:
        return ""

    cleaned_lines: list[str] = []
    for line in raw.splitlines():
        current = line
        stripped = current.strip()
        if not stripped:
            cleaned_lines.append(current)
            continue

        if (
            _ALL_CAPS_STATUS_PANEL.match(stripped)
            or _ALL_CAPS_STATUS_FIELD.match(stripped)
            or _ALL_CAPS_STATUS_FRAGMENT.match(stripped)
            or _SCENE_LABEL_ONLY.match(stripped)
        ):
            continue

        scene_match = _SCENE_LABEL_PREFIX.match(current)
        if scene_match:
            current = scene_match.group(1).lstrip()
            if not current.strip():
                continue

        bracket_match = _BRACKET_SCENE_LABEL_PREFIX.match(current)
        if bracket_match:
            current = bracket_match.group(1).lstrip()
            if not current.strip():
                continue

        if (
            _ALL_CAPS_STATUS_PANEL.match(current.strip())
            or _ALL_CAPS_STATUS_FIELD.match(current.strip())
            or _ALL_CAPS_STATUS_FRAGMENT.match(current.strip())
        ):
            continue
        cleaned_lines.append(current)

    return "\n".join(cleaned_lines).strip()


def normalize_prose_punctuation(raw: str) -> str:
    """收敛正文里高频 AI 腔标点。

    目前只处理中文破折号：把解释性/拖尾式停顿改成逗号或句号，
    避免正文反复出现「——」造成模板感。
    """
    if not raw:
        return ""
    s = raw.replace("——", "，")
    s = re.sub(r"，\s*([。！？!?])", r"\1", s)
    s = re.sub(r"，{2,}", "，", s)
    return s
