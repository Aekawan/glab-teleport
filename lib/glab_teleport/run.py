"""The teleport and verify flows shared by the CLI and the interactive wizard."""
import json
import os
import sys
import time

from . import __version__, config, report, term
from .gitlab import connect
from .i18n import t
from .plan import COMPONENTS, MOVE, NEW, SKIP, SYNCED, UPDATE, plan_group, plan_projects, print_plan, print_sync_plan
from .transfer import Session, execute
from .urls import build_rewriter
from .verify import inherited_vars, verify_all

OPT_KEYS = ("jobs", "relocate", "with_parent_vars", "rewrite_urls", "overwrite", "force_push", "activate_schedules",
            "allow_unmask", "cache_dir", "yes", "dry_run", "prune", "sync")


def session(args):
    src = connect("source", config.side_url("source", getattr(args, "source_url", None)), args.insecure)
    dst = connect("target", config.side_url("target", getattr(args, "target_url", None)), args.insecure)
    return Session(src, dst, {k: getattr(args, k, None) for k in OPT_KEYS}, config.WORK_DIR)


def header(s):
    who = lambda gl: term.style(f"{gl.user['username']}{' · admin' if gl.user.get('is_admin') else ''}", "dim")
    term.out(term.style("glab-teleport", "bold") + term.style(f" {__version__}", "dim"))
    term.kv([(t("Source", "ต้นทาง"), f"{s.src.url}  {who(s.src)}"), (t("Target", "ปลายทาง"), f"{s.dst.url}  {who(s.dst)}")])


def build_plan(s, args, kind, srcs, dst, status):
    if kind == "group":
        return plan_group(s, srcs[0], dst, args.layout or "keep", args.include, args.exclude, bool(args.relocate), status)
    return plan_projects(s, srcs, dst, status)


def equivalent(kind, srcs, dst, components, args):
    parts = ["glab-teleport", kind, *srcs, dst]
    if set(components) != set(COMPONENTS):
        parts += ["--only", ",".join(components)]
    if kind == "group" and args.layout and args.layout != "keep":
        parts += ["--layout", args.layout]
    parts += ["--" + k.replace("_", "-") for k in ("relocate", "with_parent_vars", "rewrite_urls", "overwrite", "force_push",
                                                    "activate_schedules") if getattr(args, k, False)]
    return " ".join(parts)


def read_only():
    return bool(os.environ.get("GLAB_TELEPORT_READ_ONLY") or config.load_config().get("read_only"))


