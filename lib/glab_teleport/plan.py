"""Planning: decide, for every source project, where it goes and what will happen — before anything is written."""
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from . import term
from .gitlab import GitError, ls_remote, pending_delete, q
from .history import known_pairs
from .i18n import t
from .match import compare_refs, describe_sync, guess_layout, layout_target, norm, pair_by_content

COMPONENTS = {
    "repo": ("Branches & tags (incl. LFS), wiki, default branch, protected branches/tags",
             "branch และ tag ทั้งหมด (รวม LFS), wiki, default branch, protected branch/tag"),
    "env": ("CI/CD variables in every environment scope (group + project), environments",
            "CI/CD variables ทุก environment scope (ระดับ group และ project) และ environments"),
    "runner": ("Project/group runners recreated with the same settings; registration tokens saved for you",
               "สร้าง runner ของ group/project ด้วยค่าเดิม และเก็บ token สำหรับลงทะเบียนเครื่องไว้ให้"),
    "settings": ("Project settings: merge options, CI config path, timeouts, features, archived state",
                 "ค่าตั้งของ project: merge options, CI config path, timeout, features, สถานะ archive"),
    "extras": ("Pipeline schedules (created paused), webhooks, deploy keys, members",
               "pipeline schedules (สร้างแบบปิดไว้), webhooks, deploy keys, members"),
}
ALIASES = {"code": "repo", "git": "repo", "vars": "env", "variables": "env", "ci": "env", "runners": "runner",
           "setting": "settings", "extra": "extras"}

# per-project state in a plan
NEW, SYNCED, UPDATE, MOVE, SKIP = "new", "synced", "update", "move", "skip"


def describe_component(c):
    return t(*COMPONENTS[c])


def parse_only(text):
    if not text or text.strip().lower() == "all":
        return list(COMPONENTS)
    picked = []
    for word in text.replace(",", " ").lower().split():
        if word == "all":
            return list(COMPONENTS)
        word = ALIASES.get(word, word)
        if word not in COMPONENTS:
            raise SystemExit(t("Unknown component '{w}'. Choose from: {c}, all",
                               "ไม่รู้จัก '{w}' เลือกได้จาก: {c}, all", w=word, c=", ".join(COMPONENTS)))
        picked.append(word)
    return [c for c in COMPONENTS if c in picked]


def clean_path(s):
    """Accept a path, a web URL, or an https/ssh git URL."""
    import urllib.parse
    s = s.strip()
    if "://" in s:
        s = urllib.parse.urlsplit(s).path.split("/-/")[0]
    elif s.startswith("git@") and ":" in s:
        s = s.split(":", 1)[1]
    s = s.strip("/")
    return s[:-4] if s.endswith(".git") else s


def fetch_refs(gl, projects, jobs=8):
    def one(p):
        try:
            return p["path_with_namespace"], {"refs": ls_remote(gl, p["http_url_to_repo"])}
        except GitError as e:
            return p["path_with_namespace"], {"refs": {}, "error": str(e)[:200]}
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:  # ask git even for "empty" projects: the flag can be stale
        return {k: v for k, v in ex.map(one, projects) if v.get("refs") or v.get("error")}


def _entry(sp, tp, state, reason="", src=None, dst=None):
    return {"source": sp, "target": tp, "state": state, "reason": reason, "src": src, "dst": dst,
            "sync": None, "move_from": None, "archived": bool(src and src.get("archived"))}


