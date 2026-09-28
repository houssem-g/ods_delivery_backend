"""`.env.example` is copied as-is to `.env`: a comment on the same line as an empty value
becomes the value (e.g. a guessable CRON_SECRET), so comments go on their own line."""

from pathlib import Path

from dotenv import dotenv_values


def test_env_example_values_never_carry_a_comment() -> None:
    values = dotenv_values(Path(__file__).resolve().parents[1] / ".env.example")
    assert values, ".env.example is empty"
    bad = {k: v for k, v in values.items() if v and v.lstrip().startswith("#")}
    assert bad == {}


ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"
# Read by scripts, not by app.config.Settings.
SCRIPT_ONLY = {"POSTGRES_PASSWORD", "LOCAL_ADMIN_EMAIL", "LOCAL_ADMIN_PASSWORD", "QA_CONSTANTS_PATH"}


def test_env_example_has_no_comment_after_a_value() -> None:
    """Tools other than python-dotenv (docker --env-file, kubectl create configmap --from-env-file)
    keep `# …` in the value: comments stay on their own line, for every variable."""
    for number, line in enumerate(ENV_EXAMPLE.read_text().splitlines(), 1):
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            assert " #" not in value and "\t#" not in value, f"line {number}: {key}"


def test_env_example_lists_every_setting() -> None:
    from app.config import Settings

    documented = set(dotenv_values(ENV_EXAMPLE))
    assert set(Settings.model_fields) - documented == set()
    assert documented - set(Settings.model_fields) - SCRIPT_ONLY == set()
