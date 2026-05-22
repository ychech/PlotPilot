from domain.ai.services.llm_service import GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from infrastructure.ai.provider_factory import _write_full_prompt_log


class _Provider:
    pass


def test_write_full_prompt_log_records_final_system_and_user(tmp_path, monkeypatch):
    log_path = tmp_path / "prompts.full.log"
    monkeypatch.setenv("FULL_PROMPT_LOG_FILE", str(log_path))

    _write_full_prompt_log(
        "generate",
        _Provider(),
        Prompt(
            system="系统提示词",
            user="用户提示词",
            node_key="chapter-generation-main",
            source="unit-test",
        ),
        GenerationConfig(model="test-model", max_tokens=1234, temperature=0.7),
    )

    content = log_path.read_text(encoding="utf-8")
    assert "mode=generate" in content
    assert "provider=_Provider" in content
    assert "model=test-model" in content
    assert "max_tokens=1234" in content
    assert "temperature=0.7" in content
    assert "node_key=chapter-generation-main source=unit-test" in content
    assert "----- SYSTEM -----\n系统提示词" in content
    assert "----- USER -----\n用户提示词" in content


def test_write_full_prompt_log_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("FULL_PROMPT_LOG_FILE", "")

    _write_full_prompt_log(
        "stream",
        _Provider(),
        Prompt(system="系统提示词", user="用户提示词"),
        GenerationConfig(),
    )

    assert not list(tmp_path.iterdir())