def teleport(args, kind, srcs, dst, components, s=None, interactive=False):
    s = s or session(args)
    sync = s.opt("sync")
    if read_only() and not s.opt("dry_run"):
        s.opts["dry_run"] = True
        term.out(term.style(t("Read-only mode is on (config read_only / GLAB_TELEPORT_READ_ONLY): showing the plan only.",
                              "เปิดโหมดอ่านอย่างเดียวอยู่ (config read_only / GLAB_TELEPORT_READ_ONLY): แสดงแผนอย่างเดียว"), "yellow"))
    header(s)
    with term.Status(t("Planning…", "กำลังวางแผน…")) as st:
        plan = build_plan(s, args, kind, srcs, dst, st)
        st.update(t("Reading inherited variables…", "กำลังอ่านตัวแปรที่สืบทอดจาก group…"))
        s.extra_vars, s.parent_vars = inherited_vars(s, plan, components)
    (print_sync_plan if sync else print_plan)(plan, components, s.opts)
    if interactive and not sync:
        term.out("")
        term.kv([(t("Command", "คำสั่ง"), term.style(equivalent(kind, srcs, dst, components, args), "dim"))])
    items = [i for i in plan["items"] if i["state"] != SKIP or (i.get("conflict") and s.opt("force_push"))]
    moves = [i for i in items if i["state"] == MOVE]
    term.emit(plan_json(s, plan, components, sync, confirmed=False))
    if moves and plan.get("access", 50) < 50:
        term.out("")
        term.out(term.style("  ✗ " + t("{n} projects need to be moved, which requires the Owner role on {g} (you are Maintainer). "
                                       "Ask an Owner to grant it or to run this command. Nothing was changed.",
                                       "ต้องย้ายตำแหน่ง {n} project ซึ่งต้องเป็น Owner ของ {g} (คุณเป็น Maintainer) "
                                       "กรุณาขอสิทธิ์ Owner หรือให้ Owner เป็นผู้รันคำสั่งนี้ ยังไม่มีการเปลี่ยนแปลงใดๆ",
                                       n=len(moves), g=plan["target"]), "red"))
        return 1
    if s.opt("dry_run") or (term.JSON and not s.opt("yes")):
        term.out("")
        term.out(term.style(t("Dry run — nothing was changed.", "Dry run — ยังไม่มีการเปลี่ยนแปลงใดๆ"), "dim"))
        if term.JSON and not s.opt("dry_run"):
            term.RESULT["next"] = t("Nothing was changed. Show this plan to the user; after they approve, run the same command with --yes.",
                                    "ยังไม่มีการเปลี่ยนแปลง ให้ผู้ใช้ตรวจแผนนี้ก่อน เมื่อผู้ใช้อนุมัติแล้วจึงรันคำสั่งเดิมพร้อม --yes")
        return 0
    if not items:
        term.out("")
        term.out(t("Nothing to do.", "ไม่มีรายการที่ต้องทำ"))
        return 0
    if not s.opt("yes"):
        if not sys.stdin.isatty():
            raise SystemExit(t("Confirmation required: run in a terminal or pass --yes.", "ต้องยืนยันก่อนเริ่ม: รันใน terminal หรือใส่ --yes"))
        term.out("")
        question = t("Sync {n} projects now?", "เริ่ม sync {n} project หรือไม่", n=len(items)) if sync else \
            t("Teleport {n} projects now?", "เริ่มย้าย {n} project หรือไม่", n=len(items))
        if not term.confirm(question):
            term.out(t("Cancelled. Nothing was changed.", "ยกเลิกแล้ว ไม่มีการเปลี่ยนแปลงใดๆ"))
            return 1
    s.run_dir = report.new_run_dir(s.work, f"{'sync-' if sync else ''}{kind}-{plan['source']}")
    s.rewriter = build_rewriter(s, plan["items"])
    started = time.time()
    term.heading(t("Syncing", "กำลัง sync") if sync else t("Teleporting", "กำลังย้าย"),
                 t("{n} projects · {j} at a time", "{n} project · ทำพร้อมกัน {j}", n=len(items), j=s.jobs))
    executed = execute(s, plan, components)
    with term.Status(t("Verifying…", "กำลังตรวจสอบ…")) as st:
        projects, groups = verify_all(s, executed, components, st)
    rep = report.build(s, plan, components, executed, projects, groups, started, mode="sync" if sync else "teleport")
    report.write(rep, s.run_dir)
    report.print_summary(rep, s.run_dir)
    term.emit({"ok": not rep["counts"]["fail"], "report_path": str(s.run_dir / "report.md"), **rep})
    return 1 if rep["counts"]["fail"] else 0


