"""The bundled agent skill installs cleanly and stays consistent with the CLI."""
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import helpers  # noqa: F401
from glab_teleport import cli, term

SKILL = Path(__file__).resolve().parent.parent / "lib" / "glab_teleport" / "skill"


class SkillTest(unittest.TestCase):
    def test_frontmatter(self):
        text = (SKILL / "SKILL.md").read_text()
        m = re.match(r"^---\nname: (.+)\ndescription: (.+?)\n---\n", text, re.S)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1).strip(), "glab-teleport")
        self.assertLess(len(text.splitlines()), 500)
        self.assertTrue((SKILL / "references" / "json.md").exists())

    def test_commands_in_skill_exist(self):
        commands = set(re.findall(r"glab-teleport ([a-z][a-z-]*)", (SKILL / "SKILL.md").read_text()))
        known = set(cli.COMMANDS) | {"login", "ui"}
        self.assertEqual(commands - known - {"report"}, set())

    def test_install(self):
        d = tempfile.mkdtemp()
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["skill", "install", "--dir", d, "--json"])
        term.set_json(False)
        res = json.loads(out.getvalue())
        self.assertTrue((Path(res["installed"]) / "SKILL.md").exists())


if __name__ == "__main__":
    unittest.main()
