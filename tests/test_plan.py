"""Planning a group teleport: layouts, reuse by commits, relocation, name clashes, pending deletions."""
import unittest

import helpers  # noqa: F401
from glab_teleport import plan as planmod
from glab_teleport.gitlab import GitLab
from glab_teleport.plan import MOVE, NEW, SKIP, SYNCED, plan_group


def proj(i, path, empty=False):
    return {"id": i, "path_with_namespace": path, "path": path.rsplit("/", 1)[1], "name": path.rsplit("/", 1)[1],
            "http_url_to_repo": f"https://x.example.com/{path}.git", "empty_repo": empty, "namespace": {"full_path": path.rsplit("/", 1)[0]}}


class FakeClient(GitLab):
    def __init__(self, url, groups, projects):
        super().__init__("x", url, "t")
        self.user = {"id": 1, "username": "tester", "is_admin": True}
        self.groups = {g: {"id": n, "full_path": g, "name": g.rsplit("/", 1)[-1]} for n, g in enumerate(groups, 1)}
        self.projects = projects

    def group(self, path):
        return self.groups.get(path)

    def get_all(self, path, **params):
        gid = int(path.split("/")[2])
        root = next(g for g in self.groups.values() if g["id"] == gid)["full_path"]
        if path.endswith("/projects"):
            return [p for p in self.projects if p["path_with_namespace"].startswith(root + "/")]
        return [g for p, g in self.groups.items() if p.startswith(root + "/")]


class Session:
    jobs = 1

    def __init__(self, src, dst):
        self.src, self.dst = src, dst


SRC = FakeClient("https://source.example.com", ["team", "team/chat", "team/chat/backend", "team/walk", "team/walk/backend"], [
    proj(1, "team/chat/backend/api"), proj(2, "team/chat/backend/walk"), proj(3, "team/walk/backend/app"),
    proj(4, "team/design"), proj(5, "team/old-deleted-158")])
DST = FakeClient("https://target.example.com", ["org", "org/team"], [
    proj(11, "org/team/api"), proj(12, "org/team/walk")])          # flattened copies made earlier by someone
REFS = {"team/chat/backend/api": {"refs": {"refs/heads/main": "a1"}}, "team/chat/backend/walk": {"refs": {"refs/heads/main": "w1"}},
        "team/walk/backend/app": {"refs": {"refs/heads/main": "x1"}}, "team/design": {"refs": {"refs/heads/main": "d1"}},
        "org/team/api": {"refs": {"refs/heads/main": "a1"}}, "org/team/walk": {"refs": {"refs/heads/main": "w1"}}}


class PlanTest(unittest.TestCase):
    def setUp(self):
        self._orig = planmod.fetch_refs
        planmod.fetch_refs = lambda gl, projects, jobs=8: {p["path_with_namespace"]: REFS[p["path_with_namespace"]]
                                                           for p in projects if p["path_with_namespace"] in REFS}

    def tearDown(self):
        planmod.fetch_refs = self._orig

    def states(self, **kw):
        p = plan_group(Session(SRC, DST), "team", "org/team", **kw)
        return p, {i["source"]: (i["state"], i["target"]) for i in p["items"]}

    def test_default_keeps_source_structure(self):
        p, st = self.states()
        self.assertEqual(p["layout"], "keep")

    def test_auto_follows_target_and_skips_pending_deletion(self):
        p, st = self.states(layout="auto")
        self.assertEqual(p["layout"], "flat")
        self.assertNotIn("team/old-deleted-158", st)
        self.assertEqual(st["team/chat/backend/api"], (SYNCED, "org/team/api"))     # found by commits
        self.assertEqual(st["team/design"], (NEW, "org/team/design"))

    def test_keep_detects_group_project_clash(self):
        _, st = self.states(layout="keep")
        # subgroup org/team/walk can't be created while project org/team/walk stays there
        self.assertEqual(st["team/walk/backend/app"][0], SKIP)
        self.assertEqual(st["team/chat/backend/api"], (SYNCED, "org/team/api"))

    def test_keep_with_relocate_moves_existing_and_resolves_clash(self):
        p, st = self.states(layout="keep", relocate=True)
        self.assertEqual(st["team/chat/backend/walk"], (MOVE, "org/team/chat/backend/walk"))
        self.assertEqual(st["team/walk/backend/app"], (NEW, "org/team/walk/backend/app"))
        moved = next(i for i in p["items"] if i["source"] == "team/chat/backend/walk")
        self.assertEqual(moved["move_from"], "org/team/walk")


if __name__ == "__main__":
    unittest.main()
