"""ci-update: one branch + commit + MR per project, nothing touched without --yes, re-runs are no-ops."""
import io
import json
import os
import re
import threading
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile

import helpers  # noqa: F401
from glab_teleport import cli, term
from glab_teleport.ci import DEFAULT, SAME_AS_INTO, command_line, render

NEW = "include:\n  - project: 'infra/ci-templates'\n    file: 'templates/deploy-aws.yml'\n\nvariables:\n  APPROVERS: \"a@x.com\"\n"


class Server:
    def __init__(self):
        self.group = {"id": 1, "full_path": "apps"}
        self.projects = {}
        self.files, self.branches, self.mrs, self.commits = {}, {}, [], []
        self.extra = {}         # pid -> branch names besides main
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"

    def add(self, pid, path, ci=None, **kw):
        self.projects[pid] = {"id": pid, "path_with_namespace": path, "default_branch": "main", "archived": False, "empty_repo": False, **kw}
        if ci is not None:
            self.files[(pid, "main")] = ci

    def handler(self):
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, obj=None, raw=None):
                body = raw.encode() if raw is not None else json.dumps(obj if obj is not None else {}).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                p, qs = urllib.parse.unquote(u.path[len("/api/v4"):]), dict(urllib.parse.parse_qsl(u.query))
                if p == "/user":
                    return self.send(200, {"username": "tester"})
                if p == "/groups/" + srv.group["full_path"]:
                    return self.send(200, srv.group)
                if p == "/groups":
                    return self.send(200, [srv.group] if qs.get("search", "") in srv.group["full_path"] else [])
                if p == "/projects":
                    return self.send(200, [x for x in srv.projects.values() if qs.get("search", "") in x["path_with_namespace"]])
                if p == "/groups/1/projects":
                    return self.send(200, list(srv.projects.values()))
                if p == "/groups/1/descendant_groups":
                    return self.send(200, [])
                m = re.match(r"/projects/(\d+)/repository/files/(.+)/raw$", p)
                if m:
                    text = srv.files.get((int(m[1]), qs["ref"]))
                    return self.send(200, raw=text) if text is not None else self.send(404)
                m = re.match(r"/projects/(\d+)/merge_requests$", p)
                if m:
                    return self.send(200, [r for r in srv.mrs if r["pid"] == int(m[1]) and r["source_branch"] == qs["source_branch"]
                                           and r["state"] == "opened"])
                m = re.match(r"/projects/(\d+)/repository/branches$", p)
                if m:
                    return self.send(200, [{"name": b} for b in ["main", *srv.extra.get(int(m[1]), [])]])
                m = re.match(r"/projects/(\d+)/repository/branches/(.+)$", p)
                if m:
                    return self.send(200, {}) if (int(m[1]), m[2]) in srv.branches else self.send(404)
                m = re.match(r"/projects/(.+)$", p)
                if m:
                    pr = next((x for x in srv.projects.values() if x["path_with_namespace"] == m[1]), None)
                    return self.send(200, pr) if pr else self.send(404)
                self.send(404)

            def do_DELETE(self):
                m = re.match(r"/api/v4/projects/(\d+)/repository/branches/(.+)$", urllib.parse.unquote(self.path))
                srv.branches.pop((int(m[1]), m[2]), None)
                self.send(204)

            def do_POST(self):
                d = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                pid = int(re.match(r"/api/v4/projects/(\d+)/", self.path)[1])
                if self.path.endswith("/repository/commits"):
                    srv.commits.append((pid, d))
                    srv.branches[(pid, d["branch"])] = True
                    srv.files[(pid, d["branch"])] = d["actions"][0]["content"]
                    return self.send(201, {"id": "abc"})
                mr = {"pid": pid, "iid": len(srv.mrs) + 1, "state": "opened", "source_branch": d["source_branch"],
                      "web_url": f"{srv.url}/mr/{len(srv.mrs) + 1}"}
                srv.mrs.append(mr)
                self.send(201, mr)
        return H


def cli_json(*argv):
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        try:
            cli.main([*argv, "--json"])
        except SystemExit as e:
            code = e.code
    term.set_json(False)
    term.RESULT = None
    return code, json.loads(out.getvalue())


