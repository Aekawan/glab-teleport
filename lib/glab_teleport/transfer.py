"""Executing a plan: groups, projects, and every component — with one clean progress line per project."""
import json
import re
import shutil
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import term
from .gitlab import ApiError, GitError, ls_remote, q, run_git, short_ref, sync_repo
from .i18n import t
from .plan import MOVE, NEW, SKIP, COMPONENTS

STEPS = {
    "repo": ["repo", "default_branch", "wiki", "protection"],
    "env": ["variables", "environments"],
    "runner": ["runners"],
    "settings": ["settings"],
    "extras": ["schedules", "hooks", "deploy_keys", "members"],
}
RANK = {"ok": 0, "skip": 0, "manual": 1, "warn": 2, "fail": 3}
SETTINGS_FIELDS = [
    "description", "topics", "merge_method", "squash_option", "only_allow_merge_if_pipeline_succeeds",
    "only_allow_merge_if_all_discussions_are_resolved", "remove_source_branch_after_merge", "ci_config_path",
    "ci_default_git_depth", "build_timeout", "auto_cancel_pending_pipelines", "public_jobs", "shared_runners_enabled",
    "issues_enabled", "merge_requests_enabled", "wiki_enabled", "snippets_enabled", "lfs_enabled", "request_access_enabled",
    "printing_merge_request_link_enabled", "resolve_outdated_diff_discussions", "ci_forward_deployment_enabled",
    "auto_devops_enabled", "builds_access_level", "container_registry_access_level",
]
HOOK_FIELDS = ["url", "push_events", "tag_push_events", "merge_requests_events", "issues_events", "confidential_issues_events",
               "note_events", "confidential_note_events", "job_events", "pipeline_events", "wiki_page_events",
               "deployment_events", "releases_events", "enable_ssl_verification", "push_events_branch_filter",
               "branch_filter_strategy"]
RUNNER_FIELDS = ("description", "paused", "locked", "run_untagged", "access_level", "maximum_timeout", "maintenance_note")
VAR_FLAGS = ("variable_type", "protected", "masked", "raw")

vkey = lambda v: (v["key"], v.get("environment_scope", "*"))
vlabel = lambda k: f"{k[0]} [{k[1]}]"


class Session:
    """Everything one teleport run needs and produces."""

    def __init__(self, src, dst, opts, work_dir):
        self.src, self.dst, self.opts = src, dst, opts
        self.jobs = max(1, int(opts.get("jobs") or 4))
        self.work = Path(work_dir)
        self.lock, self.glock, self.runner_lock = threading.Lock(), threading.RLock(), threading.Lock()
        self.group_cache, self.user_cache = {}, {}
        self.results, self.group_results, self.moves = {}, [], []
        self.extra_vars, self.parent_vars = {}, {}
        self.rewriter = None
        self.run_dir = self.work
        self.active = {}
        self.runner_map_file = self.work / "runner-map.json"
        self.runner_map = json.loads(self.runner_map_file.read_text()) if self.runner_map_file.exists() else {}
        self.runner_tokens = None
        self.runner_created = 0

    def opt(self, name):
        return bool(self.opts.get(name))

    def rec(self, source, step, status, detail="", **data):
        with self.lock:
            r = self.results.setdefault(source, {"steps": {}})
            r["steps"][step] = {"status": status, "detail": detail, **data}

    def act(self, source, text):
        with self.lock:
            self.active[source] = text

    def expected_value(self, value):
        if self.rewriter and self.opt("rewrite_urls") and value:
            return self.rewriter.rewrite(value)[0]
        return value


# ── groups & destination projects ──
def ensure_group(s, path, hint=None):
    with s.glock:
        if path in s.group_cache:
            return s.group_cache[path]
        ns = s.dst.try_get(f"/namespaces/{q(path)}")
        if ns:
            s.group_cache[path] = ns
            return ns
        parent, _, leaf = path.rpartition("/")
        if not parent:
            return None  # never create top-level groups
        p = ensure_group(s, parent)
        if not p:
            return None
        data = {"name": (hint or {}).get("name") or leaf, "path": leaf, "parent_id": p["id"],
                "visibility": (hint or {}).get("visibility") or "private", "description": (hint or {}).get("description") or ""}
        try:
            g = s.dst.post("/groups", data)
        except ApiError as e:
            if e.code == 400 and "visibility" in e.body.lower():
                g = s.dst.post("/groups", {**data, "visibility": "private"})
            else:
                raise
        s.group_cache[path] = g
        return g


