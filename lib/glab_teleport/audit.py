"""`glab-teleport audit`: migration status of a whole instance (or one group), verified by commits."""
import json
from collections import Counter, defaultdict
from datetime import datetime

from . import config, term
from .i18n import t
from .match import build_mapping
from .plan import fetch_refs
from .run import header, session

CATS = ("synced", "outdated", "empty", "conflict", "missing")


def category(row):
    if row["match"] == "different-code":
        return "conflict"
    if not row["target"] or row["match"] in ("none", "weak"):
        return "missing"
    state = (row.get("sync") or {}).get("state")
    if state == "identical" and row["match"] == "verified":
        return "synced"
    if state == "empty":
        return "empty"
    return "outdated" if row["match"] == "verified" else "missing"


def cat_label(c):
    return {"synced": t("in sync", "ตรงกันแล้ว"), "outdated": t("out of date", "ยังไม่อัปเดต"), "empty": t("target empty", "ปลายทางว่าง"),
            "conflict": t("different code", "code ไม่ตรงกัน"), "missing": t("not on target", "ยังไม่ได้ย้าย")}[c]


def run(args):
    s = session(args)
    header(s)
    with term.Status(t("Reading projects…", "กำลังอ่านรายการ project…")) as st:
        def side(gl, path):
            if path:
                g = gl.group(path)
                if not g:
                    raise SystemExit(t("Group '{g}' not found on {u}", "ไม่พบ group '{g}' ใน {u}", g=path, u=gl.url))
                return gl.subtree(g)[0]
            return gl.inventory()["projects"]
        sp, dp = side(s.src, args.source_group), side(s.dst, args.target_group)
        st.update(t("Comparing commits of {a} + {b} repositories…", "กำลังเทียบ commit ของ {a} + {b} repository…", a=len(sp), b=len(dp)))
        fs, fd = fetch_refs(s.src, sp, 8), fetch_refs(s.dst, dp, 8)
        rows, unpaired = build_mapping(sp, dp, fs, fd)
    for r in rows:
        r["category"] = category(r)
    by_group = defaultdict(Counter)
    for r in rows:
        by_group[r["source"].split("/")[0]][r["category"]] += 1
    term.heading(t("Migration audit", "สถานะการย้าย"), f"{args.source_group or s.src.url} → {args.target_group or s.dst.url}")
    term.out("")
    hdr = [t("SOURCE GROUP", "group ต้นทาง"), t("PROJECTS", "project")] + [cat_label(c) for c in CATS]
    table_rows = []
    for g in sorted(by_group):
        c = by_group[g]
        table_rows.append([g, str(sum(c.values()))] + [term.style(str(c[k]), {"synced": "green", "outdated": "blue", "empty": "cyan",
                                                                               "conflict": "yellow", "missing": "dim"}[k]) if c[k] else "·" for k in CATS])
    tot = Counter(r["category"] for r in rows)
    table_rows.append([term.style(t("Total", "รวม"), "bold"), term.style(str(len(rows)), "bold")] + [term.style(str(tot[k]), "bold") for k in CATS])
    term.table(hdr, table_rows, aligns=["<", ">"] + [">"] * len(CATS))
    if unpaired:
        term.out("")
        term.out(term.style("  " + t("{n} target projects have no source counterpart (created on the target, or renamed beyond recognition).",
                                     "มี {n} project ที่ปลายทางซึ่งไม่พบต้นทาง (สร้างใหม่ที่ปลายทาง หรือเปลี่ยนชื่อจนจับคู่ไม่ได้)", n=len(unpaired)), "dim"))
    out_dir = config.WORK_DIR / "audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    data = {"generated": datetime.now().isoformat(timespec="seconds"), "source": s.src.url, "target": s.dst.url,
            "source_group": args.source_group, "target_group": args.target_group, "rows": rows, "target_only": unpaired}
    (out_dir / "mapping.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    md = [f"# {t('Migration audit', 'สถานะการย้าย')}", "", f"- {t('Source', 'ต้นทาง')}: {s.src.url} `{args.source_group or '*'}`",
          f"- {t('Target', 'ปลายทาง')}: {s.dst.url} `{args.target_group or '*'}`", f"- {t('Generated', 'สร้างเมื่อ')}: {data['generated']}", ""]
    for c in CATS:
        sel = [r for r in rows if r["category"] == c]
        if not sel:
            continue
        md += [f"## {cat_label(c)} ({len(sel)})", "", f"| {t('Source', 'ต้นทาง')} | {t('Target', 'ปลายทาง')} | |", "|---|---|---|"]
        for r in sel:
            sync = r.get("sync") or {}
            note = ", ".join(x for x in (f"{len(sync.get('missing', []))} missing" if sync.get("missing") else "",
                                         f"{len(sync.get('different', []))} differ" if sync.get("different") else "") if x)
            md.append(f"| `{r['source']}` | `{r['target'] or '—'}` | {note} |")
        md.append("")
    if unpaired:
        md += [f"## {t('Only on target', 'มีเฉพาะที่ปลายทาง')} ({len(unpaired)})", ""] + [f"- `{p}`" for p in unpaired] + [""]
    (out_dir / "audit.md").write_text("\n".join(md))
    term.emit({"ok": True, "report_path": str(out_dir / "audit.md"), "summary": dict(tot),
               "by_group": {g: dict(c) for g, c in by_group.items()}, "rows": rows, "target_only": unpaired})
    term.out("")
    term.kv([(t("Report", "รายงาน"), str(out_dir / "audit.md"))])
    return 0
