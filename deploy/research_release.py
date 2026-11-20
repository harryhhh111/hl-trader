"""Repeatable research release operations; plan is offline, verify is read-only.

Only deployment commands with ``--apply`` change running services. Authorization is
still governed by AGENTS.md. Credentials never appear in records or arguments.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


class ReleaseError(RuntimeError):
    pass


SCOPES = {"frontend": ("gateway",), "web": ("web", "gateway")}
SERVICES = ("web", "worker", "gateway")
DEFAULT_COMPOSE = ("compose.yaml", "compose.managed-wallets.yaml", "compose.research-reports.yaml")

# The probe runs inside the existing Web container, through the real gateway.
# It creates no session, runs no repository scripts and only reads mounted files.
PROBE = r'''
import hashlib,json,os,re,sys,urllib.request,urllib.error
from pathlib import Path
from hl_app.services.research_reports import load_snapshot,snapshot_content_bytes,report_content_url
def get(path):
    try:
        with urllib.request.urlopen('http://gateway'+path,timeout=10) as r:
            data=r.read(8*1024*1024+1)
            if len(data)>8*1024*1024:raise RuntimeError('Response too large')
            return r.status,data
    except urllib.error.HTTPError as e:
        return e.code,e.read(16384)
status,body=get('/api/v1/health')
if status!=200 or json.loads(body)['data']['status']!='ok':raise RuntimeError('Health check failed')
status,page=get('/research')
if status!=200:raise RuntimeError('Research page unavailable')
scripts=re.findall(r'<script[^>]+src="([^\"]+)"',page.decode('utf-8'))
if not scripts or not re.fullmatch(r'/assets/[A-Za-z0-9_.-]+\.js',scripts[0]):raise RuntimeError('Invalid frontend asset path')
asset=scripts[0]
status,bundle=get(asset)
if status!=200:raise RuntimeError('Frontend asset unavailable')
text=bundle.decode('utf-8')
markers=['公司研究',*json.loads(sys.argv[1])]
if not all(marker in text for marker in markers):raise RuntimeError('Expected frontend markers missing')
snapshot=load_snapshot(Path(os.environ['HL_APP_RESEARCH_REPORTS_PATH']))
for path in snapshot.manifest.files:
    snapshot_content_bytes(snapshot.root,snapshot.snapshot_id,path)
paths=['/api/v1/research/companies']
first=next(iter(snapshot.manifest.reports.values()),None)
if first:
    paths += ['/api/v1/research/reports/'+first.report_id,report_content_url(snapshot.snapshot_id,first.entry_path)]
denied=[get(path)[0] for path in paths]
if not all(code in (401,403) for code in denied):raise RuntimeError('Anonymous access was not rejected')
cli=hashlib.sha256(Path('/usr/local/bin/hl-trader').read_bytes()).hexdigest()
print(json.dumps({'health':'ok','asset':asset,'asset_sha256':hashlib.sha256(bundle).hexdigest(),
 'snapshot_id':snapshot.snapshot_id,'totals':snapshot.manifest.totals,'verified_files':len(snapshot.manifest.files),
 'anonymous_statuses':denied,'cli_sha256':cli,'expected_text_verified':markers,
 'verification_scope':'internal_gateway_and_mounted_files','public_authenticated_browser_verified':False}))
'''

RUNNING_EXECUTIONS = r'''
import json,os,psycopg2
with psycopg2.connect(os.environ['HL_APP_DATABASE_URL'],connect_timeout=5) as conn:
    conn.set_session(readonly=True)
    with conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM hyperliquid_execution_requests WHERE status='running'")
        print(json.dumps({'running':cursor.fetchone()[0]}))
'''


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def environment(container):
    return dict(value.split("=", 1) for value in container["Config"].get("Env", []) if "=" in value)


def public_container(container):
    env = {k: v for k, v in environment(container).items() if k.startswith("HL_APP_") or k == "PGOPTIONS"}
    return {
        "container_id": container["Id"], "image_id": container["Image"],
        "running": container["State"]["Running"],
        "health": container["State"].get("Health", {}).get("Status"),
        "app_environment_sha256": digest(env),
        "mounts_sha256": digest(sorted(container.get("Mounts", []), key=lambda m: m["Destination"])),
    }


def write_record(path, record):
    """Atomic private record; the staging file is removed even after a failure."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        with os.fdopen(os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as handle:
            json.dump(record, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


class ResearchRelease:
    def __init__(self, root, env_file=".runtime/docker.env", compose_files=DEFAULT_COMPOSE):
        self.root = Path(root).resolve()
        self.compose = ["docker", "compose", "--env-file", env_file]
        for path in compose_files:
            self.compose.extend(["-f", path])
        self.records = self.root / ".runtime/deployments"

    def run(self, args, *, input=None, capture=True, timeout=60):
        try:
            result = subprocess.run(args, cwd=self.root, input=input, check=True, timeout=timeout,
                                    stdout=subprocess.PIPE if capture else None,
                                    stderr=subprocess.PIPE if capture else None)
        except (OSError, subprocess.SubprocessError) as exc:
            # Config/probe errors can contain sensitive values: do not echo raw
            # stderr or a traceback. Service logs remain available to operators.
            raise ReleaseError(f"命令失败：{' '.join(args[:3])}；请检查服务状态与配置") from exc
        return result.stdout or b""

    def capture(self):
        ids = self.run(self.compose + ["ps", "--all", "--quiet", *SERVICES]).decode().split()
        if not ids:
            raise ReleaseError("未找到现有 hl-app 服务")
        containers = json.loads(self.run(["docker", "inspect", *ids]))
        result = {}
        for container in containers:
            service = container["Config"]["Labels"]["com.docker.compose.service"]
            if service not in SERVICES or service in result:
                raise ReleaseError("服务实例不唯一，请核对 Compose 项目")
            if not container["State"]["Running"]:
                raise ReleaseError(f"服务没有运行：{service}")
            result[service] = container
        if set(result) != set(SERVICES):
            raise ReleaseError("现有 Web、worker、gateway 服务不完整")
        if result["web"]["State"].get("Health", {}).get("Status") != "healthy":
            raise ReleaseError("Web 健康检查尚未通过")
        return result

    def verify(self, expected_text=(), state=None):
        state = self.capture() if state is None else state
        probe = json.loads(self.run(["docker", "exec", "-i", state["web"]["Id"], "python", "-",
                                     json.dumps(list(expected_text), ensure_ascii=False)], input=PROBE.encode()))
        if probe.get("health") != "ok" or probe.get("public_authenticated_browser_verified") is not False:
            raise ReleaseError("核验结果格式不正确")
        return {"services": {name: public_container(c) for name, c in state.items()}, "verification": probe}

    def plan(self, scope="frontend"):
        services = SCOPES[scope]
        return {"dry_run": True, "scope": scope, "services": list(services),
                "preserved_services": [s for s in SERVICES if s not in services],
                "commands": [self.compose + ["build", *services],
                             self.compose + ["up", "-d", "--no-deps", "--force-recreate", "--timeout", "60",
                                             "--wait", "--wait-timeout", "120", *services]]}

    @contextmanager
    def lock(self):
        path = self.root / ".runtime/research-release.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ReleaseError("已有研究发布流程运行中") from exc
            yield
        finally:
            os.close(descriptor)

    def check_configuration(self, state, scope):
        config = json.loads(self.run(self.compose + ["config", "--format", "json"]))
        for service in SCOPES[scope]:
            live = state[service]
            desired = config["services"][service]
            env = environment(live)
            desired_env = desired.get("environment", {})
            changed = {key for key, value in desired_env.items() if env.get(key) != str(value)}
            changed.update(key for key in env if (key.startswith("HL_APP_") or key == "PGOPTIONS") and key not in desired_env)
            if changed:
                raise ReleaseError(f"{service} 的配置与运行中服务不同：{', '.join(sorted(changed))}")
            expected_mounts = []
            for volume in desired.get("volumes", []):
                source = volume["source"]
                if volume["type"] == "volume":
                    source = config["volumes"][source]["name"]
                expected_mounts.append((volume["type"], source, volume["target"], not volume.get("read_only", False)))
            actual_mounts = [(m["Type"], m.get("Name") if m["Type"] == "volume" else m["Source"],
                              m["Destination"], m["RW"]) for m in live.get("Mounts", []) if m["Type"] in {"volume", "bind"}]
            if sorted(expected_mounts) != sorted(actual_mounts):
                raise ReleaseError(f"{service} 的挂载与运行中服务不同")

    def deploy(self, scope="frontend", *, apply=False, expected_text=()):
        plan = self.plan(scope)
        if not apply:
            return plan
        with self.lock():
            before = self.capture()
            self.check_configuration(before, scope)
            old = self.verify(state=before)
            commit = self.run(["git", "rev-parse", "HEAD"]).decode().strip()
            release_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + commit[:12] + "-" + uuid.uuid4().hex[:8]
            prefix = self.records / ("research-" + release_id)
            record = {"release_id": release_id, "scope": scope, "code_commit": commit,
                      "tracked_diff_sha256": hashlib.sha256(self.run(["git", "diff", "--binary", "HEAD"])).hexdigest(),
                      "before": old, "commands": plan["commands"], "rollback_tags": {}}
            # Persist all proposed rollback names before tagging, so interrupted
            # builds still have an auditable record. No existing tag is reused.
            for service in SCOPES[scope]:
                record["rollback_tags"][service] = f"hl-app-{service}:before-research-{release_id}"
            write_record(prefix.with_suffix(".before.json"), record)
            try:
                for service, tag in record["rollback_tags"].items():
                    self.run(["docker", "tag", before[service]["Image"], tag])
                self.run(plan["commands"][0], capture=False, timeout=1800)
                if "web" in SCOPES[scope]:
                    cli = self.run(["docker", "run", "--rm", "--network", "none", "--read-only", "--entrypoint", "python",
                                    "hl-app-web:local", "-c", "import hashlib;print(hashlib.sha256(open('/usr/local/bin/hl-trader','rb').read()).hexdigest())"]).decode().strip()
                    if cli != old["verification"]["cli_sha256"]:
                        raise ReleaseError("交易 CLI 与线上不同；请按独立交易功能发布流程处理")
                    running = json.loads(self.run(["docker", "exec", "-i", before["web"]["Id"], "python", "-"],
                                                  input=RUNNING_EXECUTIONS.encode()))["running"]
                    if running != 0:
                        raise ReleaseError("存在运行中的执行请求，本轮未切换服务")
                # Detect other deployments or a configuration edit during build.
                current = self.capture()
                if any(current[s]["Id"] != before[s]["Id"] for s in SERVICES):
                    raise ReleaseError("构建期间服务实例发生变化，本轮未切换服务")
                self.check_configuration(current, scope)
                self.run(plan["commands"][1], capture=False, timeout=240)
                after = self.capture()
                for service in SERVICES:
                    old_service, new_service = public_container(before[service]), public_container(after[service])
                    if service not in SCOPES[scope] and old_service != new_service:
                        raise ReleaseError(f"应保持不变的服务发生变化：{service}")
                    for key in ("app_environment_sha256", "mounts_sha256"):
                        if old_service[key] != new_service[key]:
                            raise ReleaseError(f"{service} 的运行配置发生变化")
                record.update({"after": self.verify(expected_text, state=after), "status": "verified",
                               "verified_at": datetime.now(timezone.utc).isoformat(),
                               "database_migration_executed": False, "orders_submitted": False})
                if record["after"]["verification"]["cli_sha256"] != old["verification"]["cli_sha256"]:
                    raise ReleaseError("交易 CLI 核对失败")
                write_record(prefix.with_suffix(".deployed.json"), record)
                return record
            except Exception as exc:
                record.update(status="failed", failure_type=type(exc).__name__)
                write_record(prefix.with_suffix(".failed.json"), record)
                raise ReleaseError(f"发布未完成；原版本、回退标签与阶段记录保存在 {prefix.name}.*.json") from exc

    def deploy_prepared_web(self, build_record, *, apply=False):
        """Install a verified overlay image; never build from the working tree."""
        build = json.loads(Path(build_record).read_text())
        command = self.compose + ["up", "-d", "--no-deps", "--no-build", "--force-recreate",
                                  "--wait", "--wait-timeout", "120", "web"]
        if not apply:
            return {"dry_run": True, "candidate_image": build["candidate_image"], "commands": [command],
                    "preserved_services": ["gateway", "worker"], "gateway_config_reload": True}
        with self.lock():
            before = self.capture()
            self.check_configuration(before, "web")
            if before["web"]["Image"] != build["base_web_image"]:
                raise ReleaseError("线上 Web 已变更，候选镜像的基础版本不匹配")
            if build.get("status") != "built_not_deployed" or build.get("cli_unchanged") is not True:
                raise ReleaseError("候选镜像缺少已验证的构建记录")
            candidate = build["candidate_image"]
            inspected = json.loads(self.run(["docker", "image", "inspect", candidate]))[0]
            if inspected["Id"] != candidate:
                raise ReleaseError("候选镜像标识不匹配")
            old = self.verify(state=before)
            cli = self.run(["docker", "run", "--rm", "--network", "none", "--read-only", candidate,
                            "python", "-c", "import hashlib;print(hashlib.sha256(open('/usr/local/bin/hl-trader','rb').read()).hexdigest())"]).decode().strip()
            if cli != old["verification"]["cli_sha256"]:
                raise ReleaseError("候选镜像的交易 CLI 与线上不同")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
            prefix = self.records / ("research-prepared-web-" + stamp)
            rollback_tag = "hl-app-web:before-research-prepared-" + stamp
            record = {"status": "prepared", "build_record": str(build_record), "code_commit": build["commit"],
                      "candidate_image": candidate, "before": old, "rollback_tag": rollback_tag,
                      "commands": [command]}
            write_record(prefix.with_suffix(".before.json"), record)
            try:
                running = json.loads(self.run(["docker", "exec", "-i", before["web"]["Id"], "python", "-"],
                                              input=RUNNING_EXECUTIONS.encode()))["running"]
                if running != 0:
                    raise ReleaseError("存在运行中的执行请求，本轮未切换服务")
                current = self.capture()
                if any(current[s]["Id"] != before[s]["Id"] for s in SERVICES):
                    raise ReleaseError("准备期间服务实例发生变化，本轮未切换服务")
                self.check_configuration(current, "web")
                self.run(["docker", "tag", before["web"]["Image"], rollback_tag])
                self.run(["docker", "tag", candidate, "hl-app-web:local"])
                self.run(command, capture=False, timeout=240)
                # nginx resolves the Web name at configuration load time.
                # Reload workers to resolve the new address, preserving its container.
                self.run(["docker", "exec", before["gateway"]["Id"], "nginx", "-t"])
                self.run(["docker", "exec", before["gateway"]["Id"], "nginx", "-s", "reload"])
                after = self.capture()
                if after["web"]["Image"] != candidate:
                    raise ReleaseError("Web 没有使用指定候选镜像")
                for service in SERVICES:
                    previous, actual = public_container(before[service]), public_container(after[service])
                    if service != "web" and previous != actual:
                        raise ReleaseError("应保持不变的服务发生变化：" + service)
                    if any(previous[k] != actual[k] for k in ("app_environment_sha256", "mounts_sha256")):
                        raise ReleaseError("运行配置发生变化：" + service)
                verified = self.verify(state=after)
                if verified["verification"]["cli_sha256"] != cli:
                    raise ReleaseError("部署后的交易 CLI 核对失败")
                if verified["verification"]["snapshot_id"] != old["verification"]["snapshot_id"]:
                    raise ReleaseError("发布期间研究快照发生变化")
                record.update(status="verified", after=verified, gateway_config_reloaded=True,
                              database_migration_executed=False, orders_submitted=False)
                write_record(prefix.with_suffix(".deployed.json"), record)
                return record
            except Exception as exc:
                record.update(status="failed", failure_type=type(exc).__name__)
                write_record(prefix.with_suffix(".failed.json"), record)
                raise ReleaseError(f"预构建 Web 发布未完成；核对 {prefix.name}.*.json 和回退镜像") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--env-file", default=".runtime/docker.env")
    parser.add_argument("--compose-file", action="append")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("plan", "verify", "deploy"):
        command = sub.add_parser(action)
        if action != "verify":
            command.add_argument("--scope", choices=SCOPES, default="frontend")
        if action != "plan":
            command.add_argument("--expect-text", action="append", default=[])
        if action == "deploy":
            command.add_argument("--apply", action="store_true", help="执行发布；未指定时只输出计划")
    prepared = sub.add_parser("deploy-prepared-web", help="发布已验证的 Web 镜像，不重新构建")
    prepared.add_argument("--build-record", type=Path, required=True)
    prepared.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    tool = ResearchRelease(args.root, args.env_file, args.compose_file or DEFAULT_COMPOSE)
    try:
        if args.action == "deploy-prepared-web":
            result = tool.deploy_prepared_web(args.build_record, apply=args.apply)
        elif args.action == "verify":
            result = tool.verify(args.expect_text)
        elif args.action == "plan":
            result = tool.plan(args.scope)
        else:
            result = tool.deploy(args.scope, apply=args.apply, expected_text=args.expect_text)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except ReleaseError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
