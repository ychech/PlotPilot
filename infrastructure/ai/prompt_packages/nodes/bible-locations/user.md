【精简故事内核】
{premise}

【目标章节数】
{target_chapters}章

【世界观摘要】
{worldbuilding}

【已点亮锚点】
{existing_locations}

【人物活动摘要】
{characters}

【文风公约摘要】
{style_guide}

【上游一致性锁摘要】
{chain_context}

请基于以上精简信息生成完整地图（至少5-10个重要地点）。地点要符合世界观设定，优先服务主角路线、势力冲突、资源争夺和前三十章可用场景；不要生成与主角、势力关系无关的孤立景点。每个地点写清功能和冲突用途即可，不要长篇介绍风景。空间层级用parent_id表达，非父子关系用connections。输出前自检：地点层级合理、关键地点能承载冲突、名称风格一致、地点功能能服务中长期剧情。

请严格按照系统提示中的JSON格式输出，可以被Python json.loads函数解析。只给出JSON，不作解释，不作答。
