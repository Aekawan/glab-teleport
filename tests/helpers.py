import json
import os
import re
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
os.environ.setdefault("GLAB_TELEPORT_CONFIG_DIR", tempfile.mkdtemp(prefix="glt-cfg-"))
os.environ.setdefault("GLAB_TELEPORT_HOME", tempfile.mkdtemp(prefix="glt-home-"))
os.environ["NO_COLOR"] = "1"


class FakeGitLab:
    """Minimal in-memory GitLab API: projects/groups variables (paginated), enough for transfer + verify tests."""

    def __init__(self):
        self.vars = {}          # ("projects"|"groups", id) -> list of variable dicts
        self.tokens = {"src-token", "dst-token"}
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, obj, headers=None):
                body = json.dumps(obj).encode()
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def route(self):
                u = urllib.parse.urlsplit(self.path)
                m = re.match(r"/api/v4/(projects|groups)/(\d+)/variables(?:/([^/?]+))?$", u.path)
                return m, dict(urllib.parse.parse_qsl(u.query))

            def do_GET(self):
                if self.headers.get("PRIVATE-TOKEN") not in fake.tokens:
                    return self.send(401, {"message": "401"})
                if self.path.startswith("/api/v4/user"):
                    return self.send(200, {"username": "tester"})
                m, qs = self.route()
                if not m:
                    return self.send(404, {})
                items = fake.vars.setdefault((m[1], int(m[2])), [])
                page, per = int(qs.get("page", 1)), int(qs.get("per_page", 20))
                chunk = items[(page - 1) * per: page * per]
                self.send(200, chunk, {"X-Next-Page": str(page + 1) if page * per < len(items) else "", "X-Total": str(len(items))})

            def body(self):
                return json.loads(self.rfile.read(int(self.headers["Content-Length"])))

            def do_POST(self):
                m, _ = self.route()
                d = self.body()
                items = fake.vars.setdefault((m[1], int(m[2])), [])
                if any(v["key"] == d["key"] and v.get("environment_scope", "*") == d.get("environment_scope", "*") for v in items):
                    return self.send(400, {"message": {"key": ["has already been taken"]}})
                items.append(d)
                self.send(201, d)

            def do_PUT(self):
                m, qs = self.route()
                d = self.body()
                for v in fake.vars.setdefault((m[1], int(m[2])), []):
                    if v["key"] == urllib.parse.unquote(m[3]) and v.get("environment_scope", "*") == qs["filter[environment_scope]"]:
                        v.update(d)
                        return self.send(200, v)
                self.send(404, {})
        return H
