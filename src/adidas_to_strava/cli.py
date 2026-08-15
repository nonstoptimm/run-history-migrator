from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

from .converter import convert, format_summary


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m adidas_to_strava",
        description="Convert adidas Running/Runtastic exports to local TCX files.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    convert_parser = subparsers.add_parser("convert", help="convert eligible activities")
    convert_parser.add_argument("export_path", type=Path)
    convert_parser.add_argument("--since", type=_date, default=date(2023, 1, 1))
    convert_parser.add_argument("--sport", choices=("running",), default="running")
    convert_parser.add_argument("--output", type=Path, default=Path("./output"))
    convert_parser.add_argument("--dry-run", action="store_true")
    convert_parser.add_argument("--limit", type=int)
    convert_parser.add_argument("--session-id")
    convert_parser.add_argument("--overwrite", action="store_true")
    convert_parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )
    try:
        summary = convert(
            export_path=args.export_path.expanduser(),
            output_dir=args.output.expanduser(),
            since=args.since,
            sport=args.sport,
            dry_run=args.dry_run,
            limit=args.limit,
            session_id=args.session_id,
            overwrite=args.overwrite,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(format_summary(summary))
    return 1 if summary.errors else 0
