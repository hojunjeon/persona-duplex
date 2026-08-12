from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from gateway.app.persona import PersonaConflictError, build_system_prompt, list_personas, load_persona, save_persona


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


def test_custom_persona_is_merged_and_saved_atomically(tmp_path: Path) -> None:
    persona = save_persona(
        tmp_path,
        {
            "id": "custom-test",
            "name": "테스트",
            "identity": "검증 도우미",
            "relationship": "사용자의 동료",
            "speaking_style": ["짧게 말한다."],
            "behavior": [],
            "boundaries": [],
            "backchannels": ["응."],
            "max_sentences": 1,
        },
        builtin_dir=PERSONA_DIR,
    )
    assert persona.source == "custom"
    loaded = load_persona(PERSONA_DIR, "custom-test", tmp_path)
    assert loaded.name == "테스트"
    rows = list_personas(PERSONA_DIR, tmp_path)
    assert {row["source"] for row in rows if row["id"] == "custom-test"} == {"custom"}
    assert not list(tmp_path.glob("*.tmp"))


def test_concurrent_same_id_has_one_winner(tmp_path: Path) -> None:
    payload = {
        "id": "custom-race",
        "name": "동시성 테스트",
        "identity": "검증기",
        "relationship": "사용자의 동료",
        "speaking_style": [],
        "behavior": [],
        "boundaries": [],
        "backchannels": ["응."],
        "max_sentences": 2,
    }

    def attempt() -> str:
        try:
            save_persona(tmp_path, payload)
            return "ok"
        except PersonaConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: attempt(), range(8)))
    assert outcomes.count("ok") == 1
    assert outcomes.count("conflict") == 7
    assert (tmp_path / "custom-race.yaml").is_file()
    assert not list(tmp_path.glob("*.lock"))


def test_dead_owner_lock_is_recovered(tmp_path: Path) -> None:
    lock_path = tmp_path / ".custom-stale.yaml.lock"
    lock_path.write_text("pid=999999999\ncreated=0\n", encoding="ascii")
    payload = {
        "id": "custom-stale",
        "name": "복구 테스트",
        "identity": "검증기",
        "relationship": "사용자의 동료",
        "speaking_style": [],
        "behavior": [],
        "boundaries": [],
        "backchannels": ["응."],
        "max_sentences": 2,
    }
    saved = save_persona(tmp_path, payload)
    assert saved.persona_id == "custom-stale"
    assert (tmp_path / "custom-stale.yaml").is_file()
    assert not lock_path.exists()
