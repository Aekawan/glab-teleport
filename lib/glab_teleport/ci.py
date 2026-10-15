"""`glab-teleport ci-update`: put one .gitlab-ci.yml into many projects, as a branch + merge request per project (API only, no clone)."""
import json
import re
import shlex
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config, term
from .gitlab import ApiError, connect, pending_delete, q
from .i18n import t
from .plan import clean_path

UPDATE, CREATE, SAME, OPEN, SKIP = "update", "create", "same", "open", "skip"
DEFAULT, SAME_AS_INTO = "@default", "@into"
DEFAULTS = dict(file=None, targets=[], side="target", exclude=None, all=False, keep_var=None, path=".gitlab-ci.yml",
                branch="ci/update-gitlab-ci", title=None, dry_run=False, yes=False, jobs=6, into=None, from_=None)


def var_line(text, name):
    """The `  NAME: value` line of a variable in a CI file, or None."""
    m = re.search(rf"^[ \t]+{re.escape(name)}[ \t]*:.*$", text or "", re.M)
    return m.group(0) if m else None


def render(new, old, keep_vars):
    """The new file, with the project's own value kept for each variable in keep_vars."""
    for name in keep_vars:
        mine, theirs = var_line(new, name), var_line(old, name)
        if mine and theirs:
            new = new.replace(mine, theirs)
    return new


def excluded(path, patterns):
    return any(path == x or path.startswith(x.rstrip("/") + "/") for x in patterns)


def similar(gl, path):
    """Groups and projects whose full path ends with `path` (e.g. a source path that lives under another group on the target)."""
    leaf = path.rsplit("/", 1)[-1]
    found = [g["full_path"] for g in gl.get_all("/groups", search=leaf, all_available="true") if not pending_delete(g)]
    found += [p["path_with_namespace"] for p in gl.get_all("/projects", search=leaf, simple="true") if not pending_delete(p)]
    return sorted({f for f in found if f.endswith("/" + path)}, key=lambda f: (f.count("/"), f))


def resolve(gl, path, interactive):
    """Exact path, or the one group/project on this GitLab whose path ends with it."""
    if gl.group(path) or gl.project(path):
        return path
    matches = similar(gl, path)
    if len(matches) == 1 or (matches and interactive):
        pick = matches[0] if len(matches) == 1 else term.ask(t("Which '{p}'?", "'{p}' ตัวไหน", p=path),
                                                              [term.item(m, m) for m in matches], subtitle=gl.url)
        if pick:
            term.out(term.style("  " + t("{a} → {b} on {u}", "{a} → {b} ใน {u}", a=path, b=pick, u=gl.url), "dim"))
            return pick
    hint = "".join(f"\n  {m}" for m in matches)
    raise SystemExit(t("'{p}' is neither a group nor a project on {u}", "ไม่พบ '{p}' (ทั้ง group และ project) ใน {u}", p=path, u=gl.url)
                     + (t("\nDid you mean:", "\nหมายถึงตัวนี้หรือไม่:") + hint if matches else ""))


def load_inventory(gl):
    """Every group and project on this GitLab, cached like the wizard's lists."""
    from .wizard import CACHE_MAX_AGE
    cache = config.WORK_DIR / "cache" / f"inventory-{gl.side}.json"
    if cache.exists() and time.time() - cache.stat().st_mtime < CACHE_MAX_AGE:
        inv = json.loads(cache.read_text())
        if inv.get("gitlab") == gl.url:
            return inv
    with term.Status(t("Loading groups and projects…", "กำลังโหลดรายการ group และ project…")) as st:
        inv = gl.inventory(lambda i, n: st.update(t("Loading groups and projects… {i}/{n}", "กำลังโหลดรายการ group และ project… {i}/{n}", i=i, n=n)))
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(inv, ensure_ascii=False))
    return inv


