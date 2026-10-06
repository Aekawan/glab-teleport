"""Run reports: a compact terminal summary plus report.md (for people) and report.json (for tools)."""
import json
import re
import time
from datetime import datetime
from pathlib import Path

from . import __version__, term
from .i18n import lang, t
from .plan import SKIP
from .verify import totals

ICON = {"ok": "✓", "warn": "!", "manual": "☞", "fail": "✗", "info": "·"}
COLOR = {"ok": "green", "warn": "yellow", "manual": "magenta", "fail": "red", "info": "dim"}
MD_ICON = {"ok": "✅", "warn": "⚠️", "manual": "👉", "fail": "❌", "info": "ℹ️", "skip": "⏭️"}
LEVEL_ORDER = {"info": 0, "manual": 1, "warn": 2, "fail": 3}


def skipped_word(rep):
    return t("skipped", "ข้าม") if rep["mode"] == "teleport" else t("not on target yet", "ยังไม่ได้ย้าย")


def new_run_dir(work, label):
    slug = re.sub(r"[^\w.-]+", "-", label).strip("-")[:60] or "run"
    d = Path(work) / "runs" / f"{datetime.now():%Y%m%d-%H%M%S}-{slug}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def status_text(st):
    return {"ok": t("verified", "ตรวจแล้วตรงกัน"), "warn": t("warning", "มีข้อสังเกต"), "manual": t("action needed", "ต้องดำเนินการเอง"),
            "fail": t("failed", "ไม่สำเร็จ"), "skip": t("skipped", "ข้าม"), "info": "info"}[st]


def build(s, plan, components, items, projects, groups, started, mode="teleport"):
    tot, scopes = totals(projects, groups)
    by_src = {p["source"]: p for p in projects}
    skipped = [i for i in plan["items"] if i not in items]
    counts = {k: sum(1 for p in projects if p["status"] == k) for k in ("ok", "warn", "manual", "fail")}
    return {
        "tool": "glab-teleport", "version": __version__, "mode": mode, "language": lang(),
        "started": datetime.fromtimestamp(started).isoformat(timespec="seconds"), "seconds": round(time.time() - started),
        "kind": plan["kind"], "source": plan["source"], "target": plan["target"], "layout": plan.get("layout"),
        "source_url": s.src.url, "target_url": s.dst.url,
        "source_user": (s.src.user or {}).get("username"), "target_user": (s.dst.user or {}).get("username"),
        "components": components, "options": {k: v for k, v in s.opts.items() if v and k not in ("jobs",)},
        "counts": counts, "totals": dict(tot), "scopes": scopes,
        "projects": [{**by_src.get(i["source"], {"source": i["source"], "target": i["target"], "status": "fail", "issues": []}),
                      "plan_state": i["state"], "steps": (s.results.get(i["source"]) or {}).get("steps", {})} for i in items],
        "skipped": [{"source": i["source"], "target": i["target"], "reason": i["reason"]} for i in skipped],
        "groups": groups, "group_steps": s.group_results and [
            {"source": g["source"], "target": g["target"], "steps": g["steps"]} for g in s.group_results],
        "moves": s.moves,
        "runner_tokens": str(s.runner_tokens) if s.runner_created else None, "runners_created": s.runner_created,
    }