def ensure_project(s, item):
    """Existing target project, or a new one created with the source name/visibility."""
    dst = s.dst.project(item["target"])
    if dst:
        return dst, False
    ns_path, _, slug = item["target"].rpartition("/")
    grp = ensure_group(s, ns_path)
    if not grp:
        raise ApiError(404, "POST", "/projects", t("target namespace {n} does not exist", "ไม่พบ namespace ปลายทาง {n}", n=ns_path))
    src = item["src"]
    # GitLab needs unique project *names* (not only paths) inside a group. When the path had to change
    # (flattened layout, name clash), use it as the name too; fall back further if the name is still taken.
    names = [src["name"] if slug.lower() == src["path"].lower() else slug, slug,
             "-".join(src["path_with_namespace"].split("/")[1:])]
    data = {"path": slug, "namespace_id": grp["id"], "visibility": src.get("visibility") or "private",
            "description": src.get("description") or "", "initialize_with_readme": False}
    last = None
    for name in dict.fromkeys(names):
        try:
            return s.dst.post("/projects", {**data, "name": name}), True
        except ApiError as e:
            last = e
            if e.code == 400 and "visibility" in e.body.lower():
                data["visibility"] = "private"
                try:
                    return s.dst.post("/projects", {**data, "name": name}), True
                except ApiError as e2:
                    last = e2
            if not (last.code == 400 and "already been taken" in last.body and '"name"' in last.body):
                raise last
    raise last


def relocate(s, item):
    """Move an already-migrated target project into the planned path (GitLab transfer keeps history and redirects)."""
    proj = s.dst.project(item["move_from"])
    ns_path, _, slug = item["target"].rpartition("/")
    grp = ensure_group(s, ns_path)
    if not proj or not grp:
        raise ApiError(404, "PUT", "transfer", t("cannot find project or namespace", "ไม่พบ project หรือ namespace"))
    if (proj.get("namespace") or {}).get("id") != grp["id"]:
        s.dst.put(f"/projects/{proj['id']}/transfer", {"namespace": grp["id"]})
    want_name = item["src"]["name"] if slug.lower() == item["src"]["path"].lower() else slug
    changes = {k: v for k, v in (("path", slug), ("name", want_name)) if proj.get(k) != v}
    if changes:
        try:
            s.dst.put(f"/projects/{proj['id']}", changes)
        except ApiError:
            s.dst.put(f"/projects/{proj['id']}", {"path": slug})  # name taken: keep the current name
    return s.dst.get(f"/projects/{proj['id']}")


# ── project steps ──
def step_repo(s, it, src, dst, rec, act):
    if src.get("empty_repo"):
        return rec("skip", t("source repository is empty", "repository ต้นทางว่าง"))
    tmp = None
    repo_dir = (Path(s.opts["cache_dir"]) / re.sub(r"[^\w.-]", "_", src["path_with_namespace"] + ".git")) if s.opts.get("cache_dir") \
        else Path(tmp := tempfile.mkdtemp(prefix="glab-teleport-")) / "repo.git"
    try:
        r = sync_repo(s.src, s.dst, src["http_url_to_repo"], dst["http_url_to_repo"], repo_dir,
                      force=s.opt("force_push"), activity=lambda x: act("repo: " + x), prune=s.opt("prune"))
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    bad = sorted(set(r["rejected"]) | set(r["mismatched"]))
    good = [x for x in r["updated"] if x not in bad]
    detail = t("{b} branches, {t} tags", "{b} branch, {t} tag", b=r["branches"], t=r["tags"]) + (" + LFS" if r["lfs"] else "")
    if good:
        detail += t("; updated: {r}", "; อัปเดต: {r}", r=", ".join(short_ref(x) for x in good[:6]) + ("…" if len(good) > 6 else ""))
    if r["deleted"]:
        detail += t("; removed: {r}", "; ลบ: {r}", r=", ".join(short_ref(x) for x in r["deleted"][:6]))
    changes = {"refs": len(good), "refs_removed": len(r["deleted"])}
    if bad:
        return rec("fail", detail + " — " + t("not updated: {r}", "อัปเดตไม่ได้: {r}", r=", ".join(short_ref(x) for x in bad[:6]))
                   + ("" if s.opt("force_push") else t(" (target has newer or different commits)", " (ปลายทางมี commit ใหม่กว่าหรือต่างกัน)")),
                   changes=changes)
    rec("ok", detail, changes=changes)


def step_wiki(s, it, src, dst, rec, act):
    if src.get("wiki_access_level", "enabled" if src.get("wiki_enabled") else "disabled") == "disabled":
        return rec("skip", t("wiki disabled", "ไม่ได้เปิด wiki"))
    w_src, w_dst = src["http_url_to_repo"][:-4] + ".wiki.git", dst["http_url_to_repo"][:-4] + ".wiki.git"
    probe = run_git(s.src, ["ls-remote", w_src], check=False)
    if probe.returncode != 0 or not probe.stdout.strip():
        return rec("skip", t("no wiki pages", "ไม่มีหน้า wiki"))
    act("wiki")
    with tempfile.TemporaryDirectory(prefix="glab-teleport-") as tmp:
        r = sync_repo(s.src, s.dst, w_src, w_dst, Path(tmp) / "wiki.git", force=s.opt("force_push"), lfs=False)
    rec("warn" if r["rejected"] or r["mismatched"] else "ok", t("wiki copied", "คัดลอก wiki แล้ว"))


