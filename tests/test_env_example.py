"""`.env.example` is copied as-is to `.env`: a comment on the same line as an empty value
becomes the value (e.g. a guessable CRON_SECRET), so comments go on their own line."""

from pathlib import Path

from dotenv import dotenv_values


def test_env_example_values_never_carry_a_comment() -> None:
    values = dotenv_values(Path(__file__).resolve().parents[1] / ".env.example")
    assert values, ".env.example is empty"
    bad = {k: v for k, v in values.items() if v and v.lstrip().startswith("#")}
    assert bad == {}