def plan_json(s, plan, components, sync=False, confirmed=False):
    """Plan as plain data for agents (no API objects)."""
    from collections import Counter as _C
    items = [{"source": i["source"], "target": i["target"], "state": i["state"], "reason": term.strip_ansi(i.get("reason") or ""),
              "move_from": i.get("move_from"), "archived": i.get("archived"), "conflict": bool(i.get("conflict")),
              "code": i.get("sync")} for i in plan["items"]]
    counts = _C(i["state"] for i in plan["items"])
    warnings = []
    if any(i["state"] == MOVE for i in plan["items"]) and plan.get("access", 50) < 50:
        warnings.append(t("moves need the Owner role on the target group", "การย้ายตำแหน่งต้องเป็น Owner ของ group ปลายทาง"))
    diverged = [i["source"] for i in plan["items"] if (i.get("sync") or {}).get("diverged")]
    if diverged:
        warnings.append(t("{n} projects have commits on the target that are not on the source; those branches are kept",
                          "{n} project มี commit ที่ปลายทางซึ่งต้นทางไม่มี branch เหล่านั้นจะไม่ถูกเขียนทับ", n=len(diverged)))
    return {"ok": True, "mode": "plan", "command": "sync" if sync else plan["kind"], "kind": plan["kind"], "source": plan["source"],
            "target": plan["target"], "target_exists": plan.get("target_exists"), "layout": plan.get("layout"),
            "components": components, "options": {k: v for k, v in s.opts.items() if v and k in ("relocate", "prune", "overwrite",
                                                  "force_push", "rewrite_urls", "with_parent_vars", "activate_schedules")},
            "summary": {"projects": len(plan["items"]), **{k: counts.get(k, 0) for k in (NEW, SYNCED, UPDATE, MOVE, SKIP)}},
            "warnings": warnings, "items": items, "confirmed": confirmed, "source_url": s.src.url, "target_url": s.dst.url}


def verify(args, kind, srcs, dst, components):
    """Read-only: pair source and target like a teleport would, then verify everything already on the target."""
    s = session(args)
    s.opts.update(relocate=False, dry_run=True)
    header(s)
    with term.Status(t("Planning…", "กำลังวางแผน…")) as st:
        plan = build_plan(s, args, kind, srcs, dst, st)
        s.extra_vars, s.parent_vars = inherited_vars(s, plan, components)
    present = [i for i in plan["items"] if i["state"] in (SYNCED, UPDATE, MOVE) and i.get("dst")]
    for i in plan["items"]:
        if i["state"] == NEW:
            i["reason"] = t("not on target yet", "ยังไม่มีที่ปลายทาง")
    if not present:
        msg = t("Nothing to verify — none of these projects exist on the target yet.", "ไม่มีรายการให้ตรวจ — ยังไม่มี project เหล่านี้ที่ปลายทาง")
        term.out(msg)
        term.emit({"ok": False, "complete": False, "error": msg, "not_on_target": [
            {"source": i["source"], "target": i["target"], "reason": i["reason"]} for i in plan["items"]]})
        return 1
    s.rewriter = build_rewriter(s, plan["items"])
    started = time.time()
    with term.Status(t("Verifying…", "กำลังตรวจสอบ…")) as st:
        projects, groups = verify_all(s, present, components, st)
    s.run_dir = report.new_run_dir(s.work, f"verify-{plan['source']}")
    rep = report.build(s, plan, components, present, projects, groups, started, mode="verify")
    report.write(rep, s.run_dir)
    report.print_summary(rep, s.run_dir)
    complete = not rep["counts"]["fail"] and not rep["skipped"]
    term.emit({"ok": complete, "complete": complete, "not_on_target": len(rep["skipped"]),
               "report_path": str(s.run_dir / "report.md"), **rep})
    return 0 if complete else 1


