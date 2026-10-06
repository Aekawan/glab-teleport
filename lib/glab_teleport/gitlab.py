"""GitLab REST client (personal access token or OAuth with auto-refresh) and git transport helpers."""
import base64
import json
import os
import re
import shutil
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config
from .i18n import t

OAUTH_REDIRECT = "http://localhost:7171/auth/redirect"  # same as glab, so a glab OAuth app can be reused
DELETED = re.compile(r"-(deletion_scheduled|deleted)-\d+$")


def q(s):
    return urllib.parse.quote(s, safe="")


class ApiError(Exception):
    def __init__(self, code, method, path, body=""):
        self.code, self.method, self.path, self.body = code, method, path, body
        super().__init__(f"HTTP {code} {method} {path}: {body[:200]}".strip())


class GitError(Exception):
    pass


def pending_delete(p):
    """Projects/groups queued for deletion are renamed with a -deletion_scheduled-<id> / -deleted-<id> suffix."""
    path = p.get("path_with_namespace") or p.get("full_path") or ""
    return bool(DELETED.search(path) or p.get("marked_for_deletion_at") or p.get("marked_for_deletion_on"))


def post_form(url, data, insecure=False):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    ctx = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, "POST", urllib.parse.urlsplit(url).path, e.read(500).decode(errors="replace"))


def oauth_exchange(url, auth, data, insecure=False):
    res = post_form(f"{url}/oauth/token", data, insecure)
    return {**auth, "type": "oauth", "access_token": res["access_token"],
            "refresh_token": res.get("refresh_token", auth.get("refresh_token")),
            "scope": res.get("scope", auth.get("scope")),
            "expires_at": int(res.get("created_at") or time.time()) + int(res.get("expires_in") or 7200)}


