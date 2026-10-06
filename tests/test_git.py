"""Branch/tag transfer against local bare repositories."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
from glab_teleport.gitlab import GitLab, ls_remote, sync_repo


def git(*args, cwd=None):
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class SyncRepoTest(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        work = self.d / "work"
        git("init", "-q", "-b", "main", str(work))
        (work / "f").write_text("a")
        git("add", "f", cwd=work)
        git("commit", "-qm", "a", cwd=work)
        git("tag", "v1", cwd=work)
        git("checkout", "-qb", "develop", cwd=work)
        git("commit", "-q", "--allow-empty", "-m", "b", cwd=work)
        git("clone", "-q", "--bare", str(work), str(self.d / "src.git"))
        git("init", "-q", "--bare", str(self.d / "dst.git"))
        self.src, self.dst = str(self.d / "src.git"), str(self.d / "dst.git")
        self.a, self.b = GitLab("source", "https://source.example.com", "x"), GitLab("target", "https://target.example.com", "y")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_copy_all_refs_and_rerun(self):
        r = sync_repo(self.a, self.b, self.src, self.dst, self.d / "cache.git")
        self.assertEqual((r["branches"], r["tags"], r["rejected"], r["mismatched"]), (2, 1, [], []))
        self.assertEqual(ls_remote(self.a, self.src), ls_remote(self.b, self.dst))
        r = sync_repo(self.a, self.b, self.src, self.dst, self.d / "cache.git")
        self.assertEqual(r["rejected"], [])

    def test_divergence_is_not_overwritten_without_force(self):
        sync_repo(self.a, self.b, self.src, self.dst, self.d / "cache.git")
        other = self.d / "other"
        git("clone", "-q", "-b", "develop", self.dst, str(other))
        git("commit", "-q", "--allow-empty", "-m", "target-only", cwd=other)
        git("push", "-q", "origin", "develop", cwd=other)
        work = self.d / "work"
        git("commit", "-q", "--allow-empty", "-m", "source-only", cwd=work)
        git("push", "-q", self.src, "develop", cwd=work)
        r = sync_repo(self.a, self.b, self.src, self.dst, self.d / "cache.git")
        self.assertEqual(r["rejected"], ["refs/heads/develop"])
        r = sync_repo(self.a, self.b, self.src, self.dst, self.d / "cache.git", force=True)
        self.assertEqual((r["rejected"], r["mismatched"]), ([], []))

    def test_scrub_hides_tokens(self):
        self.assertNotIn("y", self.b.scrub(f"auth {self.b.basic} {self.b.token}").replace("auth", ""))


if __name__ == "__main__":
    unittest.main()
