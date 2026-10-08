"""Command line entry point."""
import argparse
import json
import os
import sys

from . import __version__, config, term
from .gitlab import connect
from .i18n import resolve_lang, set_lang, t
from .plan import COMPONENTS, clean_path, parse_only

COMMANDS = ("group", "project", "sync", "verify", "skill", "report", "audit", "refs", "repoint", "login", "logout", "doctor", "config",
            "update")


def _common(p):
    g = p.add_argument_group(t("connection", "การเชื่อมต่อ"))
    g.add_argument("--source-url", metavar="URL", help=t("source GitLab (normally set once with login)", "GitLab ต้นทาง (ปกติตั้งครั้งเดียวด้วย login)"))
    g.add_argument("--target-url", metavar="URL", help=t("target GitLab (normally set once with login)", "GitLab ปลายทาง (ปกติตั้งครั้งเดียวด้วย login)"))
    g.add_argument("--insecure", action="store_true", help=t("skip TLS certificate verification", "ไม่ตรวจสอบใบรับรอง TLS"))
    g.add_argument("--lang", choices=["en", "th"], help=t("interface language (en, th)", "ภาษาของหน้าจอ (en, th)"))
    g.add_argument("--no-color", action="store_true", help=t("disable colors", "ปิดการแสดงสี"))
    g.add_argument("--json", action="store_true", help=t("machine-readable output for scripts and AI agents (implies no prompts; "
                                                         "without --yes nothing is changed)",
                                                         "ผลลัพธ์แบบ JSON สำหรับสคริปต์และ AI agent (ไม่มีคำถามโต้ตอบ; ถ้าไม่ใส่ --yes จะไม่เปลี่ยนแปลงอะไร)"))


def _transfer_opts(p, group=False):
    p.add_argument("--only", metavar="LIST", help=t("what to transfer: {c} (default: all)", "สิ่งที่จะย้าย: {c} (ค่าเริ่มต้น: ทั้งหมด)", c=",".join(COMPONENTS)))
    p.add_argument("--dry-run", action="store_true", help=t("show the plan only", "แสดงแผนอย่างเดียว ไม่เปลี่ยนแปลงอะไร"))
    p.add_argument("-y", "--yes", action="store_true", help=t("don't ask for confirmation", "ไม่ต้องถามยืนยัน"))
    if group:
        p.add_argument("--layout", choices=["keep", "flat", "join", "auto"], default="keep",
                       help=t("subgroup structure on the target (default: keep = same as source)", "โครงสร้าง subgroup ที่ปลายทาง (ค่าเริ่มต้น: keep = เหมือนต้นทาง)"))
        p.add_argument("--relocate", action="store_true", help=t("move projects already on the target into the chosen structure",
                                                                  "ย้าย project ที่อยู่ปลายทางแล้วให้เข้าโครงสร้างที่เลือก"))
        p.add_argument("--include", action="append", metavar="PATH", help=t("only these subgroups/projects (repeatable)", "เฉพาะ subgroup/project นี้ (ระบุซ้ำได้)"))
        p.add_argument("--exclude", action="append", metavar="PATH", help=t("skip these subgroups/projects (repeatable)", "ข้าม subgroup/project นี้ (ระบุซ้ำได้)"))
    a = p.add_argument_group(t("advanced", "ขั้นสูง"))
    a.add_argument("--with-parent-vars", action="store_true", help=t("also copy variables inherited from parent groups", "คัดลอกตัวแปรที่สืบทอดจาก group แม่ด้วย"))
    a.add_argument("--rewrite-urls", action="store_true", help=t("rewrite source repository URLs inside variables", "แปลง URL ของ repository ต้นทางในค่าตัวแปร"))
    a.add_argument("--overwrite", action="store_true", help=t("overwrite target variables/rules that differ", "เขียนทับตัวแปรหรือกฎที่ปลายทางมีค่าต่างกัน"))
    a.add_argument("--force-push", action="store_true", help=t("overwrite target branches that diverged (destructive)", "เขียนทับ branch ปลายทางที่ไม่ตรงกัน (ย้อนกลับไม่ได้)"))
    a.add_argument("--activate-schedules", action="store_true", help=t("activate pipeline schedules immediately", "เปิดใช้ pipeline schedule ทันที"))
    a.add_argument("--allow-unmask", action="store_true", help=t("create masked variables unmasked if the target rejects the mask",
                                                                 "สร้างตัวแปรแบบไม่ mask หากปลายทางไม่รับเงื่อนไข mask"))
    a.add_argument("--jobs", type=int, default=4, metavar="N", help=t("projects in parallel (default 4)", "จำนวน project ที่ทำพร้อมกัน (ค่าเริ่มต้น 4)"))
    a.add_argument("--cache-dir", metavar="DIR", help=t("keep git mirrors here to speed up re-runs", "เก็บ git mirror ไว้ที่นี่เพื่อให้รันซ้ำเร็วขึ้น"))