def plan_group(session, src_path, dst_path, layout="keep", include=None, exclude=None, relocate=False, status=None):
    src, dst, jobs = session.src, session.dst, session.jobs
    say = status.update if status else (lambda s: None)
    sg = src.group(src_path)
    if not sg:
        raise SystemExit(t("Source group '{g}' was not found on {u}", "ไม่พบ group '{g}' ใน {u}", g=src_path, u=src.url))
    src_root = sg["full_path"]
    say(t("Reading source group…", "กำลังอ่าน group ต้นทาง…"))
    sprojs, ssubs = src.subtree(sg)
    under = lambda path, x: path == x or path.startswith(x.rstrip("/") + "/")
    rel = lambda path: path[len(src_root) + 1:]
    hit = lambda p, xs: any(under(rel(p["path_with_namespace"]), x) or under(p["path_with_namespace"], x) for x in xs)
    sprojs = [p for p in sprojs if (not include or hit(p, include)) and not hit(p, exclude or [])]

    dg = dst.group(dst_path)
    if dg and pending_delete(dg):
        raise SystemExit(t("Target group '{g}' is scheduled for deletion. Delete it immediately (Settings → General → Advanced) "
                           "or wait until GitLab removes it, then run again.",
                           "group ปลายทาง '{g}' อยู่ระหว่างรอลบ กรุณาลบทันที (Settings → General → Advanced) "
                           "หรือรอให้ GitLab ลบเสร็จ แล้วรันใหม่อีกครั้ง", g=dst_path))
    dst_root = dg["full_path"] if dg else dst_path
    if not dg:
        parent = dst_path.rpartition("/")[0]
        if not parent or not dst.group(parent):
            raise SystemExit(t("Target group '{g}' does not exist on {u}. Only subgroups of an existing group can be created.",
                               "ไม่พบ group ปลายทาง '{g}' ใน {u} (สร้างได้เฉพาะ subgroup ใต้ group ที่มีอยู่แล้ว)", g=dst_path, u=dst.url))
    say(t("Reading target group…", "กำลังอ่าน group ปลายทาง…"))
    dprojs, dsubs = dst.subtree(dg) if dg else ([], [])
    say(t("Comparing commits on both sides…", "กำลังเทียบ commit ทั้งสองฝั่ง…"))
    fp_src, fp_dst = fetch_refs(src, sprojs, jobs), fetch_refs(dst, dprojs, jobs)
    content = pair_by_content(fp_src, fp_dst)
    history = known_pairs(session.work) if hasattr(session, "work") else {}

    detected = None
    if layout == "auto":
        layout, n = guess_layout(content, src_root, dst_root)
        detected = n
    dmap = {p["path_with_namespace"].lower(): p for p in dprojs}
    taken = {dp for dp, _ in content.values()}
    empties = defaultdict(list)
    for p in dprojs:
        if p.get("empty_repo") and p["path_with_namespace"] not in taken:
            empties[norm(p["path"])].append(p["path_with_namespace"])
    slug_n = Counter(norm(p["path"]) for p in sprojs)

    items = []
    for p in sprojs:
        sp = p["path_with_namespace"]
        want = layout_target(dst_root, rel(sp), layout)
        prev = history.get(sp)
        prev = prev if prev and prev.lower() in dmap and prev not in taken else None
        if sp in content or prev:
            if sp in content:
                dp, n = content[sp]
                why = t("already on target ({n} matching refs)", "มีอยู่ที่ปลายทางแล้ว (ref ตรงกัน {n})", n=n)
            else:  # an earlier run put it there (e.g. an empty repository has no commits to match)
                dp, why = dmap[prev.lower()]["path_with_namespace"], t("teleported here before", "เคยย้ายมาไว้ที่นี่แล้ว")
                taken.add(dp)
            it = _entry(sp, dp, SYNCED, why, p, dmap[dp.lower()])
            if dp.lower() != want.lower() and relocate:
                it.update(state=MOVE, target=want, move_from=dp,
                          reason=t("move {a} → {b}", "ย้าย {a} → {b}", a=dp, b=want))
        else:
            t_ = dmap.get(want.lower())
            if t_ and t_["path_with_namespace"] in taken:
                it = _entry(sp, want, SKIP, t("name is used by another project on target", "ชื่อนี้ถูก project อื่นที่ปลายทางใช้อยู่"), p)
            elif t_:
                if not (fp_src.get(sp) or {}).get("refs") or not (fp_dst.get(t_["path_with_namespace"]) or {}).get("refs"):
                    it = _entry(sp, t_["path_with_namespace"], UPDATE, t("target project exists and is empty", "มี project ปลายทางแล้ว (ว่าง)"), p, t_)
                else:
                    it = _entry(sp, t_["path_with_namespace"], SKIP,
                                t("target has unrelated code (no shared commits)", "ปลายทางมี code อื่นอยู่ (ไม่มี commit ร่วมกัน)"), p, t_)
                    it["conflict"] = True
                taken.add(t_["path_with_namespace"])
            else:
                c = [x for x in empties[norm(p["path"])] if x not in taken]
                if len(c) == 1 and slug_n[norm(p["path"])] == 1:
                    it = _entry(sp, c[0], UPDATE, t("prepared empty project with the same name", "ใช้ project ว่างที่เตรียมไว้ (ชื่อตรงกัน)"),
                                p, dmap[c[0].lower()])
                    taken.add(c[0])
                else:
                    it = _entry(sp, want, NEW, "", p)
        items.append(it)

    # name clashes after flattening: fall back to subgroup-joined names (team/api -> team-api)
    used = Counter(i["target"].lower() for i in items if i["state"] != SKIP)
    for it in items:
        clash = it["state"] == NEW and (used[it["target"].lower()] > 1 or it["target"].lower() in dmap)
        if clash or (it["state"] == SKIP and not it.get("conflict") and layout != "keep"):
            alt = layout_target(dst_root, rel(it["source"]), "join")
            if alt.lower() not in dmap and used[alt.lower()] == 0:
                if it["state"] == NEW:
                    used[it["target"].lower()] -= 1
                used[alt.lower()] += 1
                it.update(state=NEW, target=alt, reason=t("renamed to avoid a name clash", "เปลี่ยนชื่อเพื่อไม่ให้ชนกัน"))
            elif it["state"] == NEW:
                it.update(state=SKIP, reason=t("name clash — teleport this project separately", "ชื่อชนกัน — ให้ย้าย project นี้แยก"))

    # a group and a project can't share a path: subgroups we must create vs. projects that stay in place
    moved_away = {i["move_from"].lower() for i in items if i["move_from"]}
    staying = (set(dmap) - moved_away) | {i["target"].lower() for i in items if i["state"] != SKIP}
    for it in items:
        if it["state"] in (NEW, MOVE):
            parts = it["target"].split("/")
            for k in range(len(dst_root.split("/")) + 1, len(parts)):
                ns = "/".join(parts[:k]).lower()
                if ns in staying:
                    it.update(state=SKIP, reason=t("subgroup {g} would collide with an existing project of the same name (use --relocate)",
                                                    "subgroup {g} ชนกับ project ที่มีชื่อเดียวกันอยู่แล้ว (ใช้ --relocate)", g="/".join(parts[:k])))
                    break

    for it in items:
        if it["state"] in (SYNCED, UPDATE, MOVE):
            dp = it["move_from"] or it["target"]
            it["sync"] = compare_refs((fp_src.get(it["source"]) or {}).get("refs"), (fp_dst.get(dp) or {}).get("refs") or {})
            if it["state"] == SYNCED and it["sync"]["state"] != "identical":
                it["state"] = UPDATE
            elif it["state"] == UPDATE and it["sync"]["state"] == "identical":
                it["state"] = SYNCED

    pairs = [(sg, dst_root)]
    relevant = [g for g in ssubs if any(under(i["source"], g["full_path"]) for i in items)]
    if layout == "keep":
        pairs += [(g, dst_root + g["full_path"][len(src_root):]) for g in relevant]
    access = 50 if (dst.user or {}).get("is_admin") else 0
    if dg and not access:
        m = dst.try_get(f"/groups/{dg['id']}/members/all/{(dst.user or {}).get('id')}")
        access = (m or {}).get("access_level", 0)
    return {"kind": "group", "source": src_root, "target": dst_root, "target_exists": bool(dg), "layout": layout, "access": access,
            "layout_detected": detected, "items": items, "group_pairs": pairs,
            "flattened_groups": [] if layout == "keep" else relevant, "runner_groups": [sg] + relevant,
            "fingerprints": (fp_src, fp_dst), "src_group": sg}


