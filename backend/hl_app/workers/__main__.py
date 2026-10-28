from __future__ import annotations

import argparse
import json

from . import dry_run, run_once, run_scheduler
from ..config import database_url


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m hl_app.workers")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--once", action="store_true")
    group.add_argument("--run", action="store_true")
    parser.add_argument("--kind", choices=["quotes", "announcements"])
    args = parser.parse_args()
    url = database_url("runtime")
    if args.dry_run:
        result = dry_run(url)
    elif args.once:
        if not args.kind:
            parser.error("--once 需要 --kind")
        result = run_once(url, args.kind)
    else:
        run_scheduler(url)
        return 0
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