def step_default_branch(s, it, src, dst, rec, act):
    want = src.get("default_branch")
    if not want or src.get("empty_repo"):
        return rec("skip")
    for attempt in range(8):  # a just-pushed branch can take a few seconds to be visible to the API
        cur = s.dst.get(f"/projects/{dst['id']}").get("default_branch")
        if cur == want:
            return rec("ok", want, changes={"default_branch": int(attempt > 0)})
        try:
            s.dst.put(f"/projects/{dst['id']}", {"default_branch": want})
        except ApiError:
            pass
        time.sleep(min(1 + attempt, 4))
    rec("warn", t("could not set default branch to {w} (still {c})", "ตั้ง default branch เป็น {w} ไม่สำเร็จ (ยังเป็น {c})", w=want, c=cur))


def _levels(entries):
    plain = [e for e in entries or [] if not (e.get("user_id") or e.get("group_id") or e.get("deploy_key_id"))]
    return (sorted(e["access_level"] for e in plain) or [None])[0], len(plain) != len(entries or [])


def step_protection(s, it, src, dst, rec, act, created=False):
    act("protection")
    made = same = 0
    notes = []
    for kind, a_key, b_key in (("branches", "push_access_levels", "merge_access_levels"), ("tags", "create_access_levels", None)):
        sl = s.src.maybe_all(f"/projects/{src['id']}/protected_{kind}")
        dl = s.dst.maybe_all(f"/projects/{dst['id']}/protected_{kind}")
        if sl is None or dl is None:
            notes.append(t("cannot read protected {k}", "อ่าน protected {k} ไม่ได้", k=kind))
            continue
        cur = {d["name"]: d for d in dl}
        for rule in sl:
            la, special_a = _levels(rule.get(a_key))
            lb, special_b = _levels(rule.get(b_key)) if b_key else (None, False)
            if special_a or special_b:
                notes.append(t("{n}: user/group-specific access must be set manually", "{n}: สิทธิ์ระดับ user/group ต้องตั้งเอง", n=rule["name"]))
            payload = {"name": rule["name"]}
            if kind == "branches":
                payload["allow_force_push"] = bool(rule.get("allow_force_push"))
                if la is not None:
                    payload["push_access_level"] = la
                if lb is not None:
                    payload["merge_access_level"] = lb
            elif la is not None:
                payload["create_access_level"] = la
            existing = cur.get(rule["name"])
            if existing:
                ea, _ = _levels(existing.get(a_key))
                eb, _ = _levels(existing.get(b_key)) if b_key else (None, False)
                if ea == la and eb == lb and (kind == "tags" or bool(existing.get("allow_force_push")) == payload["allow_force_push"]):
                    same += 1
                    continue
                if not (s.opt("overwrite") or created or it.get("fresh")):
                    notes.append(t("{n}: differs on target (use --overwrite)", "{n}: ปลายทางตั้งไว้ต่างกัน (ใช้ --overwrite)", n=rule["name"]))
                    continue
                s.dst.delete(f"/projects/{dst['id']}/protected_{kind}/{q(rule['name'])}")
            s.dst.post(f"/projects/{dst['id']}/protected_{kind}", payload)
            made += 1
    rec("warn" if notes else "ok", t("{m} rules applied, {s} already correct", "ตั้งกฎ {m} ข้อ, ตรงอยู่แล้ว {s} ข้อ", m=made, s=same)
        + ("; " + "; ".join(notes[:4]) if notes else ""), changes={"protection": made})


def load_vars(gl, base):
    vs = gl.maybe_all(f"{base}/variables")
    return None if vs is None else {vkey(v): v for v in vs}


def copy_variables(s, src_vars, dst_base):
    """Copy variables keyed by (key, environment_scope). Returns stats; never logs values."""
    dst = load_vars(s.dst, dst_base)
    if dst is None:
        return None
    st = {"total": len(src_vars), "created": 0, "same": 0, "updated": 0, "deleted": 0, "differs": [], "failed": [], "hidden": [],
          "unmasked": [], "rewritten": []}
    if s.opt("prune"):  # variables removed on the source are removed on the target too
        for k in sorted(set(dst) - set(src_vars)):
            try:
                s.dst.delete(f"{dst_base}/variables/{q(k[0])}", **{"filter[environment_scope]": k[1]})
                st["deleted"] += 1
            except ApiError as e:
                st["failed"].append(f"{vlabel(k)} ({e.code})")
    for k, v in sorted(src_vars.items()):
        if v.get("hidden") or v.get("value") is None:
            st["hidden"].append(vlabel(k))
            continue
        value = s.expected_value(v["value"])
        if value != v["value"]:
            st["rewritten"].append(vlabel(k))
        payload = {"value": value, "variable_type": v.get("variable_type", "env_var"), "protected": bool(v.get("protected")),
                   "masked": bool(v.get("masked")), "raw": bool(v.get("raw"))}
        if v.get("description"):
            payload["description"] = v["description"]
        try:
            if k in dst:
                same = all(dst[k].get(f) == payload.get(f) for f in ("value", *VAR_FLAGS))
                if same:
                    st["same"] += 1
                elif s.opt("overwrite"):
                    s.dst.put(f"{dst_base}/variables/{q(k[0])}", payload, **{"filter[environment_scope]": k[1]})
                    st["updated"] += 1
                else:
                    st["differs"].append(vlabel(k))
                continue
            data = {"key": k[0], "environment_scope": k[1], **payload}
            try:
                s.dst.post(f"{dst_base}/variables", data)
            except ApiError as e:
                if e.code == 400 and payload["masked"] and s.opt("allow_unmask"):
                    s.dst.post(f"{dst_base}/variables", {**data, "masked": False})
                    st["unmasked"].append(vlabel(k))
                else:
                    raise
            st["created"] += 1
        except ApiError as e:
            st["failed"].append(f"{vlabel(k)} ({e.code})")
    return st


