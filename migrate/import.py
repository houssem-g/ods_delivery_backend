"""Import a Base44 export into PostgreSQL (transform + idempotent upsert, see migrate/importer.py).

uv run python -m migrate.import EXPORT_DIR --database-url URL [--dry-run] [--files]
"""

import argparse
import asyncio
import sys
from pathlib import Path

from migrate.importer import import_bundle, print_stats
from migrate.transform import qa_emails_from_constants, transform


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("export_dir", type=Path)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--files", action="store_true", help="upload the exported files to the bucket")
    parser.add_argument("--fallback-region", action="append", default=None)
    parser.add_argument("--qa-constants", type=Path, default=None)
    args = parser.parse_args(argv)
    bundle = transform(
        args.export_dir,
        fallback_regions=tuple(args.fallback_region or ["CH"]),
        qa_emails=qa_emails_from_constants(args.qa_constants),
    )
    stats = asyncio.run(import_bundle(bundle, args.database_url, dry_run=args.dry_run, files=args.files))
    print_stats(stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
