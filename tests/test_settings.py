from pathlib import Path

import pytest
from pydantic import ValidationError

from api_test_agent.settings import Settings

COMMIT = "55cf875b9d0350e80cfed14b971b7bec8e22b50b"


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_load_resolves_runtime_dir_next_to_the_config_file(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        f"""
[target]
source_repo = 'C:\\somewhere\\repo'
commit = "{COMMIT}"
""",
    )
    settings = Settings.load(path)
    assert settings.runtime_root == (tmp_path / "runtime").resolve()
    assert settings.target.port == 18080
    assert settings.target.python == "3.12"


@pytest.mark.parametrize(
    "extra",
    [
        'commit = "55cf875"',  # abbreviated hashes are ambiguous; pin the full 40 characters
        "port = 80",  # privileged port
        'python = "latest"',
        "unknown_key = 1",
        'ready_path = "health"',
    ],
)
def test_invalid_target_settings_are_rejected(tmp_path: Path, extra: str) -> None:
    base = {"source_repo": "'x'", "commit": f'"{COMMIT}"'}
    key = extra.split("=")[0].strip()
    base.pop(key, None)
    lines = "\n".join(f"{k} = {v}" for k, v in base.items())
    path = write_config(tmp_path, f"[target]\n{lines}\n{extra}\n")
    with pytest.raises(ValidationError):
        Settings.load(path)


def test_root_cannot_be_configured(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        f'root = "elsewhere"\n[target]\nsource_repo = "x"\ncommit = "{COMMIT}"\n',
    )
    with pytest.raises(ValueError, match="root"):
        Settings.load(path)


def test_target_host_is_not_configurable(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        f'[target]\nsource_repo = "x"\ncommit = "{COMMIT}"\nhost = "0.0.0.0"\n',
    )
    with pytest.raises(ValidationError):
        Settings.load(path)