# ── terminal ──
def print_summary(rep, run_dir):
    tot = rep["totals"]
    title = t("Teleport result", "ผลการย้าย") if rep["mode"] == "teleport" else t("Verification result", "ผลการตรวจสอบ")
    term.heading(title, f"{rep['kind']} · {rep['source']} → {rep['target']} · {term.elapsed(rep['seconds'])}")
    rows = []

    def line(label, a, b, indent=False):
        ok = a == b
        mark = term.style("✓", "green") if ok else term.style("✗", "red")
        rows.append(((" " * 2 if indent else "") + label, str(a), str(b), mark if not indent else ""))

    n_proj = len(rep["projects"])
    line(t("Projects", "Project"), n_proj, sum(1 for p in rep["projects"] if not any(i["area"] == "project" for i in p["issues"])))
    comps = rep["components"]
    if "repo" in comps:
        line(t("Branches", "Branch"), tot.get("source_branches", 0), tot.get("source_branches", 0) - _ref_gap(rep, "branch"))
        line(t("Tags", "Tag"), tot.get("source_tags", 0), tot.get("source_tags", 0) - _ref_gap(rep, "tag"))
        line(t("Protected rules", "Protected rules"), tot.get("source_protected", 0), tot.get("matched_protected", 0))
    if "env" in comps:
        line(t("CI/CD variables", "CI/CD variables"), tot.get("source_vars", 0), tot.get("matched_vars", 0))
        for sc in sorted(rep["scopes"], key=lambda x: (x != "*", x)):
            a, b = rep["scopes"][sc]
            line(t("scope {s}", "scope {s}", s=sc), a, b, indent=True)
        if tot.get("source_group_vars"):
            line(t("Group variables", "Group variables"), tot.get("source_group_vars", 0), tot.get("matched_group_vars", 0))
        line(t("Environments", "Environments"), tot.get("source_envs", 0), tot.get("matched_envs", 0))
    if "runner" in comps and tot.get("source_runners"):
        line(t("Runners", "Runners"), tot.get("source_runners", 0), tot.get("matched_runners", 0))
    term.out("")
    term.table(["", t("Source", "ต้นทาง"), t("Target", "ปลายทาง"), ""], rows, aligns=["<", ">", ">", "<"])
    c = rep["counts"]
    parts = [term.style(f"✓ {c['ok']} " + t("verified", "ตรงกันครบ"), "green")]
    if c["warn"]:
        parts.append(term.style(f"! {c['warn']} " + t("with warnings", "มีข้อสังเกต"), "yellow"))
    if c["manual"]:
        parts.append(term.style(f"☞ {c['manual']} " + t("need manual action", "ต้องดำเนินการเอง"), "magenta"))
    if c["fail"]:
        parts.append(term.style(f"✗ {c['fail']} " + t("failed", "ไม่สำเร็จ"), "red"))
    if rep["skipped"]:
        parts.append(term.style(f"– {len(rep['skipped'])} " + skipped_word(rep), "dim"))
    term.out("")
    term.out("  " + "   ".join(parts))
    attention = [p for p in rep["projects"] if p["status"] != "ok"] + [g for g in rep["groups"] if g["status"] != "ok"]
    if attention:
        term.out("")
        term.out("  " + term.style(t("Needs attention", "รายการที่ต้องตรวจสอบ"), "bold"))
        for p in attention[:12]:
            term.out(f"  {term.style(ICON[p['status']], COLOR[p['status']])} {p['source']}")
            seen = []
            for i in sorted((i for i in p["issues"] if i["level"] != "info"), key=lambda i: -LEVEL_ORDER[i["level"]]):
                if i["area"] in seen:
                    continue
                seen.append(i["area"])
                term.out("      " + term.style(f"{i['area']}: ", "dim") + term.fit(i["text"], term.term_cols() - 20))
        if len(attention) > 12:
            term.out(term.style("  " + t("…and {n} more in the report", "…และอีก {n} รายการในรายงาน", n=len(attention) - 12), "dim"))
    failed_moves = [m for m in rep["moves"] if m["status"] != "ok"]
    for m in failed_moves:
        term.out(term.style(f"  ✗ " + t("could not move {a} → {b} ({d}); it was updated in place",
                                         "ย้าย {a} → {b} ไม่ได้ ({d}) จึงอัปเดตที่ตำแหน่งเดิม", a=m["from"], b=m["to"], d=m.get("detail", "")), "red"))
    if rep["runner_tokens"]:
        term.out("")
        term.out("  " + term.style("☞ ", "magenta") + t("{n} runners created. Register your runner hosts with the tokens in:",
                                                        "สร้าง runner ใหม่ {n} ตัว ลงทะเบียนเครื่อง runner ด้วย token ในไฟล์:", n=rep["runners_created"]))
        term.out("    " + rep["runner_tokens"])
    term.out("")
    term.kv([(t("Report", "รายงาน"), str(run_dir / "report.md"))])


def _ref_gap(rep, kind):
    n = 0
    for p in rep["projects"]:
        r = p.get("refs")
        if r:
            bad = set(r["missing"]) | set(r["different"])
            n += sum(1 for x in bad if (x.startswith("refs/heads/") if kind == "branch" else x.startswith("refs/tags/")))
    return n


# ── files ──
def write(rep, run_dir):
    run_dir = Path(run_dir)
    (run_dir / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=list))
    (run_dir / "report.md").write_text(markdown(rep))
    return run_dir / "report.md"


def _yn(b):
    return "✓" if b else ""