def pick_targets(gl, kind, inv):
    """A group (single) or projects (Tab selects several); None = back."""
    from .wizard import tree
    if kind == "group":
        g = term.ask(t("Which group? (every project inside it)", "group ไหน (ทุก project ข้างใน)"), tree(inv["groups"], inv["projects"]), subtitle=gl.url)
        return [g] if g else None
    rows = [term.item(p["path_with_namespace"], p["path_with_namespace"].rsplit("/", 1)[1], (p.get("last_activity_at") or "")[:10],
                      p["path_with_namespace"].rsplit("/", 1)[0] + "/")
            for p in inv["projects"] if live(p)]
    return term.ask(t("Which projects? (Tab selects several)", "project ไหนบ้าง (Tab เลือกได้หลายรายการ)"), rows, multi=True, subtitle=gl.url) or None


def collect(gl, targets, exclude):
    projects = {}
    for path in targets:
        g = gl.group(path)
        found = gl.subtree(g)[0] if g else [p for p in [gl.project(path)] if p]
        projects.update((p["id"], p) for p in found if not excluded(p["path_with_namespace"], exclude))
    return sorted(projects.values(), key=lambda p: p["path_with_namespace"].lower())


def branch_list(text):
    """`develop,@default` → ["develop", "@default"]; None/empty → the project's default branch."""
    return [b.strip() for b in (text or "").split(",") if b.strip()] or [DEFAULT]


def live(p):
    return not p.get("archived") and not p.get("empty_repo") and p.get("default_branch")


def branches(gl, p):
    return [b["name"] for b in gl.get_all(f"/projects/{p['id']}/repository/branches")]


def choose(names, have, default):
    """The first of `names` this project has (`@default` = its default branch), or None."""
    for n in names:
        if n == DEFAULT:
            return default
        if n in have:
            return n
    return None


def plan_one(gl, p, new, opts):
    row = {"project": p["path_with_namespace"], "id": p["id"], "into": p.get("default_branch"), "from": p.get("default_branch"),
           "state": SKIP, "note": ""}
    if p.get("archived"):
        return {**row, "note": t("archived", "archive แล้ว")}
    if not live(p):
        return {**row, "note": t("empty repository", "repository ว่าง")}
    wanted = opts["into"] + [x for x in opts["from"] if x != SAME_AS_INTO]
    have = p.get("_branches") if p.get("_branches") is not None else (branches(gl, p) if set(wanted) - {DEFAULT} else [])
    into = choose(opts["into"], have, p["default_branch"])
    start = into if opts["from"] == [SAME_AS_INTO] else choose(opts["from"], have, p["default_branch"])
    row.update({"into": into, "from": start})
    if not into or not start:
        missing = "/".join(x for x in (opts["into"] if not into else opts["from"]) if x != DEFAULT)
        return {**row, "note": t("no branch {b}", "ไม่มี branch {b}", b=missing)}
    old = gl.get_text(f"/projects/{p['id']}/repository/files/{q(opts['path'])}/raw", ref=start)
    if old is None and not opts["all"]:
        return {**row, "note": t("no {f}", "ไม่มี {f}", f=opts["path"])}
    content = render(new, old, opts["keep_var"])
    if old is not None and old.strip() == content.strip():
        return {**row, "state": SAME}
    mrs = gl.get_all(f"/projects/{p['id']}/merge_requests", state="opened", source_branch=opts["branch"])
    if mrs:
        return {**row, "state": OPEN, "mr": mrs[0]["web_url"], "note": f"!{mrs[0]['iid']}"}
    return {**row, "state": UPDATE if old is not None else CREATE, "content": content}


def apply_one(gl, row, opts):
    pid, branch = row["id"], opts["branch"]
    try:
        if gl.try_get(f"/projects/{pid}/repository/branches/{q(branch)}"):
            gl.delete(f"/projects/{pid}/repository/branches/{q(branch)}")  # left from an earlier run whose MR was closed
        gl.post(f"/projects/{pid}/repository/commits", {
            "branch": branch, "start_branch": row["from"], "commit_message": opts["title"],
            "actions": [{"action": row["state"], "file_path": opts["path"], "content": row["content"]}]})
        mr = gl.post(f"/projects/{pid}/merge_requests", {
            "source_branch": branch, "target_branch": row["into"], "title": opts["title"], "remove_source_branch": True,
            "description": opts["description"]})
        return {**row, "ok": True, "mr": mr["web_url"], "note": f"!{mr['iid']}"}
    except ApiError as e:
        return {**row, "ok": False, "note": f"HTTP {e.code} {gl.scrub(e.body)[:120]}"}