def parser():
    ap = argparse.ArgumentParser(
        prog="glab-teleport", formatter_class=argparse.RawDescriptionHelpFormatter,
        description=t("Teleport GitLab groups and projects to another GitLab — code, CI/CD variables, runners and settings.",
                      "ย้าย group และ project ของ GitLab ไปยัง GitLab อีกเครื่อง พร้อม code, CI/CD variables, runner และค่าตั้ง"),
        epilog=t("""examples:
  glab-teleport                                   interactive mode
  glab-teleport group my-group new-org/my-group   teleport a group
  glab-teleport project my-group/api new-org/team --only repo,env
  glab-teleport sync my-group                     bring the target up to date again
  glab-teleport verify my-group new-org/my-group  read-only check
  glab-teleport doctor                            check your setup

docs: https://github.com/Aekawan/glab-teleport""",
                 """ตัวอย่าง:
  glab-teleport                                   โหมดเลือกจากรายการ
  glab-teleport group my-group new-org/my-group   ย้ายทั้ง group
  glab-teleport project my-group/api new-org/team --only repo,env
  glab-teleport sync my-group                     อัปเดตปลายทางให้ตามต้นทางล่าสุด
  glab-teleport verify my-group new-org/my-group  ตรวจสอบโดยไม่เปลี่ยนแปลงอะไร
  glab-teleport doctor                            ตรวจสอบความพร้อมของเครื่อง

เอกสาร: https://github.com/Aekawan/glab-teleport/blob/main/README.th.md"""))
    ap.add_argument("-V", "--version", action="version", version=f"glab-teleport {__version__}")
    sub = ap.add_subparsers(dest="cmd", metavar=t("command", "คำสั่ง"))

    p = sub.add_parser("group", help=t("teleport a group with all subgroups and projects", "ย้ายทั้ง group รวม subgroup และ project"))
    p.add_argument("source", help=t("source group path or URL", "path หรือ URL ของ group ต้นทาง"))
    p.add_argument("target", help=t("target group (created if its parent exists)", "group ปลายทาง (สร้างให้หากมี group แม่อยู่แล้ว)"))
    _transfer_opts(p, group=True)
    _common(p)

    p = sub.add_parser("project", help=t("teleport one or more projects", "ย้าย project หนึ่งหรือหลายรายการ"))
    p.add_argument("source", nargs="+", help=t("source project path(s) or URL(s)", "path หรือ URL ของ project ต้นทาง"))
    p.add_argument("target", help=t("target group (keeps the name) or full project path", "group ปลายทาง (ใช้ชื่อเดิม) หรือ path ของ project ปลายทาง"))
    _transfer_opts(p)
    _common(p)

    p = sub.add_parser("sync", help=t("update an earlier teleport with the latest from the source", "อัปเดตปลายทางให้ตามต้นทางล่าสุด (หลังจากเคยย้ายแล้ว)"))
    p.add_argument("source", nargs="?", help=t("source group or project (omit to pick from earlier teleports)", "group หรือ project ต้นทาง (ไม่ใส่ = เลือกจากรายการที่เคยย้าย)"))
    p.add_argument("target", nargs="?", help=t("target (default: where it was teleported before)", "ปลายทาง (ค่าเริ่มต้น: ที่เคยย้ายไว้)"))
    p.add_argument("--only", metavar="LIST", help=t("what to sync (default: same as the original teleport)", "สิ่งที่จะ sync (ค่าเริ่มต้น: เหมือนตอนย้ายครั้งแรก)"))
    p.add_argument("--prune", action="store_true", help=t("also delete branches, tags and variables that no longer exist on the source",
                                                           "ลบ branch, tag และตัวแปรที่ต้นทางไม่มีแล้วด้วย"))
    p.add_argument("--no-overwrite", action="store_true", help=t("only add what is missing; keep target values that differ",
                                                                  "เพิ่มเฉพาะที่ยังไม่มี ไม่แก้ค่าที่ปลายทางต่างจากต้นทาง"))
    p.add_argument("--dry-run", action="store_true", help=t("show what would change and stop", "แสดงสิ่งที่จะเปลี่ยนแล้วหยุด"))
    p.add_argument("-y", "--yes", action="store_true", help=t("don't ask for confirmation", "ไม่ต้องถามยืนยัน"))
    p.add_argument("--layout", choices=["keep", "flat", "join", "auto"], help=argparse.SUPPRESS)
    a = p.add_argument_group(t("advanced", "ขั้นสูง"))
    a.add_argument("--force-push", action="store_true", help=t("overwrite target branches that diverged (destructive)", "เขียนทับ branch ปลายทางที่ไม่ตรงกัน (ย้อนกลับไม่ได้)"))
    a.add_argument("--rewrite-urls", action="store_true", help=t("rewrite source repository URLs inside variables", "แปลง URL ของ repository ต้นทางในค่าตัวแปร"))
    a.add_argument("--with-parent-vars", action="store_true", help=t("also copy variables inherited from parent groups", "คัดลอกตัวแปรที่สืบทอดจาก group แม่ด้วย"))
    a.add_argument("--jobs", type=int, default=6, metavar="N", help=t("projects in parallel (default 6)", "จำนวน project ที่ทำพร้อมกัน (ค่าเริ่มต้น 6)"))
    _common(p)

    p = sub.add_parser("verify", help=t("compare source and target without changing anything", "เทียบต้นทางกับปลายทางโดยไม่เปลี่ยนแปลงอะไร"))
    p.add_argument("source", help=t("source group or project", "group หรือ project ต้นทาง"))
    p.add_argument("target", help=t("target group or project", "group หรือ project ปลายทาง"))
    p.add_argument("--only", metavar="LIST", help=t("what to check (default: repo,env)", "สิ่งที่จะตรวจ (ค่าเริ่มต้น: repo,env)"))
    p.add_argument("--layout", choices=["keep", "flat", "join", "auto"], default="keep")
    p.add_argument("--with-parent-vars", action="store_true", help=t("expect inherited parent-group variables too", "ตรวจตัวแปรที่สืบทอดจาก group แม่ด้วย"))
    p.add_argument("--rewrite-urls", action="store_true", help=t("expect rewritten URLs in variables", "คาดว่า URL ในตัวแปรถูกแปลงแล้ว"))
    p.add_argument("--jobs", type=int, default=6, metavar="N")
    _common(p)

    p = sub.add_parser("report", help=t("list runs or show a run summary", "ดูประวัติหรือสรุปของแต่ละรอบ"))
    p.add_argument("run", nargs="?", help=t("'latest' or a run id prefix", "'latest' หรือรหัสรอบ"))
    _common(p)

    p = sub.add_parser("audit", help=t("migration status of a whole instance or group", "สถานะการย้ายทั้งระบบหรือทั้ง group"))
    p.add_argument("source_group", nargs="?", help=t("limit to this source group", "จำกัดเฉพาะ group ต้นทางนี้"))
    p.add_argument("target_group", nargs="?", help=t("limit to this target group", "จำกัดเฉพาะ group ปลายทางนี้"))
    _common(p)

    p = sub.add_parser("refs", help=t("find references to the source GitLab left in target projects", "ค้นหาจุดที่ project ปลายทางยังอ้างถึง GitLab ต้นทาง"))
    p.add_argument("target", nargs="?", help=t("target group or project (default: everything teleported)", "group หรือ project ปลายทาง (ค่าเริ่มต้น: ทุกรายการที่ย้ายแล้ว)"))
    _common(p)

    p = sub.add_parser("repoint", help=t("generate a script that repoints teammates' local clones", "สร้างสคริปต์เปลี่ยน git remote ในเครื่องของทีม"))
    p.add_argument("-o", "--output", default="repoint.sh", help=t("output file (default repoint.sh)", "ไฟล์ผลลัพธ์ (ค่าเริ่มต้น repoint.sh)"))
    _common(p)

    p = sub.add_parser("login", help=t("set URLs and sign in to the source and target GitLab", "ตั้งค่า URL และเข้าสู่ระบบ GitLab ต้นทางและปลายทาง"))
    p.add_argument("--side", choices=["source", "target", "both"], default="both")
    p.add_argument("--method", choices=["web-token", "oauth", "paste"], help=t("skip the sign-in method menu", "ข้ามเมนูเลือกวิธีเข้าสู่ระบบ"))
    p.add_argument("--client-id", help=t("OAuth Application ID", "Application ID ของ OAuth"))
    p.add_argument("--token-stdin", action="store_true", help=t("read a token from stdin (for scripts and CI)", "อ่าน token จาก stdin (สำหรับสคริปต์และ CI)"))
    _common(p)

    p = sub.add_parser("logout", help=t("remove saved credentials", "ลบข้อมูลเข้าสู่ระบบที่บันทึกไว้"))
    p.add_argument("--side", choices=["source", "target", "both"], default="both")
    _common(p)

    p = sub.add_parser("doctor", help=t("check network, credentials, scopes and permissions", "ตรวจเครือข่าย ข้อมูลเข้าสู่ระบบ scope และสิทธิ์"))
    _common(p)

    p = sub.add_parser("config", help=t("show or change settings (lang, source, target)", "ดูหรือเปลี่ยนการตั้งค่า (lang, source, target)"))
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    _common(p)

    p = sub.add_parser("skill", help=t("install the glab-teleport skill so AI agents (Claude Code, Codex, OpenCode, pi) can use this tool safely",
                                       "ติดตั้ง skill ให้ AI agent (Claude Code, Codex, OpenCode, pi) ใช้เครื่องมือนี้ได้อย่างปลอดภัย"))
    p.add_argument("action", choices=["install", "path"], help=t("install: copy the skill; path: show where it is bundled",
                                                                  "install: คัดลอก skill; path: แสดงตำแหน่งไฟล์ skill"))
    p.add_argument("--for", dest="agents", metavar="AGENTS",
                   help=t("agents to install for: claude, codex, opencode, pi or all (default: the ones found on this machine)",
                          "ติดตั้งให้ agent เหล่านี้: claude, codex, opencode, pi หรือ all (ค่าเริ่มต้น: ตัวที่พบในเครื่องนี้)"))
    p.add_argument("--dir", help=t("install into this skills folder instead", "ติดตั้งลงโฟลเดอร์นี้แทน"))
    p.add_argument("--refresh", action="store_true", help=t("only update copies that are already installed", "อัปเดตเฉพาะ skill ที่ติดตั้งไว้แล้ว"))
    _common(p)

    p = sub.add_parser("update", help=t("update glab-teleport to the latest version (and the agent skill)",
                                        "อัปเดต glab-teleport เป็นเวอร์ชันล่าสุด (รวมถึง skill ของ AI agent)"))
    p.add_argument("--check", action="store_true", help=t("only check whether a new version exists", "ตรวจอย่างเดียวว่ามีเวอร์ชันใหม่หรือไม่"))
    p.add_argument("-y", "--yes", action="store_true", help=t("install without asking (needed with --json)", "ติดตั้งโดยไม่ถาม (ต้องใส่เมื่อใช้ --json)"))
    _common(p)

    p = sub.add_parser("ui")
    p.add_argument("--refresh", action="store_true", help=t("reload the group/project lists", "โหลดรายการ group/project ใหม่"))
    _common(p)
    sub._choices_actions = [a for a in sub._choices_actions if a.dest != "ui"]  # internal: `glab-teleport` with no command
    return ap


