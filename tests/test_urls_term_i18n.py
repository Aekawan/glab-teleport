import io
import unittest
from contextlib import redirect_stdout

import helpers  # noqa: F401
from glab_teleport import term
from glab_teleport.i18n import set_lang, t
from glab_teleport.urls import UrlRewriter

OLD = "https://gitlab.old.example.com"


class RewriterTest(unittest.TestCase):
    def setUp(self):
        self.rw = UrlRewriter(OLD, "https://git.new.example.org/", "ssh://git@git.new.example.org:2222/",
                              {"team/svc/api": "org/team/api"})

    def test_forms(self):
        cases = {
            f"{OLD}/team/svc/api.git": "https://git.new.example.org/org/team/api.git",
            "git@gitlab.old.example.com:team/svc/api.git": "ssh://git@git.new.example.org:2222/org/team/api.git",
            "ssh://git@gitlab.old.example.com:22/team/svc/api.git": "ssh://git@git.new.example.org:2222/org/team/api.git",
            "https://gitlab-ci-token:${CI_JOB_TOKEN}@gitlab.old.example.com/team/svc/api.git":
                "https://gitlab-ci-token:${CI_JOB_TOKEN}@git.new.example.org/org/team/api.git",
            f"{OLD}/team/svc/api/-/blob/main/README.md": "https://git.new.example.org/org/team/api/-/blob/main/README.md",
        }
        for old, new in cases.items():
            self.assertEqual(self.rw.rewrite(old)[0], new, old)

    def test_leaves_unknown_and_flags_leftovers(self):
        text = "image: registry.gitlab.old.example.com/team/svc/api:1.0"
        self.assertEqual(self.rw.rewrite(text), (text, 0))
        self.assertTrue(self.rw.mentions_old(text))
        self.assertFalse(self.rw.mentions_old("https://newgitlab.old.example.com/x"))  # not the same host


class TermTest(unittest.TestCase):
    def test_thai_width(self):
        self.assertEqual(term.width("ย้ายที่"), 4)        # combining vowels/tones take no column
        self.assertEqual(term.width("สร้างใหม่"), 7)
        self.assertLessEqual(term.width(term.fit("ตรวจสอบตัวแปรทั้งหมด", 6)), 6)

    def test_fit_path_keeps_project_name(self):
        out = term.fit_path("team/backend/service/payments-api", 24)
        self.assertTrue(out.endswith("payments-api"))
        self.assertLessEqual(term.width(out), 24)

    def test_table_never_truncates_when_piped(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            term.table(["A", "B"], [("x" * 150, "y")])
        self.assertIn("x" * 150, buf.getvalue())


class I18nTest(unittest.TestCase):
    def tearDown(self):
        set_lang("en")

    def test_switch(self):
        set_lang("th")
        self.assertEqual(t("Source", "ต้นทาง"), "ต้นทาง")
        set_lang("xx")
        self.assertEqual(t("Source", "ต้นทาง"), "Source")

    def test_help_renders_in_both_languages(self):
        from glab_teleport import cli
        for code in ("en", "th"):
            set_lang(code)
            buf = io.StringIO()
            with redirect_stdout(buf):
                cli.parser().print_help()
            self.assertIn("glab-teleport", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
