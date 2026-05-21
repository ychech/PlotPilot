<STORY_CONTEXT>
{worldview_context}
</STORY_CONTEXT>

<TARGET_SCOPE>
目标总篇幅：精确 {target_chapters} 章
【强制约束】所有卷/幕的 estimated_chapters 之和必须等于 {target_chapters}
</TARGET_SCOPE>

请立即生成叙事骨架，严格按以下 JSON 格式输出：
{{
  "parts": [
    {{
      "title": "部标题（如：深渊觉醒）",
      "volumes": [
        {{
          "title": "卷标题（如：血色的黎明）",
          "estimated_chapters": 预估章数,
          "acts": [
            {{
              "title": "幕标题（如：青铜门下的背叛）",
              "estimated_chapters": 预估章数,
              "core_conflict": "主角 vs 反派，赌注是...",
              "emotional_turn": "从...到...",
              "description": "情节摘要；必须包含：内容标签、激励事件、升级过程、高潮转折、阶段结果、下一幕钩子",
              "key_characters": ["角色1", "角色2"],
              "key_locations": ["地点1"]
            }}
          ]
        }}
      ]
    }}
  ]
}}