class GitLab:
    def __init__(self, side, url, auth, insecure=False):
        self.side, self.url, self.insecure = side, url.rstrip("/"), insecure
        self.auth = auth if isinstance(auth, dict) else {"type": "pat", "token": auth}
        self.api = self.url + "/api/v4"
        self.ssl = ssl._create_unverified_context() if insecure else None
        parts = urllib.parse.urlsplit(self.url)
        self.origin, self.host = f"{parts.scheme}://{parts.netloc}", parts.netloc.lower()
        self.user = None
        self._lock = threading.Lock()
        self._seen = set()

    # ── auth ──
    @property
    def token(self):
        return self.auth.get("access_token") if self.auth.get("type") == "oauth" else self.auth.get("token")

    @property
    def basic(self):
        return base64.b64encode(f"oauth2:{self.token}".encode()).decode()

    def _headers(self):
        self._seen.update(x for x in (self.token, self.basic, self.auth.get("refresh_token")) if x)
        if self.auth.get("type") == "oauth":
            return {"Authorization": f"Bearer {self.token}"}
        return {"PRIVATE-TOKEN": self.token}

    def refresh(self, force=False):
        """OAuth access tokens live ~2h; refresh transparently so long transfers never stall."""
        if self.auth.get("type") != "oauth":
            return False
        with self._lock:
            if not force and self.auth.get("expires_at", 0) - time.time() > 120:
                return False
            self.auth = oauth_exchange(self.url, self.auth, {
                "client_id": self.auth["client_id"], "refresh_token": self.auth["refresh_token"],
                "grant_type": "refresh_token", "redirect_uri": self.auth.get("redirect_uri", OAUTH_REDIRECT),
                "code_verifier": self.auth.get("code_verifier", "")}, self.insecure)
            config.put_cred(self.url, self.auth)
            return True

    def scrub(self, text):
        for s in self._seen | {self.token, self.basic, self.auth.get("refresh_token")}:
            if s:
                text = text.replace(s, "***")
        return text

    # ── REST ──
    def request(self, method, path, params=None, data=None, text=False):
        url = self.api + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        body = json.dumps(data).encode() if data is not None else None
        refreshed = self.refresh()
        for attempt in range(5):
            headers = self._headers()
            if body is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=120, context=self.ssl) as resp:
                    raw = resp.read()
                    if text:
                        return raw.decode(errors="replace"), resp.headers
                    return (json.loads(raw) if raw else None), resp.headers
            except urllib.error.HTTPError as e:
                msg = e.read(2000).decode(errors="replace")
                if e.code == 401 and self.auth.get("type") == "oauth" and not refreshed:
                    try:
                        refreshed = self.refresh(force=True)
                        continue
                    except ApiError:
                        pass
                if (e.code == 429 or (method == "GET" and e.code in (502, 503, 504))) and attempt < 4:
                    time.sleep(int(e.headers.get("Retry-After") or 2 ** attempt))
                    continue
                raise ApiError(e.code, method, path, msg)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if method == "GET" and attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError(0, method, path, str(getattr(e, "reason", e)))

    def get(self, path, **params):
        return self.request("GET", path, params or None)[0]

    def try_get(self, path, **params):
        try:
            return self.get(path, **params)
        except ApiError as e:
            if e.code == 404:
                return None
            raise

    def get_text(self, path, **params):
        try:
            return self.request("GET", path, params or None, text=True)[0]
        except ApiError as e:
            if e.code in (400, 404):
                return None
            raise

    def get_all(self, path, **params):
        params = {"per_page": 100, **params}
        items, page = [], "1"
        while page:
            data, headers = self.request("GET", path, {**params, "page": page})
            items.extend(data or [])
            page = headers.get("X-Next-Page")
        return items

    def maybe_all(self, path, **params):
        """List, or None when access is denied (401/403), or [] when the feature does not exist (404)."""
        try:
            return self.get_all(path, **params)
        except ApiError as e:
            if e.code in (401, 403):
                return None
            if e.code == 404:
                return []
            raise

    def post(self, path, data=None, **params):
        return self.request("POST", path, params or None, data if data is not None else {})[0]

    def put(self, path, data=None, **params):
        return self.request("PUT", path, params or None, data if data is not None else {})[0]

    def delete(self, path, **params):
        return self.request("DELETE", path, params or None)[0]

    def project(self, path):
        return self.try_get(f"/projects/{q(path)}")

    def group(self, path):
        return self.try_get(f"/groups/{q(path)}")

    def subtree(self, group):
        """All live projects and subgroups below a group (any depth)."""
        projects = [p for p in self.get_all(f"/groups/{group['id']}/projects", include_subgroups="true", with_shared="false")
                    if not pending_delete(p)]
        groups = [g for g in self.get_all(f"/groups/{group['id']}/descendant_groups", all_available="true") if not pending_delete(g)]
        return sorted(projects, key=lambda p: p["path_with_namespace"].lower()), sorted(groups, key=lambda g: g["full_path"])

    def inventory(self, progress=None):
        """Every group and live project this account can see."""
        groups = {g["id"]: g for g in self.get_all("/groups") if not pending_delete(g)}
        roots = [g for g in list(groups.values()) if g.get("parent_id") not in groups]
        projects = {}
        for i, root in enumerate(roots, 1):
            if progress:
                progress(i, len(roots))
            for g in self.get_all(f"/groups/{root['id']}/descendant_groups", all_available="true"):
                if not pending_delete(g):
                    groups.setdefault(g["id"], g)
            for p in self.get_all(f"/groups/{root['id']}/projects", include_subgroups="true", with_shared="false"):
                projects[p["id"]] = p
        for p in self.get_all("/projects", membership="true"):
            projects.setdefault(p["id"], p)
        keep = ("id", "name", "path", "path_with_namespace", "visibility", "archived", "default_branch", "empty_repo",
                "http_url_to_repo", "ssh_url_to_repo", "web_url", "last_activity_at")
        slim = lambda p: {**{k: p.get(k) for k in keep}, "namespace": {"full_path": (p.get("namespace") or {}).get("full_path"),
                                                                        "kind": (p.get("namespace") or {}).get("kind")}}
        return {"gitlab": self.url, "generated_at": int(time.time()),
                "groups": sorted(({k: g.get(k) for k in ("id", "full_path", "name", "path", "parent_id", "visibility")}
                                  for g in groups.values()), key=lambda g: g["full_path"].lower()),
                "projects": sorted((slim(p) for p in projects.values() if not pending_delete(p)),
                                   key=lambda p: p["path_with_namespace"].lower())}

    # ── git ──
    def git_env(self):
        self.refresh()
        self._seen.update((self.token, self.basic))
        env = dict(os.environ)
        env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_COUNT="1",
                   GIT_CONFIG_KEY_0=f"http.{self.origin}/.extraHeader",
                   GIT_CONFIG_VALUE_0=f"Authorization: Basic {self.basic}")
        if self.insecure:
            env["GIT_SSL_NO_VERIFY"] = "1"
        return env


