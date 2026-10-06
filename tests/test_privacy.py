"""The published package must not contain real hostnames — only example domains and well-known public hosts."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWED = ("example.com", "example.org", "example.net", "localhost", "127.0.0.1", "gitlab.com", "docs.gitlab.com",
           "npmjs.com", "www.npmjs.com", "registry.npmjs.org", "nodejs.org", "python.org", "www.python.org", "pypi.org",
           "github.com", "img.shields.io", "keepachangelog.com", "semver.org", "opensource.org", "brew.sh",
           "agentskills.io")
HOST = re.compile(r"(?:https?|ssh)://(?:[^@/\s'\"`]+@)?([a-z0-9.-]+\.[a-z]{2,}|localhost|127\.0\.0\.1)", re.I)


class PrivacyTest(unittest.TestCase):
    def test_only_example_hosts(self):
        files = [p for p in ROOT.rglob("*") if p.is_file() and p.suffix in (".py", ".js", ".json", ".md", ".toml", ".txt", ".sh")
                 and "node_modules" not in p.parts and ".git" not in p.parts]
        leaks = []
        for f in files:
            for m in HOST.finditer(f.read_text(errors="ignore")):
                host = m.group(1).lower().rstrip(".")
                if not any(host == a or host.endswith("." + a) for a in ALLOWED):
                    leaks.append(f"{f.relative_to(ROOT)}: {host}")
        self.assertEqual(leaks, [], "non-example hostnames found")


if __name__ == "__main__":
    unittest.main()
