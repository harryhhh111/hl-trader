from __future__ import annotations

import argparse
import json

from .config import database_url
from .services import news_sync, news_sync_store
from .workers import WorkerLifecycle


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hl_app.news_sync")
    parser.add_argument("command", choices=["bootstrap", "status", "activate"])
    args = parser.parse_args(argv)
    if args.command == "status":
        result = news_sync_store.status(database_url("runtime"))
    elif args.command == "bootstrap":
        url = database_url("runtime")
        lifecycle = WorkerLifecycle(url)
        lifecycle.acquire()
        try:
            result = news_sync.run(url, bootstrap=True)
        finally:
            lifecycle.close()
    else:
        url = database_url("migration")
        lifecycle = WorkerLifecycle(url)
        lifecycle.acquire()
        try:
            result = news_sync_store.activate(url)
        finally:
            lifecycle.close()
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
