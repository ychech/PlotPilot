故事创意：{premise}

目标章节数：{target_chapters}章

{% if chain_context %}
精简链路上下文：
{chain_context}
{% endif %}

请生成世界观「{dim_label}」中的「{field_label_cn}」字段。{field_desc}{context_block}{sibling_block}

篇幅规则：{field_length_rule}

要求：该字段必须承接故事创意原文和已生成字段，不能新增会推翻上游设定的规则；写完前自检它是否能支撑角色动机、冲突来源和章节推进。

输出严格 JSON，格式如下：
{
  "summary": "解释性正文，完整闭合。",
  "quick_ref": {
    "label": "{field_label_cn}",
    "keywords": ["关键词1", "关键词2", "关键词3"],
    "ladder": ["层级/路线/结构1", "层级/路线/结构2"],
    "rules": ["规则1", "规则2"],
    "costs": ["代价或限制1"]
  }
}
