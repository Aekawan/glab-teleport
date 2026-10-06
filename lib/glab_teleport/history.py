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
        if rep.get("mode") != "teleport":
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