def shorthand(argv):
    """`glab-teleport <src> <dst>` → group or project, decided by looking at the source GitLab."""
    pos = [a for a in argv if not a.startswith("-")]
    if not argv or argv[0] in COMMANDS + ("ui",) or argv[0].startswith("-") or len(pos) < 2:
        return argv
    gl = connect("source", config.side_url("source"), "--insecure" in argv)
    src = clean_path(pos[0])
    if gl.group(src):
        return ["group", *argv]
    if gl.project(src):
        return ["project", *argv]
    raise SystemExit(t("'{p}' is neither a group nor a project on {u}", "ไม่พบ '{p}' (ทั้ง group และ project) ใน {u}", p=src, u=gl.url))


def dispatch(args):
    from . import account, audit, run, urls, wizard
    cmd = args.cmd or "ui"
    if cmd == "ui":
        return wizard.run(args)
    if cmd == "login":
        return account.cmd_login(args)
    if cmd == "logout":
        return account.cmd_logout(args)
    if cmd == "doctor":
        return account.cmd_doctor(args)
    if cmd == "config":
        return account.cmd_config(args)
    if cmd == "report":
        return run.show_report(args)
    if cmd == "audit":
        return audit.run(args)
    if cmd in ("group", "project"):
        srcs = [clean_path(x) for x in (args.source if isinstance(args.source, list) else [args.source])]
        return run.teleport(args, cmd, srcs, clean_path(args.target), parse_only(args.only))
    if cmd == "sync":
        for k, v in dict(activate_schedules=False, allow_unmask=False, cache_dir=None, relocate=False).items():
            if not hasattr(args, k):
                setattr(args, k, v)
        return run.sync(args, args.source, args.target)
    if cmd == "verify":
        s = clean_path(args.source)
        gl = connect("source", config.side_url("source", args.source_url), args.insecure)
        kind = "group" if gl.group(s) else "project"
        for k, v in dict(include=None, exclude=None, relocate=False).items():
            setattr(args, k, v)
        return run.verify(args, kind, [s], clean_path(args.target), parse_only(args.only or "repo,env"))
    if cmd == "refs":
        return refs(args)
    if cmd == "skill":
        return skill(args)
    if cmd == "update":
        from . import update
        return update.run(args)
    if cmd == "repoint":
        return repoint(args)


