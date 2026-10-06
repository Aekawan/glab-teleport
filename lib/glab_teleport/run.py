"""The teleport and verify flows shared by the CLI and the interactive wizard."""
import json
import sys
import time

from . import __version__, config, report, term
from .gitlab import connect
from .i18n import t
from .plan import COMPONENTS, MOVE, NEW, SKIP, SYNCED, UPDATE, plan_group, plan_projects, print_plan
from .transfer import Session, execute
from .urls import build_rewriter
from .verify import inherited_vars, verify_all

OPT_KEYS = ("jobs", "relocate", "with_parent_vars", "rewrite_urls", "overwrite", "force_push", "activate_schedules",
            "allow_unmask", "cache_dir", "yes", "dry_run")


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


def teleport(args, kind, srcs, dst, components, s=None, interactive=False):
    s = s or session(args)
    header(s)
    with term.Status(t("Planning…", "กำลังวางแผน…")) as st:
        plan = build_plan(s, args, kind, srcs, dst, st)
        st.update(t("Reading inherited variables…", "กำลังอ่านตัวแปรที่สืบทอดจาก group…"))
        s.extra_vars, s.parent_vars = inherited_vars(s, plan, components)
    print_plan(plan, components, s.opts)
    if interactive:
        term.out("")
        term.kv([(t("Command", "คำสั่ง"), term.style(equivalent(kind, srcs, dst, components, args), "dim"))])
    items = [i for i in plan["items"] if i["state"] != SKIP or (i.get("conflict") and s.opt("force_push"))]
    moves = [i for i in items if i["state"] == MOVE]
    if moves and plan.get("access", 50) < 50:
        term.out("")
        term.out(term.style("  ✗ " + t("{n} projects need to be moved, which requires the Owner role on {g} (you are Maintainer). "
                                       "Ask an Owner to grant it or to run this command. Nothing was changed.",
                                       "ต้องย้ายตำแหน่ง {n} project ซึ่งต้องเป็น Owner ของ {g} (คุณเป็น Maintainer) "
                                       "กรุณาขอสิทธิ์ Owner หรือให้ Owner เป็นผู้รันคำสั่งนี้ ยังไม่มีการเปลี่ยนแปลงใดๆ",
                                       n=len(moves), g=plan["target"]), "red"))
        return 1
    if s.opt("dry_run"):
        term.out("")
        term.out(term.style(t("Dry run — nothing was changed.", "Dry run — ยังไม่มีการเปลี่ยนแปลงใดๆ"), "dim"))
        return 0
    if not items:
        term.out("")
        term.out(t("Nothing to teleport.", "ไม่มีรายการที่ต้องย้าย"))
        return 0
    if not s.opt("yes"):
        if not sys.stdin.isatty():
            raise SystemExit(t("Confirmation required: run in a terminal or pass --yes.", "ต้องยืนยันก่อนเริ่ม: รันใน terminal หรือใส่ --yes"))
        term.out("")
        if not term.confirm(t("Teleport {n} projects now?", "เริ่มย้าย {n} project หรือไม่", n=len(items))):
            term.out(t("Cancelled. Nothing was changed.", "ยกเลิกแล้ว ไม่มีการเปลี่ยนแปลงใดๆ"))
            return 1
    s.run_dir = report.new_run_dir(s.work, f"{kind}-{plan['source']}")
    s.rewriter = build_rewriter(s, plan["items"])
    started = time.time()
    term.heading(t("Teleporting", "กำลังย้าย"), t("{n} projects · {j} at a time", "{n} project · ทำพร้อมกัน {j}", n=len(items), j=s.jobs))
    executed = execute(s, plan, components)
    with term.Status(t("Verifying…", "กำลังตรวจสอบ…")) as st:
        projects, groups = verify_all(s, executed, components, st)
    rep = report.build(s, plan, components, executed, projects, groups, started)
    report.write(rep, s.run_dir)
    report.print_summary(rep, s.run_dir)
    return 1 if rep["counts"]["fail"] else 0


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
        term.out(t("Nothing to verify — none of these projects exist on the target yet.", "ไม่มีรายการให้ตรวจ — ยังไม่มี project เหล่านี้ที่ปลายทาง"))
        return 1
    s.rewriter = build_rewriter(s, plan["items"])
    started = time.time()
    with term.Status(t("Verifying…", "กำลังตรวจสอบ…")) as st:
        projects, groups = verify_all(s, present, components, st)
    s.run_dir = report.new_run_dir(s.work, f"verify-{plan['source']}")
    rep = report.build(s, plan, components, present, projects, groups, started, mode="verify")
    report.write(rep, s.run_dir)
    report.print_summary(rep, s.run_dir)
    return 1 if rep["counts"]["fail"] else 0


def show_report(args):
    runs = report.list_runs(config.WORK_DIR)
    if not runs:
        term.out(t("No runs yet.", "ยังไม่มีประวัติการทำงาน"))
        return 0
    if not args.run:
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
    report.print_summary(json.loads((d / "report.json").read_text()), d)
    return 0
