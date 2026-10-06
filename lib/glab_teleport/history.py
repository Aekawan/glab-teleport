"""Where earlier runs put each project, so re-runs reuse the same target even without commit evidence (e.g. empty repos)."""
import json
from pathlib import Path


def known_pairs(work):
    """source path -> target path from every teleport run (latest wins), plus commit-verified audit pairs."""
    pairs = {}
    runs = Path(work) / "runs"
    for f in sorted(runs.glob("*/report.json")) if runs.exists() else []:
        try:
            rep = json.loads(f.read_text())
        except ValueError:
            continue
        if rep.get("mode") not in ("teleport", "sync"):
            continue
        for p in rep.get("projects", []):
            if not any(i.get("area") == "project" for i in p.get("issues", [])):
                pairs[p["source"]] = p["target"]
    audit = Path(work) / "audit" / "mapping.json"
    if audit.exists():
        for r in json.loads(audit.read_text()).get("rows", []):
            if r.get("match") == "verified":
                pairs.setdefault(r["source"], r["target"])
    return pairs


def runs(work):
    """Every teleport/sync run, oldest first."""
    out = []
    d = Path(work) / "runs"
    for f in sorted(d.glob("*/report.json")) if d.exists() else []:
        try:
            rep = json.loads(f.read_text())
        except ValueError:
            continue
        if rep.get("mode") in ("teleport", "sync"):
            row = {k: rep.get(k) for k in ("kind", "source", "target", "layout", "components", "options", "started", "mode")}
            projects = rep.get("projects", [])
            if rep.get("kind") == "project" and len(projects) == 1 and projects[0].get("target"):
                row["target"] = projects[0]["target"]   # the real project path, not the group it was placed in
            out.append({**row, "dir": f.parent, "projects": len(projects)})
    return out


def sync_targets(work):
    """Distinct things that were teleported (latest run of each), newest first — what `sync` can refresh."""
    latest = {}
    for r in runs(work):
        if r["kind"] == "group" or (r["kind"] == "project" and "/" in (r["source"] or "")):
            latest[(r["kind"], r["source"])] = r
    return sorted(latest.values(), key=lambda r: r["started"] or "", reverse=True)


def previous(work, kind, source):
    """Everything earlier runs did for this source (directly or via a parent group): all components ever
    transferred, options that were switched on, and the layout of the latest run."""
    covering = [r for r in runs(work)
                if r["source"] == source or (r["kind"] == "group" and source.startswith((r["source"] or "") + "/"))]
    if not covering:
        return None
    comps, opts = [], {}
    for r in covering:
        comps += [c for c in r.get("components") or [] if c not in comps]
        opts.update({k: v for k, v in (r.get("options") or {}).items() if v})
    return {**covering[-1], "components": comps, "options": opts}


def infer_target(work, kind, source):
    """Where an earlier run put `source`, so `sync <source>` needs no target."""
    from collections import Counter
    for r in reversed(runs(work)):
        if r["kind"] == kind and r["source"] == source:
            return r["target"]
    pairs = known_pairs(work)
    if kind == "project":
        return pairs.get(source)
    votes = Counter()
    for sp, tp in pairs.items():
        if sp.startswith(source + "/"):
            rel = sp[len(source) + 1:]
            if tp.lower().endswith("/" + rel.lower()):
                votes[tp[:-len(rel) - 1]] += 1
    return votes.most_common(1)[0][0] if votes else None