def var_summary(st):
    if st is None:
        return "fail", t("cannot read target variables (permission)", "อ่าน variables ปลายทางไม่ได้ (สิทธิ์ไม่พอ)")
    parts = [t("{n} variables: {c} created, {s} unchanged", "{n} ตัวแปร: สร้าง {c}, ตรงอยู่แล้ว {s}", n=st["total"], c=st["created"], s=st["same"])]
    if st["updated"]:
        parts.append(t("{n} updated", "อัปเดต {n}", n=st["updated"]))
    if st["deleted"]:
        parts.append(t("{n} removed", "ลบ {n}", n=st["deleted"]))
    if st["rewritten"]:
        parts.append(t("{n} URLs rewritten", "แปลง URL {n}", n=len(st["rewritten"])))
    if st["differs"]:
        parts.append(t("{n} differ on target (use --overwrite): {k}", "ปลายทางมีค่าต่างกัน {n} (ใช้ --overwrite): {k}",
                       n=len(st["differs"]), k=", ".join(st["differs"][:4])))
    if st["hidden"]:
        parts.append(t("{n} hidden on source — set manually: {k}", "ต้นทางซ่อนค่าไว้ {n} ตัว ต้องตั้งเอง: {k}",
                       n=len(st["hidden"]), k=", ".join(st["hidden"][:4])))
    if st["failed"]:
        parts.append(t("failed: {k}", "สร้างไม่ได้: {k}", k=", ".join(st["failed"][:4])))
    status = "fail" if st["failed"] else "manual" if st["hidden"] else "warn" if st["differs"] or st["unmasked"] else "ok"
    return status, "; ".join(parts)


def step_variables(s, it, src, dst, rec, act):
    act("variables")
    own = load_vars(s.src, f"/projects/{src['id']}")
    if own is None:
        return rec("fail", t("cannot read source variables (Maintainer required)", "อ่าน variables ต้นทางไม่ได้ (ต้องเป็น Maintainer)"))
    inherited = s.extra_vars.get(it["source"]) or {}
    st = copy_variables(s, {**inherited, **own}, f"/projects/{dst['id']}")
    status, detail = var_summary(st)
    if inherited:
        detail += t("; includes {n} inherited from flattened groups", "; รวมตัวแปรที่สืบทอดจาก group {n} ตัว", n=len(inherited))
    rec(status, detail, changes={"variables": (st["created"] + st["updated"] + st["deleted"]) if st else 0})


def step_environments(s, it, src, dst, rec, act):
    se = s.src.maybe_all(f"/projects/{src['id']}/environments")
    de = s.dst.maybe_all(f"/projects/{dst['id']}/environments")
    if se is None or de is None:
        return rec("warn", t("cannot read environments", "อ่าน environments ไม่ได้"))
    have, made = {e["name"] for e in de}, 0
    for e in se:
        if e["name"] not in have:
            s.dst.post(f"/projects/{dst['id']}/environments", {k: e[k] for k in ("name", "external_url") if e.get(k)})
            made += 1
    rec("ok", t("{n} environments ({m} created)", "{n} environment (สร้าง {m})", n=len(se), m=made), changes={"environments": made})


def step_settings(s, it, src, dst, rec, act):
    act("settings")
    data = {k: src[k] for k in SETTINGS_FIELDS if src.get(k) is not None}
    notes, failed = [], []
    cfg = data.get("ci_config_path") or ""
    if "@" in cfg:  # CI config stored in another project: file.yml@group/project[:ref]
        file_, _, rest = cfg.partition("@")
        proj, _, ref = rest.partition(":")
        mapped = s.rewriter.map_path(proj) if s.rewriter else None
        if mapped and s.opt("rewrite_urls"):
            data["ci_config_path"] = f"{file_}@{mapped}" + (f":{ref}" if ref else "")
        else:
            notes.append(t("CI config lives in {p} — update ci_config_path after it moves", "CI config อยู่ใน {p} — ต้องแก้ ci_config_path หลังย้าย", p=proj))
    try:
        s.dst.put(f"/projects/{dst['id']}", data)
    except ApiError as e:
        if e.code not in (400, 422):
            raise
        for k, v in data.items():
            try:
                s.dst.put(f"/projects/{dst['id']}", {k: v})
            except ApiError:
                failed.append(k)
    if failed:
        notes.append(t("not supported on target: {f}", "ปลายทางไม่รองรับ: {f}", f=", ".join(failed)))
    rec("warn" if notes else "ok", t("{n} settings", "ตั้งค่า {n} รายการ", n=len(data) - len(failed)) + ("; " + "; ".join(notes) if notes else ""))