def refs(args):
    from .run import header, session
    from .urls import build_rewriter, known_pairs, scan_project
    s = session(args)
    header(s)
    rw = build_rewriter(s)
    targets = []
    with term.Status(t("Collecting projects…", "กำลังรวบรวม project…")) as st:
        if args.target:
            path = clean_path(args.target)
            g = s.dst.group(path)
            targets = s.dst.subtree(g)[0] if g else [p for p in [s.dst.project(path)] if p]
        else:
            targets = [p for p in (s.dst.project(x) for x in sorted(set(known_pairs(s.work).values()))) if p]
        found = []
        for i, p in enumerate(targets, 1):
            st.update(t("Scanning {i}/{n} · {p}", "กำลังสแกน {i}/{n} · {p}", i=i, n=len(targets), p=p["path_with_namespace"]))
            scan_project(s, rw, s.dst.get(f"/projects/{p['id']}"), found)
    term.heading(t("References to {h}", "จุดที่ยังอ้างถึง {h}", h=rw.old_host), t("{n} projects scanned", "สแกน {n} project", n=len(targets)))
    term.emit({"ok": True, "source_host": rw.old_host, "projects_scanned": len(targets), "references": found})
    if not found:
        term.out("  " + term.style("✓ ", "green") + t("Nothing points to the source GitLab.", "ไม่พบการอ้างถึง GitLab ต้นทาง"))
        return 0
    term.out("")
    term.table([t("PROJECT", "project"), t("KIND", "ประเภท"), t("WHERE", "ตำแหน่ง"), t("SUGGESTED FIX", "วิธีแก้ที่แนะนำ")],
               [(f["project"], f["kind"], f["where"], term.style(f["fix"], "dim")) for f in found])
    out = config.WORK_DIR / "refs.md"
    out.write_text("\n".join([f"# {t('References to', 'จุดที่ยังอ้างถึง')} {rw.old_host}", "",
                              f"| Project | {t('Kind', 'ประเภท')} | {t('Where', 'ตำแหน่ง')} | {t('Found', 'ที่พบ')} | {t('Suggested fix', 'วิธีแก้ที่แนะนำ')} |",
                              "|---|---|---|---|---|"] +
                             [f"| `{f['project']}` | {f['kind']} | `{f['where']}` | `{f['found']}` | `{f['fix']}` |" for f in found]) + "\n")
    term.out("")
    term.kv([(t("Report", "รายงาน"), str(out))])
    return 0


