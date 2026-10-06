"""Where AI coding agents look for skills, and installing the bundled glab-teleport skill there.

Claude Code reads ~/.claude/skills, Codex and pi read ~/.agents/skills, and OpenCode reads both (plus its own
~/.config/opencode/skills). One copy per folder is enough, so OpenCode reuses a folder it already reads.
"""
import shutil
from pathlib import Path

from .i18n import t

NAME = "glab-teleport"
AGENTS = ("claude", "codex", "opencode", "pi")
LABELS = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode", "pi": "pi"}
HINTS = {"claude": (".claude",), "codex": (".codex",), "opencode": (".config/opencode", ".opencode"), "pi": (".pi",)}


def folders(home=None):
    h = Path(home) if home else Path.home()
    return {"claude": h / ".claude" / "skills", "shared": h / ".agents" / "skills", "opencode": h / ".config" / "opencode" / "skills"}


def detect(home=None, which=shutil.which):
    """Agents that look installed here: their command is on PATH or their config folder exists."""
    h = Path(home) if home else Path.home()
    return [a for a in AGENTS if which(a) or any((h / p).exists() for p in HINTS[a])]


def parse(value):
    names = [x.strip().lower() for x in (value or "").split(",") if x.strip()]
    if "all" in names:
        return list(AGENTS)
    bad = [x for x in names if x not in AGENTS]
    if bad or not names:
        raise SystemExit(t("Unknown agent '{a}'. Choose from: {c}, all", "ไม่รู้จัก agent '{a}' เลือกได้จาก: {c}, all",
                           a=", ".join(bad) or value, c=", ".join(AGENTS)))
    return [a for a in AGENTS if a in names]


def plan(agents, home=None):
    """agents -> {folder: [agents]}."""
    f = folders(home)
    out = {}
    for a in agents:
        if a != "opencode":
            out.setdefault(f["claude"] if a == "claude" else f["shared"], []).append(a)
    if "opencode" in agents:
        reuse = [d for d in (f["claude"], f["shared"]) if d in out] or \
                [d for d in (f["claude"], f["shared"], f["opencode"]) if (d / NAME / "SKILL.md").exists()]
        out.setdefault(reuse[0] if reuse else f["opencode"], []).append("opencode")
    return out


def install(src, folder):
    target = Path(folder).expanduser() / NAME
    if target.is_symlink():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, target)
    return target


def opencode_sees_twice(home=None):
    f = folders(home)
    return sum((d / NAME / "SKILL.md").exists() for d in f.values()) > 1