def connect(side, url, insecure=False):
    """Authenticated client or a friendly SystemExit explaining what to do next."""
    label = t("source", "ต้นทาง") if side == "source" else t("target", "ปลายทาง")
    if not url:
        raise SystemExit(t("The {s} GitLab is not configured. Run: glab-teleport login",
                           "ยังไม่ได้ตั้งค่า GitLab {s} กรุณารัน: glab-teleport login", s=label))
    auth = config.resolve_auth(url, side)
    if not auth:
        raise SystemExit(t("Not signed in to the {s} GitLab ({u}). Run: glab-teleport login",
                           "ยังไม่ได้เข้าสู่ระบบ GitLab {s} ({u}) กรุณารัน: glab-teleport login", s=label, u=url))
    gl = GitLab(side, url, auth, insecure)
    try:
        gl.user = gl.get("/user")
    except ApiError as e:
        if e.code == 0 and re.search(r"nodename|not known|resolve", e.body, re.I):
            raise SystemExit(t("Cannot resolve {u}. Check your network or VPN.",
                               "ไม่พบเซิร์ฟเวอร์ {u} กรุณาตรวจสอบการเชื่อมต่อเครือข่ายหรือ VPN", u=url))
        if e.code == 401:
            raise SystemExit(t("The {s} credentials were rejected or have expired. Run: glab-teleport login",
                               "ข้อมูลเข้าสู่ระบบของ{s}ไม่ถูกต้องหรือหมดอายุ กรุณารัน: glab-teleport login", s=label))
        raise SystemExit(t("Cannot reach {u} (HTTP {c}). Run: glab-teleport doctor",
                           "เชื่อมต่อ {u} ไม่ได้ (HTTP {c}) กรุณารัน: glab-teleport doctor", u=url, c=e.code))
    return gl


# ── git transport ──
def run_git(gl, args, cwd=None, check=True):
    p = subprocess.run(["git", *args], cwd=cwd, env=gl.git_env(), capture_output=True, text=True, errors="replace")
    if check and p.returncode != 0:
        raise GitError(gl.scrub(f"git {args[0]}: {(p.stderr or p.stdout).strip()[-500:]}"))
    return p


def ls_remote(gl, url):
    p = run_git(gl, ["ls-remote", url, "refs/heads/*", "refs/tags/*"])
    refs = {}
    for line in p.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref and not ref.endswith("^{}"):
            refs[ref] = sha
    return refs


def local_refs(repo):
    p = subprocess.run(["git", "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads", "refs/tags"],
                       cwd=repo, capture_output=True, text=True, errors="replace")
    return {r: s for s, _, r in (ln.partition(" ") for ln in p.stdout.splitlines())}


def _set_remote(gl, repo, name, url):
    if run_git(gl, ["remote", "get-url", name], cwd=repo, check=False).returncode == 0:
        run_git(gl, ["remote", "set-url", name, url], cwd=repo)
    else:
        run_git(gl, ["remote", "add", name, url], cwd=repo)


def uses_lfs(repo, refs):
    heads = [r for r in refs if r.startswith("refs/heads/")][:200]
    p = subprocess.run(["git", "grep", "-l", "filter=lfs", *heads, "--", ".gitattributes"],
                       cwd=repo, capture_output=True, text=True, errors="replace")
    return bool(p.stdout.strip())


