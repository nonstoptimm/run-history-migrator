"""Command-line interface for inspection, conversion, OAuth, and upload."""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

from .config import load_strava_settings
from .converter import convert, format_summary
from .inspect_export import format_inspection, inspect_export
from .strava_auth import authorize, credentials_location
from .strava_client import StravaAuthError, StravaClient, StravaError, StravaRateLimitError
from .uploader import format_upload_summary, upload_activities


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return parsed


def _add_selection_arguments(parser: argparse.ArgumentParser, *, session: bool) -> None:
    parser.add_argument(
        "--since",
        type=_date,
        help="include activities on or after this local calendar date (inclusive)",
    )
    parser.add_argument(
        "--until",
        type=_date,
        help="include activities on or before this local calendar date (inclusive)",
    )
    parser.add_argument("--sport", choices=("running",), default="running")
    parser.add_argument("--limit", type=_positive_int)
    if session:
        parser.add_argument("--session-id")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m adidas_to_strava",
        description="Inspect and convert adidas Running exports, then optionally upload TCX.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="inspect an adidas export")
    inspect_parser.add_argument("export_path", type=Path)
    _add_selection_arguments(inspect_parser, session=True)
    inspect_parser.add_argument("--verbose", action="store_true")

    convert_parser = subparsers.add_parser("convert", help="convert eligible activities")
    convert_parser.add_argument("export_path", type=Path)
    _add_selection_arguments(convert_parser, session=True)
    convert_parser.add_argument("--output", type=Path, default=Path("./output"))
    convert_parser.add_argument("--dry-run", action="store_true")
    convert_parser.add_argument("--overwrite", action="store_true")
    convert_parser.add_argument("--verbose", action="store_true")

    auth_parser = subparsers.add_parser("auth", help="authorize this CLI with Strava")
    auth_parser.add_argument("--env-file", type=Path)
    auth_parser.add_argument("--code", help="exchange a returned one-use authorization code")
    auth_parser.add_argument("--scope", default="", help=argparse.SUPPRESS)
    auth_parser.add_argument("--port", type=_positive_int, default=8765)
    auth_parser.add_argument("--timeout", type=_positive_int, default=180)
    auth_parser.add_argument("--no-browser", action="store_true")
    auth_parser.add_argument("--verbose", action="store_true")

    upload_parser = subparsers.add_parser("upload", help="upload converted TCX files")
    upload_parser.add_argument("output_dir", type=Path)
    _add_selection_arguments(upload_parser, session=False)
    upload_parser.add_argument("--dry-run", action="store_true")
    upload_parser.add_argument(
        "--force",
        action="store_true",
        help="resubmit completed sessions; may create duplicates",
    )
    upload_parser.add_argument("--env-file", type=Path)
    upload_parser.add_argument("--verbose", action="store_true")
    return parser


def _validate_date_range(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if getattr(args, "since", None) and getattr(args, "until", None):
        if args.since > args.until:
            parser.error("--since must be on or before --until")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _validate_date_range(parser, args)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )
    try:
        if args.command == "inspect":
            summary = inspect_export(
                args.export_path.expanduser(),
                args.since,
                args.until,
                args.sport,
                args.session_id,
                args.limit,
            )
            print(format_inspection(summary))
            return 0
        if args.command == "convert":
            summary = convert(
                export_path=args.export_path.expanduser(),
                output_dir=args.output.expanduser(),
                since=args.since,
                until=args.until,
                sport=args.sport,
                dry_run=args.dry_run,
                limit=args.limit,
                session_id=args.session_id,
                overwrite=args.overwrite,
            )
            print(format_summary(summary))
            return 1 if summary.errors else 0
        if args.command == "auth":
            settings = load_strava_settings(args.env_file)
            client = StravaClient(settings)
            authorize(
                settings,
                client,
                code=args.code,
                granted_scope=args.scope,
                port=args.port,
                timeout=args.timeout,
                open_browser=not args.no_browser,
            )
            print(f"Strava authorization saved to {credentials_location(client.settings)}")
            return 0
        if args.command == "upload":
            client = None
            if not args.dry_run:
                settings = load_strava_settings(args.env_file)
                client = StravaClient(settings)
            summary = upload_activities(
                args.output_dir.expanduser(),
                client=client,
                since=args.since,
                until=args.until,
                sport=args.sport,
                limit=args.limit,
                dry_run=args.dry_run,
                force=args.force,
            )
            print(format_upload_summary(summary))
            return 1 if summary.failed else 0
    except StravaRateLimitError as exc:
        print(f"Rate limit stop: {exc}")
        return 2
    except (ValueError, StravaAuthError, StravaError) as exc:
        parser.error(str(exc))
    return 0