def plan_projects(session, src_paths, dst_path, status=None):
    src, dst = session.src, session.dst
    if len(src_paths) > 1 and dst.project(dst_path):
        raise SystemExit(t("When teleporting several projects the destination must be a group, not project '{p}'.",
                           "เมื่อย้ายหลาย project ปลายทางต้องเป็น group ไม่ใช่ project '{p}'", p=dst_path))
    items = []
    for sp in src_paths:
        if status:
            status.update(t("Checking {p}…", "กำลังตรวจ {p}…", p=sp))
        p = src.project(sp)
        if not p:
            raise SystemExit(t("Source project '{p}' was not found on {u}", "ไม่พบ project '{p}' ใน {u}", p=sp, u=src.url))
        target = dst.project(dst_path)
        tp = target["path_with_namespace"] if target else dst_path
        if not target:
            ns = dst.try_get(f"/namespaces/{q(dst_path)}")
            if ns:
                tp = f"{ns['full_path']}/{p['path']}"
                target = dst.project(tp)
            elif not dst.try_get(f"/namespaces/{q(dst_path.rpartition('/')[0])}"):
                parent = dst_path.rpartition("/")[0].rpartition("/")[0]
                if not parent or not dst.group(parent):
                    raise SystemExit(t("No parent group for '{p}' on {u}", "ไม่พบ group ปลายทางของ '{p}' ใน {u}", p=dst_path, u=dst.url))
        it = _entry(p["path_with_namespace"], tp, NEW, "", p, target)
        if target:
            a = fetch_refs(src, [p]).get(p["path_with_namespace"])
            b = fetch_refs(dst, [target]).get(target["path_with_namespace"])
            sa, sb = (a or {}).get("refs") or {}, (b or {}).get("refs") or {}
            if sa and sb and not set(sa.values()) & set(sb.values()):
                it.update(state=SKIP, reason=t("target has unrelated code (no shared commits)", "ปลายทางมี code อื่นอยู่ (ไม่มี commit ร่วมกัน)"),
                          conflict=True)
            else:
                it["sync"] = compare_refs(sa, sb)
                it["state"] = SYNCED if it["sync"]["state"] == "identical" else UPDATE
        items.append(it)
    seen = Counter(i["target"].lower() for i in items)
    for i in items:
        if seen[i["target"].lower()] > 1:
            i.update(state=SKIP, reason=t("two selected projects would get the same name", "มี project ที่เลือกได้ชื่อซ้ำกัน"))
    return {"kind": "project", "source": src_paths[0] if len(src_paths) == 1 else t("{n} projects", "{n} project", n=len(src_paths)),
            "target": dst_path, "target_exists": True, "layout": None, "layout_detected": None, "items": items,
            "group_pairs": [], "flattened_groups": [], "runner_groups": [], "fingerprints": ({}, {}), "src_group": None}


