from application.audit.services.final_draft_auditor import audit_final_draft_text


def test_audit_dialogue_depth_flags_dense_short_dialogue():
    text = """
林渊停在门口。

“谁？”

“我。”

“为什么？”

“不知道。”

“进去？”

“不。”

“怕了？”

“没有。”

“那走。”

“等等。”

“说。”

“有人。”

风从走廊尽头卷过来，灯管闪了一下。
"""
    issues = audit_final_draft_text(text, strict_length=False)
    messages = [issue.message for issue in issues]
    assert any("对白过短" in message or "极短对白" in message for message in messages)


def test_audit_dialogue_depth_allows_sparse_short_lines():
    text = """
林渊把检测仪推到鼠仔面前。

“先别问我从哪弄来的。”他说，“赵镜的监控服务器不在协会主网里，这东西能帮你绕开第一层校验，但只能用三十秒。”

鼠仔盯着屏幕，手指停在接口上方。

“三十秒够了。”他说。

苏荻在门边回头：“不够。你们还得给他一个会让赵镜相信的假信号。”
"""
    issues = audit_final_draft_text(text, strict_length=False)
    messages = [issue.message for issue in issues]
    assert not any("对白过短" in message or "极短对白" in message for message in messages)


def test_audit_opening_chapter_flags_missing_early_stakes():
    text = """
林渊在窗边坐了很久。

雨落在玻璃上，远处的灯一点点亮起来。他想起很多年前的旧事，想起那些没有说出口的话。

“你还好吗？”

“还好。”

屋里安静下来。
"""
    issues = audit_final_draft_text(text, strict_length=False, chapter_number=1)
    messages = [issue.message for issue in issues]
    assert any("目标/任务" in message for message in messages)
    assert any("代价" in message for message in messages)


def test_audit_opening_chapter_accepts_goal_stakes_and_payoff():
    text = """
系统界面在林渊眼前炸开：收割任务倒计时只剩七十一小时，目标是进入基因裂隙并掠夺1398号源髓。

赵镜的监控波形压在他左臂上，抑制回路随时会触发。失败不是受伤，是基因链不可逆崩解。

林渊把定位芯片按进掌心，决定先绕开协会主网，锁定追踪盲区。

裂隙门打开时，他确认了第一条路标。目标没有远离，反而主动向他靠近。

他收起刀，选择深入。
"""
    issues = audit_final_draft_text(text, strict_length=False, chapter_number=1)
    messages = [issue.message for issue in issues]
    assert not any("目标/任务" in message for message in messages)
    assert not any("代价" in message for message in messages)
    assert not any("类型承诺" in message for message in messages)
