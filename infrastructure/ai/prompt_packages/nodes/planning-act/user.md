{context}

请为这一幕规划 {chapter_count} 个章节。每章必须包含爽点标注和伏笔操作。

【爽点类型说明：thrill_type 必选其一】
- power_reveal: 实力/能力展露（主角亮出底牌，旁观者震惊）
- identity_reveal: 身份/地位揭露（隐藏身份曝光，全场震动）
- action: 战斗/对峙高潮（激烈冲突，胜负翻转）
- suspense: 悬念爆发（重大真相揭露，认知颠覆）
- emotion: 情感爆发（极致情感冲击，催泪/燃点）
- hook: 钩子开场（以强冲突开场，立刻抓住读者）

【伏笔操作说明：foreshadow_action 必选其一】
- plant: 种下新伏笔（埋下未来线索，暗示更大秘密）
- resolve: 回收旧伏笔（揭晓之前的悬念，给读者满足感）
- plant_and_resolve: 同时种新收旧（满足读者同时吊住胃口）
- none: 无伏笔操作（仅限纯动作/过渡章节，每幕不超过2章）

【前三章铁律】
第1章：必须是 hook + power_reveal 或 identity_reveal
第2章：必须有 power_reveal 或 identity_reveal
第3章：必须有 action 或 power_reveal

【伏笔节奏铁律】
- 本幕内种下的伏笔，必须有至少1条在本幕或下一幕回收。
- 不能连续2章都是 foreshadow_action=none。
- 最后一章必须 resolve 或 plant_and_resolve。

请直接输出 JSON：
{{
  "chapters": [
    {{
      "number": 1,
      "title": "章节标题",
      "outline": "章节大纲（100-200字，必须包含叙事功能标签、承接点、核心冲突、主角行动、阻力升级、阶段结果、下一章钩子）",
      "characters": ["人物ID"],
      "locations": ["地点ID"],
      "thrill_type": "power_reveal",
      "thrill_description": "爽点描述：主角在什么场景下展露了什么实力/身份，局势如何改变，旁观者/对手如何产生具体反应",
      "foreshadow_action": "plant",
      "foreshadow_detail": "伏笔细节：种下/回收了什么伏笔"
    }}
  ]
}}
