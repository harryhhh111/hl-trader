from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..config import ConfigError, database_url
from .common import BundleError
from .service import activate, export_baseline, import_bundle, inspect_bundle, maintenance, rollback_export


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m hl_app.migration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--bundle", type=Path, required=True)
    maintenance_parser = subparsers.add_parser("maintenance")
    maintenance_parser.add_argument("--enable", action="store_true", required=True)
    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--bundle", type=Path, required=True)
    mode = import_parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--purpose", choices=["baseline"], required=True)
    export_parser.add_argument("--output", type=Path, required=True)
    activate_parser = subparsers.add_parser("activate")
    activate_parser.add_argument("--baseline", type=Path, required=True)
    rollback_parser = subparsers.add_parser("rollback-export")
    rollback_parser.add_argument("--baseline", type=Path, required=True)
    rollback_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "inspect":
        result = inspect_bundle(args.bundle)
    elif args.command == "maintenance":
        result = maintenance(database_url("migration"))
    elif args.command == "import":
        result = import_bundle(database_url("migration"), args.bundle, args.apply)
    elif args.command == "export":
        result = export_baseline(database_url("migration"), args.output)
    elif args.command == "activate":
        result = activate(database_url("migration"), args.baseline)
    else:
        result = rollback_export(database_url("migration"), args.baseline, args.output)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ConfigError, BundleError) as exc:
        raise SystemExit(str(exc)) from exc