def runner_error(e):
    if e.code == 403:
        return t("permission denied (project runners need Maintainer, group runners need Owner)",
                 "สิทธิ์ไม่พอ (project runner ต้องเป็น Maintainer, group runner ต้องเป็น Owner)")
    return f"HTTP {e.code}"


def ensure_runner(s, full, kind, target_id):
    """Create each source runner once on the target (remembered in runner-map.json); reuse it for other projects."""
    key = f"{s.src.url}#{full['id']}"
    with s.runner_lock:
        rid = s.runner_map.get(key)
        if rid and not s.dst.try_get(f"/runners/{rid}"):
            rid = None  # the runner was deleted on the target since the last run: create it again
        if rid:
            if kind == "project_type":
                try:
                    s.dst.post(f"/projects/{target_id}/runners", {"runner_id": rid})
                except ApiError as e:
                    if e.code not in (400, 403, 409):
                        raise
            return False
        data = {"runner_type": kind, **{k: full[k] for k in RUNNER_FIELDS if full.get(k) not in (None, "")}}
        if full.get("tag_list"):
            data["tag_list"] = full["tag_list"]
        data["project_id" if kind == "project_type" else "group_id"] = target_id
        res = s.dst.post("/user/runners", data)
        s.runner_map[key] = res["id"]
        s.runner_map_file.parent.mkdir(parents=True, exist_ok=True)
        s.runner_map_file.write_text(json.dumps(s.runner_map, indent=1))
        new_file = not s.runner_tokens.exists()
        with open(s.runner_tokens, "a") as f:
            if new_file:
                f.write("# Runner registration tokens — keep secret.\n"
                        f"# On each runner host: gitlab-runner register --url {s.dst.url} --token <token>\n")
            f.write(f"{kind}\tid={res['id']}\t{full.get('description') or ''}\ttags={','.join(full.get('tag_list') or [])}\t{res['token']}\n")
        s.runner_tokens.chmod(0o600)
        s.runner_created += 1
        return True


def step_runners(s, it, src, dst, rec, act):
    rs = s.src.maybe_all(f"/projects/{src['id']}/runners", type="project_type")
    if rs is None:
        return rec("warn", t("cannot read source runners", "อ่าน runner ต้นทางไม่ได้"))
    made = linked = 0
    failed = []
    for r in rs:
        try:
            if ensure_runner(s, s.src.get(f"/runners/{r['id']}"), "project_type", dst["id"]):
                made += 1
            else:
                linked += 1
        except ApiError as e:
            failed.append(f"{r.get('description') or r['id']}: {runner_error(e)}")
    detail = t("{n} project runners ({m} created, {l} reused)", "{n} project runner (สร้าง {m}, ใช้ซ้ำ {l})", n=len(rs), m=made, l=linked)
    if src.get("shared_runners_enabled"):
        detail += t("; also uses instance runners — make sure the target has runners with matching tags",
                    "; ใช้ instance runner ด้วย — ปลายทางต้องมี runner ที่ tag ตรงกัน")
    if failed:
        detail += "; " + "; ".join(failed[:3])
    rec("fail" if failed else "ok", detail, created=made)


def step_schedules(s, it, src, dst, rec, act):
    sl = s.src.maybe_all(f"/projects/{src['id']}/pipeline_schedules")
    dl = s.dst.maybe_all(f"/projects/{dst['id']}/pipeline_schedules")
    if sl is None or dl is None:
        return rec("warn", t("cannot read pipeline schedules", "อ่าน pipeline schedules ไม่ได้"))
    have, made = {(x["description"], x["ref"], x["cron"]) for x in dl}, 0
    for sch in sl:
        if (sch["description"], sch["ref"], sch["cron"]) in have:
            continue
        full = s.src.get(f"/projects/{src['id']}/pipeline_schedules/{sch['id']}")
        new = s.dst.post(f"/projects/{dst['id']}/pipeline_schedules", {
            "description": sch["description"], "ref": sch["ref"], "cron": sch["cron"],
            "cron_timezone": sch.get("cron_timezone") or "UTC", "active": bool(sch.get("active")) and s.opt("activate_schedules")})
        for v in full.get("variables") or []:
            s.dst.post(f"/projects/{dst['id']}/pipeline_schedules/{new['id']}/variables",
                       {"key": v["key"], "value": v["value"], "variable_type": v.get("variable_type", "env_var")})
        made += 1
    paused = sl and not s.opt("activate_schedules")
    rec("manual" if paused and made else "ok",
        t("{n} schedules ({m} created)", "{n} schedule (สร้าง {m})", n=len(sl), m=made)
        + (t(" — paused; activate them when you switch over", " — ปิดไว้ก่อน เปิดใช้เมื่อพร้อมย้ายระบบจริง") if paused and made else ""))


