"""Build reviewed adjustment modules over a pinned Web image; never restart services.

Rust/Python come from --commit. Frontend preserves the tested working tree,
including existing deployed changes; its complete file fingerprint is recorded.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile

from research_release import ResearchRelease, write_record

MODULES = ("routes/hyperliquid.py", "services/hyperliquid_adjustments.py",
           "services/hyperliquid_managed_adjustments.py")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", default="HEAD")
    parser.add_argument("--base-web-image", required=True)
    parser.add_argument("--web-image", required=True)
    parser.add_argument("--gateway-image", required=True)
    parser.add_argument("--record", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    tool = ResearchRelease(root)
    commit = tool.run(["git", "rev-parse", args.commit]).decode().strip()
    builds = root / ".runtime/builds"
    builds.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="adjustment-", dir=builds) as directory:
        context = Path(directory)
        payload = tool.run(["git", "archive", commit, "Cargo.toml", "Cargo.lock", "src",
                            *["backend/hl_app/" + path for path in MODULES]])
        with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
            for member in archive:
                if not member.isfile():
                    continue
                target = context / member.name
                if context not in target.resolve().parents:
                    raise RuntimeError("Source archive path escaped build directory")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(member).read())
        shutil.copytree(root / "frontend", context / "frontend", ignore=shutil.ignore_patterns(
            "node_modules", "dist", "playwright-report", "test-results", ".env", ".env.*", ".git"))
        shutil.copytree(root / "tests/fixtures/hyperliquid", context / "tests/fixtures/hyperliquid")
        (context / "deploy/docker").mkdir(parents=True)
        for path in ("deploy/docker/Dockerfile.adjustment", "deploy/docker/nginx.conf", ".dockerignore"):
            shutil.copyfile(root / path, context / path)
        hashes = {str(p.relative_to(context)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in context.rglob("*") if p.is_file()}
        record = {"commit": commit, "base_web_image": args.base_web_image,
                  "web_image": args.web_image, "gateway_image": args.gateway_image,
                  "source_sha256": hashes, "status": "building"}
        write_record(args.record, record)
        # Test the committed Rust tree with the existing dependency cache.
        work = "/work/" + str(context.relative_to(root))
        tool.run(["docker", "run", "--rm", "--network", "none", "-e", "CARGO_HOME=/work/.runtime/cargo",
                  "-e", "CARGO_TARGET_DIR=/work/target", "-v", "hl-trader-rustup:/usr/local/rustup",
                  "-v", str(root) + ":/work", "-w", work, "rust:1.94-slim-bookworm",
                  "cargo", "test", "--offline", "--", "--test-threads=2"], capture=False, timeout=600)
        for target, tag in (("web", args.web_image), ("gateway", args.gateway_image)):
            tool.run(["docker", "build", "-f", str(context / "deploy/docker/Dockerfile.adjustment"),
                      "--target", target, "--build-arg", "BASE_WEB_IMAGE=" + args.base_web_image,
                      "--build-arg", "CODE_COMMIT=" + commit, "-t", tag, str(context)],
                     capture=False, timeout=1800)
        record.update(status="built", images={target: json.loads(tool.run(
            ["docker", "image", "inspect", tag]))[0]["Id"]
            for target, tag in (("web", args.web_image), ("gateway", args.gateway_image))})
        write_record(args.record, record)
        print(json.dumps({"status": "built", "images": record["images"], "record": str(args.record)}))


if __name__ == "__main__":
    main()
