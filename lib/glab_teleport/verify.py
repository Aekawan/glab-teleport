"""Read-only verification: compare source and target after a teleport (or any time with `glab-teleport verify`).

Variables are compared per (key, environment_scope) on value, type, protected, masked and raw.
Values are compared in memory only and never printed or written to a report.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from .gitlab import ApiError, GitError, ls_remote
from .i18n import t
from .match import compare_refs
from .transfer import RANK, VAR_FLAGS, load_vars, vkey, vlabel

LEVEL_RANK = {"info": 0, "manual": 1, "warn": 2, "fail": 3}


def inherited_vars(s, plan, components):
    """Variables that must land on project level: flattened subgroups (group plans) and,
    with --with-parent-vars, ancestors of the source (project plans). Group plans put ancestor vars on the target group."""
    out, parent = {}, {}
    if "env" not in components:
        return out, parent
    if plan["kind"] == "group":
        if plan["flattened_groups"]:
            cache = {}
            for it in plan["items"]:
                chain = [g for g in plan["flattened_groups"] if it["source"].startswith(g["full_path"] + "/")]
                merged = {}
                for g in sorted(chain, key=lambda g: g["full_path"].count("/")):
                    if g["id"] not in cache:
                        cache[g["id"]] = load_vars(s.src, f"/groups/{g['id']}") or {}
                    merged.update(cache[g["id"]])
                if merged:
                    out[it["source"]] = merged
        if s.opt("with_parent_vars") and plan["src_group"]:
            parent = _ancestor_vars(s, plan["source"])
    elif s.opt("with_parent_vars"):
        for it in plan["items"]:
            v = _ancestor_vars(s, it["source"].rpartition("/")[0], include_self=True)
            if v:
                out[it["source"]] = v
    return out, parent


def _ancestor_vars(s, path, include_self=False):
    parts = path.split("/")
    merged = {}
    for i in range(1, len(parts) + (1 if include_self else 0)):
        g = s.src.group("/".join(parts[:i]))
        if g:
            merged.update(load_vars(s.src, f"/groups/{g['id']}") or {})
    return merged


def compare_vars(s, expected, actual):
    """Row per (key, scope) with a status; plus per-scope counts. Never includes values."""
    rows, scopes = [], {}
    for k, v in sorted(expected.items()):
        scopes.setdefault(k[1], [0, 0])[0] += 1
        row = {"key": k[0], "scope": k[1], "type": v.get("variable_type", "env_var"), "protected": bool(v.get("protected")),
               "masked": bool(v.get("masked")), "status": "ok", "detail": ""}
        got = actual.get(k)
        if got:
            scopes[k[1]][1] += 1
        if v.get("hidden") or v.get("value") is None:
            row["status"], row["detail"] = ("manual", t("hidden on source — value cannot be verified", "ต้นทางซ่อนค่าไว้ — ตรวจค่าไม่ได้")) if got \
                else ("fail", t("hidden on source — create it manually", "ต้นทางซ่อนค่าไว้ — ต้องสร้างเอง"))
        elif not got:
            row["status"], row["detail"] = "fail", t("missing on target", "ไม่มีที่ปลายทาง")
        else:
            diffs = [f for f in VAR_FLAGS if bool(got.get(f)) != bool(v.get(f)) and f != "variable_type"]
            if got.get("variable_type", "env_var") != v.get("variable_type", "env_var"):
                diffs.insert(0, "type")
            if got.get("value") is not None and got.get("value") != s.expected_value(v.get("value")):
                diffs.insert(0, "value")
            if diffs == ["masked"] and s.opt("allow_unmask") and v.get("masked") and not got.get("masked"):
                row["status"], row["detail"] = "warn", t("created unmasked (target rejected the mask)", "สร้างแบบไม่ mask (ปลายทางไม่รับ)")
            elif diffs:
                row["status"], row["detail"] = "fail", t("differs: {d}", "ไม่ตรง: {d}", d=", ".join(diffs))
        rows.append(row)
    extra = sorted(k for k in actual if k not in expected)
    for k in extra:
        rows.append({"key": k[0], "scope": k[1], "type": actual[k].get("variable_type", "env_var"),
                     "protected": bool(actual[k].get("protected")), "masked": bool(actual[k].get("masked")),
                     "status": "info", "detail": t("only on target", "มีเฉพาะที่ปลายทาง")})
    return rows, scopes


def verify_project(s, it, components):
    sp, tp = it["source"], it["target"]
    res = {"source": sp, "target": tp, "status": "ok", "issues": [], "refs": None, "variables": None, "scopes": {},
           "environments": None, "protected": None, "runners": None}

    def issue(level, area, text):
        res["issues"].append({"level": level, "area": area, "text": text})

    try:
        src, dst = s.src.project(sp), s.dst.project(tp)
        if not src or not dst:
            issue("fail", "project", t("target project not found", "ไม่พบ project ปลายทาง") if src else t("source project not found", "ไม่พบ project ต้นทาง"))
            return _finish(res)
        if "repo" in components:
            # always ask git: GitLab's empty_repo flag can lag behind a push
            a = ls_remote(s.src, src["http_url_to_repo"])
            b = ls_remote(s.dst, dst["http_url_to_repo"])
            cmp = compare_refs(a, b)
            nb = lambda refs: sum(1 for r in refs if r.startswith("refs/heads/"))
            res["refs"] = {"source_branches": nb(a), "target_branches": nb(b), "source_tags": len(a) - nb(a), "target_tags": len(b) - nb(b),
                           "missing": cmp["missing"], "different": cmp["different"], "extra": cmp["extra"]}
            if cmp["missing"]:
                issue("fail", "refs", t("{n} branches/tags missing on target", "ปลายทางขาด branch/tag {n} รายการ", n=len(cmp["missing"])))
            if cmp["different"]:
                issue("fail", "refs", t("{n} branches/tags point to different commits", "branch/tag {n} รายการชี้ไปคนละ commit", n=len(cmp["different"])))
            if src.get("default_branch") and a and dst.get("default_branch") != src.get("default_branch"):
                issue("warn", "refs", t("default branch is {d} (source: {s})", "default branch เป็น {d} (ต้นทาง: {s})",
                                        d=dst.get("default_branch"), s=src.get("default_branch")))
            prot = {}
            for kind in ("branches", "tags"):
                sa = {x["name"] for x in s.src.maybe_all(f"/projects/{src['id']}/protected_{kind}") or []}
                da = {x["name"] for x in s.dst.maybe_all(f"/projects/{dst['id']}/protected_{kind}") or []}
                prot[kind] = [len(sa), len(sa & da)]
                if sa - da:
                    issue("fail", "protected", t("protected {k} missing: {n}", "ขาด protected {k}: {n}", k=kind, n=", ".join(sorted(sa - da)[:5])))
            res["protected"] = prot
        if "env" in components:
            own = load_vars(s.src, f"/projects/{src['id']}")
            got = load_vars(s.dst, f"/projects/{dst['id']}")
            if own is None or got is None:
                issue("fail", "variables", t("cannot read variables (permission)", "อ่าน variables ไม่ได้ (สิทธิ์ไม่พอ)"))
            else:
                expected = {**(s.extra_vars.get(sp) or {}), **own}
                rows, scopes = compare_vars(s, expected, got)
                res["variables"], res["scopes"] = rows, scopes
                for level in ("fail", "manual", "warn"):
                    groups = {}
                    for r in rows:
                        if r["status"] == level:
                            groups.setdefault(r["detail"], []).append(f"{r['key']} [{r['scope']}]")
                    for detail, keys in groups.items():
                        issue(level, "variables", f"{len(keys)} × {detail}: " + ", ".join(keys[:5])
                              + (t(" (+{n} more)", " (และอีก {n})", n=len(keys) - 5) if len(keys) > 5 else ""))
            se = {e["name"] for e in s.src.maybe_all(f"/projects/{src['id']}/environments") or []}
            de = {e["name"] for e in s.dst.maybe_all(f"/projects/{dst['id']}/environments") or []}
            res["environments"] = [len(se), len(se & de)]
            if se - de:
                issue("fail", "environments", t("environments missing: {n}", "ขาด environment: {n}", n=", ".join(sorted(se - de)[:5])))
        if "runner" in components:
            sr = s.src.maybe_all(f"/projects/{src['id']}/runners", type="project_type") or []
            dr = {r["id"] for r in s.dst.maybe_all(f"/projects/{dst['id']}/runners", type="project_type") or []}
            mapped = [s.runner_map.get(f"{s.src.url}#{r['id']}") for r in sr]
            ok = sum(1 for m in mapped if m in dr)
            res["runners"] = [len(sr), ok]
            if ok < len(sr):
                issue("fail", "runners", t("{n} runners not attached on target", "ปลายทางยังไม่ได้ผูก runner {n} ตัว", n=len(sr) - ok))
            for rid in {m for m in mapped if m in dr}:
                r = s.dst.try_get(f"/runners/{rid}") or {}
                if r.get("status") != "online":
                    issue("manual", "runners", t("runner #{i} “{d}” is {st} — register it on a runner host with its token from {f}",
                                                 "runner #{i} “{d}” สถานะ {st} — ต้องลงทะเบียนบนเครื่อง runner ด้วย token ในไฟล์ {f}",
                                                 i=rid, d=r.get("description") or "-", st=r.get("status") or "?", f=s.work / "runner-tokens.txt"))
    except (ApiError, GitError) as e:
        issue("fail", "verify", s.dst.scrub(s.src.scrub(str(e)))[:200])
    return _finish(res)


def _finish(res):
    worst = max((i["level"] for i in res["issues"]), key=lambda x: LEVEL_RANK[x], default="info")
    res["status"] = {"info": "ok", "manual": "manual", "warn": "warn", "fail": "fail"}[worst]
    return res


def verify_groups(s, components):
    out = []
    if "env" not in components:
        return out
    for g in s.group_results:
        if "expected_vars" not in g or "target_id" not in g:
            continue
        got = load_vars(s.dst, f"/groups/{g['target_id']}")
        if got is None:
            out.append({"source": g["source"], "target": g["target"], "status": "fail", "variables": [], "scopes": {},
                        "issues": [{"level": "fail", "area": "variables", "text": t("cannot read target group variables", "อ่าน variables ของ group ปลายทางไม่ได้")}]})
            continue
        rows, scopes = compare_vars(s, g["expected_vars"], got)
        issues = [{"level": r["status"], "area": "variables", "text": f"{r['key']} [{r['scope']}]: {r['detail']}"}
                  for r in rows if r["status"] in ("fail", "manual", "warn")]
        res = {"source": g["source"], "target": g["target"], "variables": rows, "scopes": scopes, "issues": issues}
        out.append(_finish(res))
    return out


def verify_all(s, items, components, status=None):
    done = [0]

    def one(it):
        r = verify_project(s, it, components)
        done[0] += 1
        if status:
            status.update(t("Verifying {d}/{n} · {p}", "กำลังตรวจสอบ {d}/{n} · {p}", d=done[0], n=len(items), p=it["source"]))
        return r
    with ThreadPoolExecutor(max_workers=s.jobs) as ex:
        projects = list(ex.map(one, items))
    return projects, verify_groups(s, components)


def totals(projects, groups):
    """Source-vs-target counts for the summary table."""
    tot = Counter()
    scopes = {}
    for p in projects:
        if p["refs"]:
            for k in ("source_branches", "target_branches", "source_tags", "target_tags"):
                tot[k] += p["refs"][k]
            tot["matched_refs"] += p["refs"]["source_branches"] + p["refs"]["source_tags"] - len(p["refs"]["missing"]) - len(p["refs"]["different"])
        if p["variables"] is not None:
            for r in p["variables"]:
                if r["status"] == "info":
                    continue
                tot["source_vars"] += 1
                tot["matched_vars"] += r["status"] in ("ok", "warn", "manual")
                tot["exact_vars"] += r["status"] == "ok"
            for sc, (a, b) in p["scopes"].items():
                x = scopes.setdefault(sc, [0, 0])
                x[0] += a
                x[1] += b
        if p["environments"]:
            tot["source_envs"] += p["environments"][0]
            tot["matched_envs"] += p["environments"][1]
        if p["protected"]:
            for a, b in p["protected"].values():
                tot["source_protected"] += a
                tot["matched_protected"] += b
        if p["runners"]:
            tot["source_runners"] += p["runners"][0]
            tot["matched_runners"] += p["runners"][1]
    for g in groups:
        for r in g.get("variables") or []:
            if r["status"] == "info":
                continue
            tot["source_group_vars"] += 1
            tot["matched_group_vars"] += r["status"] in ("ok", "warn", "manual")
    return tot, scopes
