"""Interactive mode: `glab-teleport` with no arguments."""
import argparse
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from . import config, term
from .account import cmd_login
from . import i18n
from .i18n import set_lang, t
from .match import SEG_OK, sim
from .plan import COMPONENTS, describe_component
from .run import session, teleport
from .urls import known_pairs

CACHE_MAX_AGE = 1800


def tree(groups, projects, moved=None):
    count, done = Counter(), Counter()
    for p in projects:
        parts = p["path_with_namespace"].split("/")[:-1]
        for i in range(1, len(parts) + 1):
            count["/".join(parts[:i])] += 1
            if moved and p["path_with_namespace"] in moved:
                done["/".join(parts[:i])] += 1
    rows = []
    for g in sorted(groups, key=lambda g: g["full_path"].lower()):
        fp = g["full_path"]
        prefix, _, leaf = fp.rpartition("/")
        meta = t("{n} projects", "{n} project", n=count[fp]) + (t(" · {d} teleported", " · ย้ายแล้ว {d}", d=done[fp]) if done[fp] else "")
        rows.append(term.item(fp, leaf, meta, prefix + "/" if prefix else ""))
    return rows


def suggestions(src_group, groups, pairs):
    votes = Counter()
    for sp, tp in pairs.items():
        if sp.startswith(src_group + "/"):
            votes[tp.rpartition("/")[0]] += 3
    leaf = src_group.split("/")[-1]
    for g in groups:
        score = sim(leaf, g["full_path"].split("/")[-1])
        if score >= SEG_OK:
            votes[g["full_path"]] += score
    return [g for g, _ in votes.most_common(3)]


def load_lists(s, refresh=False):
    cache = config.WORK_DIR / "cache"
    files = [cache / "inventory-source.json", cache / "inventory-target.json"]
    if not refresh and all(f.exists() and time.time() - f.stat().st_mtime < CACHE_MAX_AGE for f in files):
        return [json.loads(f.read_text()) for f in files]
    progress = {}

    def prog(side):
        def cb(i, n):
            progress[side] = f"{i}/{n}"
            st.update(t("Loading groups and projects… {p}", "กำลังโหลดรายการ group และ project… {p}", p="  ".join(progress.values())))
        return cb
    with term.Status(t("Loading groups and projects…", "กำลังโหลดรายการ group และ project…")) as st:
        with ThreadPoolExecutor(2) as ex:
            a, b = ex.submit(s.src.inventory, prog("source")), ex.submit(s.dst.inventory, prog("target"))
            invs = [a.result(), b.result()]
    cache.mkdir(parents=True, exist_ok=True)
    for f, inv in zip(files, invs):
        f.write_text(json.dumps(inv, ensure_ascii=False))
    return invs


def defaults(args):
    a = argparse.Namespace(**vars(args))
    for k, v in dict(only=None, layout="keep", include=None, exclude=None, dry_run=False, yes=False, relocate=False,
                     force_push=False, overwrite=False, rewrite_urls=False, with_parent_vars=False, activate_schedules=False,
                     allow_unmask=False, cache_dir=None, jobs=4, source_url=None, target_url=None, insecure=False).items():
        if getattr(a, k, None) is None:
            setattr(a, k, v)
    return a


