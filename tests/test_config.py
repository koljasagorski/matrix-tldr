import pytest

from matrix_tldr.config import ConfigError, load_settings

VALID = {
    "MATRIX_HOMESERVER": "https://matrix.example.org/",
    "MATRIX_USER_ID": "@tldr:example.org",
    "MATRIX_PASSWORD": "geheim",
    "AI_PROVIDER": "anthropic",
    "AI_MODEL": "ein-modell",
    "ANTHROPIC_API_KEY": "sk-ant-test",
}


def load(extra=None, remove=(), **kwargs):
    env = {**VALID, **(extra or {})}
    for key in remove:
        env.pop(key, None)
    return load_settings(env, read_dotenv=False, **kwargs)


def test_defaults_are_applied(tmp_path):
    settings = load({"MATRIX_STORE_PATH": str(tmp_path)})
    assert settings.matrix_homeserver == "https://matrix.example.org"
    assert settings.matrix_device_name == "matrix-tldr"
    assert settings.ai_max_input_chars == 12000
    assert settings.ai_timeout_seconds == 30
    assert settings.fetch_timeout_seconds == 10
    assert settings.fetch_max_bytes == 5242880
    assert settings.max_links_per_message == 1
    assert settings.rate_limit_per_room_per_minute == 5
    assert settings.allowed_rooms == frozenset()
    assert settings.auto_join_allowed_inviters == frozenset()
    assert settings.verify_auto_confirm is False
    assert settings.log_level == "INFO"
    assert settings.ai_effort == "low"


def test_lists_are_parsed():
    settings = load(
        {
            "ALLOWED_ROOMS": "!a:x, !b:x ,",
            "AUTO_JOIN_ALLOWED_INVITERS": "@kolja:x",
            "VERIFY_AUTO_CONFIRM": "TRUE",
        }
    )
    assert settings.allowed_rooms == {"!a:x", "!b:x"}
    assert settings.auto_join_allowed_inviters == {"@kolja:x"}
    assert settings.verify_auto_confirm is True


def test_model_is_required_and_never_hardcoded():
    with pytest.raises(ConfigError, match="AI_MODEL"):
        load(remove=["AI_MODEL"])


def test_matching_api_key_is_required_for_provider():
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        load(remove=["ANTHROPIC_API_KEY"])
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        load({"AI_PROVIDER": "openai"})
    # Key des anderen Providers genügt nicht
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        load({"AI_PROVIDER": "openai", "ANTHROPIC_API_KEY": "x"})
    assert load({"AI_PROVIDER": "openai", "OPENAI_API_KEY": "k"}).ai_provider == "openai"


def test_unknown_provider_is_rejected():
    with pytest.raises(ConfigError, match="AI_PROVIDER"):
        load({"AI_PROVIDER": "gemini"})


def test_password_is_only_needed_without_stored_session(tmp_path):
    with pytest.raises(ConfigError, match="MATRIX_PASSWORD"):
        load({"MATRIX_STORE_PATH": str(tmp_path)}, remove=["MATRIX_PASSWORD"])
    (tmp_path / "session.json").write_text("{}")
    settings = load({"MATRIX_STORE_PATH": str(tmp_path)}, remove=["MATRIX_PASSWORD"])
    assert settings.matrix_password is None


def test_all_problems_are_reported_together():
    with pytest.raises(ConfigError) as info:
        load_settings({"AI_PROVIDER": "anthropic"}, read_dotenv=False)
    message = str(info.value)
    for name in ("MATRIX_HOMESERVER", "MATRIX_USER_ID", "AI_MODEL", "ANTHROPIC_API_KEY"):
        assert name in message


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MATRIX_HOMESERVER", "matrix.example.org"),
        ("MATRIX_USER_ID", "tldr:example.org"),
        ("FETCH_TIMEOUT_SECONDS", "abc"),
        ("FETCH_MAX_BYTES", "10"),
        ("MAX_LINKS_PER_MESSAGE", "0"),
        ("RATE_LIMIT_PER_ROOM_PER_MINUTE", "-1"),
        ("VERIFY_AUTO_CONFIRM", "vielleicht"),
        ("LOG_LEVEL", "LAUT"),
        ("OPENAI_BASE_URL", "ftp://x"),
        ("AI_EFFORT", "turbo"),
    ],
)
def test_invalid_values_are_rejected(name, value):
    with pytest.raises(ConfigError, match=name):
        load({name: value})


def test_effort_none_disables_it_and_blank_falls_back_to_default():
    assert load({"AI_EFFORT": "none"}).ai_effort is None
    assert load({"AI_EFFORT": " "}).ai_effort == "low"
    assert load({"AI_EFFORT": "medium"}).ai_effort == "medium"
