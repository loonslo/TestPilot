from pathlib import Path

from api_test_agent.sandbox import SandboxSecrets, build_env

SECRETS = SandboxSecrets(
    session_secret="s" * 64,
    password_pepper="p" * 64,
    email_code_pepper="e" * 64,
    accounts={"A": ("agent_a", "Aa1-secret-a"), "B": ("agent_b", "Aa1-secret-b")},
)

HOSTILE_ENV = {
    "SYSTEMROOT": r"C:\Windows",
    "TEMP": r"C:\Temp",
    "PATH": r"C:\Users\me\bin;C:\Windows",
    "QWEN_API_KEY": "sk-real",
    "OPENAI_API_KEY": "sk-real",
    "DATABASE_PATH": r"C:\real\finunity.db",
    "HTTPS_PROXY": "http://127.0.0.1:7890",
    "OCR_ENABLED": "true",
    "APP_ENV": "production",
    "SMTP_PASSWORD": "real",
}


def test_only_allow_listed_names_are_inherited() -> None:
    env = build_env(SECRETS, Path(r"D:\sandbox\finunity.db"), HOSTILE_ENV)
    for leaked in ("QWEN_API_KEY", "OPENAI_API_KEY", "HTTPS_PROXY", "SMTP_PASSWORD"):
        assert leaked not in env
    assert env["SYSTEMROOT"] == r"C:\Windows"
    assert env["TEMP"] == r"C:\Temp"


def test_path_is_rebuilt_not_copied() -> None:
    env = build_env(SECRETS, Path(r"D:\sandbox\finunity.db"), HOSTILE_ENV)
    assert "me" not in env["PATH"]
    assert env["PATH"].startswith(r"C:\Windows\System32")


def test_the_machine_environment_cannot_override_sandbox_settings() -> None:
    env = build_env(SECRETS, Path(r"D:\sandbox\finunity.db"), HOSTILE_ENV)
    assert env["DATABASE_PATH"] == r"D:\sandbox\finunity.db"
    assert env["OCR_ENABLED"] == "false"
    assert env["APP_ENV"] == "development"
    assert env["EMAIL_AUTH_ENABLED"] == "false"
    assert env["SESSION_SECRET"] == "s" * 64


def test_python_does_not_write_bytecode_into_the_exported_copy() -> None:
    env = build_env(SECRETS, Path(r"D:\sandbox\finunity.db"), HOSTILE_ENV)
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_secrets_never_appear_in_repr() -> None:
    assert "secret" not in repr(SECRETS)
    assert "s" * 20 not in repr(SECRETS)
    assert repr(SECRETS) == "SandboxSecrets(<redacted>)"
