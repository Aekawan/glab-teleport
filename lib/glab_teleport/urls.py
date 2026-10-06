"""URL awareness: rewrite repository URLs from source to target, find leftover references, and repoint local clones."""
import json
import posixpath
import re
import urllib.parse
from datetime import datetime
from pathlib import Path

from . import term
from .gitlab import ApiError, q
from .i18n import t
from .history import known_pairs  # noqa: F401  (re-exported)
from .transfer import load_vars, vlabel


def url_prefixes(gl, sample=None):
    """(https prefix, ssh prefix) as GitLab reports them (handles SSH ports and sub-path installs)."""
    for p in sample or []:
        tail = p["path_with_namespace"] + ".git"
        h, s = p.get("http_url_to_repo") or "", p.get("ssh_url_to_repo") or ""
        if h.endswith(tail) and s.endswith(tail):
            return h[:-len(tail)], s[:-len(tail)]
    host = urllib.parse.urlsplit(gl.url).netloc
    return gl.url + "/", f"git@{host}:"


class UrlRewriter:
    """Rewrite references to source repositories into target URLs, path-aware (host AND path change).
    Handles https://, http://, ssh://host:port/, git@host:, and user/token@ prefixes (e.g. gitlab-ci-token:${CI_JOB_TOKEN}@)."""

    def __init__(self, src_url, dst_https, dst_ssh, pairs):
        self.old_host = urllib.parse.urlsplit(src_url).netloc.lower()
        self.new_https, self.new_ssh = dst_https, dst_ssh
        self.paths = {k.lower(): v for k, v in pairs.items() if v}
        host = re.escape(self.old_host)
        self.host_rx = re.compile(rf"(?<![\w-]){host}(?![\w-])", re.I)
        alts = "|".join(re.escape(p) for p in sorted(self.paths, key=len, reverse=True))
        self.rx = re.compile(
            rf"(?P<scheme>https?://|ssh://)?(?P<user>[\w.%${{}}:-]+@)?(?<![\w.-]){host}"
            rf"(?P<sep>:\d+/|:|/)(?P<path>{alts})(?P<git>\.git)?(?=[/?#\s'\"`),;:\]]|$)", re.I) if alts else None

    def _sub(self, m):
        new_path, git = self.paths[m["path"].lower()], m["git"] or ""
        if m["scheme"] == "ssh://" or (m["scheme"] is None and m["sep"].startswith(":")):
            return self.new_ssh + new_path + git
        scheme, rest = self.new_https.split("://", 1)
        return (f"{scheme}://" if m["scheme"] else "") + (m["user"] or "") + rest + new_path + git

    def rewrite(self, text):
        if not text or not self.rx:
            return text, 0
        return self.rx.subn(self._sub, text)

    def mentions_old(self, text):
        return bool(text) and bool(self.host_rx.search(text))

    def map_path(self, path):
        return self.paths.get(path.strip("/").lower())


def build_rewriter(s, items=()):
    pairs = known_pairs(s.work)
    pairs.update({i["source"]: i["target"] for i in items})
    sample = [i["dst"] for i in items if i.get("dst")]
    https, ssh = url_prefixes(s.dst, sample)
    return UrlRewriter(s.src.url, https, ssh, pairs)


# ── refs: find references to the source GitLab in target projects ──
CI_PROJECT_RE = re.compile(r"^\s*(?:-\s*)?project:\s*['\"]?([\w.\-/]+?)['\"]?\s*(?:#.*)?$")
mask_userinfo = lambda s: re.sub(r"(://)[^/@\s'\"]+@", r"\1***@", s)


