"""transform → import → verify → report, in one command (`make import-base44`).

    uv run python -m migrate.pipeline EXPORT_DIR --database-url URL [--dry-run] [--files]
        [--report PATH] [--qa-constants PATH] [--fallback-region CH] [--samples 20]

Exit code 0 only when verify is green (a dry run skips verify: nothing was kept).
"""

import argparse
import asyncio
import sys
from pathlib import Path

from migrate.importer import import_bundle, print_stats
from migrate.report import render, write_report
from migrate.transform import qa_emails_from_constants, transform
from migrate.verify import print_result, verify

DEFAULT_CONSTANTS = (
    Path(__file__).resolve().parents[2] / "ods-delivery" / "tests" / "helpers" / "constants.ts"
)


async def run(args: argparse.Namespace) -> int:
    regions = tuple(args.fallback_region or ["CH"])
    bundle = transform(
        args.export_dir, fallback_regions=regions, qa_emails=qa_emails_from_constants(args.qa_constants)
    )
    stats = await import_bundle(bundle, args.database_url, dry_run=args.dry_run, files=args.files)
    print_stats(stats)
    result = None
    if not args.dry_run:
        # a fresh transform: the import re-pointed ids of adopted accounts inside `bundle`
        result = await verify(
            args.export_dir,
            args.database_url,
            samples=args.samples,
            seed=args.seed,
            files=args.files,
            fallback_regions=regions,
        )
        print_result(result)
    path = write_report(
        args.report or args.export_dir / "_report.md",
        render(bundle, args.export_dir, args.database_url, stats, result),
    )
    print(f"report: {path}")
    return 0 if (result is None or result.ok) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("export_dir", type=Path)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--files", action="store_true", help="upload the exported files to the bucket")
    parser.add_argument("--report", type=Path, default=None, help="default: EXPORT_DIR/_report.md")
    parser.add_argument("--qa-constants", type=Path, default=DEFAULT_CONSTANTS)
    parser.add_argument("--fallback-region", action="append", default=None)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--seed", type=int, default=1)
    return asyncio.run(run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