def step_hooks(s, it, src, dst, rec, act):
    sl = s.src.maybe_all(f"/projects/{src['id']}/hooks")
    dl = s.dst.maybe_all(f"/projects/{dst['id']}/hooks")
    if sl is None or dl is None:
        return rec("warn", t("cannot read webhooks", "อ่าน webhooks ไม่ได้"))
    have, made = {h["url"] for h in dl}, 0
    for h in sl:
        if h["url"] not in have:
            s.dst.post(f"/projects/{dst['id']}/hooks", {k: h[k] for k in HOOK_FIELDS if h.get(k) is not None})
            made += 1
    rec("manual" if made else "ok", t("{n} webhooks ({m} created)", "{n} webhook (สร้าง {m})", n=len(sl), m=made)
        + (t(" — secret tokens can't be read; re-enter them", " — อ่าน secret token ไม่ได้ ต้องใส่ใหม่") if made else ""))


def step_deploy_keys(s, it, src, dst, rec, act):
    sl = s.src.maybe_all(f"/projects/{src['id']}/deploy_keys")
    dl = s.dst.maybe_all(f"/projects/{dst['id']}/deploy_keys")
    if sl is None or dl is None:
        return rec("warn", t("cannot read deploy keys", "อ่าน deploy keys ไม่ได้"))
    body = lambda k: ((k.get("key") or "").split() + ["", ""])[1]
    have, made, bad = {body(k) for k in dl}, 0, []
    for k in sl:
        if body(k) in have:
            continue
        try:
            s.dst.post(f"/projects/{dst['id']}/deploy_keys", {"title": k["title"], "key": k["key"], "can_push": bool(k.get("can_push"))})
            made += 1
        except ApiError as e:
            bad.append(f"{k['title']} ({e.code})")
    rec("warn" if bad else "ok", t("{n} deploy keys ({m} created)", "{n} deploy key (สร้าง {m})", n=len(sl), m=made)
        + (("; " + ", ".join(bad)) if bad else ""))


def _user_id(s, username):
    with s.lock:
        if username in s.user_cache:
            return s.user_cache[username]
    users = s.dst.get("/users", username=username) or []
    with s.lock:
        s.user_cache[username] = users[0]["id"] if users else None
    return s.user_cache[username]


def copy_members(s, kind, sid, did, cap=None):
    sl = s.src.maybe_all(f"/{kind}/{sid}/members")
    if sl is None:
        return "warn", t("cannot read members", "อ่าน members ไม่ได้")
    added = existing = 0
    missing, denied = [], []
    for m in sl:
        if re.match(r"^(project|group)_\d+_bot", m["username"]) or m.get("state") not in (None, "active"):
            continue
        uid = _user_id(s, m["username"])
        if not uid:
            missing.append(m["username"])
            continue
        data = {"user_id": uid, "access_level": min(m["access_level"], cap or 40)}  # Maintainers cannot grant Owner
        if m.get("expires_at"):
            data["expires_at"] = m["expires_at"]
        try:
            s.dst.post(f"/{kind}/{did}/members", data)
            added += 1
        except ApiError as e:
            if e.code in (409, 400):  # 400: already has the same or higher role through a parent group
                existing += 1
            elif e.code == 403:
                denied.append(m["username"])
            else:
                missing.append(f"{m['username']} ({e.code})")
    detail = t("{n} members ({a} added, {e} already have access)", "{n} member (เพิ่ม {a}, มีสิทธิ์อยู่แล้ว {e})", n=len(sl), a=added, e=existing)
    if missing:
        detail += t("; no account on target yet: {u}", "; ยังไม่มีบัญชีที่ปลายทาง: {u}", u=", ".join(missing[:8]))
    if denied:
        detail += t("; your role cannot add: {u}", "; สิทธิ์ของคุณเพิ่มไม่ได้: {u}", u=", ".join(denied[:8]))
    return ("manual" if missing or denied else "ok"), detail


def step_members(s, it, src, dst, rec, act):
    rec(*copy_members(s, "projects", src["id"], dst["id"], cap=40))


STEP_FN = {"repo": step_repo, "default_branch": step_default_branch, "wiki": step_wiki, "protection": step_protection,
           "variables": step_variables, "environments": step_environments, "settings": step_settings,
           "runners": step_runners, "schedules": step_schedules, "hooks": step_hooks, "deploy_keys": step_deploy_keys,
           "members": step_members}


