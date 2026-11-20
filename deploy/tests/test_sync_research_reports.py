"""Git and atomic content publishing checks; no production services or network."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_release import ResearchRelease, ReleaseError
from sync_research_reports import ResearchSync, checked_snapshot


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout.decode().strip()


def snapshot(root, number):
    snapshot_id = "s" + str(number) * 32
    content = b"<html>company research</html>"
    record = {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content), "media_type": "text/html"}
    manifest = {"snapshot_id": snapshot_id, "generated_at": "2026-11-20T00:00:00Z",
                "files": {"CO/main.html": record}, "originals": {"CO/main.html": record},
                "reports": {"co-main": {}}, "totals": {"companies": 1, "reports": 1},
                "source": {"index_path": "index.json"}, "excluded": []}
    target = root / "snapshots" / snapshot_id
    for directory in ("content", "originals"):
        path = target / directory / "CO/main.html"
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
    (target / "manifest.json").write_text(json.dumps(manifest))
    (root / "current.json").write_text(json.dumps({"snapshot_id": snapshot_id}))
    return manifest


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="research-sync-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "live"
        self.candidate = self.root / "candidate"
        self.old = snapshot(self.output, 1)
        self.new = snapshot(self.candidate, 2)
        self.tool = ResearchSync(ResearchRelease(self.root), self.root / "source")
        self.before = (self.output / "current.json").read_bytes()

    def test_publish_keeps_root_and_historical_generation(self):
        inode = self.output.stat().st_ino

        def verify():
            return checked_snapshot(self.output)["snapshot_id"]

        self.assertEqual(self.tool.publish(self.candidate, self.output, self.new, verify), self.new["snapshot_id"])
        self.assertEqual(self.output.stat().st_ino, inode)
        self.assertTrue((self.output / "snapshots" / self.old["snapshot_id"]).is_dir())
        self.assertEqual((self.output / "current.json").stat().st_mode & 0o777, 0o644)

    def test_failed_live_verification_restores_exact_pointer(self):
        def fail():
            raise ReleaseError("Web cannot read snapshot")

        with self.assertRaisesRegex(ReleaseError, "已恢复"):
            self.tool.publish(self.candidate, self.output, self.new, fail)
        self.assertEqual((self.output / "current.json").read_bytes(), self.before)
        self.assertEqual(checked_snapshot(self.output)["snapshot_id"], self.old["snapshot_id"])

    def test_failed_verification_does_not_overwrite_another_publisher(self):
        def concurrent_change():
            (self.output / "current.json").write_bytes(b'{"snapshot_id":"another-publisher"}')
            raise ReleaseError("failed")

        with self.assertRaisesRegex(ReleaseError, "未覆盖其他发布"):
            self.tool.publish(self.candidate, self.output, self.new, concurrent_change)
        self.assertIn(b"another-publisher", (self.output / "current.json").read_bytes())

    def test_identical_snapshot_can_be_reused_but_corruption_is_rejected(self):
        self.tool.publish(self.candidate, self.output, self.new, lambda: None)
        self.tool.publish(self.candidate, self.output, self.new, lambda: checked_snapshot(self.output))
        (self.output / "snapshots" / self.new["snapshot_id"] / "originals/CO/main.html").write_bytes(b"tampered")
        pointer = (self.output / "current.json").read_bytes()
        with self.assertRaisesRegex(ReleaseError, "哈希"):
            self.tool.publish(self.candidate, self.output, self.new, lambda: None)
        self.assertEqual((self.output / "current.json").read_bytes(), pointer)

    def test_symlinked_snapshot_file_is_rejected(self):
        target = self.candidate / "snapshots" / self.new["snapshot_id"] / "content/CO/main.html"
        external = self.root / "external.html"
        external.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(external)
        with self.assertRaisesRegex(ReleaseError, "符号链接"):
            checked_snapshot(self.candidate)

    def test_unexpected_missing_company_html_blocks_publication(self):
        self.tool.source.mkdir()
        (self.tool.source / "index.json").write_text(json.dumps({"reports": [
            {"report_id": "co-main", "entity_id": "company:co", "status": "draft",
             "report_type": "company_research", "entry_path": "CO/main.html"},
            {"report_id": "demo", "entity_id": None, "status": "ready",
             "report_type": "demo", "entry_path": "demo.html"}]}))
        self.tool.check_selection(self.new)
        incomplete = copy.deepcopy(self.new)
        incomplete["reports"] = {}
        incomplete["excluded"] = [{"report_id": "co-main", "reason": "missing file"}]
        with self.assertRaisesRegex(ReleaseError, "co-main"):
            self.tool.check_selection(incomplete)
        self.assertEqual((self.output / "current.json").read_bytes(), self.before)


class GitSyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="research-git-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote.git"
        self.author = self.root / "author"
        self.local = self.root / "source"
        git(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        git(self.root, "clone", str(self.remote), str(self.author))
        self.configure(self.author)
        self.commit(self.author, "base")
        git(self.author, "push", "-u", "origin", "main")
        git(self.root, "clone", str(self.remote), str(self.local))
        self.configure(self.local)
        self.tool = ResearchSync(ResearchRelease(self.root), self.local)

    def configure(self, root):
        git(root, "config", "user.name", "Test")
        git(root, "config", "user.email", "test@example.invalid")

    def commit(self, root, text):
        (root / "report.html").write_text(text)
        git(root, "add", "report.html")
        git(root, "commit", "-m", text)

    def advance_remote(self):
        self.commit(self.author, "updated")
        git(self.author, "push")

    def test_pull_fast_forwards_current_upstream(self):
        self.advance_remote()
        before, after = self.tool.pull()
        self.assertEqual(before["branch"], "main")
        self.assertEqual(after["upstream"], "origin/main")
        self.assertNotEqual(before["commit"], after["commit"])
        self.assertEqual((self.local / "report.html").read_text(), "updated")

    def test_untracked_and_modified_files_are_preserved(self):
        (self.local / "notes.txt").write_text("uncommitted")
        original = self.tool.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(ReleaseError, "未提交"):
            self.tool.pull()
        self.assertEqual(self.tool.git("rev-parse", "HEAD"), original)
        self.assertEqual((self.local / "notes.txt").read_text(), "uncommitted")

    def test_diverged_and_local_ahead_branches_are_never_reset(self):
        self.commit(self.local, "local-only")
        original = self.tool.git("rev-parse", "HEAD")
        for diverged in (False, True):
            if diverged:
                self.advance_remote()
            with self.assertRaisesRegex(ReleaseError, "未推送提交或与上游分叉"):
                self.tool.pull()
            self.assertEqual(self.tool.git("rev-parse", "HEAD"), original)

    def test_failed_fetch_preserves_source_and_does_not_print_remote(self):
        git(self.local, "remote", "set-url", "origin", str(self.root / "secret-does-not-exist"))
        original = self.tool.git("rev-parse", "HEAD")
        with self.assertRaises(ReleaseError) as caught:
            self.tool.pull()
        self.assertNotIn("secret-does-not-exist", str(caught.exception))
        self.assertEqual(self.tool.git("rev-parse", "HEAD"), original)


if __name__ == "__main__":
    unittest.main()
