"""The bundled agent skill installs cleanly and stays consistent with the CLI."""
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import helpers  # noqa: F401
from glab_teleport import agents, cli, term

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
        self.assertTrue((Path(res["installed"][0]["path"]) / "SKILL.md").exists())

    def test_agent_folders(self):
        h = Path(tempfile.mkdtemp())
        claude, shared, oc = h / ".claude/skills", h / ".agents/skills", h / ".config/opencode/skills"
        self.assertEqual(agents.plan(agents.parse("all"), h), {claude: ["claude", "opencode"], shared: ["codex", "pi"]})
        self.assertEqual(agents.plan(["codex", "opencode"], h), {shared: ["codex", "opencode"]})
        self.assertEqual(agents.plan(["opencode"], h), {oc: ["opencode"]})
        agents.install(SKILL, shared)                                      # OpenCode reuses a copy it already reads
        self.assertEqual(agents.plan(["opencode"], h), {shared: ["opencode"]})
        self.assertFalse(agents.opencode_sees_twice(h))
        agents.install(SKILL, claude)
        self.assertTrue(agents.opencode_sees_twice(h))
        with self.assertRaises(SystemExit):
            agents.parse("cursor")

    def test_detect(self):
        h = Path(tempfile.mkdtemp())
        (h / ".codex").mkdir()
        self.assertEqual(agents.detect(h, which=lambda c: c == "pi"), ["codex", "pi"])
        self.assertEqual(agents.detect(h / "none", which=lambda c: None), [])


if __name__ == "__main__":
    unittest.main()
