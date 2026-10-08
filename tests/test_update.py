"""`glab-teleport update`: version checks, the npm upgrade and refreshing installed skill copies (no network, no real npm)."""
import argparse
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401
from glab_teleport import __version__, agents, term, update

SKILL = Path(update.__file__).resolve().parent / "skill"


def bump(v):
    a, b, c = update.parse(v)
    return f"{a}.{b}.{c + 1}"


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        self.env.start()
        term.RESULT = None
        term.set_json(True)

    def tearDown(self):
        self.env.stop()
        term.set_json(False)

    def run_update(self, latest, kind="npm", **kw):
        args = argparse.Namespace(**{"check": False, "yes": False, **kw})
        with mock.patch.object(update, "latest", return_value=latest), mock.patch.object(update, "install_kind", return_value=kind), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = update.run(args)
        return code, term.RESULT

    def test_parse_and_kind(self):
        self.assertGreater(update.parse("0.10.0"), update.parse("0.9.9"))
        self.assertEqual(update.parse("1.2.3-beta.1"), (1, 2, 3))
        self.assertEqual(update.install_kind("/usr/local/lib/node_modules/glab-teleport/lib"), "npm")
        self.assertEqual(update.install_kind("/home/u/.local/pipx/venvs/glab-teleport/lib/python3.12/site-packages"), "pipx")

    def test_up_to_date_refreshes_stale_skill(self):
        stale = self.home / ".claude" / "skills" / "glab-teleport"
        stale.mkdir(parents=True)
        (stale / "SKILL.md").write_text("old")
        self.run_update(__version__, check=True)                                        # --check never writes
        self.assertEqual((stale / "SKILL.md").read_text(), "old")
        code, res = self.run_update(__version__, yes=True)
        self.assertEqual((code, res["updated"]), (0, False))
        self.assertEqual(res["skills"], [{"path": str(stale), "changed": True}])
        self.assertEqual((stale / "SKILL.md").read_text(), (SKILL / "SKILL.md").read_text())
        self.assertFalse((self.home / ".agents" / "skills" / "glab-teleport").exists())     # never installs new places

    def test_json_needs_yes(self):
        with mock.patch.object(subprocess, "run") as sp:
            code, res = self.run_update(bump(__version__))
        sp.assert_not_called()
        self.assertEqual((code, res["available"], res["updated"]), (0, True, False))

    def test_npm_upgrade(self):
        new = bump(__version__)
        done = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(update, "npm_bin", return_value="/x/npm"), \
                mock.patch.object(subprocess, "run", return_value=done) as sp, \
                mock.patch.object(update, "_new_code", side_effect=[{"version": new}, {"installed": []}]):
            code, res = self.run_update(new, yes=True)
        self.assertEqual(sp.call_args[0][0][:4], ["/x/npm", "install", "-g", "glab-teleport@latest"])
        self.assertEqual((code, res["updated"], res["version"]), (0, True, new))

    def test_npm_failure_explains_permissions(self):
        failed = subprocess.CompletedProcess([], 243, "", "npm error code EACCES")
        with mock.patch.object(update, "npm_bin", return_value="/x/npm"), mock.patch.object(subprocess, "run", return_value=failed):
            with self.assertRaises(SystemExit) as ex:
                self.run_update(bump(__version__), yes=True)
        self.assertIn("nvm", str(ex.exception.code))

    def test_refresh_only_touches_installed_copies(self):
        agents.install(SKILL, self.home / ".agents" / "skills")
        self.assertEqual(agents.refresh(SKILL, self.home),
                         [{"path": str(self.home / ".agents" / "skills" / "glab-teleport"), "changed": False}])


if __name__ == "__main__":
    unittest.main()