def scan_project(s, rw, proj, found):
    """Look at a TARGET project: variables, CI includes, submodules and files that still point at the source GitLab."""
    db = proj.get("default_branch") or "HEAD"
    path = proj["path_with_namespace"]

    def add(kind, where, text, fix):
        found.append({"project": path, "kind": kind, "where": where, "found": mask_userinfo(text)[:200], "fix": mask_userinfo(fix)[:200]})

    for v in s.dst.maybe_all(f"/projects/{proj['id']}/variables") or []:
        if rw.mentions_old(v.get("value")):
            new, n = rw.rewrite(v["value"])
            add("variable", vlabel((v["key"], v.get("environment_scope", "*"))), t("(value hidden)", "(ไม่แสดงค่า)"),
                t("rewrite with --rewrite-urls", "แก้อัตโนมัติได้ด้วย --rewrite-urls") if n and not rw.mentions_old(new)
                else t("update manually", "แก้ไขเอง"))
    ci = proj.get("ci_config_path") or ".gitlab-ci.yml"
    text = "" if "@" in ci else (s.dst.get_text(f"/projects/{proj['id']}/repository/files/{q(ci)}/raw", ref=db) or "")
    for i, line in enumerate(text.splitlines(), 1):
        m = CI_PROJECT_RE.match(line)
        if m and not m[1].startswith("$"):
            target = rw.map_path(m[1])
            if target and target.lower() != m[1].lower():
                add("ci-include", f"{ci}:{i}", line.strip(), line.strip().replace(m[1], target))
    text = s.dst.get_text(f"/projects/{proj['id']}/repository/files/.gitmodules/raw", ref=db) or ""
    src_path = next((k for k, v in rw.paths.items() if v.lower() == path.lower()), None)
    for i, line in enumerate(text.splitlines(), 1):
        m = re.match(r"^\s*url\s*=\s*(\S+)", line)
        if not m:
            continue
        url = m[1]
        if url.startswith(".") and src_path:
            old_target = posixpath.normpath(posixpath.join(src_path, url)).removesuffix(".git")
            new_target = rw.map_path(old_target)
            now = posixpath.normpath(posixpath.join(path, url)).removesuffix(".git")
            if new_target and now.lower() != new_target.lower():
                add("submodule", f".gitmodules:{i}", line.strip(),
                    "url = " + posixpath.relpath(new_target, path) + (".git" if url.endswith(".git") else ""))
        elif rw.mentions_old(url):
            add("submodule", f".gitmodules:{i}", line.strip(), rw.rewrite(line.strip())[0])
    try:
        blobs = s.dst.get_all(f"/projects/{proj['id']}/search", scope="blobs", search=rw.old_host)
    except ApiError:
        blobs = []
    for b in blobs:
        for n, line in enumerate((b.get("data") or "").splitlines()):
            if rw.mentions_old(line):
                new, k = rw.rewrite(line)
                add("file", f"{b.get('path') or b.get('filename')}:{(b.get('startline') or 1) + n}", line.strip(),
                    new.strip() if k and not rw.mentions_old(new) else t("update manually (registry/API/unknown project)",
                                                                         "แก้ไขเอง (registry/API/project ที่ไม่รู้จัก)"))


# ── repoint: script for teammates to switch their local clones ──
REPOINT = r"""#!/usr/bin/env bash
# Repoint local git clones from __SRC__ to __DST__ (generated by glab-teleport on __WHEN__, __COUNT__ repositories).
#   ./repoint.sh [dir ...]           preview (default: current directory, searches 5 levels deep)
#   ./repoint.sh --apply [dir ...]   change remotes
set -u
OLD_HOST='__HOST__'
APPLY=0
if [ "${1:-}" = "--apply" ]; then APPLY=1; shift; fi
[ $# -eq 0 ] && set -- .
PORT_RE='^:[0-9]+/'
mapping() {
cat <<'__MAP__'
__DATA__
__MAP__
}
lookup() { mapping | awk -F'\t' -v k="$1" -v c="$2" 'tolower($1)==k { print $c; f=1; exit } END { exit !f }'; }
for root in "$@"; do
  find "$root" -maxdepth 5 -name .git -prune 2>/dev/null | while IFS= read -r gitdir; do
    repo=$(dirname "$gitdir")
    for remote in $(git -C "$repo" remote 2>/dev/null); do
      url=$(git -C "$repo" remote get-url "$remote" 2>/dev/null) || continue
      low=$(printf '%s' "$url" | tr 'A-Z' 'a-z')
      case "$low" in *"$OLD_HOST":*|*"$OLD_HOST"/*) ;; *) continue ;; esac
      p=${low#*"$OLD_HOST"}
      if [[ $p =~ $PORT_RE ]]; then p=${p#*/}; else p=${p#[:/]}; fi
      p=${p%/}; p=${p%.git}
      case "$url" in http://*|https://*) col=2 ;; *) col=3 ;; esac
      shown=$(printf '%s' "$url" | sed -E 's#://[^/@]+@#://***@#')
      if new=$(lookup "$p" "$col"); then
        echo "✓ $repo [$remote]  $shown  ->  $new"
        [ "$APPLY" = 1 ] && git -C "$repo" remote set-url "$remote" "$new"
      else
        echo "· $repo [$remote]  $shown  (not teleported yet — skipped)"
      fi
    done
  done
done
[ "$APPLY" = 1 ] || echo "(preview only — run again with --apply to change remotes)"
"""


def repoint_script(src_gl, dst_https, dst_ssh, pairs):
    data = "\n".join(f"{s.lower()}\t{dst_https}{d}.git\t{dst_ssh}{d}.git" for s, d in sorted(pairs.items()))
    return (REPOINT.replace("__HOST__", urllib.parse.urlsplit(src_gl.url).netloc.lower()).replace("__SRC__", src_gl.url)
            .replace("__DST__", dst_https.rstrip("/")).replace("__WHEN__", f"{datetime.now():%Y-%m-%d %H:%M}")
            .replace("__COUNT__", str(len(pairs))).replace("__DATA__", data))