def run_project(s, it, components):
    started = time.time()
    sp = it["source"]
    rec_raw = lambda step, status, detail="", **d: s.rec(sp, step, status, detail, **d)
    try:
        src = s.src.project(sp)
        if not src:
            rec_raw("lookup", "fail", t("source project not found", "ไม่พบ project ต้นทาง"))
            return sp
        s.act(sp, t("preparing", "กำลังเตรียม"))
        dst, created = ensure_project(s, it)
        it["dst"], it["created"] = dst, created
        if created:
            rec_raw("create", "ok", dst["path_with_namespace"], changes={"created": 1})
        it["fresh"] = created or bool(dst.get("empty_repo"))
    except ApiError as e:
        rec_raw("lookup", "fail", f"HTTP {e.code} {e.body[:120]}")
        return sp
    for comp in components:
        for step in STEPS[comp]:
            rec = lambda status, detail="", step=step, **d: s.rec(sp, step, status, detail, component=comp, **d)
            try:
                if step == "protection":
                    step_protection(s, it, src, dst, rec, lambda x: s.act(sp, x), created=it["fresh"])
                else:
                    STEP_FN[step](s, it, src, dst, rec, lambda x: s.act(sp, x))
            except (ApiError, GitError) as e:
                rec("fail", s.dst.scrub(s.src.scrub(str(e)))[:300])
            except Exception as e:  # noqa — one broken step must not abort the whole run
                rec("fail", f"{type(e).__name__}: {e}"[:300])
    if "repo" in components and src.get("default_branch") and not src.get("empty_repo"):
        try:
            time.sleep(2)
            step_default_branch(s, it, src, dst, lambda status, detail="", **d: s.rec(sp, "default_branch", status, detail, component="repo"),
                                lambda x: s.act(sp, x))
        except ApiError:
            pass
    if "settings" in components and src.get("archived"):
        try:
            s.dst.post(f"/projects/{dst['id']}/archive")
            s.rec(sp, "archive", "ok", t("archived like the source", "archive ตามต้นทาง"), component="settings")
        except ApiError as e:
            s.rec(sp, "archive", "warn", f"HTTP {e.code}", component="settings")
    with s.lock:
        s.results[sp]["target"] = it["target"]
        s.results[sp]["seconds"] = round(time.time() - started, 1)
        s.active.pop(sp, None)
    return sp


def change_summary(result):
    """Short human list of what actually changed on the target for one project."""
    c = Counter()
    for v in (result or {}).get("steps", {}).values():
        for k, n in (v.get("changes") or {}).items():
            c[k] += n
    parts = []
    if c["created"]:
        parts.append(t("new project", "project ใหม่"))
    if c["refs"]:
        parts.append(t("{n} branches/tags updated", "อัปเดต {n} branch/tag", n=c["refs"]))
    if c["refs_removed"]:
        parts.append(t("{n} branches/tags removed", "ลบ {n} branch/tag", n=c["refs_removed"]))
    if c["variables"]:
        parts.append(t("{n} variables", "ตัวแปร {n} ตัว", n=c["variables"]))
    if c["protection"]:
        parts.append(t("{n} protection rules", "กฎ protected {n} ข้อ", n=c["protection"]))
    if c["environments"]:
        parts.append(t("{n} environments", "environment {n} รายการ", n=c["environments"]))
    if c["default_branch"]:
        parts.append(t("default branch", "default branch"))
    return parts


def component_status(result, comp):
    sts = [v["status"] for k, v in result["steps"].items() if v.get("component") == comp]
    if not sts:
        return None
    return max(sts, key=lambda x: RANK.get(x, 0))


MARK = {"ok": ("✓", "green"), "warn": ("!", "yellow"), "manual": ("☞", "magenta"), "fail": ("✗", "red"), "skip": ("–", "dim"), None: ("–", "dim")}


def badges(result, components):
    out = []
    for c in components:
        sym, color = MARK[component_status(result, c)]
        out.append(f"{c} {term.style(sym, color)}")
    return "  ".join(out)


def overall(result):
    sts = [v["status"] for v in result["steps"].values()]
    worst = max(sts, key=lambda x: RANK.get(x, 0)) if sts else "fail"
    return {"ok": "ok", "skip": "ok", "manual": "manual", "warn": "warn", "fail": "fail"}[worst]


def teleport_groups(s, plan, components):
    """Group-level work: create groups, group variables, group runners, group members."""
    flattened = {g["id"] for g in plan["flattened_groups"]}
    for sg, dst_path in plan["group_pairs"]:
        label = sg["full_path"]
        res = {"source": label, "target": dst_path, "steps": {}}
        s.group_results.append(res)
        try:
            dst = ensure_group(s, dst_path, sg)
            if not dst:
                res["steps"]["group"] = {"status": "fail", "detail": t("cannot create target group", "สร้าง group ปลายทางไม่ได้")}
                continue
            res["target_id"] = dst["id"]
            if "env" in components:
                own = load_vars(s.src, f"/groups/{sg['id']}")
                if own is None:
                    res["steps"]["variables"] = {"status": "fail", "detail": t("cannot read source group variables", "อ่าน variables ของ group ต้นทางไม่ได้")}
                else:
                    if sg["full_path"] == plan["source"] and s.opt("with_parent_vars"):
                        own = {**s.parent_vars, **own}
                    if not own:
                        res["steps"]["variables"] = {"status": "ok", "detail": t("no group variables", "ไม่มี group variables")}
                    else:
                        res["expected_vars"] = own
                        st_ = copy_variables(s, own, f"/groups/{dst['id']}")
                        st, detail = var_summary(st_)
                        if st_ is None:
                            st, detail = "manual", t("{n} group variables not copied: managing group variables on the target needs the Owner role",
                                                     "ยังไม่ได้คัดลอก group variables {n} ตัว: การจัดการ group variables ที่ปลายทางต้องเป็น Owner", n=len(own))
                        res["steps"]["variables"] = {"status": st, "detail": detail}
            if "runner" in components:
                copy_group_runners(s, sg, dst, res)
            if "extras" in components:
                st, detail = copy_members(s, "groups", sg["id"], dst["id"])
                res["steps"]["members"] = {"status": st, "detail": detail}
        except ApiError as e:
            res["steps"]["group"] = {"status": "fail", "detail": f"HTTP {e.code} {e.body[:120]}"}
    if plan["flattened_groups"] and "runner" in components:
        top = ensure_group(s, plan["target"])
        for sg in plan["flattened_groups"]:
            res = {"source": sg["full_path"], "target": plan["target"], "steps": {}}
            s.group_results.append(res)
            copy_group_runners(s, sg, top, res)


