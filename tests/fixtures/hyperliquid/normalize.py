from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ALLOWED_ADDRESSES = {
    "0x0000000000000000000000000000000000000042",
    "0x0000000000000000000000000000000000000043",
    None,
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for path in sorted(ROOT.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if "pair" in payload and payload["pair"].get("account_address") not in ALLOWED_ADDRESSES:
            raise SystemExit(f"non-fixture account address in {path.name}")
        normalized = json.dumps(payload, ensure_ascii=False, indent=4) + "\n"
        if args.check and path.read_text(encoding="utf-8") != normalized:
            raise SystemExit(f"fixture is not normalized: {path.name}")
        if args.write:
            path.write_text(normalized, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