def skill(args):
    from pathlib import Path
    from . import agents
    src = Path(__file__).resolve().parent / "skill"
    if args.action == "path":
        term.emit({"ok": True, "path": str(src)})
        term.out(str(src))
        return 0
    if args.refresh:
        done = agents.refresh(src)
        term.emit({"ok": True, "installed": done})
        if not done:
            term.out(t("No installed skill to refresh. Install it with: glab-teleport skill install",
                       "ยังไม่ได้ติดตั้ง skill ติดตั้งด้วย: glab-teleport skill install"))
        for i in done:
            term.out(term.style("✓ ", "green") + (t("updated", "อัปเดตแล้ว") if i["changed"] else t("up to date", "ล่าสุดแล้ว")) + f"  {i['path']}")
        return 0
    if args.dir:
        plan = {Path(args.dir).expanduser(): []}
    else:
        chosen = agents.parse(args.agents) if args.agents else (agents.detect() or ["claude"])
        plan = agents.plan(chosen)
    done = [{"path": str(agents.install(src, d)), "agents": a} for d, a in plan.items()]
    twice = agents.opencode_sees_twice()
    term.emit({"ok": True, "installed": done, "opencode_duplicate": twice})
    term.out(term.style("✓ ", "green") + t("Skill installed", "ติดตั้ง skill แล้ว"))
    term.kv([(", ".join(agents.LABELS[x] for x in i["agents"]) or t("folder", "โฟลเดอร์"), i["path"]) for i in done])
    term.out(term.style("  " + t("Start a new agent session to load it.", "เปิด session ใหม่ของ agent เพื่อให้โหลด skill"), "dim"))
    if twice and "opencode" in agents.detect():
        term.out(term.style("  " + t("OpenCode reads both skill folders, so it will warn about a duplicate skill name. "
                                     "This is harmless; set OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1 to hide the warning.",
                                     "OpenCode อ่านทั้งสองโฟลเดอร์ จึงจะเตือนว่ามี skill ชื่อซ้ำ ไม่มีผลต่อการทำงาน "
                                     "ถ้าไม่อยากเห็นคำเตือนให้ตั้ง OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1"), "dim"))
    return 0