def show_report(args):
    runs = report.list_runs(config.WORK_DIR)
    if not runs:
        term.out(t("No runs yet.", "ยังไม่มีประวัติการทำงาน"))
        return 0
    if not args.run:
        term.emit({"ok": True, "runs": [{"id": d.name, **{k: v for k, v in json.loads((d / "report.json").read_text()).items()
                                                         if k in ("mode", "kind", "source", "target", "started", "counts")},
                                         "report_path": str(d / "report.md")} for d in runs[:50]]})
        rows = []
        for d in runs[:20]:
            rep = json.loads((d / "report.json").read_text())
            c = rep["counts"]
            res = "  ".join(term.style(f"{sym} {c[k]}", col) for k, sym, col in
                            (("ok", "✓", "green"), ("warn", "!", "yellow"), ("manual", "☞", "magenta"), ("fail", "✗", "red")) if c.get(k))
            rows.append((d.name[:15], rep["mode"], f"{rep['source']} → {rep['target']}", res))
        term.table([t("RUN", "รอบ"), t("MODE", "ประเภท"), t("WHAT", "รายการ"), t("RESULT", "ผล")], rows)
        term.out(term.style("\n  glab-teleport report latest", "dim"))
        return 0
    d = runs[0] if args.run == "latest" else next((r for r in runs if r.name.startswith(args.run)), None)
    if not d:
        raise SystemExit(t("Run '{r}' not found.", "ไม่พบรอบ '{r}'", r=args.run))
    rep = json.loads((d / "report.json").read_text())
    term.emit({"ok": not rep["counts"].get("fail"), "report_path": str(d / "report.md"), **rep})
    report.print_summary(rep, d)
    return 0


def sync(args, source=None, target=None):
    """Bring a previous teleport up to date with the source (project or group). The source always wins."""
    from . import history
    from .gitlab import connect as _connect
    from .plan import clean_path, parse_only
    work = config.WORK_DIR
    s = session(args)
    if not source:
        if not sys.stdin.isatty():
            choices = history.sync_targets(work)
            raise SystemExit(t("Choose what to sync: glab-teleport sync <source> [target]\n", "เลือกสิ่งที่จะ sync: glab-teleport sync <ต้นทาง> [ปลายทาง]\n")
                             + "\n".join(f"  {c['source']}  →  {c['target']}" for c in choices))
        if term.JSON:
            raise SystemExit(t("Give the source: glab-teleport sync <source> [target] --json", "ระบุต้นทาง: glab-teleport sync <ต้นทาง> [ปลายทาง] --json"))
        from .wizard import pick_sync
        picked = pick_sync(s)
        if not picked:
            return 0
        source, target = picked
    source = clean_path(source)
    src_gl = _connect("source", config.side_url("source", getattr(args, "source_url", None)), args.insecure)
    kind = "group" if src_gl.group(source) else "project" if src_gl.project(source) else None
    if not kind:
        raise SystemExit(t("'{p}' was not found on the source GitLab", "ไม่พบ '{p}' ใน GitLab ต้นทาง", p=source))
    target = clean_path(target) if target else history.infer_target(work, kind, source)
    if not target:  # never teleported with this tool: suggest where it belongs and ask
        from .wizard import guess_target, load_lists
        guess = guess_target(kind, source, load_lists(s)[1], history.known_pairs(work))
        if guess and sys.stdin.isatty() and not term.JSON and term.confirm(t("Sync {a} → {b}?", "sync {a} → {b} หรือไม่", a=source, b=guess), True):
            target = guess
        else:
            raise SystemExit(t("Where should '{p}' go on the target? Run: glab-teleport sync {p} <target>",
                               "ต้องระบุปลายทางของ '{p}': glab-teleport sync {p} <ปลายทาง>", p=source)
                             + (t("  (suggested: {g})", "  (แนะนำ: {g})", g=guess) if guess else ""))
    prev = history.previous(work, kind, source) or {}
    opts = prev.get("options") or {}
    if not args.layout:
        args.layout = prev.get("layout") or "keep"
    for flag in ("rewrite_urls", "with_parent_vars", "allow_unmask"):
        if opts.get(flag) and not getattr(args, flag, False):
            setattr(args, flag, True)       # same choices as the original teleport
    components = parse_only(args.only) if args.only else [c for c in COMPONENTS if c in (prev.get("components") or COMPONENTS)]
    args.sync, args.overwrite = True, not args.no_overwrite
    args.relocate = False
    args.include = args.exclude = None
    s.opts.update({k: getattr(args, k, None) for k in OPT_KEYS})
    return teleport(args, kind, [source], target, components, s=s)
