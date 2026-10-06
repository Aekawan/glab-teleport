"""End to end: teleport a project between two in-memory GitLab servers backed by real git repositories."""
import argparse
import io
import json
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import helpers  # noqa: F401
from glab_teleport import config, run


class Server:
    """Just enough of the GitLab REST API for a teleport."""

    def __init__(self, root, token):
        self.root, self.token, self.next_id = Path(root), token, 100
        self.groups, self.projects, self.data = {}, {}, {}
        self.lock = threading.RLock()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self.handler())
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"

    def add_group(self, path):
        with self.lock:
            return self._add_group(path)

    def _add_group(self, path):
        self.next_id += 1
        parent = self.groups.get(path.rpartition("/")[0])
        g = {"id": self.next_id, "full_path": path, "path": path.rsplit("/", 1)[-1], "name": path.rsplit("/", 1)[-1],
             "parent_id": parent["id"] if parent else None, "kind": "group", "visibility": "private"}
        self.groups[path] = g
        return g

    def add_project(self, path, repo=None, **kw):
        with self.lock:
            return self._add_project(path, repo, **kw)

    def _add_project(self, path, repo=None, **kw):
        self.next_id += 1
        ns = self.groups[path.rpartition("/")[0]]
        repo = repo or self.root / f"{self.next_id}.git"
        if not Path(repo).exists():
            subprocess.run(["git", "init", "-q", "--bare", str(repo)], check=True)
        p = {"id": self.next_id, "path_with_namespace": path, "path": path.rsplit("/", 1)[1], "name": path.rsplit("/", 1)[1],
             "namespace": {"id": ns["id"], "full_path": ns["full_path"], "kind": "group"}, "http_url_to_repo": str(repo),
             "ssh_url_to_repo": str(repo), "visibility": "private", "default_branch": "main", "empty_repo": False,
             "wiki_enabled": False, "archived": False, "shared_runners_enabled": True, **kw}
        self.projects[p["id"]] = p
        return p

    def coll(self, key):
        return self.data.setdefault(key, [])

    def handler(self):
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, obj=None, headers=None):
                body = json.dumps(obj if obj is not None else {}).encode()
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def parse(self):
                u = urllib.parse.urlsplit(self.path)
                return urllib.parse.unquote(u.path[len("/api/v4"):]), dict(urllib.parse.parse_qsl(u.query))

            def body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n)) if n else {}

            def project(self, ref):
                if ref.isdigit():
                    return srv.projects.get(int(ref))
                return next((p for p in srv.projects.values() if p["path_with_namespace"] == ref), None)

            def do_GET(self):
                if self.headers.get("PRIVATE-TOKEN") != srv.token:
                    return self.send(401)
                path, qs = self.parse()
                if path == "/user":
                    return self.send(200, {"username": "tester", "is_admin": False})
                m = re.match(r"^/(projects|groups)/(\d+)/(variables|protected_branches|protected_tags|environments|runners|"
                             r"pipeline_schedules|hooks|deploy_keys|members)$", path)
                if m:
                    items = srv.coll((m[1], int(m[2]), m[3]))
                    return self.send(200, items, {"X-Next-Page": "", "X-Total": str(len(items))})
                m = re.match(r"^/groups/(\d+)/(projects|descendant_groups)$", path)
                if m:
                    g = next(g for g in srv.groups.values() if g["id"] == int(m[1]))
                    if m[2] == "projects":
                        out = [p for p in srv.projects.values() if p["path_with_namespace"].startswith(g["full_path"] + "/")]
                    else:
                        out = [x for k, x in srv.groups.items() if k.startswith(g["full_path"] + "/")]
                    return self.send(200, out, {"X-Next-Page": ""})
                m = re.match(r"^/(groups|namespaces)/(.+)$", path)
                if m and m[2] in srv.groups:
                    return self.send(200, srv.groups[m[2]])
                m = re.match(r"^/projects/(.+)$", path)
                if m and self.project(m[1]):
                    return self.send(200, self.project(m[1]))
                self.send(404)

            def do_POST(self):
                path, _ = self.parse()
                d = self.body()
                if path == "/groups":
                    parent = next(g for g in srv.groups.values() if g["id"] == d["parent_id"])
                    return self.send(201, srv.add_group(parent["full_path"] + "/" + d["path"]))
                if path == "/projects":
                    ns = next(g for g in srv.groups.values() if g["id"] == d["namespace_id"])
                    with srv.lock:
                        siblings = [p for p in srv.projects.values() if p["namespace"]["id"] == ns["id"]]
                        if any(p["name"].lower() == d["name"].lower() for p in siblings):   # like GitLab: names are unique per namespace
                            return self.send(400, {"message": {"project_namespace.name": ["has already been taken"],
                                                               "name": ["has already been taken"]}})
                        p = srv.add_project(ns["full_path"] + "/" + d["path"], empty_repo=True)
                        p["name"] = d["name"]
                    return self.send(201, p)
                m = re.match(r"^/(projects|groups)/(\d+)/(variables|protected_branches|protected_tags|environments)$", path)
                if m:
                    items = srv.coll((m[1], int(m[2]), m[3]))
                    if m[3] == "variables" and any(v["key"] == d["key"] and v["environment_scope"] == d["environment_scope"] for v in items):
                        return self.send(400, {"message": "taken"})
                    if m[3].startswith("protected"):
                        lv = lambda k: [{"access_level": d[k]}] if k in d else []
                        d = {"name": d["name"], "push_access_levels": lv("push_access_level"), "merge_access_levels": lv("merge_access_level"),
                             "create_access_levels": lv("create_access_level"), "allow_force_push": d.get("allow_force_push", False)}
                    items.append(d)
                    return self.send(201, d)
                self.send(404)

            def do_PUT(self):
                path, qs = self.parse()
                d = self.body()
                m = re.match(r"^/projects/(\d+)$", path)
                if m:
                    srv.projects[int(m[1])].update(d)
                    return self.send(200, srv.projects[int(m[1])])
                self.send(404)

            def do_DELETE(self):
                self.send(204)
        return H