class CiUpdateTest(unittest.TestCase):
    def setUp(self):
        self.api = Server()
        self.api.add(10, "apps/web", "old: 1\nvariables:\n  APPROVERS: \"web@x.com\"\n")
        self.api.add(11, "apps/api", NEW)
        self.api.add(12, "apps/docs")
        self.api.add(13, "apps/old", "x: 1\n", archived=True)
        self.api.add(14, "apps/skipme", "x: 1\n")
        self.file = Path(tempfile.mkdtemp()) / "gitlab-ci.yml"
        self.file.write_text(NEW)
        os.environ["GLAB_TELEPORT_TARGET_TOKEN"] = "t"

    def tearDown(self):
        self.api.srv.shutdown()
        self.api.srv.server_close()
        os.environ.pop("GLAB_TELEPORT_TARGET_TOKEN", None)

    def run_ci(self, *extra):
        return cli_json("ci-update", str(self.file), "apps", "--target-url", self.api.url, "--exclude", "apps/skipme", *extra)

    def states(self, res):
        return {r["project"]: r["state"] for r in res["projects"]}

    def test_plan_changes_nothing_without_yes(self):
        code, res = self.run_ci()
        self.assertEqual(code, 0)
        self.assertEqual(self.states(res), {"apps/web": "update", "apps/api": "same", "apps/docs": "skip", "apps/old": "skip"})
        self.assertEqual((self.api.commits, self.api.mrs), ([], []))
        self.assertIn("next", res)

    def test_apply_opens_one_mr_and_rerun_is_noop(self):
        code, res = self.run_ci("--yes", "--all", "--keep-var", "APPROVERS")
        self.assertEqual(code, 0, res)
        self.assertEqual(len(res["opened"]), 2)
        pushed = {pid: d for pid, d in self.api.commits}
        self.assertEqual(pushed[10]["actions"][0]["action"], "update")
        self.assertIn('APPROVERS: "web@x.com"', pushed[10]["actions"][0]["content"])
        self.assertEqual(pushed[12]["actions"][0]["action"], "create")
        self.assertEqual(pushed[12]["start_branch"], "main")
        code, res = self.run_ci("--yes", "--all")
        self.assertEqual(self.states(res)["apps/web"], "open")
        self.assertEqual(len(self.api.mrs), 2)

    def test_short_path_resolves_to_the_one_match(self):
        self.api.group["full_path"] = "org/apps"
        for p in self.api.projects.values():
            p["path_with_namespace"] = "org/" + p["path_with_namespace"]
        code, res = cli_json("ci-update", str(self.file), "apps", "--target-url", self.api.url)
        self.assertEqual((code, res["targets"]), (0, ["org/apps"]))
        self.assertEqual(self.states(res)["org/apps/web"], "update")
        code, res = cli_json("ci-update", str(self.file), "nope", "--target-url", self.api.url)
        self.assertEqual((code, res["ok"]), (1, False))
        self.assertIn("'nope'", res["error"])

    def test_no_targets_without_a_terminal_explains(self):
        code, res = cli_json("ci-update", str(self.file), "--target-url", self.api.url)
        self.assertEqual(code, 1)
        self.assertIn("ci-update <", res["error"])

    def test_into_and_from_pick_the_first_branch_each_project_has(self):
        self.api.extra = {10: ["develop", "staging"]}
        self.api.files[(10, "develop")] = "old: 2\n"
        self.api.files[(10, "staging")] = self.api.files[(11, "staging")] = "x: 1\n"
        code, res = self.run_ci("--into", "develop,@default")
        rows = {r["project"]: r for r in res["projects"]}
        self.assertEqual((rows["apps/web"]["into"], rows["apps/web"]["from"]), ("develop", "develop"))
        self.assertEqual((rows["apps/api"]["into"], rows["apps/api"]["state"]), ("main", "same"))
        code, res = self.run_ci("--into", "develop", "--from", "staging", "--yes")
        rows = {r["project"]: r for r in res["projects"]}
        self.assertEqual((rows["apps/web"]["from"], rows["apps/web"]["into"]), ("staging", "develop"))
        self.assertIn("no branch develop", rows["apps/api"]["note"])
        pid, commit = self.api.commits[0]
        self.assertEqual((pid, commit["start_branch"]), (10, "staging"))

    def test_command_line_repeats_the_choices(self):
        cmd = lambda into, start: command_line("ci.yml", ["apps"], "target", {"into": into, "from": start})
        self.assertEqual(cmd([DEFAULT], [SAME_AS_INTO]), "glab-teleport ci-update ci.yml apps")
        self.assertEqual(cmd(["develop"], ["develop"]), "glab-teleport ci-update ci.yml apps --into develop")
        self.assertEqual(cmd(["develop"], ["main"]), "glab-teleport ci-update ci.yml apps --from main --into develop")
        self.assertEqual(cmd(["develop"], [DEFAULT]), "glab-teleport ci-update ci.yml apps --from @default --into develop")

    def test_render_keeps_project_value(self):
        self.assertIn('APPROVERS: "me@x.com"', render(NEW, 'variables:\n  APPROVERS: "me@x.com"\n', ["APPROVERS"]))
        self.assertEqual(render(NEW, None, ["APPROVERS"]), NEW)


if __name__ == "__main__":
    unittest.main()
