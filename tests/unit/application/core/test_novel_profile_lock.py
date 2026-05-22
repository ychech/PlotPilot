from application.core.novel_profile_lock import build_novel_profile_lock


def test_profile_lock_extracts_genre_world_after_internal_prefix():
    premise = """【系统内部·叙事结构规划（勿向读者展示本段标题与标签）】
规划目标体量：约 300,000 字。

【类型：玄幻升级；世界观基调：丹武修行】

少年偶获神鼎，从此丹武双修。"""

    lock = build_novel_profile_lock(
        title="神鼎武帝",
        premise=premise,
        target_chapters=120,
    )

    assert "题材/赛道：玄幻升级" in lock
    assert "世界观基调：丹武修行" in lock
    assert "少年偶获神鼎" in lock
