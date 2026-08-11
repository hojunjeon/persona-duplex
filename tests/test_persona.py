from pathlib import Path

from gateway.app.persona import build_system_prompt, list_personas, load_persona


PERSONA_DIR = Path(__file__).resolve().parents[1] / "gateway" / "personas"


def test_persona_load_and_prompt() -> None:
    persona = load_persona(PERSONA_DIR, "default")
    prompt = build_system_prompt(persona)
    assert persona.name == "에코"
    assert "실시간 음성 대화" in prompt
    assert "사용자인 척" in prompt


def test_persona_listing() -> None:
    ids = {item["id"] for item in list_personas(PERSONA_DIR)}
    assert {"default", "analyst"} <= ids