def copy_group_runners(s, sg, dst, res):
    rs = s.src.maybe_all(f"/groups/{sg['id']}/runners", type="group_type")
    if rs is None:
        res["steps"]["runners"] = {"status": "warn", "detail": t("cannot read group runners", "อ่าน group runner ไม่ได้")}
        return
    n = made = 0
    failed = []
    for r in rs:
        full = s.src.get(f"/runners/{r['id']}")
        if sg["id"] not in [g["id"] for g in full.get("groups") or []]:
            continue  # runner belongs to a parent group
        n += 1
        try:
            made += ensure_runner(s, full, "group_type", dst["id"])
        except ApiError as e:
            failed.append(f"{full.get('description') or r['id']}: {runner_error(e)}")
    if n or failed:
        perm = failed and all("permission" in f or "สิทธิ์" in f for f in failed)
        res["steps"]["runners"] = {"status": ("manual" if perm else "fail") if failed else "ok",
                                   "detail": t("{n} group runners ({m} created)", "{n} group runner (สร้าง {m})", n=n, m=made)
                                   + ("; " + "; ".join(failed) if failed else "")}


def execute(s, plan, components):
    """Run the plan. Prints one line per finished project; details go to the report."""
    items = [i for i in plan["items"] if i["state"] != SKIP or (i.get("conflict") and s.opt("force_push"))]
    s.runner_tokens = s.work / "runner-tokens.txt"
    total = len(items)
    done = 0
    with term.Status(t("Starting…", "กำลังเริ่ม…")) as status:
        pending = [i for i in items if i["state"] == MOVE]
        for _ in range(3):  # a move can need a path that another move frees first
            failed = []
            for it in pending:
                status.update(t("Moving {a} → {b}", "กำลังย้าย {a} → {b}", a=it["move_from"], b=it["target"]))
                try:
                    relocate(s, it)
                    s.moves.append({"from": it["move_from"], "to": it["target"], "status": "ok"})
                except ApiError as e:
                    failed.append((it, e))
            if len(failed) == len(pending):
                break
            pending = [it for it, _ in failed]
        for it, e in failed if pending else []:
            why = t("needs the Owner role", "ต้องเป็น Owner") if e.code == 403 else f"HTTP {e.code}"
            s.moves.append({"from": it["move_from"], "to": it["target"], "status": "fail", "detail": why})
            it["target"] = it["move_from"]
        status.update(t("Preparing groups…", "กำลังเตรียม group…"))
        teleport_groups(s, plan, components)

        def tick():
            while not stop.is_set():
                with s.lock:
                    act = list(s.active.items())
                text = f"{done}/{total}"
                if act:
                    text += " · " + act[0][0] + " — " + act[0][1] + (f"  (+{len(act) - 1})" if len(act) > 1 else "")
                status.update(text)
                stop.wait(0.2)
        stop = threading.Event()
        threading.Thread(target=tick, daemon=True).start()
        quiet = s.opt("sync")
        unchanged = 0
        try:
            with ThreadPoolExecutor(max_workers=s.jobs) as ex:
                futs = {ex.submit(run_project, s, it, components): it for it in items}
                for f in as_completed(futs):
                    it = futs[f]
                    f.result()
                    done += 1
                    r = s.results.get(it["source"], {"steps": {}})
                    sym, color = MARK[overall(r)]
                    if quiet:  # sync: only show what changed or needs attention
                        what = change_summary(r)
                        if not what and overall(r) == "ok":
                            unchanged += 1
                            continue
                        term.out(f"  {term.style('↑' if what and overall(r) == 'ok' else sym, 'cyan' if what and overall(r) == 'ok' else color)} "
                                 f"{term.pad(term.fit(it['source'], 44), 44)}  {term.style(', '.join(what), 'dim') if what else badges(r, components)}")
                        continue
                    term.out(f"  {term.style(sym, color)} {term.pad(term.fit(it['source'], 44), 44)}  {badges(r, components)}"
                             f"  {term.style(term.elapsed(r.get('seconds', 0)), 'dim')}")
        finally:
            stop.set()
        if quiet and unchanged:
            term.out(term.style("  ✓ " + t("{n} projects already up to date", "{n} project เป็นปัจจุบันแล้ว", n=unchanged), "green"))
    return items