def run(args):
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise SystemExit(t("Interactive mode needs a terminal. Use: glab-teleport group <source> <target>",
                           "โหมดเลือกจากรายการต้องรันใน terminal หรือใช้: glab-teleport group <ต้นทาง> <ปลายทาง>"))
    args = defaults(args)
    cfg = config.load_config()
    if not i18n.explicit:
        choice = term.ask("Language / ภาษา", [term.item("en", "English"), term.item("th", "ไทย")])
        if choice:
            cfg["lang"] = choice
            config.save_config(cfg)
            set_lang(choice)
    if not all(config.side_url(sd) and config.resolve_auth(config.side_url(sd), sd) for sd in config.SIDES):
        term.out(term.style(t("Welcome to glab-teleport. Let's connect your source and target GitLab first.",
                              "ยินดีต้อนรับสู่ glab-teleport เริ่มจากเชื่อมต่อ GitLab ต้นทางและปลายทางก่อน"), "bold"))
        cmd_login(argparse.Namespace(**{**vars(args), "side": "both", "method": None, "client_id": None, "token_stdin": False}))
    s = session(args)
    src_inv, dst_inv = load_lists(s, getattr(args, "refresh", False))
    moved = known_pairs(s.work)
    step, kind, srcs, dst, comps = "kind", None, None, None, None
    sub = f"{s.src.url}  →  {s.dst.url}"
    while True:
        if step == "kind":
            kind = term.ask(t("What do you want to teleport?", "ต้องการย้ายอะไร"), [
                term.item("group", t("A group", "ทั้ง group"), t("every project and subgroup inside it", "ทุก project และ subgroup ภายใน")),
                term.item("project", t("Projects", "เลือก project"), t("pick one or more (Tab)", "เลือกได้หลายรายการ (Tab)"))], subtitle=sub)
            if kind is None:
                return 0
            step = "src"
        elif step == "src":
            if kind == "group":
                g = term.ask(t("Source group", "group ต้นทาง"), tree(src_inv["groups"], src_inv["projects"], moved), subtitle=s.src.url)
                srcs = [g] if g else None
            else:
                rows = [term.item(p["path_with_namespace"], p["path_with_namespace"].rsplit("/", 1)[1],
                                  (t("empty · ", "ว่าง · ") if p.get("empty_repo") else "") + (p.get("last_activity_at") or "")[:10],
                                  p["path_with_namespace"].rsplit("/", 1)[0] + "/",
                                  tag=("→ " + moved[p["path_with_namespace"]]) if p["path_with_namespace"] in moved else "")
                        for p in src_inv["projects"]]
                srcs = term.ask(t("Source projects", "project ต้นทาง"), rows,
                                multi=True, subtitle=s.src.url)
            step = "dst" if srcs else "kind"
        elif step == "dst":
            sug = suggestions(srcs[0], dst_inv["groups"], moved) if kind == "group" else \
                ([moved[srcs[0]]] if len(srcs) == 1 and srcs[0] in moved else [])
            rows = [term.item(x, x, t("suggested", "แนะนำ"), tag="★") for x in sug]
            rows += [r for r in tree(dst_inv["groups"], dst_inv["projects"]) if r["value"] not in sug]
            title = t("Target group (type a new path to create a subgroup)", "group ปลายทาง (พิมพ์ path ใหม่เพื่อสร้าง subgroup)") if kind == "group" \
                else t("Target group, or type a full project path", "group ปลายทาง หรือพิมพ์ path ของ project เอง")
            d = term.ask(title, rows, custom=t("use", "ใช้"), subtitle=s.dst.url)
            if d is None:
                step = "src"
                continue
            dst = d[1] if isinstance(d, tuple) else d
            step = "what"
        elif step == "what":
            comps = term.ask(t("What should be transferred?", "เลือกสิ่งที่จะย้าย"),
                             [term.item(c, c, describe_component(c)) for c in COMPONENTS], multi=True, preselect=list(COMPONENTS))
            if comps is None:
                step = "dst"
                continue
            step = "layout" if kind == "group" else "options"
        elif step == "layout":
            lay = term.ask(t("Subgroup structure on the target", "โครงสร้าง subgroup ที่ปลายทาง"), [
                term.item("keep", "keep", t("same structure as the source (recommended)", "เหมือนต้นทางทุกชั้น (แนะนำ)"),
                          help=t("team/svc/backend/api → <target>/svc/backend/api", "team/svc/backend/api → <ปลายทาง>/svc/backend/api")),
                term.item("auto", "auto", t("copy the structure of projects already on the target", "ทำตามโครงสร้างของ project ที่อยู่ปลายทางแล้ว"),
                          help=t("Copies whatever structure the target already has — including mistakes made by earlier migrations. Prefer keep unless the target layout is intentional.",
                                 "ทำตามโครงสร้างที่ปลายทางมีอยู่ ซึ่งรวมถึงความผิดพลาดจากการย้ายครั้งก่อนด้วย ควรใช้ keep เว้นแต่โครงสร้างปลายทางตั้งใจจัดไว้แบบนั้น")),
                term.item("flat", "flat", t("drop subgroups", "ตัด subgroup ออก"),
                          help=t("team/svc/backend/api → <target>/api   (duplicates fall back to joined names)",
                                 "team/svc/backend/api → <ปลายทาง>/api   (ถ้าชื่อซ้ำจะใช้ชื่อแบบ join แทน)")),
                term.item("join", "join", t("fold subgroup names into the project name", "รวมชื่อ subgroup เข้ากับชื่อ project"),
                          help=t("team/svc/backend/api → <target>/svc-backend-api", "team/svc/backend/api → <ปลายทาง>/svc-backend-api"))])
            if lay is None:
                step = "what"
                continue
            args.layout = lay
            step = "options"
        elif step == "options":
            opts = [term.item("relocate", "--relocate", t("put projects already on the target into this structure", "จัด project ที่อยู่ปลายทางแล้วให้เข้าโครงสร้างนี้"),
                              help=t("Projects that were copied to the target earlier but sit at a different path are moved into the structure you chose "
                                     "(GitLab Transfer: history kept, the old path redirects). Without it they are updated where they are. Needs Owner on the target group.",
                                     "project ที่เคยถูกย้ายไปปลายทางแล้วแต่อยู่คนละตำแหน่ง จะถูกย้ายเข้าโครงสร้างที่เลือก (ใช้ Transfer ของ GitLab: history ครบ "
                                     "และ path เดิม redirect ให้) ถ้าไม่เลือก จะอัปเดตที่ตำแหน่งเดิม ต้องเป็น Owner ของ group ปลายทาง"))] if kind == "group" else []
            opts += [
                term.item("with_parent_vars", "--with-parent-vars", t("include variables from groups above the source", "รวมตัวแปรจาก group ที่อยู่เหนือต้นทาง"),
                          help=t("Projects also receive CI/CD variables from the parent groups ABOVE what you selected (e.g. teleporting team/backend also brings team's variables). "
                                 "Use it when pipelines depend on those inherited variables.",
                                 "project ได้รับ CI/CD variables จาก group ที่อยู่เหนือสิ่งที่เลือกด้วย (เช่น ย้าย team/backend จะได้ตัวแปรของ team ไปด้วย) "
                                 "ใช้เมื่อ pipeline ต้องใช้ตัวแปรที่สืบทอดมา")),
                term.item("rewrite_urls", "--rewrite-urls", t("change source repo URLs inside variables", "เปลี่ยน URL ของ repo ต้นทางที่อยู่ในตัวแปร"),
                          help=t("Variable values that contain a source repository URL (https, ssh or git@) are rewritten to the matching target URL. "
                                 "Registry and API URLs are left as they are and listed in the report.",
                                 "ค่าตัวแปรที่มี URL ของ repository ต้นทาง (https, ssh หรือ git@) จะถูกเปลี่ยนเป็น URL ปลายทางที่ตรงกัน "
                                 "ส่วน URL ของ registry หรือ API จะไม่ถูกแก้และแสดงไว้ในรายงาน")),
                term.item("overwrite", "--overwrite", t("replace target values that differ", "เขียนทับค่าปลายทางที่ต่างจากต้นทาง"),
                          help=t("If a variable or protection rule already exists on the target with a different value, the source value wins. "
                                 "Without it the target value is kept and the difference is reported.",
                                 "ถ้าปลายทางมีตัวแปรหรือกฎ protected อยู่แล้วแต่ค่าต่างกัน จะใช้ค่าจากต้นทางแทน ถ้าไม่เลือก "
                                 "จะเก็บค่าปลายทางไว้และรายงานความต่างให้ดู")),
                term.item("activate_schedules", "--activate-schedules", t("start scheduled pipelines now", "เปิดให้ pipeline ตามเวลารันทันที"),
                          help=t("Pipeline schedules are created paused by default so jobs don't run twice while the old GitLab is still live. "
                                 "Tick this only when you switch over.",
                                 "ปกติ pipeline schedule จะถูกสร้างแบบปิดไว้ เพื่อไม่ให้ job รันซ้ำกับ GitLab เดิมที่ยังใช้งานอยู่ "
                                 "เลือกตัวนี้เมื่อพร้อมย้ายไปใช้ระบบใหม่จริงเท่านั้น")),
                term.item("dry_run", "--dry-run", t("only show the plan", "แสดงแผนอย่างเดียว"),
                          help=t("Shows exactly what would happen and stops. Nothing is created or changed — a good first step.",
                                 "แสดงสิ่งที่จะเกิดขึ้นทั้งหมดแล้วหยุด ไม่มีการสร้างหรือแก้ไขอะไร เหมาะสำหรับรอบแรก"))]
            picked = term.ask(t("Options — none are required", "ตัวเลือกเพิ่มเติม — ไม่เลือกก็ได้"), opts, multi=True, empty_ok=True)
            if picked is None:
                step = "layout" if kind == "group" else "what"
                continue
            for o in picked:
                setattr(args, o, True)
            s.opts.update({o: True for o in picked})
            break
    return teleport(args, kind, srcs, dst, comps, s=s, interactive=True)
