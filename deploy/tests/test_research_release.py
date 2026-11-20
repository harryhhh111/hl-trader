"""Release guardrails, using a fake Docker runtime (no service mutations)."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("research_release", Path(__file__).parents[1] / "research_release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
SECRET = "test-secret-never-print"


class FakeRuntime(release.ResearchRelease):
    def __init__(self, root):
        super().__init__(root)
        self.calls = []
        self.running_executions = 0
        self.built_cli = b"same-cli\n"
        self.change_worker = False
        self.state = {
            name: {"Id": name + "-old", "Image": name + "-image",
                   "Config": {"Env": ["HL_APP_SECRET=" + SECRET],
                              "Labels": {"com.docker.compose.service": name}},
                   "State": {"Running": True, "Health": {"Status": "healthy"}}, "Mounts": []}
            for name in release.SERVICES
        }
        self.config = {"services": {name: {"environment": {"HL_APP_SECRET": SECRET}}
                                    for name in release.SERVICES}}

    def run(self, args, **kwargs):
        self.calls.append(args)
        if args[:2] == ["docker", "inspect"]:
            return json.dumps(list(self.state.values())).encode()
        if args[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Id": args[3]}]).encode()
        if args[:2] == ["docker", "exec"]:
            if "nginx" in args:
                return b""
            if kwargs["input"] == release.RUNNING_EXECUTIONS.encode():
                return json.dumps({"running": self.running_executions}).encode()
            return json.dumps({"health": "ok", "cli_sha256": "same-cli",
                               "snapshot_id": "same-snapshot",
                               "public_authenticated_browser_verified": False}).encode()
        if args[:2] == ["docker", "run"]:
            return self.built_cli
        if args[:2] == ["docker", "tag"]:
            if args[-1] == "hl-app-web:local":
                self.prepared_image = args[2]
            return b""
        if args[:2] == ["git", "rev-parse"]:
            return b"abcdef1234567890\n"
        if args[:2] == ["git", "diff"]:
            return b"unrelated workspace changes"
        action = args[len(self.compose):]
        if action[:1] == ["ps"]:
            return b"web-old worker-old gateway-old"
        if action[:1] == ["config"]:
            return json.dumps(self.config).encode()
        if action[:1] == ["build"]:
            return b""
        if action[:1] == ["up"]:
            selected = [name for name in release.SERVICES if name in action]
            for name in selected + (["worker"] if self.change_worker else []):
                self.state[name]["Id"] += "-new"
                self.state[name]["Image"] += "-new"
                if name == "web" and hasattr(self, "prepared_image"):
                    self.state[name]["Image"] = self.prepared_image
            return b""
        raise AssertionError("Unexpected command: " + repr(args))

    def switched(self):
        return [c for c in self.calls if c[:len(self.compose) + 1] == self.compose + ["up"]]


class ResearchReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hl-release-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tool = FakeRuntime(self.root)

    def test_plan_and_deploy_without_apply_are_offline(self):
        self.assertEqual(self.tool.plan(), self.tool.deploy())
        self.assertEqual(self.tool.calls, [])
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertEqual(self.tool.plan()["services"], ["gateway"])

    def test_verify_only_reads_runtime_and_hides_secrets(self):
        result = self.tool.verify()
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(self.tool.switched(), [])
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertFalse(result["verification"]["public_authenticated_browser_verified"])

    def test_config_changes_and_removals_stop_before_build(self):
        for value in ({}, {"HL_APP_SECRET": "different-secret"}):
            with self.subTest(value=value):
                self.tool.config["services"]["gateway"]["environment"] = value
                self.tool.calls.clear()
                with self.assertRaisesRegex(release.ReleaseError, "HL_APP_SECRET") as error:
                    self.tool.deploy(apply=True)
                self.assertNotIn(SECRET, str(error.exception))
                self.assertFalse(any(c[:2] == ["docker", "tag"] for c in self.tool.calls))
                self.assertEqual(self.tool.switched(), [])

    def test_frontend_release_preserves_web_worker_and_records_rollback(self):
        original = copy.deepcopy(self.tool.state)
        result = self.tool.deploy(apply=True)
        self.assertEqual(result["status"], "verified")
        for service in ("web", "worker"):
            self.assertEqual(original[service], self.tool.state[service])
        self.assertNotEqual(original["gateway"]["Id"], self.tool.state["gateway"]["Id"])
        self.assertEqual(set(result["rollback_tags"]), {"gateway"})
        self.assertIn("--no-deps", self.tool.switched()[0])
        records = list(self.tool.records.glob("*.json"))
        self.assertEqual(len(records), 2)
        for path in records:
            self.assertNotIn(SECRET, path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_web_release_refuses_running_execution_and_records_failure(self):
        self.tool.running_executions = 1
        with self.assertRaisesRegex(release.ReleaseError, "发布未完成"):
            self.tool.deploy("web", apply=True)
        self.assertEqual(self.tool.switched(), [])
        failure = json.loads(next(self.tool.records.glob("*.failed.json")).read_text())
        self.assertEqual(failure["status"], "failed")
        self.assertNotIn(SECRET, json.dumps(failure))

    def test_web_release_refuses_changed_cli_before_switching(self):
        self.tool.built_cli = b"different-cli\n"
        with self.assertRaises(release.ReleaseError):
            self.tool.deploy("web", apply=True)
        self.assertEqual(self.tool.switched(), [])

    def test_web_release_preserves_worker(self):
        worker = copy.deepcopy(self.tool.state["worker"])
        result = self.tool.deploy("web", apply=True)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(worker, self.tool.state["worker"])
        self.assertEqual(set(result["rollback_tags"]), {"web", "gateway"})

    def prepared_record(self, **updates):
        payload = {"status": "built_not_deployed", "commit": "reviewed-commit", "cli_unchanged": True,
                   "base_web_image": self.tool.state["web"]["Image"], "candidate_image": "candidate-image"}
        payload.update(updates)
        path = self.root / "build.json"
        path.write_text(json.dumps(payload))
        return path

    def test_prepared_plan_is_read_only(self):
        result = self.tool.deploy_prepared_web(self.prepared_record())
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.tool.calls, [])

    def test_prepared_image_updates_only_web_without_building(self):
        before = copy.deepcopy(self.tool.state)
        result = self.tool.deploy_prepared_web(self.prepared_record(), apply=True)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(self.tool.state["web"]["Image"], "candidate-image")
        for service in ("worker", "gateway"):
            self.assertEqual(self.tool.state[service], before[service])
        self.assertIn("--no-build", self.tool.switched()[0])
        self.assertEqual(self.tool.switched()[0][-1], "web")
        self.assertFalse(any(c[:len(self.tool.compose)+1] == self.tool.compose+["build"] for c in self.tool.calls))
        self.assertIn(["docker", "exec", "gateway-old", "nginx", "-s", "reload"], self.tool.calls)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_prepared_image_requires_matching_base(self):
        with self.assertRaisesRegex(release.ReleaseError, "基础版本"):
            self.tool.deploy_prepared_web(self.prepared_record(base_web_image="other-base"), apply=True)
        self.assertEqual(self.tool.switched(), [])
        self.assertFalse(any(c[:2] == ["docker", "tag"] for c in self.tool.calls))

    def test_prepared_image_requires_same_cli(self):
        self.tool.built_cli = b"different-cli"
        with self.assertRaisesRegex(release.ReleaseError, "交易 CLI"):
            self.tool.deploy_prepared_web(self.prepared_record(), apply=True)
        self.assertEqual(self.tool.switched(), [])

    def test_prepared_image_stops_before_switch_when_execution_is_running(self):
        self.tool.running_executions = 1
        with self.assertRaises(release.ReleaseError):
            self.tool.deploy_prepared_web(self.prepared_record(), apply=True)
        self.assertEqual(self.tool.switched(), [])
        self.assertFalse(any(c[:2] == ["docker", "tag"] for c in self.tool.calls))

    def test_prepared_image_detects_unexpected_worker_change(self):
        self.tool.change_worker = True
        with self.assertRaises(release.ReleaseError):
            self.tool.deploy_prepared_web(self.prepared_record(), apply=True)
        self.assertEqual(list(self.tool.records.glob("*.deployed.json")), [])

    def test_mount_change_stops_before_build(self):
        self.tool.config["services"]["gateway"]["volumes"] = [
            {"type": "bind", "source": "/new-path", "target": "/reports", "read_only": True}]
        with self.assertRaisesRegex(release.ReleaseError, "挂载"):
            self.tool.deploy(apply=True)
        self.assertEqual(self.tool.switched(), [])

    def test_lock_prevents_concurrent_deployment(self):
        with self.tool.lock():
            with self.assertRaisesRegex(release.ReleaseError, "已有研究发布流程"):
                self.tool.deploy(apply=True)
        self.assertEqual(self.tool.calls, [])

    def test_unexpected_worker_replacement_is_not_reported_as_success(self):
        self.tool.change_worker = True
        with self.assertRaises(release.ReleaseError):
            self.tool.deploy(apply=True)
        self.assertEqual(list(self.tool.records.glob("*.deployed.json")), [])
        self.assertEqual(len(list(self.tool.records.glob("*.failed.json"))), 1)

    def test_releases_have_unique_records_and_rollback_tags(self):
        first = self.tool.deploy(apply=True)
        second = self.tool.deploy(apply=True)
        self.assertNotEqual(first["release_id"], second["release_id"])
        self.assertNotEqual(first["rollback_tags"], second["rollback_tags"])
        self.assertEqual(len(list(self.tool.records.glob("*.json"))), 4)

    def test_mount_order_does_not_change_configuration_fingerprint(self):
        state = copy.deepcopy(self.tool.state["web"])
        state["Mounts"] = [{"Destination": "/a"}, {"Destination": "/b"}]
        before = release.public_container(state)
        state["Mounts"].reverse()
        self.assertEqual(before, release.public_container(state))

    def test_atomic_record_failure_leaves_no_staging_file(self):
        path = self.root / "records" / "record.json"
        with patch.object(release.os, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                release.write_record(path, {"status": "verified"})
        self.assertEqual(list(path.parent.iterdir()), [])

    def test_command_error_does_not_disclose_stderr(self):
        tool = release.ResearchRelease(self.root)
        error = subprocess.CalledProcessError(1, ["docker", "inspect"], stderr=SECRET.encode())
        with patch.object(release.subprocess, "run", side_effect=error):
            with self.assertRaises(release.ReleaseError) as caught:
                tool.run(["docker", "inspect", "example"])
        self.assertNotIn(SECRET, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
