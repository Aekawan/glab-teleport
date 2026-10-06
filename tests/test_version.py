"""package.json and the Python package must always report the same version."""
import json
import unittest
from pathlib import Path

import helpers  # noqa: F401
from glab_teleport import __version__


class VersionTest(unittest.TestCase):
    def test_versions_match(self):
        pkg = json.loads((Path(__file__).resolve().parent.parent / "package.json").read_text())
        self.assertEqual(pkg["version"], __version__)


if __name__ == "__main__":
    unittest.main()
