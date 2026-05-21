"""prompt_packages / prompt_seed 加载与规范化单测。"""
from __future__ import annotations

from pathlib import Path

import pytest

from infrastructure.ai.prompt_seed.loader import PACKAGES_ROOT, load_seed_bundle, load_node_dir
from infrastructure.ai.prompt_seed.normalize import normalize_prompt_record


def test_normalize_merges_underscore_fields_into_variables():
    raw = {
        "id": "test-node",
        "variables": [{"name": "x", "type": "string"}],
        "_directives": {"OPENING": "hello"},
    }
    n = normalize_prompt_record(raw)
    names = {v.get("name") for v in n["variables"]}
    assert "_directives" in names
    assert "x" in names


def test_load_seed_bundle_non_empty():
    meta, prompts = load_seed_bundle()
    assert meta.get("version")
    assert len(prompts) >= 1
    ids = {p.get("id") for p in prompts}
    assert "chapter-generation-main" in ids


def test_load_node_dir_roundtrip(tmp_path: Path):
    nd = tmp_path / "my-node"
    nd.mkdir()
    (nd / "package.yaml").write_text(
        "id: my-node\nname: T\ncategory: generation\ntags: []\nvariables: []\n",
        encoding="utf-8",
    )
    (nd / "system.md").write_text("SYS", encoding="utf-8")
    (nd / "user.md").write_text("USR", encoding="utf-8")
    rec = load_node_dir(nd)
    assert rec["system"] == "SYS"
    assert rec["user_template"] == "USR"
    norm = normalize_prompt_record(rec)
    assert norm["id"] == "my-node"


@pytest.mark.skipif(not (PACKAGES_ROOT / "nodes").is_dir(), reason="no prompt_packages")
def test_lifecycle_extras_present():
    ex = PACKAGES_ROOT / "nodes" / "lifecycle-phase-directives" / "extras.json"
    assert ex.is_file(), "lifecycle 节点应含 extras.json（_directives）"


@pytest.mark.skipif(not (PACKAGES_ROOT / "nodes").is_dir(), reason="no prompt_packages")
def test_core_generation_prompts_keep_kernel_tags_and_strong_structure():
    nodes = PACKAGES_ROOT / "nodes"

    bible_system = (nodes / "bible-all" / "system.md").read_text(encoding="utf-8")
    macro_system = (nodes / "planning-quick-macro" / "system.md").read_text(encoding="utf-8")
    chapter_system = (nodes / "chapter-generation-main" / "system.md").read_text(encoding="utf-8")
    chapter_user = (nodes / "chapter-generation-main" / "user.md").read_text(encoding="utf-8")
    chapter_sync_system = (nodes / "chapter-narrative-sync" / "system.md").read_text(encoding="utf-8")
    beat_system = (nodes / "outline-beat-partition" / "system.md").read_text(encoding="utf-8")

    assert "内容内核标签" in bible_system
    assert "叙事功能标签" in bible_system
    assert "内容标签锁定" in macro_system
    assert "强叙事结构" in macro_system
    assert "章节结构卡" in chapter_system
    assert "最高目标：写出能直接阅读的完整小说正文" in chapter_system
    assert "一章一推进" in chapter_system
    assert "承接状态 → 激励事件 → 应对行动 → 阻力升级 → 高潮转折 → 阶段结果 → 钩子" in chapter_system
    assert "先在心里给本章打标签" in chapter_user
    assert "本章变化量" in chapter_user
    assert "章后承接锚点" in chapter_system
    assert "本章变化量" in chapter_sync_system
    assert "叙事功能标签" in beat_system
