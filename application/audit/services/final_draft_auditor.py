"""终稿正文轻量审计工具。

这些函数被正文生成门禁和导出服务共用。保持无外部依赖，避免后端启动时因审计模块缺失而失败。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, List, Sequence


@dataclass(frozen=True)
class FinalDraftIssue:
    """终稿审计问题。"""

    message: str
    severity: str = "warning"
    chapter_number: int | None = None


def iter_body_paragraphs(text: str) -> List[str]:
    """返回正文段落，过滤空行和常见章节标题。"""
    paragraphs: List[str] = []
    for raw in re.split(r"\n\s*\n+|\r?\n", text or ""):
        line = raw.strip()
        if not line:
            continue
        if re.fullmatch(r"第[一二三四五六七八九十百千万\d]+章.*", line):
            continue
        paragraphs.append(line)
    return paragraphs


def remove_near_duplicate_paragraphs(text: str) -> str:
    """移除相邻的完全重复段落，避免模型重复输出同一段。"""
    paragraphs = iter_body_paragraphs(text or "")
    if not paragraphs:
        return (text or "").strip()

    cleaned: List[str] = []
    previous = ""
    for paragraph in paragraphs:
        normalized = re.sub(r"\s+", "", paragraph)
        if normalized and normalized == previous:
            continue
        cleaned.append(paragraph)
        previous = normalized
    return "\n\n".join(cleaned)


def extract_dialogue_utterances(text: str) -> List[str]:
    """提取中文引号内的台词内容。"""
    utterances: List[str] = []
    for pattern in (r"“([^”]{1,260})”", r"「([^」]{1,260})」", r"『([^』]{1,260})』"):
        utterances.extend(m.group(1).strip() for m in re.finditer(pattern, text or ""))
    return [u for u in utterances if u]


def audit_dialogue_depth(text: str) -> List[FinalDraftIssue]:
    """检查是否出现大量过短对白刷屏。短促对白可用，但不能替代交锋和信息。"""
    utterances = extract_dialogue_utterances(text)
    if len(utterances) < 10:
        return []

    def meaningful_len(s: str) -> int:
        return len(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]", s))

    lengths = [meaningful_len(u) for u in utterances]
    short_count = sum(1 for n in lengths if n <= 8)
    very_short_count = sum(1 for n in lengths if n <= 4)
    avg_len = sum(lengths) / max(1, len(lengths))
    short_ratio = short_count / max(1, len(lengths))

    issues: List[FinalDraftIssue] = []
    if short_count >= 7 and short_ratio >= 0.55 and avg_len < 14:
        issues.append(
            FinalDraftIssue(
                "对白过短且密集：大量台词少于 8 字，容易变成问一句答一句。关键交锋应让每轮台词承载态度、试探、信息或威胁，并穿插动作/沉默/环境反应。",
                severity="warning",
            )
        )
    if very_short_count >= 5 and very_short_count / max(1, len(lengths)) >= 0.35:
        issues.append(
            FinalDraftIssue(
                "极短对白偏多：多处 4 字以内台词会削弱人物声线，建议合并或扩成带潜台词的完整回应。",
                severity="warning",
            )
        )
    return issues


def audit_opening_chapter_quality(text: str, *, chapter_number: int | None = None) -> List[FinalDraftIssue]:
    """前三章质量画像：目标、阻碍、代价、类型承诺与主角主动性。"""
    if chapter_number is None or chapter_number > 3:
        return []
    body = (text or "").strip()
    if not body:
        return []

    issues: List[FinalDraftIssue] = []
    head = body[:1400]
    full = body
    tail = body[-900:]

    goal_terms = r"(任务|目标|倒计时|必须|前往|进入|找到|掠夺|选择|决定|签署|授权|追踪|锁定)"
    obstacle_terms = r"(失败|崩解|死亡|监控|封印|追踪|抑制|威胁|阻止|危险|裂隙|禁区|敌|赵镜|血源会|收割者)"
    stake_terms = r"(失败|死亡|崩解|代价|抽离|终止|抑制|失控|倒计时|供养|回收|淘汰|不可逆)"
    promise_terms = r"(系统|基因|武道|收割者|源髓|解锁|裂隙|方舟|高武|掠夺)"
    payoff_terms = r"(完成|确认|发现|拿到|融合|解锁|授权|决定|选择|成功|失败|暴露|升级|坠落|出发|锁定|反制|追猎|深入)"

    if not re.search(goal_terms, head):
        issues.append(FinalDraftIssue("前三章开篇缺少清晰目标/任务信号，读者不够快知道主角要解决什么", severity="warning"))
    if not re.search(obstacle_terms, head):
        issues.append(FinalDraftIssue("前三章开篇阻力不够明确，建议更早呈现敌人、规则压迫或环境危险", severity="warning"))
    if not re.search(stake_terms, full[:2200]):
        issues.append(FinalDraftIssue("前三章代价不够早落地，建议在前 2000 字内明确失败后果", severity="warning"))
    if not re.search(promise_terms, head):
        issues.append(FinalDraftIssue("前三章类型承诺不够早，建议开篇即显露本书核心卖点/能力规则/世界异常", severity="warning"))
    if not re.search(payoff_terms, tail):
        issues.append(FinalDraftIssue("章节末尾缺少阶段性兑现，建议章末给出结果后再抬高下一章风险", severity="warning"))

    if chapter_number == 3:
        agency_terms = r"(决定|选择|主动|反制|锁定|追猎|出发|去找|先一步|深入|松手|坠落|握刀|收起|塞进)"
        if not re.search(agency_terms, tail):
            issues.append(FinalDraftIssue("第三章末尾主角主动性不足，建议从被动承受推进到主动反制/追猎", severity="warning"))

    return issues


def audit_final_draft_text(
    text: str,
    *,
    target_words: int | None = None,
    strict_length: bool = True,
    chapter_number: int | None = None,
) -> List[FinalDraftIssue]:
    """审计单章正文是否具备可交付的基本完整度。"""
    issues: List[FinalDraftIssue] = []
    body = (text or "").strip()
    if not body:
        return [FinalDraftIssue("正文为空", severity="error")]

    paragraphs = iter_body_paragraphs(body)
    if not paragraphs:
        issues.append(FinalDraftIssue("正文缺少有效段落", severity="error"))
    else:
        seen: set[str] = set()
        duplicate_count = 0
        for paragraph in paragraphs:
            normalized = re.sub(r"\s+", "", paragraph)
            if len(normalized) < 6:
                continue
            if normalized in seen:
                duplicate_count += 1
            else:
                seen.add(normalized)
        if duplicate_count:
            issues.append(
                FinalDraftIssue(
                    f"检测到重复段落 {duplicate_count} 处",
                    severity="warning",
                )
            )

    if strict_length and target_words:
        cjk_count = len(re.findall(r"[\u4e00-\u9fff]", body))
        min_words = max(500, int(target_words * 0.65))
        if cjk_count < min_words:
            issues.append(
                FinalDraftIssue(
                    f"正文长度不足：约 {cjk_count} 字，建议不少于 {min_words} 字",
                    severity="warning",
                )
            )

    tail = body.rstrip()
    if tail.endswith(("，", "、", "：", "；", ",", ":", ";", "——", "…")):
        issues.append(FinalDraftIssue("正文结尾疑似未完成句子", severity="error"))
    if tail.count("“") != tail.count("”"):
        issues.append(FinalDraftIssue("正文存在未闭合中文引号", severity="error"))

    issues.extend(audit_dialogue_depth(body))
    issues.extend(audit_opening_chapter_quality(body, chapter_number=chapter_number))

    return issues


def audit_export_chapters(
    chapters: Sequence[object] | Iterable[object],
    *,
    target_words: int | None = None,
    require_completed: bool = True,
) -> List[FinalDraftIssue]:
    """审计导出章节集合。"""
    issues: List[FinalDraftIssue] = []
    for ch in chapters:
        number = int(getattr(ch, "number", 0) or 0) or None
        status = getattr(ch, "status", "")
        status_value = status.value if hasattr(status, "value") else str(status or "")
        if require_completed and status_value and status_value != "completed":
            issues.append(
                FinalDraftIssue(
                    f"第 {number or '?'} 章状态不是 completed，当前为 {status_value}",
                    severity="warning",
                    chapter_number=number,
                )
            )
        for issue in audit_final_draft_text(
            getattr(ch, "content", "") or "",
            target_words=target_words,
            strict_length=False,
            chapter_number=number,
        ):
            issues.append(
                FinalDraftIssue(
                    f"第 {number or '?'} 章：{issue.message}",
                    severity=issue.severity,
                    chapter_number=number,
                )
            )
    return issues
