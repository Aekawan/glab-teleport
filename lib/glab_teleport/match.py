"""Pairing source and target projects.

Commit evidence always wins: if a branch/tag SHA of a source project exists on a target project, they are
the same repository no matter how it was renamed. Name similarity is only a fallback (and is never trusted
on its own to overwrite code).
"""
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher

from .i18n import t

norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
tokens = lambda s: {x for x in re.split(r"[^a-z0-9]+", s.lower()) if x}
SEG_OK = 0.75
VERIFIED = 140  # score of a commit-verified pair (always above any name score)


def split_path(path):
    parts = path.split("/")
    return parts[:-1], parts[-1]


def sim(a, b):
    """Similarity of two path segments (0..1), tolerant to longer/renamed names."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = tokens(a), tokens(b)
    if ta == tb:                                       # web-shop <-> shop_web
        return 0.95
    r = SequenceMatcher(None, na, nb).ratio()
    if r >= 0.9:                                       # small typo
        return round(0.95 * r, 3)
    short, long_ = sorted((na, nb), key=len)
    if len(short) >= 3 and (ta <= tb or tb <= ta):     # shop <-> shop-team
        return 0.8
    if len(short) >= 4 and short in long_:
        return 0.75
    return 0.0


def ns_relation(old, new):
    if len(old) == len(new) and all(norm(a) == norm(b) for a, b in zip(old, new)):
        return "exact", 100
    if len(new) >= len(old):
        s = [sim(a, b) for a, b in zip(old, new[len(new) - len(old):])]
        if all(x >= SEG_OK for x in s):
            return ("prefix-added", 95) if all(x == 1 for x in s) else ("renamed", round(80 + 10 * sum(s) / len(s), 1))
    it, s = iter(new), []
    for a in old:
        for b in it:
            if sim(a, b) >= SEG_OK:
                s.append(sim(a, b))
                break
    if len(s) == len(old):
        return "subsequence", round(70 + 10 * sum(s) / len(s), 1)
    o_tok = set().union(*map(tokens, old)) if old else set()
    n_tok = set().union(*map(tokens, new)) if new else set()
    if o_tok:
        cov = sum(1 for x in o_tok if any(sim(x, u) >= SEG_OK for u in n_tok)) / len(o_tok)
        if cov == 1:
            return "tokens", 65
        if cov > 0:
            return "partial", round(30 + 30 * cov, 1)
    return "name-only", 15


def slug_join_count(o_ns, o_slug, n_slug):
    """How many trailing source subgroups were folded into the target project name (0 = same name)."""
    for k in range(0, len(o_ns) + 1):
        if norm("".join(o_ns[len(o_ns) - k:] + [o_slug])) == norm(n_slug):
            return k
    return None


def score_pair(old_path, new_path):
    (o_ns, o_slug), (n_ns, n_slug) = split_path(old_path), split_path(new_path)
    best = None
    k = slug_join_count(o_ns, o_slug, n_slug)
    if k and len(o_ns) - k >= 1:  # subgroup folded into the name: team/api -> team-api
        rel, base = ns_relation(o_ns[:len(o_ns) - k], n_ns)
        if rel in ("exact", "prefix-added", "renamed", "subsequence"):
            best = (round(base * 0.97, 1), f"{rel}+joined({k})")
    s = sim(o_slug, n_slug)
    if s >= SEG_OK:
        rel, base = ns_relation(o_ns, n_ns)
        if rel in ("partial", "tokens", "name-only"):  # subgroup dropped: team/service/api -> team/api
            for j in range(len(o_ns) - 1, 0, -1):
                if ns_relation(o_ns[:j], n_ns)[0] in ("exact", "prefix-added", "renamed"):
                    rel, base = f"dropped({len(o_ns) - j})", 75
                    break
        cand = (round(base * s, 1), rel + ("" if s == 1 else f"+name~{s:.2f}"))
        if best is None or cand[0] > best[0]:
            best = cand
    return best


def content_evidence(fp_src, fp_dst):
    """target path -> (Counter(source path -> shared ref SHAs), total SHAs of target)."""
    idx = defaultdict(set)
    for sp, d in fp_src.items():
        for sha in set((d.get("refs") or {}).values()):
            idx[sha].add(sp)
    out = {}
    for dp, d in fp_dst.items():
        shas = set((d.get("refs") or {}).values())
        c = Counter(sp for s in shas for sp in idx.get(s, ()))
        if c:
            out[dp] = (c, len(shas))
    return out


def pair_by_content(fp_src, fp_dst):
    """Greedy 1:1 pairing by number of shared SHAs -> {source: (target, shared)}."""
    ev = content_evidence(fp_src, fp_dst)
    pairs, taken = {}, set()
    for n, dp, sp in sorted(((n, dp, sp) for dp, (c, _) in ev.items() for sp, n in c.items()), reverse=True):
        if sp not in pairs and dp not in taken:
            pairs[sp] = (dp, n)
            taken.add(dp)
    return pairs


def compare_refs(src_refs, dst_refs):
    """Branch/tag comparison -> dict(state=identical|empty|behind|diverged, missing, different, extra)."""
    src_refs, dst_refs = src_refs or {}, dst_refs or {}
    missing = sorted(r for r in src_refs if r not in dst_refs)
    different = sorted(r for r in src_refs if r in dst_refs and src_refs[r] != dst_refs[r])
    extra = sorted(r for r in dst_refs if r not in src_refs)
    if not dst_refs and src_refs:
        state = "empty"
    elif not missing and not different:
        state = "identical"
    else:
        state = "outdated"
    return {"state": state, "missing": missing, "different": different, "extra": extra}


def describe_sync(cmp):
    if not cmp:
        return ""
    if cmp["state"] == "identical":
        return t("code in sync", "code ตรงกันแล้ว")
    if cmp["state"] == "empty":
        return t("target repository is empty", "repository ปลายทางยังว่าง")
    parts = []
    if cmp["missing"]:
        parts.append(t("{n} new refs", "ref ใหม่ {n}", n=len(cmp["missing"])))
    behind, diverged = cmp.get("behind"), cmp.get("diverged")
    if behind is None and cmp["different"]:
        parts.append(t("{n} refs differ", "ต่างกัน {n} refs", n=len(cmp["different"])))
    if behind:
        parts.append(t("{n} refs behind", "ตามหลัง {n} refs", n=len(behind)))
    if diverged:
        names = ", ".join(r.split("/", 2)[-1] for r in diverged[:3]) + ("…" if len(diverged) > 3 else "")
        parts.append(t("⚠ {n} changed on the target ({r}) — kept, not overwritten", "⚠ ปลายทางมี commit ของตัวเอง {n} ({r}) — จะไม่เขียนทับ",
                       n=len(diverged), r=names))
    return ", ".join(parts)


# ── layouts ──
LAYOUTS = ("auto", "keep", "flat", "join")


def layout_target(dst_group, rel, layout):
    parts = rel.split("/")
    if layout == "flat":
        return f"{dst_group}/{parts[-1]}"
    if layout == "join":
        return f"{dst_group}/" + "-".join(parts)
    return f"{dst_group}/{rel}"


def guess_layout(content_pairs, src_root, dst_root):
    """Learn the layout from projects that already exist on the target."""
    votes = Counter()
    for sp, (dp, _) in content_pairs.items():
        rel_o = sp[len(src_root) + 1:]
        if "/" not in rel_o or not dp.lower().startswith(dst_root.lower() + "/"):
            continue
        rel_n = dp[len(dst_root) + 1:].lower()
        for lay in ("keep", "flat", "join"):
            if layout_target("", rel_o, lay)[1:].lower() == rel_n:
                votes[lay] += 1
    if votes:
        lay, n = votes.most_common(1)[0]
        return lay, n
    return "keep", 0


# ── whole-instance mapping (audit) ──
def build_mapping(src_projects, dst_projects, fp_src=None, fp_dst=None):
    """Pair every source project with its most likely target. Returns rows sorted by source path."""
    fp_src, fp_dst = fp_src or {}, fp_dst or {}
    dst = {p["path_with_namespace"]: p for p in dst_projects}
    cands = {}
    for sp in (p["path_with_namespace"] for p in src_projects):
        cs = []
        for dp in dst:
            r = score_pair(sp, dp)
            if r:
                cs.append({"path": dp, "score": r[0], "method": r[1]})
        cands[sp] = cs
    for dp, (c, total) in content_evidence(fp_src, fp_dst).items():
        for sp, n in c.items():
            if sp in cands and dp in dst:
                hit = next((x for x in cands[sp] if x["path"] == dp), None)
                if hit:
                    hit.update(score=VERIFIED + 10 + min(n, 49), method="commits")
                else:
                    cands[sp].append({"path": dp, "score": VERIFIED + 10 + min(n, 49), "method": "commits"})
    # group consensus: projects of one source group usually land in one target group
    assigned = _assign(cands)
    votes = defaultdict(Counter)
    for sp, dp in assigned.items():
        if next(c for c in cands[sp] if c["path"] == dp)["score"] >= 60:
            votes["/".join(split_path(sp)[0])]["/".join(split_path(dp)[0])] += 1
    strong = {g: c.most_common(1)[0][0] for g, c in votes.items() if c.most_common(1)[0][1] >= 2
              and c.most_common(1)[0][1] / sum(c.values()) >= 0.6}
    for sp, cs in cands.items():
        want = strong.get("/".join(split_path(sp)[0]))
        for c in cs:
            if want is not None and c["score"] < VERIFIED:
                c["score"] = round(min(100, c["score"] + (8 if "/".join(split_path(c["path"])[0]) == want else -8)), 1)
        cs.sort(key=lambda c: (-c["score"], len(c["path"])))
    assigned = _assign(cands)
    owner = {dp: sp for sp, dp in assigned.items()}
    rows = []
    for p in src_projects:
        sp = p["path_with_namespace"]
        row = {"source": sp, "target": "", "match": "none", "sync": None, "alternatives": [c["path"] for c in cands[sp][:3]]}
        if sp in assigned:
            best = next(c for c in cands[sp] if c["path"] == assigned[sp])
            row["target"] = best["path"]
            if best["score"] >= VERIFIED:
                row["match"] = "verified"
            else:
                has_code = (fp_src.get(sp) or {}).get("refs") and (fp_dst.get(best["path"]) or {}).get("refs")
                if fp_src and fp_dst and has_code:
                    row["match"] = "different-code" if best["score"] >= 60 else "none"
                    if best["score"] < 60:
                        row["target"] = ""
                else:
                    row["match"] = "name" if best["score"] >= 60 else "weak"
            if row["target"] and row["match"] != "different-code":
                row["sync"] = compare_refs((fp_src.get(sp) or {}).get("refs"), (fp_dst.get(best["path"]) or {}).get("refs")) \
                    if fp_src and fp_dst else None
        rows.append(row)
    unpaired = sorted(dp for dp in dst if dp not in owner)
    return rows, unpaired


def _assign(cands):
    pairs = sorted(((c["score"], -len(c["path"]), sp, c["path"]) for sp, cs in cands.items() for c in cs), reverse=True)
    out, taken = {}, set()
    for _, _, sp, dp in pairs:
        if sp not in out and dp not in taken:
            out[sp] = dp
            taken.add(dp)
    return out