# ── presentation ──
STATE_VIEW = {
    NEW: ("new", "+", "cyan"), SYNCED: ("synced", "✓", "green"), UPDATE: ("update", "↻", "blue"),
    MOVE: ("move", "↪", "magenta"), SKIP: ("skip", "!", "yellow"),
}


def state_label(state):
    labels = {NEW: t("new", "สร้างใหม่"), SYNCED: t("in sync", "ตรงกันแล้ว"), UPDATE: t("update", "อัปเดต"),
              MOVE: t("move", "ย้ายที่"), SKIP: t("skip", "ข้าม")}
    _, sym, color = STATE_VIEW[state]
    return term.style(f"{sym} {labels[state]}", color)


def print_plan(plan, components, opts):
    items = plan["items"]
    head = t("Plan", "แผนการย้าย")
    term.heading(head, f"{plan['kind']} · {plan['source']} → {plan['target']}")
    rows = []
    if plan["kind"] == "group":
        if not plan["target_exists"]:
            rows.append((t("Target", "ปลายทาง"), term.style(t("will be created", "จะสร้าง group ใหม่"), "cyan")))
        lay = plan["layout"]
        how = {"keep": t("keep subgroups as they are", "คงโครงสร้าง subgroup เดิม"),
               "flat": t("drop subgroups (team/svc/api → team/api)", "ตัด subgroup ออก (team/svc/api → team/api)"),
               "join": t("fold subgroups into names (team/svc/api → team/svc-api)", "รวมชื่อ subgroup เข้ากับชื่อ project (team/svc/api → team/svc-api)")}[lay]
        if plan["layout_detected"]:
            how += term.style(t("  · detected from {n} projects already on target", "  · ตรวจพบจาก {n} project ที่อยู่ปลายทางแล้ว",
                                n=plan["layout_detected"]), "dim")
        rows.append((t("Layout", "โครงสร้าง"), f"{lay} — {how}"))
    rows.append((t("Transfer", "สิ่งที่ย้าย"), "  ".join(term.style(c, "bold") for c in components)
                 + (term.style("   " + t("not included: ", "ไม่รวม: ") + ", ".join(c for c in COMPONENTS if c not in components), "dim")
                    if len(components) < len(COMPONENTS) else "")))
    flags = [f for f in ("relocate", "with_parent_vars", "rewrite_urls", "overwrite", "force_push") if opts.get(f)]
    if flags:
        rows.append((t("Options", "ตัวเลือก"), ", ".join("--" + f.replace("_", "-") for f in flags)))
    term.out("")
    term.kv(rows)
    term.out("")
    src_root = plan["source"] + "/" if plan["kind"] == "group" else ""
    dst_root = plan["target"] + "/" if plan["kind"] == "group" else ""
    rel_s = lambda p: p[len(src_root):] if src_root and p.startswith(src_root) else p
    rel_d = lambda p: p[len(dst_root):] if dst_root and p.lower().startswith(dst_root.lower()) else p
    table_rows = []
    for i in items:
        if i["state"] == MOVE:
            note = t("from {p}", "จาก {p}", p=rel_d(i["move_from"]))
        elif i["state"] == SKIP or (i["state"] == UPDATE and not i["sync"]):
            note = i["reason"]
        else:
            note = describe_sync(i["sync"]) or i["reason"]
        if i["archived"]:
            note = (note + " · " if note else "") + "archived"
        table_rows.append((state_label(i["state"]), rel_s(i["source"]), rel_d(i["target"]), term.style(note, "dim")))
    if src_root:
        term.kv([(t("Paths", "path"), term.style(t("relative to {a} → {b}", "นับจาก {a} → {b}", a=src_root, b=dst_root), "dim"))])
        term.out("")
    term.table([t("STATUS", "สถานะ"), t("SOURCE", "ต้นทาง"), t("TARGET", "ปลายทาง"), ""], table_rows, paths=(1, 2), shrink_last=False)
    c = Counter(i["state"] for i in items)
    parts = [f"{len(items)} " + t("projects", "project")]
    for st in (NEW, SYNCED, UPDATE, MOVE, SKIP):
        if c[st]:
            parts.append(f"{c[st]} " + strip_sym(state_label(st)))
    term.out("")
    term.out("  " + term.style(" · ".join(parts), "bold"))
    if c[SKIP] and any(i.get("conflict") for i in items) and not opts.get("force_push"):
        term.out(term.style("  " + t("Projects marked 'skip' are left untouched. Review them before using --force-push.",
                                     "project ที่ 'ข้าม' จะไม่ถูกแตะต้อง ควรตรวจสอบก่อนใช้ --force-push"), "dim"))
    moved_elsewhere = [i for i in items if i["state"] in (SYNCED, UPDATE) and plan["kind"] == "group" and plan["layout"] == "keep"
                       and i["dst"] and i["target"].lower() != layout_target(plan["target"], i["source"][len(plan["source"]) + 1:], "keep").lower()]
    if moved_elsewhere and not opts.get("relocate"):
        term.out(term.style("  " + t("{n} projects already exist on target at a different path. Add --relocate to move them into the same structure.",
                                     "มี {n} project ที่อยู่ปลายทางแล้วแต่คนละตำแหน่ง ใส่ --relocate เพื่อย้ายเข้าโครงสร้างเดียวกัน",
                                     n=len(moved_elsewhere)), "yellow"))