def repoint(args):
    from pathlib import Path
    from .run import session
    from .urls import known_pairs, repoint_script, url_prefixes
    s = session(args)
    pairs = known_pairs(s.work)
    if not pairs:
        raise SystemExit(t("Nothing teleported yet.", "ยังไม่มีรายการที่ย้ายแล้ว"))
    sample = [p for p in (s.dst.project(v) for v in list(pairs.values())[:3]) if p]
    https, ssh = url_prefixes(s.dst, sample)
    out = Path(args.output)
    out.write_text(repoint_script(s.src, https, ssh, pairs))
    out.chmod(0o755)
    term.emit({"ok": True, "script": str(out.resolve()), "repositories": len(pairs)})
    term.out(term.style("✓ ", "green") + t("{f} covers {n} repositories. Share it with your team:", "สร้าง {f} สำหรับ {n} repository แล้ว ส่งให้ทีมใช้ได้เลย:", f=out, n=len(pairs)))
    term.out(term.style(f"  ./{out.name} ~/code            " + t("# preview", "# ดูตัวอย่าง"), "dim"))
    term.out(term.style(f"  ./{out.name} --apply ~/code    " + t("# change remotes", "# เปลี่ยน remote"), "dim"))
    return 0


def _strip_globals(argv):
    """--lang and --no-color work anywhere on the command line; apply them and remove them before parsing."""
    out, skip = [], False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if a == "--lang":
            skip = True
            continue
        if a.startswith("--lang=") or a in ("--no-color", "--json"):
            continue
        out.append(a)
    return out