def sync_repo(src_gl, dst_gl, src_url, dst_url, repo_dir, force=False, lfs=True, activity=None, prune=False):
    """Bring every branch and tag on dst up to date with src (plus LFS objects), then confirm every SHA on the remote.
    Fast path: when both sides already match, nothing is downloaded or pushed.
    prune=True also deletes branches/tags that no longer exist on src.
    Returns dict(branches, tags, lfs, updated=[refs], deleted=[refs], rejected=[...], mismatched=[...], empty)."""
    say = activity or (lambda s: None)
    say(t("comparing", "กำลังเทียบ"))
    src_refs, dst_refs = ls_remote(src_gl, src_url), ls_remote(dst_gl, dst_url)
    heads = sum(1 for r in src_refs if r.startswith("refs/heads/"))
    res = {"branches": heads, "tags": len(src_refs) - heads, "lfs": False, "rejected": [], "mismatched": [],
           "updated": sorted(r for r, sha in src_refs.items() if dst_refs.get(r) != sha), "deleted": [], "empty": not src_refs}
    extra = sorted(r for r in dst_refs if r not in src_refs) if prune else []
    if not src_refs or (not res["updated"] and not extra):
        return res
    repo_dir = Path(repo_dir)
    if not repo_dir.exists():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        run_git(src_gl, ["init", "--bare", "-q", str(repo_dir)])
    _set_remote(src_gl, repo_dir, "src", src_url)
    _set_remote(src_gl, repo_dir, "dst", dst_url)
    push = lambda *specs: _push(dst_gl, repo_dir, specs)
    refs = {}
    if res["updated"]:
        say(t("fetching", "กำลังดึงข้อมูล"))
        run_git(src_gl, ["fetch", "--prune", "--no-tags", "src", "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"], cwd=repo_dir)
        refs = local_refs(repo_dir)
        if lfs and uses_lfs(repo_dir, refs):
            if not shutil.which("git-lfs"):
                raise GitError(t("Repository uses Git LFS but git-lfs is not installed — nothing was pushed.",
                                 "repository นี้ใช้ Git LFS แต่เครื่องนี้ยังไม่ได้ติดตั้ง git-lfs จึงยังไม่ได้ push"))
            say("LFS")
            run_git(src_gl, ["lfs", "fetch", "--all", "src"], cwd=repo_dir)
            run_git(dst_gl, ["lfs", "push", "--all", "dst"], cwd=repo_dir)
            res["lfs"] = True
        say(t("pushing", "กำลัง push"))
        plus = "+" if force else ""
        res["rejected"] += push(f"{plus}refs/heads/*:refs/heads/*", f"{plus}refs/tags/*:refs/tags/*")
    if extra:
        say(t("pruning", "กำลังลบ ref ที่ต้นทางไม่มีแล้ว"))
        res["rejected"] += push(*[":" + r for r in extra])
    remote = ls_remote(dst_gl, dst_url)
    res["mismatched"] = sorted(r for r, s in (refs or src_refs).items() if remote.get(r) != s)
    res["deleted"] = [r for r in extra if r not in remote]
    return res


def _push(dst_gl, repo_dir, specs):
    """git push with ci.skip (copying history must never start pipelines on the target). Returns rejected refs."""
    base = ["-c", "http.postBuffer=524288000", "push", "--porcelain"]
    p = run_git(dst_gl, [*base, "-o", "ci.skip", "dst", *specs], cwd=repo_dir, check=False)
    if p.returncode != 0 and "push options" in (p.stderr or ""):
        p = run_git(dst_gl, [*base, "dst", *specs], cwd=repo_dir, check=False)
    rejected = [ln.split("\t")[1].split(":")[-1] for ln in p.stdout.splitlines() if ln.startswith("!") and ln.count("\t") >= 2]
    if p.returncode != 0 and not rejected:
        raise GitError(dst_gl.scrub(f"git push: {(p.stderr or p.stdout).strip()[-500:]}"))
    return rejected


def short_ref(r):
    return r.replace("refs/heads/", "").replace("refs/tags/", "tag:")