def print_sync_plan(plan, components, opts):
    items = plan["items"]
    term.heading(t("Sync", "Sync"), f"{plan['kind']} · {plan['source']} → {plan['target']}")
    term.out("")
    flags = [("--prune", opts.get("prune")), ("--no-overwrite", not opts.get("overwrite"))]
    term.kv([(t("Transfer", "สิ่งที่ sync"), "  ".join(term.style(c, "bold") for c in components)),
             (t("Rule", "หลักการ"), t("the source wins: changed values on the target are updated", "ยึดต้นทางเป็นหลัก: ค่าที่ต่างกันที่ปลายทางจะถูกอัปเดต")
              if opts.get("overwrite") else t("only add what is missing", "เพิ่มเฉพาะสิ่งที่ยังไม่มี"))]
            + ([(t("Options", "ตัวเลือก"), ", ".join(f for f, on in flags if on))] if any(on for _, on in flags) else []))
    src_root = plan["source"] + "/" if plan["kind"] == "group" else ""
    rel = lambda p: p[len(src_root):] if src_root and p.startswith(src_root) else p
    rows = []
    for i in items:
        if i["state"] == SYNCED:
            continue
        note = i["reason"] if i["state"] in (SKIP, MOVE, NEW) else describe_sync(i["sync"])
        if i["state"] == NEW:
            note = t("new on the source", "มีใหม่ที่ต้นทาง")
        rows.append((state_label(i["state"]), rel(i["source"]), term.style(note, "dim")))
    term.out("")
    if rows:
        term.table([t("STATUS", "สถานะ"), t("SOURCE", "ต้นทาง"), ""], rows, paths=(1,))
    same = sum(1 for i in items if i["state"] == SYNCED)
    if same:
        term.out(term.style("  ✓ " + t("{n} projects: code already up to date — variables, rules and settings are still checked",
                                       "{n} project: code เป็นปัจจุบันแล้ว — ยังตรวจตัวแปร กฎ และค่าตั้งให้", n=same), "dim"))
    c = Counter(i["state"] for i in items)
    if c[SKIP]:
        term.out(term.style("  " + t("Projects marked 'skip' are left untouched.", "project ที่ 'ข้าม' จะไม่ถูกแตะต้อง"), "dim"))


def strip_sym(s):
    return term.strip_ansi(s)[2:]
