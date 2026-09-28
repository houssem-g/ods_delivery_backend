"""Fail (exit 1) unless the Alembic history has exactly one head. Offline: reads the scripts only."""

import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    heads = script.get_heads()
    if len(heads) != 1:
        print(f"expected exactly 1 alembic head, found {len(heads)}: {', '.join(heads) or 'none'}")
        return 1
    print(f"single alembic head: {heads[0]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