def markdown(rep):
    tot = rep["totals"]
    c = rep["counts"]
    L = []
    title = t("Teleport report", "รายงานการย้าย") if rep["mode"] == "teleport" else t("Verification report", "รายงานการตรวจสอบ")
    L += [f"# {title}: `{rep['source']}` → `{rep['target']}`", ""]
    result = f"✅ {c['ok']} " + t("verified", "ตรงกันครบ")
    if c["warn"]:
        result += f" · ⚠️ {c['warn']} " + t("warnings", "มีข้อสังเกต")
    if c["manual"]:
        result += f" · 👉 {c['manual']} " + t("manual action", "ต้องดำเนินการเอง")
    if c["fail"]:
        result += f" · ❌ {c['fail']} " + t("failed", "ไม่สำเร็จ")
    if rep["skipped"]:
        result += f" · {'⏭️' if rep['mode'] == 'teleport' else '⬜'} {len(rep['skipped'])} " + skipped_word(rep)
    L += ["| | |", "|---|---|",
          f"| {t('Result', 'ผลลัพธ์')} | {result} |",
          f"| {t('Started', 'เริ่ม')} | {rep['started'].replace('T', ' ')} · {term.elapsed(rep['seconds'])} |",
          f"| {t('Source', 'ต้นทาง')} | {rep['source_url']}/{rep['source']} ({rep['source_user']}) |",
          f"| {t('Target', 'ปลายทาง')} | {rep['target_url']}/{rep['target']} ({rep['target_user']}) |",
          f"| {t('Transferred', 'สิ่งที่ย้าย') if rep['mode'] == 'teleport' else t('Checked', 'สิ่งที่ตรวจ')} | {', '.join(rep['components'])} |"]
    if rep.get("layout"):
        L.append(f"| {t('Layout', 'โครงสร้าง')} | {rep['layout']} |")
    shown = [k for k in rep["options"] if k not in ("cache_dir", "yes", "dry_run")]
    if shown:
        L.append(f"| {t('Options', 'ตัวเลือก')} | {', '.join('--' + k.replace('_', '-') for k in shown)} |")
    L += [f"| {t('Tool', 'เครื่องมือ')} | glab-teleport {rep['version']} |", ""]

    L += [f"## {t('Summary', 'สรุป')}", "", f"| | {t('Source', 'ต้นทาง')} | {t('Target', 'ปลายทาง')} | |", "|---|---:|---:|:-:|"]

    def row(label, a, b):
        L.append(f"| {label} | {a} | {b} | {'✅' if a == b else '❌'} |")
    comps = rep["components"]
    row(t("Projects", "Project"), len(rep["projects"]), sum(1 for p in rep["projects"] if not any(i["area"] == "project" for i in p["issues"])))
    if "repo" in comps:
        row(t("Branches", "Branch"), tot.get("source_branches", 0), tot.get("source_branches", 0) - _ref_gap(rep, "branch"))
        row(t("Tags", "Tag"), tot.get("source_tags", 0), tot.get("source_tags", 0) - _ref_gap(rep, "tag"))
        row(t("Protected branch/tag rules", "กฎ protected branch/tag"), tot.get("source_protected", 0), tot.get("matched_protected", 0))
    if "env" in comps:
        row(t("CI/CD variables (project)", "CI/CD variables (project)"), tot.get("source_vars", 0), tot.get("matched_vars", 0))
        if tot.get("source_group_vars"):
            row(t("CI/CD variables (group)", "CI/CD variables (group)"), tot.get("source_group_vars", 0), tot.get("matched_group_vars", 0))
        row(t("Environments", "Environments"), tot.get("source_envs", 0), tot.get("matched_envs", 0))
    if "runner" in comps:
        row(t("Project runners", "Project runners"), tot.get("source_runners", 0), tot.get("matched_runners", 0))
    L.append("")
    if rep["scopes"]:
        L += [f"### {t('CI/CD variables by environment scope', 'CI/CD variables แยกตาม environment scope')}", "",
              f"| Scope | {t('Source', 'ต้นทาง')} | {t('Target', 'ปลายทาง')} | |", "|---|---:|---:|:-:|"]
        for sc in sorted(rep["scopes"], key=lambda x: (x != "*", x)):
            a, b = rep["scopes"][sc]
            L.append(f"| `{sc}` | {a} | {b} | {'✅' if a == b else '❌'} |")
        L += ["", "> " + t("Variables are compared per key and environment scope on value, type, protected, masked and raw. "
                           "Values are never written to this report.",
                           "ตรวจ variables ทีละ key และ environment scope ทั้งค่า type, protected, masked และ raw "
                           "โดยไม่บันทึกค่าจริงลงในรายงานนี้"), ""]

    attention = [p for p in rep["projects"] if p["status"] != "ok"] + [g for g in rep["groups"] if g["status"] != "ok"]
    if attention or rep["runner_tokens"]:
        L += [f"## {t('Needs attention', 'รายการที่ต้องตรวจสอบ')}", ""]
        for p in attention:
            L.append(f"- {MD_ICON[p['status']]} **{p['source']}** → `{p['target']}`")
            for i in p["issues"]:
                if i["level"] != "info":
                    L.append(f"  - {i['area']}: {i['text']}")
        if rep["runner_tokens"]:
            L.append("- 👉 " + t("Register runner hosts with the tokens saved in `{f}`: `gitlab-runner register --url {u} --token <token>`",
                                  "ลงทะเบียนเครื่อง runner ด้วย token ในไฟล์ `{f}`: `gitlab-runner register --url {u} --token <token>`",
                                  f=rep["runner_tokens"], u=rep["target_url"]))
        L.append("")

    L += [f"## {t('Projects', 'Project')}", "",
          f"| | {t('Source', 'ต้นทาง')} | {t('Target', 'ปลายทาง')} | Branches | Tags | Variables | {t('Notes', 'หมายเหตุ')} |",
          "|:-:|---|---|---:|---:|---:|---|"]
    for p in rep["projects"]:
        r = p.get("refs") or {}
        br = f"{r.get('target_branches', '')}/{r.get('source_branches', '')}" if r else ""
        tg = f"{r.get('target_tags', '')}/{r.get('source_tags', '')}" if r else ""
        vs = p.get("variables")
        vv = f"{sum(1 for x in vs if x['status'] in ('ok', 'warn', 'manual'))}/{sum(1 for x in vs if x['status'] != 'info')}" if vs is not None else ""
        note = "; ".join(i["text"] for i in p["issues"] if i["level"] in ("fail", "warn"))[:120]
        L.append(f"| {MD_ICON[p['status']]} | `{p['source']}` | `{p['target']}` | {br} | {tg} | {vv} | {note} |")
    for sk in rep["skipped"]:
        L.append(f"| {'⏭️' if rep['mode'] == 'teleport' else '⬜'} | `{sk['source']}` | `{sk['target']}` | | | | {sk['reason']} |")
    L += ["", "_" + t("Counts are target / source.", "ตัวเลขคือ ปลายทาง / ต้นทาง") + "_", ""]

    if rep["moves"]:
        L += [f"## {t('Relocated projects', 'Project ที่ย้ายตำแหน่ง')}", ""]
        L += [f"- {MD_ICON['ok' if m['status'] == 'ok' else 'fail']} `{m['from']}` → `{m['to']}` {m.get('detail', '')}" for m in rep["moves"]]
        L.append("")

    if rep["groups"]:
        L += [f"## {t('Group variables', 'Group variables')}", ""]
        for g in rep["groups"]:
            L += [f"### {MD_ICON[g['status']]} `{g['source']}` → `{g['target']}`", ""] + _var_table(g["variables"]) + [""]

    L += [f"## {t('Details', 'รายละเอียด')}", ""]
    for p in rep["projects"]:
        r = p.get("refs")
        if not (p.get("steps") or p.get("variables") or (r and (r["missing"] or r["different"]))):
            continue
        L += [f"### {MD_ICON[p['status']]} `{p['source']}` → `{p['target']}`", ""]
        if p.get("steps"):
            L += [f"| {t('Step', 'ขั้นตอน')} | | {t('Detail', 'รายละเอียด')} |", "|---|:-:|---|"]
            for step, v in p["steps"].items():
                L.append(f"| {step} | {MD_ICON.get(v['status'], '')} | {v.get('detail', '')} |")
            L.append("")
        r = p.get("refs")
        if r and (r["missing"] or r["different"]):
            L.append(t("Branches/tags not matching: ", "branch/tag ที่ไม่ตรง: ") + ", ".join(f"`{x}`" for x in (r["missing"] + r["different"])[:30]))
            L.append("")
        if p.get("variables"):
            L += _var_table(p["variables"]) + [""]
    L += ["---", t("Generated by glab-teleport {v}.", "สร้างโดย glab-teleport {v}", v=rep["version"]), ""]
    return "\n".join(L)


def _var_table(rows):
    if not rows:
        return ["_" + t("No variables.", "ไม่มีตัวแปร") + "_"]
    out = [f"| Key | Scope | Type | Protected | Masked | {t('Result', 'ผล')} |", "|---|---|---|:-:|:-:|---|"]
    for r in rows:
        res = MD_ICON.get(r["status"], "") + (" " + r["detail"] if r["detail"] else "")
        out.append(f"| `{r['key']}` | `{r['scope']}` | {r['type']} | {_yn(r['protected'])} | {_yn(r['masked'])} | {res} |")
    return out


def list_runs(work):
    d = Path(work) / "runs"
    return sorted((p for p in d.glob("*") if (p / "report.json").exists()), reverse=True) if d.exists() else []
