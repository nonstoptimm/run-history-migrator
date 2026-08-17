"""Command-line interface for inspection, conversion, OAuth, and upload."""

from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path
from typing import Protocol, cast

from .config import load_strava_settings
from .converter import convert, format_summary
from .inspect_export import format_inspection, inspect_export
from .strava_auth import authorize, credentials_location
from .strava_client import StravaAuthError, StravaClient, StravaError, StravaRateLimitError
from .uploader import format_upload_summary, upload_activities


class _BaseArguments(Protocol):
    command: str
    verbose: bool


class _SelectionArguments(Protocol):
    since: date | None
    until: date | None
    sport: str
    limit: int | None


class _InspectArguments(_SelectionArguments, Protocol):
    export_path: Path
    session_id: str | None


class _ConvertArguments(_SelectionArguments, Protocol):
    export_path: Path
    session_id: str | None
    output: Path
    dry_run: bool
    overwrite: bool


class _AuthArguments(Protocol):
    env_file: Path | None
    code: str | None
    scope: str
    port: int
    timeout: int
    no_browser: bool


class _UploadArguments(_SelectionArguments, Protocol):
    output_dir: Path
    dry_run: bool
    force: bool
    env_file: Path | None


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
    """Build the argparse command tree without parsing process arguments.

    Returns:
        The configured top-level parser for all supported commands and flags.
    """
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
    since = cast(date | None, getattr(args, "since", None))
    until = cast(date | None, getattr(args, "until", None))
    if since and until and since > until:
        parser.error("--since must be on or before --until")


def main(argv: list[str] | None = None) -> int:
    """Run one CLI command while preserving argparse's validation boundary.

    Args:
        argv: Arguments excluding the executable name, or ``None`` for ``sys.argv``.

    Returns:
        Zero on success, one for completed conversion/upload runs with item failures,
        or two when Strava rate limiting stops an upload.

    Raises:
        SystemExit: When argparse displays help or reports a CLI validation error.
    """
    parser = build_parser()
    namespace = parser.parse_args(argv)
    _validate_date_range(parser, namespace)
    args = cast(_BaseArguments, namespace)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s: %(message)s",
    )
    try:
        if args.command == "inspect":
            inspect_args = cast(_InspectArguments, namespace)
            inspection_summary = inspect_export(
                inspect_args.export_path.expanduser(),
                inspect_args.since,
                inspect_args.until,
                inspect_args.sport,
                inspect_args.session_id,
                inspect_args.limit,
            )
            print(format_inspection(inspection_summary))
            return 0
        if args.command == "convert":
            convert_args = cast(_ConvertArguments, namespace)
            conversion_summary = convert(
                export_path=convert_args.export_path.expanduser(),
                output_dir=convert_args.output.expanduser(),
                since=convert_args.since,
                until=convert_args.until,
                sport=convert_args.sport,
                dry_run=convert_args.dry_run,
                limit=convert_args.limit,
                session_id=convert_args.session_id,
                overwrite=convert_args.overwrite,
            )
            print(format_summary(conversion_summary))
            return 1 if conversion_summary.errors else 0
        if args.command == "auth":
            auth_args = cast(_AuthArguments, namespace)
            settings = load_strava_settings(auth_args.env_file)
            auth_client = StravaClient(settings)
            authorize(
                settings,
                auth_client,
                code=auth_args.code,
                granted_scope=auth_args.scope,
                port=auth_args.port,
                timeout=auth_args.timeout,
                open_browser=not auth_args.no_browser,
            )
            print(f"Strava authorization saved to {credentials_location(auth_client.settings)}")
            return 0
        if args.command == "upload":
            upload_args = cast(_UploadArguments, namespace)
            upload_client: StravaClient | None = None
            if not upload_args.dry_run:
                settings = load_strava_settings(upload_args.env_file)
                upload_client = StravaClient(settings)
            upload_summary = upload_activities(
                upload_args.output_dir.expanduser(),
                client=upload_client,
                since=upload_args.since,
                until=upload_args.until,
                sport=upload_args.sport,
                limit=upload_args.limit,
                dry_run=upload_args.dry_run,
                force=upload_args.force,
            )
            print(format_upload_summary(upload_summary))
            return 1 if upload_summary.failed else 0
    except StravaRateLimitError as exc:
        print(f"Rate limit stop: {exc}")
        return 2
    except (ValueError, StravaAuthError, StravaError) as exc:
        parser.error(str(exc))
    return 0
