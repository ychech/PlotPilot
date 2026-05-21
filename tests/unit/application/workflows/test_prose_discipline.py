"""prose_discipline 模块单测。"""
from application.workflows.prose_discipline import build_prose_discipline_block


def test_build_prose_discipline_block_core_rules():
    text = build_prose_discipline_block()
    assert "纠正式对照" in text
    assert "全章禁止" in text
    assert "破折号" in text
    assert "最多 1 句" in text
    assert "除了/除此之外/此外" in text
    assert "合计最多 1 次" in text
    assert "300～500 字" in text
    assert "情节密度" in text


def test_build_prose_discipline_block_tight_beat():
    text = build_prose_discipline_block(beat_mode=True, beat_target_words=800)
    assert "节拍" in text
