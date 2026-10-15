"""`glab-teleport ci-update`: put one .gitlab-ci.yml into many projects, as a branch + merge request per project (API only, no clone)."""
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config, term
from .gitlab import ApiError, connect, q
from .i18n import t
from .plan import clean_path

UPDATE, CREATE, SAME, OPEN, SKIP = "update", "create", "same", "open", "skip"


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


def collect(gl, targets, exclude):
    projects = {}
    for raw in targets:
        path = clean_path(raw)
        g = gl.group(path)
        found = gl.subtree(g)[0] if g else [p for p in [gl.project(path)] if p]
        if not found and not g:
            raise SystemExit(t("'{p}' is neither a group nor a project on {u}", "ไม่พบ '{p}' (ทั้ง group และ project) ใน {u}", p=path, u=gl.url))
        projects.update((p["id"], p) for p in found if not excluded(p["path_with_namespace"], exclude))
    return sorted(projects.values(), key=lambda p: p["path_with_namespace"].lower())


def plan_one(gl, p, new, opts):
    row = {"project": p["path_with_namespace"], "id": p["id"], "branch": p.get("default_branch"), "state": SKIP, "note": ""}
    if p.get("archived"):
        return {**row, "note": t("archived", "archive แล้ว")}
    if p.get("empty_repo") or not p.get("default_branch"):
        return {**row, "note": t("empty repository", "repository ว่าง")}
    old = gl.get_text(f"/projects/{p['id']}/repository/files/{q(opts['path'])}/raw", ref=p["default_branch"])
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
            "branch": branch, "start_branch": row["branch"], "commit_message": opts["title"],
            "actions": [{"action": row["state"], "file_path": opts["path"], "content": row["content"]}]})
        mr = gl.post(f"/projects/{pid}/merge_requests", {
            "source_branch": branch, "target_branch": row["branch"], "title": opts["title"], "remove_source_branch": True,
            "description": opts["description"]})
        return {**row, "ok": True, "mr": mr["web_url"], "note": f"!{mr['iid']}"}
    except ApiError as e:
        return {**row, "ok": False, "note": f"HTTP {e.code} {gl.scrub(e.body)[:120]}"}


def state_label(s):
    return {UPDATE: t("↻ update", "↻ แก้ไข"), CREATE: t("+ create", "+ สร้าง"), SAME: t("✓ same", "✓ เหมือนแล้ว"),
            OPEN: t("◷ MR open", "◷ มี MR อยู่"), SKIP: t("– skip", "– ข้าม")}[s]


def state_color(s):
    return {UPDATE: "blue", CREATE: "cyan", SAME: "green", OPEN: "yellow", SKIP: "dim"}[s]


def public(row):
    return {k: v for k, v in row.items() if k != "content"}


def run(args):
    from .run import read_only
    src = Path(args.file).expanduser()
    try:
        new = src.read_text()
    except OSError as e:
        raise SystemExit(t("Cannot read {f}: {e}", "อ่านไฟล์ {f} ไม่ได้: {e}", f=src, e=e.strerror))
    new = new if new.endswith("\n") else new + "\n"
    opts = {"path": args.path, "all": args.all, "keep_var": args.keep_var or [], "branch": args.branch,
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
    with term.Status(t("Reading projects…", "กำลังอ่านรายการ project…")) as st:
        projects = collect(gl, args.targets, args.exclude or [])
        st.update(t("Reading {f} in {n} projects…", "กำลังอ่าน {f} ใน {n} project…", f=args.path, n=len(projects)))
        with ThreadPoolExecutor(max(1, args.jobs)) as ex:
            rows = list(ex.map(lambda p: plan_one(gl, p, new, opts), projects))
    todo = [r for r in rows if r["state"] in (UPDATE, CREATE)]
    counts = {s: sum(r["state"] == s for r in rows) for s in (UPDATE, CREATE, SAME, OPEN, SKIP)}

    term.heading(t("Plan", "แผน") + f"  ci-update · {args.path}",
                 f"{gl.url}  {gl.user['username']} · " + t("branch {b}", "branch {b}", b=args.branch))
    term.out("")
    term.table([t("STATUS", "สถานะ"), t("PROJECT", "project"), t("BRANCH", "branch"), ""],
               [(term.style(state_label(r["state"]), state_color(r["state"])), r["project"], r["branch"] or "–", term.style(r["note"], "dim"))
                for r in rows], paths=(1,))
    term.out("")
    term.out("  " + " · ".join(f"{n} {state_label(s).split(' ', 1)[1]}" for s, n in counts.items() if n))
    result = {"ok": True, "file": str(src), "path": args.path, "branch": args.branch, "gitlab": gl.url,
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
