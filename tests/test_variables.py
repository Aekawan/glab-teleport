"""Variables must be copied and verified exactly per (key, environment_scope) — the core promise of the tool."""
import tempfile
import unittest

from helpers import FakeGitLab
from glab_teleport.gitlab import GitLab
from glab_teleport.transfer import Session, copy_variables, load_vars
from glab_teleport.verify import compare_vars


def var(key, value, scope="*", **kw):
    return {"key": key, "value": value, "environment_scope": scope, "variable_type": "env_var",
            "protected": False, "masked": False, "raw": False, **kw}


class VariablesTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitLab()
        self.api.vars[("projects", 1)] = [
            var("DB_URL", "postgres://prod", "production", protected=True),
            var("DB_URL", "postgres://staging", "staging"),
            var("DB_URL", "postgres://dev"),
            var("API_KEY", "abcdefgh12345678", "review/*", masked=True, raw=True),
            var("KUBECONFIG", "apiVersion: v1\n", variable_type="file", protected=True),
            var("SECRET", None, hidden=True, masked=True),
        ] + [var(f"V{i}", str(i)) for i in range(140)]          # > 100 forces pagination
        self.api.vars[("projects", 2)] = [var("DB_URL", "postgres://dev"), var("DB_URL", "WRONG", "staging")]
        src = GitLab("source", self.api.url, "src-token")
        dst = GitLab("target", self.api.url, "dst-token")
        self.s = Session(src, dst, {}, tempfile.mkdtemp())

    def tearDown(self):
        self.api.close()

    def test_copy_then_verify(self):
        src_vars = load_vars(self.s.src, "/projects/1")
        self.assertEqual(len(src_vars), 146)
        st = copy_variables(self.s, src_vars, "/projects/2")
        self.assertEqual((st["created"], st["same"], len(st["differs"]), len(st["hidden"])), (143, 1, 1, 1))
        rows, scopes = compare_vars(self.s, src_vars, load_vars(self.s.dst, "/projects/2"))
        status = {(r["key"], r["scope"]): r["status"] for r in rows}
        self.assertEqual(status[("DB_URL", "staging")], "fail")            # differs and was not overwritten
        self.assertEqual(status[("DB_URL", "production")], "ok")
        self.assertEqual(status[("API_KEY", "review/*")], "ok")
        self.assertEqual(status[("SECRET", "*")], "fail")                  # hidden: must be created manually
        self.assertEqual(scopes["staging"], [1, 1])

        self.s.opts["overwrite"] = True
        st = copy_variables(self.s, src_vars, "/projects/2")
        self.assertEqual(st["updated"], 1)
        rows, _ = compare_vars(self.s, src_vars, load_vars(self.s.dst, "/projects/2"))
        self.assertEqual([r for r in rows if r["status"] not in ("ok",)][0]["key"], "SECRET")

    def test_rerun_is_idempotent(self):
        src_vars = load_vars(self.s.src, "/projects/1")
        copy_variables(self.s, src_vars, "/projects/3")
        st = copy_variables(self.s, src_vars, "/projects/3")
        self.assertEqual(st["created"], 0)
        self.assertEqual(st["same"], 145)

    def test_flags_are_compared(self):
        expected = {("A", "*"): var("A", "1", protected=True)}
        actual = {("A", "*"): var("A", "1", protected=False)}
        rows, _ = compare_vars(self.s, expected, actual)
        self.assertEqual(rows[0]["status"], "fail")
        self.assertIn("protected", rows[0]["detail"])


if __name__ == "__main__":
    unittest.main()
