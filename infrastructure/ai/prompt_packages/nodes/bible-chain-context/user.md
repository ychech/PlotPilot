【链式生成原则】
1. 所有新增设定必须能回扣到故事创意原文或上游产物，不要凭空改主角、赛道、核心矛盾。
2. 如果故事创意明确指定主角、人称、世界规则或文风，后续节点必须继承；未指定时才可补全。
3. 每一步先做结构化取舍，再输出最终内容；不要在最终答案中暴露推理过程或解释文字。
4. 输出前自检：名称一致、阵营关系一致、力量体系一致、叙事视角一致、章节规模匹配。
当前节点：{stage}；目标章节数：{target_chapters}章。

{% if premise %}
【故事创意原文】
{premise}
{% endif %}
{% if profile_lock %}
【故事内核锁】
{profile_lock}
{% endif %}
{% if worldbuilding_summary and worldbuilding_summary != "无" %}
【上游世界观摘要】
{worldbuilding_summary}
{% endif %}
{% if style_guide %}
【上游文风公约】
{style_guide}
{% endif %}
{% if characters_summary and characters_summary != "无" %}
【上游人物摘要】
{characters_summary}
{% endif %}