@unittest.skipUnless(shutil.which("git"), "git not installed")
class TeleportEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        work = self.tmp / "work"
        g = lambda *a: subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *a], cwd=work, check=True, capture_output=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
        (work / "app.py").write_text("print('hi')\n")
        g("add", ".")
        g("commit", "-qm", "init")
        g("tag", "v1.0.0")
        g("checkout", "-qb", "develop")
        g("commit", "-q", "--allow-empty", "-m", "dev")
        src_repo = self.tmp / "src.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(work), str(src_repo)], check=True)

        self.src, self.dst = Server(self.tmp, "src-token"), Server(self.tmp, "dst-token")
        self.src.add_group("team")
        p = self.src.add_project("team/api", repo=src_repo)
        pid = p["id"]
        self.src.coll(("projects", pid, "variables")).extend([
            {"key": "DB_URL", "value": "postgres://prod", "environment_scope": "production", "variable_type": "env_var",
             "protected": True, "masked": False, "raw": False},
            {"key": "DB_URL", "value": "postgres://staging", "environment_scope": "staging", "variable_type": "env_var",
             "protected": False, "masked": False, "raw": False},
            {"key": "REPO", "value": f"{self.src.url}/team/api.git", "environment_scope": "*", "variable_type": "env_var",
             "protected": False, "masked": False, "raw": False}])
        self.src.coll(("projects", pid, "protected_branches")).append(
            {"name": "main", "push_access_levels": [{"access_level": 40}], "merge_access_levels": [{"access_level": 30}], "allow_force_push": False})
        self.src.coll(("projects", pid, "environments")).extend([{"name": "production"}, {"name": "staging"}])
        self.dst.add_group("org")
        config.write_json(config.CRED_FILE, {config.host_key(self.src.url): {"type": "pat", "token": "src-token"},
                                             config.host_key(self.dst.url): {"type": "pat", "token": "dst-token"}}, secret=True)

    def tearDown(self):
        self.src.srv.shutdown()
        self.dst.srv.shutdown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_project_teleport_and_report(self):
        args = argparse.Namespace(source_url=self.src.url, target_url=self.dst.url, insecure=False, only=None, dry_run=False,
                                  yes=True, layout="auto", include=None, exclude=None, relocate=False, with_parent_vars=False,
                                  rewrite_urls=True, overwrite=False, force_push=False, activate_schedules=False,
                                  allow_unmask=False, jobs=2, cache_dir=None)
        out = io.StringIO()
        with redirect_stdout(out):
            code = run.teleport(args, "project", ["team/api"], "org", ["repo", "env", "settings"])
        self.assertEqual(code, 0, out.getvalue())
        dst = next(p for p in self.dst.projects.values() if p["path_with_namespace"] == "org/api")
        src_refs = subprocess.run(["git", "ls-remote", self.src.projects[min(self.src.projects)]["http_url_to_repo"]],
                                  capture_output=True, text=True).stdout
        dst_refs = subprocess.run(["git", "ls-remote", dst["http_url_to_repo"]], capture_output=True, text=True).stdout
        refs = lambda text: sorted(line for line in text.splitlines() if "\trefs/" in line)   # HEAD is a server-side symref
        self.assertEqual(refs(src_refs), refs(dst_refs))
        vars_ = {(v["key"], v["environment_scope"]): v for v in self.dst.coll(("projects", dst["id"], "variables"))}
        self.assertEqual(set(vars_), {("DB_URL", "production"), ("DB_URL", "staging"), ("REPO", "*")})
        self.assertTrue(vars_[("DB_URL", "production")]["protected"])
        self.assertEqual(vars_[("REPO", "*")]["value"], f"{self.dst.url}/org/api.git")      # --rewrite-urls
        self.assertEqual([b["name"] for b in self.dst.coll(("projects", dst["id"], "protected_branches"))], ["main"])
        self.assertEqual({e["name"] for e in self.dst.coll(("projects", dst["id"], "environments"))}, {"production", "staging"})
        reports = sorted((config.WORK_DIR / "runs").glob("*/report.json"))
        rep = json.loads(reports[-1].read_text())
        self.assertEqual(rep["counts"]["ok"], 1, json.dumps(rep["projects"], indent=1)[:2000])
        self.assertEqual(rep["scopes"], {"*": [1, 1], "production": [1, 1], "staging": [1, 1]})
        md = (reports[-1].parent / "report.md").read_text()
        self.assertNotIn("postgres://", md)                                                      # values never leak

        out = io.StringIO()
        with redirect_stdout(out):                                                                # second run: nothing to do
            run.teleport(args, "project", ["team/api"], "org", ["repo", "env", "settings"])
        self.assertIn("in sync", out.getvalue())


    def test_flat_group_with_duplicate_names(self):
        """Two subgroups each holding a project called `config` must both arrive when subgroups are dropped."""
        for sub in ("team/a", "team/b"):
            self.src.add_group(sub)
            work = self.tmp / ("w-" + sub.replace("/", "-"))
            subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
            subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", sub],
                           cwd=work, check=True)
            bare = self.tmp / (sub.replace("/", "-") + ".git")
            subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
            self.src.add_project(sub + "/config", repo=bare)
        self.dst.add_group("org/team")
        args = argparse.Namespace(source_url=self.src.url, target_url=self.dst.url, insecure=False, only=None, dry_run=False,
                                  yes=True, layout="flat", include=None, exclude=None, relocate=False, with_parent_vars=False,
                                  rewrite_urls=False, overwrite=False, force_push=False, activate_schedules=False,
                                  allow_unmask=False, jobs=3, cache_dir=None)
        out = io.StringIO()
        with redirect_stdout(out):
            code = run.teleport(args, "group", ["team"], "org/team", ["repo"])
        self.assertEqual(code, 0, out.getvalue())
        created = sorted(p["path_with_namespace"] for p in self.dst.projects.values() if p["path_with_namespace"].startswith("org/team/"))
        self.assertEqual(len(created), 3, created)                       # api + two configs
        names = [p["name"] for p in self.dst.projects.values() if p["path_with_namespace"].startswith("org/team/")]
        self.assertEqual(len(names), len(set(n.lower() for n in names)))


if __name__ == "__main__":
    unittest.main()