ARGPARSE_TH = {
    "usage: ": "วิธีใช้: ", "positional arguments": "อาร์กิวเมนต์", "options": "ตัวเลือก", "optional arguments": "ตัวเลือก",
    "show this help message and exit": "แสดงวิธีใช้แล้วออก", "show program's version number and exit": "แสดงเวอร์ชันแล้วออก",
    "the following arguments are required: %s": "ต้องระบุ: %s", "unrecognized arguments: %s": "ไม่รู้จักอาร์กิวเมนต์: %s",
    "invalid choice: %(value)r (choose from %(choices)s)": "ค่า %(value)r ไม่ถูกต้อง (เลือกจาก %(choices)s)",
    "expected one argument": "ต้องระบุค่า 1 ค่า", "%(prog)s: error: %(message)s\n": "%(prog)s: ผิดพลาด: %(message)s\n",
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if set_lang(resolve_lang(argv, config.load_config())) == "th":
        argparse._ = lambda s: ARGPARSE_TH.get(s, s)
    if "--no-color" in argv:
        term.set_color(False)
    as_json = "--json" in argv
    if as_json:
        term.set_json(True)
    argv = _strip_globals(argv)
    if as_json:
        code = 0
        try:
            main_inner(argv)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            if isinstance(e.code, str) and (term.RESULT is None or term.RESULT.get("ok") is not False):
                term.emit({"ok": False, "error": e.code})
        except KeyboardInterrupt:
            code = 130
            term.emit({"ok": False, "error": "interrupted"})
        except Exception as e:  # noqa — agents always get valid JSON
            code = 1
            term.emit({"ok": False, "error": f"{type(e).__name__}: {e}"})
        print(json.dumps(term.RESULT if term.RESULT is not None else {"ok": code == 0}, ensure_ascii=False, indent=2, default=str))
        raise SystemExit(code)
    main_inner(argv)


def main_inner(argv):
    try:
        if argv in (["--version"], ["-V"]):
            if term.JSON:
                return term.emit({"ok": True, "version": __version__})
            print(f"glab-teleport {__version__}")
            return 0
        values = {"--source-url", "--target-url"}
        positional = [a for i, a in enumerate(argv) if not a.startswith("-") and (i == 0 or argv[i - 1] not in values)]
        if not positional and not any(a in ("-h", "--help") for a in argv):
            argv = ["ui", *argv]  # no command → interactive mode
        args = parser().parse_args(shorthand(argv))
        raise SystemExit(dispatch(args) or 0)
    except KeyboardInterrupt:
        term.err("\n" + t("Interrupted. Re-running is safe: finished work is detected and skipped.",
                          "หยุดการทำงานแล้ว สามารถรันซ้ำได้อย่างปลอดภัย งานที่ทำเสร็จแล้วจะถูกข้าม"))
        raise SystemExit(130)