def state_label(s):
    return {UPDATE: t("↻ update", "↻ แก้ไข"), CREATE: t("+ create", "+ สร้าง"), SAME: t("✓ same", "✓ เหมือนแล้ว"),
            OPEN: t("◷ MR open", "◷ มี MR อยู่"), SKIP: t("– skip", "– ข้าม")}[s]


def state_color(s):
    return {UPDATE: "blue", CREATE: "cyan", SAME: "green", OPEN: "yellow", SKIP: "dim"}[s]


def pick_branch(projects, title, first, gl):
    """Picker over the branch names found in these projects, most common first. Typing a name that isn't listed uses it."""
    from collections import Counter
    count = Counter(b for p in projects for b in p["_branches"])
    defaults = Counter(p["default_branch"] for p in projects)
    rows = [first, term.item(DEFAULT, t("default branch of each project", "default branch ของแต่ละ project"),
                             " · ".join(f"{b} {n}" for b, n in defaults.most_common(4)))]
    rows += [term.item(b, b, t("in {n}/{m} projects", "มีใน {n}/{m} project", n=n, m=len(projects))) for b, n in count.most_common(60)]
    pick = term.ask(title, [r for r in rows if r], custom=t("use", "ใช้"), subtitle=gl.url)
    return pick[1] if isinstance(pick, tuple) else pick


def load_projects(gl, targets, args, interactive, with_branches):
    resolved = [resolve(gl, x, interactive) for x in targets]
    with term.Status(t("Reading projects…", "กำลังอ่านรายการ project…")) as st:
        projects = collect(gl, resolved, args.exclude or [])
        if with_branches:
            st.update(t("Reading branches of {n} projects…", "กำลังอ่าน branch ของ {n} project…", n=len(projects)))
            alive = [p for p in projects if live(p)]
            with ThreadPoolExecutor(max(1, args.jobs)) as ex:
                for p, names in zip(alive, ex.map(lambda p: branches(gl, p), alive)):
                    p["_branches"] = names
    return resolved, projects


def steps(gl, args, opts, targets):
    """Like sync: group or projects → which ones → branch to start from → branch to merge into. Esc goes back.
    Returns (resolved targets, projects) or None when cancelled."""
    order = (["kind", "list"] if not targets else []) + ["load"] + ([] if args.from_ else ["from"]) + ([] if args.into else ["into"])
    i, kind, inv, loaded, resolved, projects = 0, None, None, None, None, None
    while i < len(order):
        step, back = order[i], False
        if step == "kind":
            kind = term.ask(t("What do you want to update?", "ต้องการอัปเดตอะไร"), [
                term.item("group", t("A group", "ทั้ง group"), t("every project inside it", "ทุก project ข้างใน")),
                term.item("project", t("Projects", "เลือก project"), t("pick one or more (Tab)", "เลือกได้หลายรายการ (Tab)"))],
                subtitle=f"{gl.url} · {opts['path']}")
            if kind is None:
                return None
        elif step == "list":
            inv = inv or load_inventory(gl)
            picked = pick_targets(gl, kind, inv)
            back = picked is None
            targets = picked or targets
        elif step == "load":
            if loaded != targets:
                resolved, projects = load_projects(gl, targets, args, True, True)
                loaded = list(targets)
        elif step == "from":
            pick = pick_branch([p for p in projects if live(p)], t("Which branch should the change start from?", "จะเอาไฟล์จาก branch ไหนมาแก้"), None, gl)
            back = pick is None
            opts["from"] = [pick] if pick else opts["from"]
        elif step == "into":
            same = term.item("@same", t("same branch as the source", "branch เดียวกับต้นทาง"),
                             t("recommended: the MR contains only this file", "แนะนำ: MR จะมีแค่ไฟล์นี้"))
            pick = pick_branch([p for p in projects if live(p)], t("Open the merge requests into which branch?", "จะเปิด MR เข้า branch ไหน"), same, gl)
            back = pick is None
            if pick:
                opts["into"] = list(opts["from"]) if pick == "@same" else [pick]
        if back:
            i -= 1
            while i >= 0 and order[i] == "load":
                i -= 1
            if i < 0:
                return None
            continue
        i += 1
    return resolved, projects


