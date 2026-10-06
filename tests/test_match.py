import unittest

import helpers  # noqa: F401  (sets up import path)
from glab_teleport.match import (build_mapping, compare_refs, guess_layout, layout_target, pair_by_content,
                                 score_pair, sim)


class SimilarityTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(sim("payments", "payments"), 1.0)
        self.assertGreaterEqual(sim("web-shop", "shop_web"), 0.9)       # same words, different order
        self.assertGreaterEqual(sim("shop", "shop-team"), 0.75)         # longer name, same meaning
        self.assertEqual(sim("billing", "inventory"), 0.0)

    def test_pairs(self):
        self.assertGreaterEqual(score_pair("team/svc/api", "org/team/svc/api")[0], 90)     # prefix added
        self.assertGreaterEqual(score_pair("team/svc/api", "org/team/svc-api")[0], 85)     # subgroup folded into name
        self.assertIsNotNone(score_pair("team/svc/api", "org/team/api"))                   # subgroup dropped
        self.assertIsNone(score_pair("team/svc/api", "org/team/billing"))


class ContentTest(unittest.TestCase):
    def test_commits_beat_names(self):
        src = {"g/a/api": {"refs": {"refs/heads/main": "aaa"}}, "g/b/api": {"refs": {"refs/heads/main": "bbb"}}}
        dst = {"org/g/api": {"refs": {"refs/heads/main": "bbb"}}}
        self.assertEqual(pair_by_content(src, dst), {"g/b/api": ("org/g/api", 1)})

    def test_compare_refs(self):
        self.assertEqual(compare_refs({"refs/heads/main": "1"}, {"refs/heads/main": "1"})["state"], "identical")
        self.assertEqual(compare_refs({"refs/heads/main": "1"}, {})["state"], "empty")
        r = compare_refs({"refs/heads/main": "1", "refs/tags/v1": "2"}, {"refs/heads/main": "9", "refs/heads/x": "3"})
        self.assertEqual((r["state"], r["missing"], r["different"], r["extra"]),
                         ("outdated", ["refs/tags/v1"], ["refs/heads/main"], ["refs/heads/x"]))

    def test_layouts(self):
        self.assertEqual(layout_target("org/team", "svc/backend/api", "keep"), "org/team/svc/backend/api")
        self.assertEqual(layout_target("org/team", "svc/backend/api", "flat"), "org/team/api")
        self.assertEqual(layout_target("org/team", "svc/backend/api", "join"), "org/team/svc-backend-api")
        content = {"team/svc/api": ("org/team/api", 3), "team/svc/web": ("org/team/web", 2)}
        self.assertEqual(guess_layout(content, "team", "org/team"), ("flat", 2))

    def test_mapping_flags_different_code(self):
        src = [{"path_with_namespace": "team/api"}]
        dst = [{"path_with_namespace": "org/team/api"}]
        rows, unpaired = build_mapping(src, dst, {"team/api": {"refs": {"refs/heads/main": "1"}}},
                                       {"org/team/api": {"refs": {"refs/heads/main": "2"}}})
        self.assertEqual(rows[0]["match"], "different-code")


if __name__ == "__main__":
    unittest.main()
