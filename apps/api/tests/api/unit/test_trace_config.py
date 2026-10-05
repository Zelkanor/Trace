import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings, resolve_data_path
from app.domain.schemas import RunMode

BASE = {"api_port": 3001, "web_origin": "http://localhost:3000"}
UA = "TRACE research contact@example.test"


def make_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **{**BASE, **overrides})  # type: ignore[arg-type]


def test_defaults_are_replay_with_connectors_disabled() -> None:
    settings = make_settings()
    assert settings.trace_mode is RunMode.REPLAY
    assert settings.trace_device == "cpu"
    assert settings.trace_db_path == Path("data/trace.sqlite3")
    assert settings.gdelt_enabled is False
    assert settings.sec_enabled is False
    assert settings.sec_user_agent is None


def test_production_replay_is_valid() -> None:
    settings = make_settings(app_env="production", trace_mode="REPLAY")
    assert settings.is_production
    assert settings.trace_mode is RunMode.REPLAY


def test_critical_log_level_is_valid() -> None:
    assert make_settings(log_level="CRITICAL").log_level == "CRITICAL"


def test_settings_normalize_origin_and_hosts() -> None:
    settings = Settings(
        _env_file=None, api_port=1, web_origin="http://localhost:3000/", allowed_hosts="a, b"
    )
    assert settings.web_origin == "http://localhost:3000"
    assert settings.allowed_hosts == ["a", "b"]


@pytest.mark.parametrize("origin", ["*", "localhost:3000", "http://x/path"])
def test_settings_reject_bad_origin(origin: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, api_port=1, web_origin=origin)


@pytest.mark.parametrize(
    "origin",
    [
        "http://:3000",
        "https://example.test:not-a-port",
        "https://example.test:99999",
        "https://example.test:-1",
        "https://user@example.test",
        "https://user:password@example.test",
        "https://exam ple.test",
        "https://exam\tple.test",
        "https://exam\nple.test",
        "https://exam\rple.test",
    ],
)
def test_settings_reject_malformed_origin(origin: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(web_origin=origin)


def test_replay_rejects_live_connectors() -> None:
    with pytest.raises(ValidationError, match="REPLAY"):
        make_settings(trace_mode="REPLAY", gdelt_enabled=True)
    with pytest.raises(ValidationError, match="REPLAY"):
        make_settings(trace_mode="REPLAY", sec_enabled=True, sec_user_agent=UA)


def test_live_requires_a_connector() -> None:
    with pytest.raises(ValidationError, match="LIVE"):
        make_settings(trace_mode="LIVE")
    assert make_settings(trace_mode="LIVE", gdelt_enabled=True).gdelt_enabled


def test_hybrid_is_valid() -> None:
    assert make_settings(trace_mode="HYBRID").trace_mode is RunMode.HYBRID
    assert make_settings(trace_mode="HYBRID", gdelt_enabled=True).gdelt_enabled


def test_sec_requires_user_agent() -> None:
    for blank in (None, "", "   "):
        with pytest.raises(ValidationError, match="SEC_USER_AGENT"):
            make_settings(trace_mode="LIVE", sec_enabled=True, sec_user_agent=blank)
    with pytest.raises(ValidationError, match="SEC_USER_AGENT"):
        make_settings(trace_mode="HYBRID", sec_enabled=True)
    ok = make_settings(trace_mode="LIVE", sec_enabled=True, sec_user_agent=f"  {UA} ")
    assert ok.sec_user_agent == UA


def test_blank_user_agent_means_unset_when_sec_disabled() -> None:
    assert make_settings(sec_user_agent="").sec_user_agent is None


def test_user_agent_rejects_control_characters() -> None:
    with pytest.raises(ValidationError):
        make_settings(trace_mode="HYBRID", sec_enabled=True, sec_user_agent="a\r\nX-Evil: 1")


@pytest.mark.parametrize(
    "overrides",
    [
        {"trace_mode": "replay"},
        {"trace_mode": "OFFLINE"},
        {"trace_device": "gpu"},
        {"trace_device": "CUDA"},
        {"trace_db_path": ""},
        {"trace_db_path": "  "},
    ],
)
def test_rejects_invalid_values(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make_settings(**overrides)


def test_cuda_is_validated_without_importing_torch() -> None:
    import sys

    assert make_settings(trace_device="cuda").trace_device == "cuda"
    assert "torch" not in sys.modules


def test_settings_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("TRACE_MODE", "HYBRID")
    monkeypatch.setenv("TRACE_DEVICE", "cuda")
    monkeypatch.setenv("TRACE_DB_PATH", "var/x.sqlite3")
    monkeypatch.setenv("SEC_ENABLED", "true")
    monkeypatch.setenv("SEC_USER_AGENT", UA)
    settings = Settings(_env_file=None, **BASE)  # type: ignore[arg-type]
    assert settings.app_env == "production"
    assert settings.trace_mode is RunMode.HYBRID
    assert settings.trace_device == "cuda"
    assert settings.trace_db_path == Path("var/x.sqlite3")
    assert settings.sec_enabled and settings.sec_user_agent == UA


def test_env_example_values_are_valid() -> None:
    example = Path(__file__).resolve().parents[3] / ".env.example"  # apps/api/tests/api/unit -> apps/api
    settings = Settings(_env_file=example)
    assert settings.trace_mode is RunMode.REPLAY
    assert not settings.gdelt_enabled and not settings.sec_enabled
    assert settings.sec_user_agent is None


def test_get_settings_cache_respects_cleared_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_PORT", "3001")
    monkeypatch.setenv("WEB_ORIGIN", "http://localhost:3000")
    monkeypatch.setenv("TRACE_MODE", "HYBRID")
    assert get_settings().trace_mode is RunMode.HYBRID

    monkeypatch.setenv("TRACE_MODE", "REPLAY")
    assert get_settings().trace_mode is RunMode.HYBRID  # cached
    get_settings.cache_clear()
    assert get_settings().trace_mode is RunMode.REPLAY


# --- path resolution -------------------------------------------------------


def test_path_resolution_ignores_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    other = tmp_path / "elsewhere"
    other.mkdir()

    monkeypatch.chdir(tmp_path)
    first = resolve_data_path("data/trace.sqlite3", root)
    monkeypatch.chdir(other)
    second = resolve_data_path("data/trace.sqlite3", root)

    assert first == second == root / "data" / "trace.sqlite3"
    assert first.is_absolute()


def test_path_resolution_has_no_filesystem_side_effects(tmp_path: Path) -> None:
    root = tmp_path / "does-not-exist"
    settings = make_settings()
    resolved = settings.resolved_db_path(root)
    assert resolved == root / "data" / "trace.sqlite3"
    assert not root.exists()
    assert os.listdir(tmp_path) == []


def test_absolute_path_is_kept_and_normalized(tmp_path: Path) -> None:
    absolute = tmp_path / "a" / ".." / "db.sqlite3"
    assert resolve_data_path(absolute, "/app") == tmp_path / "db.sqlite3"
    assert resolve_data_path("data/../x.db", "/app") == Path("/app/x.db")


def test_path_resolution_rejects_relative_root_and_blank_path(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        resolve_data_path("data/x", "relative/root")
    with pytest.raises(ValueError, match="blank"):
        resolve_data_path(" ", tmp_path)


def test_importing_and_validating_creates_no_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    make_settings(trace_db_path="data/trace.sqlite3")
    assert list(tmp_path.iterdir()) == []