def command_line(src, resolved, side, opts):
    cmd = ["glab-teleport", "ci-update", str(src), *resolved] + (["--side", side] if side != "target" else [])
    into, start = ",".join(opts["into"]), ",".join(opts["from"])
    if opts["from"] == [SAME_AS_INTO] or into == start:
        cmd += ["--into", into] if opts["into"] != [DEFAULT] else []
    else:
        cmd += ["--from", start, "--into", into]
    return " ".join(shlex.quote(x) for x in cmd)


def branch_label(r):
    return r["into"] if r["from"] == r["into"] else f"{r['from']} → {r['into']}"


def public(row):
    return {k: v for k, v in row.items() if k != "content"}


def run(args):
    from .run import read_only
    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not term.JSON
    if not args.file:
        if not interactive:
            raise SystemExit(t("Give the new file: glab-teleport ci-update <file> <group|project>...",
                               "ระบุไฟล์ใหม่: glab-teleport ci-update <ไฟล์> <group|project>..."))
        args.file = term.prompt(t("New {f} file on this machine", "ไฟล์ {f} ใหม่ในเครื่องนี้", f=args.path)).strip().strip("'\"")
        if not args.file:
            return 0
    src = Path(args.file).expanduser()
    try:
        new = src.read_text()
    except OSError as e:
        raise SystemExit(t("Cannot read {f}: {e}", "อ่านไฟล์ {f} ไม่ได้: {e}", f=src, e=e.strerror))
    new = new if new.endswith("\n") else new + "\n"
    opts = {"path": args.path, "all": args.all, "keep_var": args.keep_var or [], "branch": args.branch,
            "into": branch_list(args.into), "from": branch_list(args.from_) if args.from_ else [SAME_AS_INTO],
            "title": args.title or t("ci: update {f}", "ci: อัปเดต {f}", f=args.path),
            "description": t("Replaces `{f}` with the shared version (glab-teleport ci-update).",
                             "แทนที่ `{f}` ด้วยเวอร์ชันกลาง (glab-teleport ci-update)", f=args.path)}
    side = args.side
    gl = connect(side, config.side_url(side, getattr(args, f"{side}_url")), args.insecure)
    dry = args.dry_run
    if read_only() and not dry:
        dry = True
        term.out(term.style(t("Read-only mode is on (config read_only / GLAB_TELEPORT_READ_ONLY): showing the plan only.",
                              "เปิดโหมดอ่านอย่างเดียวอยู่ (config read_only / GLAB_TELEPORT_READ_ONLY): แสดงแผนอย่างเดียว"), "yellow"))
    targets = [clean_path(x) for x in args.targets]
    if not targets and not interactive:
        raise SystemExit(t("Give the groups or projects: glab-teleport ci-update <file> <group|project>...",
                           "ระบุ group หรือ project: glab-teleport ci-update <ไฟล์> <group|project>..."))
    if interactive and not (targets and args.into and args.from_):
        picked = steps(gl, args, opts, targets)
        if not picked:
            return 0
        resolved, projects = picked
        term.out("")
        term.kv([(t("Command", "คำสั่ง"), term.style(command_line(src, resolved, side, opts), "dim"))])
    else:
        resolved, projects = load_projects(gl, targets, args, interactive, False)
        if resolved != targets:
            term.kv([(t("Command", "คำสั่ง"), term.style(command_line(src, resolved, side, opts), "dim"))])
    with term.Status(t("Reading {f} in {n} projects…", "กำลังอ่าน {f} ใน {n} project…", f=args.path, n=len(projects))) as st:
        with ThreadPoolExecutor(max(1, args.jobs)) as ex:
            rows = list(ex.map(lambda p: plan_one(gl, p, new, opts), projects))
    todo = [r for r in rows if r["state"] in (UPDATE, CREATE)]
    counts = {s: sum(r["state"] == s for r in rows) for s in (UPDATE, CREATE, SAME, OPEN, SKIP)}

    term.heading(t("Plan", "แผน") + f"  ci-update · {args.path}",
                 f"{gl.url}  {gl.user['username']} · " + t("branch {b}", "branch {b}", b=args.branch))
    term.out("")
    term.table([t("STATUS", "สถานะ"), t("PROJECT", "project"), t("BRANCH", "branch"), ""],
               [(term.style(state_label(r["state"]), state_color(r["state"])), r["project"], branch_label(r) if r["into"] else "–", term.style(r["note"], "dim"))
                for r in rows], paths=(1,))
    term.out("")
    term.out("  " + " · ".join(f"{n} {state_label(s).split(' ', 1)[1]}" for s, n in counts.items() if n))
    result = {"ok": True, "file": str(src), "path": args.path, "branch": args.branch, "gitlab": gl.url, "targets": resolved,
              "summary": counts, "projects": [public(r) for r in rows], "confirmed": False}
    term.emit(result)
    if dry or (term.JSON and not args.yes):
        term.out("")
        term.out(term.style(t("Dry run — nothing was changed.", "Dry run — ยังไม่มีการเปลี่ยนแปลงใดๆ"), "dim"))
        if term.JSON and not dry:
            term.RESULT["next"] = t("Nothing was changed. Show this plan to the user; after they approve, run the same command with --yes.",
                                    "ยังไม่มีการเปลี่ยนแปลง ให้ผู้ใช้ตรวจแผนนี้ก่อน เมื่อผู้ใช้อนุมัติแล้วจึงรันคำสั่งเดิมพร้อม --yes")
        return 0
    if not todo:
        term.out("")
        term.out(t("Nothing to do.", "ไม่มีรายการที่ต้องทำ"))
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            raise SystemExit(t("Confirmation required: run in a terminal or pass --yes.", "ต้องยืนยันก่อนเริ่ม: รันใน terminal หรือใส่ --yes"))
        term.out("")
        if not term.confirm(t("Open {n} merge requests now?", "เปิด merge request {n} รายการเลยหรือไม่", n=len(todo))):
            term.out(t("Cancelled. Nothing was changed.", "ยกเลิกแล้ว ไม่มีการเปลี่ยนแปลงใดๆ"))
            return 1

    term.heading(t("Opening merge requests", "กำลังเปิด merge request"), t("{n} projects", "{n} project", n=len(todo)))
    done = []
    with ThreadPoolExecutor(max(1, args.jobs)) as ex:
        for r in ex.map(lambda r: apply_one(gl, r, opts), todo):
            done.append(r)
            mark = term.style("✓", "green") if r["ok"] else term.style("✗", "red")
            term.out(f"  {mark} {r['project']}  " + term.style(r.get("mr") or r["note"], "dim"))
    failed = [r for r in done if not r["ok"]]
    term.out("")
    term.out("  " + t("{a} merge requests opened", "เปิด merge request แล้ว {a} รายการ", a=len(done) - len(failed)) +
             (term.style("  " + t("{b} failed", "ล้มเหลว {b}", b=len(failed)), "red") if failed else ""))
    by_id = {r["id"]: r for r in done}
    term.emit({**result, "ok": not failed, "confirmed": True,
               "projects": [public(by_id.get(r["id"], r)) for r in rows],
               "opened": [r["mr"] for r in done if r["ok"]], "failed": [public(r) for r in failed]})
    return 1 if failed else 0
