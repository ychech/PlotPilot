import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from interfaces.main import app
from interfaces.api.dependencies import (
    get_auto_bible_generator,
    get_auto_knowledge_generator,
    get_novel_service,
)


class _StubNovel:
    title = "测试小说"
    premise = "测试创意"
    target_chapters = 10


class _StubNovelService:
    def get_novel(self, novel_id: str):
        return _StubNovel()


class _StubBibleService:
    def get_bible_by_novel(self, novel_id: str):
        return {"id": "b1"}

    def create_bible(self, bible_id: str, novel_id: str):
        return {"id": bible_id}

    def add_style_note(self, **kwargs):
        return None


class _StubGenerator:
    def __init__(self):
        self.bible_service = _StubBibleService()
        self.saved = []

    async def _generate_style(self, premise: str, target_chapters: int) -> str:
        return "冷峻克制"

    def get_worldbuilding_field_plan(self):
        return [
            {
                "dimension": "core_rules",
                "dimension_label": "核心法则",
                "field": "power_system",
                "field_label": "力量体系",
                "field_desc": "力量体系/科技树的描述",
            }
        ]

    async def _stream_worldbuilding_fields(self, premise: str, target_chapters: int):
        yield {
            "type": "field_chunk",
            "dimension": "core_rules",
            "dimension_label": "核心法则",
            "field": "power_system",
            "field_label": "力量体系",
            "chunk": "灵气",
        }
        yield {
            "type": "field_done",
            "dimension": "core_rules",
            "dimension_label": "核心法则",
            "field": "power_system",
            "field_label": "力量体系",
            "value": "灵气修行体系",
        }

    async def _save_worldbuilding(self, novel_id: str, data):
        self.saved.append((novel_id, data))

    def _load_worldbuilding(self, novel_id: str):
        return {}

    def _load_characters(self, novel_id: str):
        return []


class _StubKnowledgeGenerator:
    async def generate_and_save(self, novel_id: str, title: str, bible_summary: str):
        return {}


@pytest.fixture
def client():
    stub_gen = _StubGenerator()
    app.dependency_overrides[get_novel_service] = lambda: _StubNovelService()
    app.dependency_overrides[get_auto_bible_generator] = lambda: stub_gen
    app.dependency_overrides[get_auto_knowledge_generator] = lambda: _StubKnowledgeGenerator()
    try:
        yield TestClient(app), stub_gen
    finally:
        app.dependency_overrides.clear()


def test_generate_stream_emits_field_level_worldbuilding_events(client):
    test_client, stub_gen = client

    with patch("interfaces.api.v1.world.bible.get_novel_service", return_value=_StubNovelService()):
        with test_client.stream(
            "POST",
            "/api/v1/bible/novels/test-novel/generate-stream?stage=worldbuilding",
            json={},
        ) as response:
            assert response.status_code == 200
            body = "".join(response.iter_text())

    assert '"type": "style"' in body
    assert '"type": "worldbuilding_field_chunk"' in body
    assert '"type": "worldbuilding_field_done"' in body
    assert '"type": "worldbuilding_field"' in body
    assert '"field": "power_system"' in body
    assert stub_gen.saved == [
        ("test-novel", {"core_rules": {"power_system": "灵气修行体系"}})
    ]
