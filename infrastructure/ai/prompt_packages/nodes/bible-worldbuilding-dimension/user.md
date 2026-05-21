故事创意：{premise}

目标章节数：{target_chapters}章

链路上下文：
{chain_context}

请生成世界观的「{dim_label}」维度。{context_block}

要求：该维度必须从故事创意和链路上下文推导，不能改写已指定主角、核心矛盾、题材赛道和既有字段。输出前自检该维度与其他维度是否一致。

请严格按照以下JSON格式输出，字段名不要修改，可以被Python json.loads函数解析。只给出JSON，不作解释，不作答：
```json
{{
{fields_desc}
}}
```
