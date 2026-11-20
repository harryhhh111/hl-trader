"""Pull the research library and atomically publish its display snapshot.

Only ``sync --apply`` fetches/fast-forwards Git and switches live content.
Uses the running Web image for Python dependencies; never restarts services.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

from research_release import ResearchRelease, ReleaseError, environment, write_record


def atomic_pointer(path, payload):
    """Keep the bind-mounted directory intact; replace only its readable pointer."""
    temporary = path.with_name(".current-" + uuid.uuid4().hex)
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644), "wb") as handle:
            os.fchmod(handle.fileno(), 0o644)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def checked_snapshot(root, expected=None, snapshot_id=None):
    """Verify both served files and retained originals before changing the pointer."""
    snapshot_id = snapshot_id or json.loads((root / "current.json").read_bytes())["snapshot_id"]
    if not re.fullmatch(r"s[0-9a-f]{32}", snapshot_id):
        raise ReleaseError("快照标识无效")
    directory = root / "snapshots" / snapshot_id
    if directory.is_symlink():
        raise ReleaseError("快照目录不能是符号链接")
    manifest = json.loads((directory / "manifest.json").read_bytes())
    if manifest["snapshot_id"] != snapshot_id:
        raise ReleaseError("快照清单与指针不一致")
    if expected is not None:
        # Repeated builds have the same content address, but a different clock.
        semantic = lambda m: {k: v for k, v in m.items() if k != "generated_at"}
        if semantic(manifest) != semantic(expected):
            raise ReleaseError("已有快照与候选清单不一致")
    for section, folder in (("files", "content"), ("originals", "originals")):
        for relative, record in manifest[section].items():
            path = directory / folder / relative
            if Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ReleaseError("快照文件路径越界")
            if any(p.is_symlink() for p in (path, *path.parents)):
                raise ReleaseError("快照文件不能经过符号链接")
            payload = path.read_bytes()
            if len(payload) != record["size"] or hashlib.sha256(payload).hexdigest() != record["sha256"]:
                raise ReleaseError("快照文件哈希不一致：" + relative)
    return manifest


class ResearchSync:
    def __init__(self, release, source):
        self.release = release
        self.source = Path(source).expanduser().resolve()

    def git(self, *args):
        try:
            return subprocess.run(
                ["git", "-c", "core.hooksPath=/dev/null", "-c", "merge.autoStash=false", *args],
                cwd=self.source, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=True, timeout=120, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            ).stdout.decode().strip()
        except (OSError, subprocess.SubprocessError) as exc:
            # Remote URLs and credential helper errors must not leak into records.
            raise ReleaseError("研究库 Git 操作失败：" + args[0] + "；请检查分支、网络和凭据配置") from exc

    def source_state(self):
        if Path(self.git("rev-parse", "--show-toplevel")).resolve() != self.source:
            raise ReleaseError("source 必须是研究库仓库根目录")
        branch = self.git("symbolic-ref", "--quiet", "--short", "HEAD")
        return {"branch": branch, "upstream": self.git("rev-parse", "--abbrev-ref", "@{upstream}"),
                "commit": self.git("rev-parse", "HEAD"),
                "dirty": bool(self.git("status", "--porcelain", "--untracked-files=all"))}

    def pull(self):
        before = self.source_state()
        if before["dirty"]:
            raise ReleaseError("研究库有未提交修改；保留原状，请先由研究任务提交后再同步")
        remote = self.git("config", "--get", "branch." + before["branch"] + ".remote")
        if remote == "." or remote.startswith("-"):
            raise ReleaseError("当前分支必须跟踪一个远端仓库分支")
        self.git("fetch", "--no-tags", "--", remote)
        ahead, _ = map(int, self.git("rev-list", "--left-right", "--count", "HEAD...@{upstream}").split())
        if ahead:
            raise ReleaseError("研究库有未推送提交或与上游分叉；停止同步，未重置或覆盖本地分支")
        self.git("merge", "--ff-only", "--no-edit", "@{upstream}")
        after = self.source_state()
        if after["dirty"] or after["branch"] != before["branch"] or after["upstream"] != before["upstream"]:
            raise ReleaseError("同步期间研究库发生其他修改，停止发布")
        return before, after

    def output_root(self, state):
        web = state["web"]
        target = environment(web).get("HL_APP_RESEARCH_REPORTS_PATH")
        mounts = [m for m in web["Mounts"] if m["Destination"] == target]
        if len(mounts) != 1 or mounts[0]["Type"] != "bind" or mounts[0].get("RW", True):
            raise ReleaseError("Web 必须将研究快照目录以只读 bind mount 挂载")
        root = Path(mounts[0]["Source"]).resolve()
        if not root.is_dir() or root == self.source or root in self.source.parents or self.source in root.parents:
            raise ReleaseError("研究快照目录不存在或与研究库重叠")
        return root

    def plan(self):
        state = self.release.capture()
        output = self.output_root(state)
        current = json.loads((output / "current.json").read_bytes())
        return {"dry_run": True, "source": str(self.source), "git": self.source_state(),
                "output": str(output), "current_snapshot_id": current["snapshot_id"],
                "actions": ["fetch current upstream", "fast-forward only", "build and verify candidate",
                            "atomically switch current.json", "verify live mount and gateway"],
                "restart_services": False}

    def build(self, state, candidate):
        # Dependencies come from the installed Web image. The current consumer
        # code and research source are read-only mounts; no source scripts run.
        self.release.run([
            "docker", "run", "--rm", "--network", "none", "--read-only",
            "--user", f"{os.getuid()}:{os.getgid()}", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONPATH=/work/backend",
            "--mount", f"type=bind,src={self.release.root},dst=/work,readonly",
            "--mount", f"type=bind,src={self.source},dst=/research,readonly",
            "--mount", f"type=bind,src={candidate},dst=/output",
            state["web"]["Image"], "python", "-m", "hl_app.research_reports", "snapshot",
            "--source", "/research", "--output", "/output",
        ], timeout=180)
        manifest = checked_snapshot(candidate)
        # Git refuses a cross-owner repository mounted in Docker. The build
        # runs as the host owner, so provenance must remain available/clean.
        return manifest

    def check_selection(self, manifest):
        index = self.source / manifest["source"]["index_path"]
        document = json.loads(index.read_bytes())
        eligible = {r["report_id"] for r in document["reports"]
                    if re.fullmatch(r"company:[a-z0-9]+(?:-[a-z0-9]+)*", r.get("entity_id") or "")
                    and r["status"] in {"draft", "ready"}
                    and r["report_type"] in {"company_research", "topic", "update"}
                    and Path(r["entry_path"]).suffix.lower() in {".html", ".htm"}}
        missing = eligible - manifest["reports"].keys()
        identity_errors = [item for item in manifest["excluded"] if not item["report_id"]]
        if missing or identity_errors:
            raise ReleaseError("候选公司 HTML 或身份入口未通过校验，停止发布：" +
                               json.dumps([r for r in manifest["excluded"]
                                           if r["report_id"] in missing or not r["report_id"]], ensure_ascii=False))
        if not manifest["totals"]["companies"] or not manifest["totals"]["reports"]:
            raise ReleaseError("候选快照没有公司 HTML，停止发布以保留线上内容")

    def publish(self, candidate, output, manifest, verify):
        pointer = output / "current.json"
        previous = pointer.read_bytes()
        target = output / "snapshots" / manifest["snapshot_id"]
        if target.exists():
            # Validate an existing immutable directory against the freshly
            # built manifest without changing its original generation date.
            manifest = checked_snapshot(output, manifest, manifest["snapshot_id"])
        else:
            os.replace(candidate / "snapshots" / manifest["snapshot_id"], target)
        payload = json.dumps({"snapshot_id": manifest["snapshot_id"],
                              "generated_at": manifest["generated_at"]}).encode()
        if pointer.read_bytes() != previous:
            raise ReleaseError("线上快照指针被其他流程修改，停止切换")
        atomic_pointer(pointer, payload)
        try:
            return verify()
        except Exception as exc:
            if pointer.read_bytes() != payload:
                raise ReleaseError("复验失败且快照指针被其他流程修改，请人工核对；未覆盖其他发布") from exc
            atomic_pointer(pointer, previous)
            raise ReleaseError("发布复验失败，已恢复原来的线上快照") from exc

    def sync(self):
        with self.release.lock():
            state = self.release.capture()
            output = self.output_root(state)
            before_live = self.release.verify(state=state)
            record = {"before": before_live, "source": str(self.source), "output": str(output)}
            name = "research-sync-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
            record_path = self.release.records / (name + ".json")
            try:
                before, after = self.pull()
                record.update(git_before=before, git_after=after)
                # Stage on the destination filesystem so promotion is atomic
                # even when the mounted content lives on another volume.
                with tempfile.TemporaryDirectory(prefix=".research-sync-", dir=output) as temp:
                    candidate = Path(temp)
                    manifest = self.build(state, candidate)
                    self.check_selection(manifest)
                    final_source = self.source_state()
                    if final_source != after or manifest["source"]["commit"] != after["commit"] or manifest["source"]["dirty"]:
                        raise ReleaseError("研究库在构建期间发生变化或 Git 来源不匹配，停止发布")
                    record.update(snapshot_id=manifest["snapshot_id"], totals=manifest["totals"],
                                  excluded=manifest["excluded"])

                    def verify():
                        result = self.release.verify()
                        if result["verification"]["snapshot_id"] != manifest["snapshot_id"]:
                            raise ReleaseError("Web 读取的快照不是本次发布版本")
                        if result["services"] != before_live["services"]:
                            raise ReleaseError("发布期间服务实例、配置或镜像发生变化")
                        return result

                    record["after"] = self.publish(candidate, output, manifest, verify)
                record["status"] = "published"
                write_record(record_path, record)
            except Exception as exc:
                record["status"] = "failed"
                record["error"] = str(exc) if isinstance(exc, ReleaseError) else type(exc).__name__
                record["current_pointer"] = json.loads((output / "current.json").read_bytes())
                write_record(record_path.with_suffix(".failed.json"), record)
                raise
            return {"status": record["status"], "branch": after["branch"], "commit": after["commit"],
                    "snapshot_id": manifest["snapshot_id"], "totals": manifest["totals"],
                    "excluded": manifest["excluded"], "record": str(record_path), "restart_services": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source", type=Path, default=Path.home() / "projects/stock-research")
    parser.add_argument("--env-file", default=".runtime/docker.env")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="只读查看当前分支和线上快照，不联网拉取")
    sync = commands.add_parser("sync", help="拉取上游并更新线上快照；默认只查看计划")
    sync.add_argument("--apply", action="store_true", help="执行已获授权的拉取与内容发布")
    args = parser.parse_args()
    tool = ResearchSync(ResearchRelease(args.root, env_file=args.env_file), args.source)
    result = tool.sync() if args.command == "sync" and args.apply else tool.plan()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ReleaseError, OSError, ValueError, KeyError) as exc:
        message = str(exc) if isinstance(exc, ReleaseError) else type(exc).__name__
        raise SystemExit("同步失败：" + message)
