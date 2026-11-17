"""Generate the research report display snapshot.

Usage::

    python -m hl_app.research_reports snapshot \
      --source /absolute/path/to/stock-research \
      --output /absolute/path/to/research-reports

The command reads the research library index, copies the reports this
application is allowed to show into an immutable snapshot and switches the
current snapshot pointer. It never executes research scripts, contacts data
vendors or runs git pull.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .services.research_reports import (
    ResearchReportError,
    build_snapshot,
    load_snapshot,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m hl_app.research_reports")
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot = subparsers.add_parser("snapshot", help="从研究库生成不可变展示快照")
    snapshot.add_argument("--source", required=True, help="研究库根目录（绝对路径）")
    snapshot.add_argument("--output", required=True, help="快照产物根目录（绝对路径）")
    inspect = subparsers.add_parser("inspect", help="查看当前快照摘要")
    inspect.add_argument("--root", required=True, help="快照产物根目录（绝对路径）")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "snapshot":
        manifest = build_snapshot(Path(args.source), Path(args.output))
        print(
            json.dumps(
                {
                    "snapshot_id": manifest["snapshot_id"],
                    "generated_at": manifest["generated_at"],
                    "companies": manifest["totals"]["companies"],
                    "reports": manifest["totals"]["reports"],
                    "excluded": len(manifest["excluded"]),
                    "source": manifest["source"],
                },
                ensure_ascii=False,
            )
        )
        return 0
    snapshot = load_snapshot(Path(args.root))
    print(
        json.dumps(
            {
                "snapshot_id": snapshot.manifest.snapshot_id,
                "generated_at": snapshot.manifest.generated_at,
                "companies": snapshot.manifest.totals.get("companies", 0),
                "reports": snapshot.manifest.totals.get("reports", 0),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ResearchReportError as exc:
        print(f"生成失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
