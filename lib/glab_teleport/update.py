"""`glab-teleport update`: install the latest release, then refresh every installed copy of the agent skill."""
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

from . import __version__, term
from .i18n import t

PACKAGE = "glab-teleport"
REGISTRY = "https://registry.npmjs.org/glab-teleport/latest"
CHANGELOG = "https://github.com/Aekawan/glab-teleport/blob/main/CHANGELOG.md"
LIB = Path(__file__).resolve().parent.parent  # <npm package>/lib, a source checkout's lib/, or site-packages


def parse(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v).split("-")[0])[:3])


def npm_bin():
    """The npm that belongs to the Node running the launcher (nvm, fnm…), else the one on PATH."""
    node = os.environ.get("GLAB_TELEPORT_NODE")
    if node:
        for name in ("npm", "npm.cmd"):
            if (Path(node).parent / name).exists():
                return str(Path(node).parent / name)
    return shutil.which("npm")


def latest(timeout=5, npm_fallback=True):
    """Latest published version, or None when it can't be found out."""
    try:
        with urllib.request.urlopen(REGISTRY, timeout=timeout) as r:
            return json.load(r)["version"]
    except Exception:  # noqa — offline, proxy, TLS interception: try npm with the user's own registry settings
        pass
    npm = npm_bin() if npm_fallback else None
    if npm:
        try:
            r = subprocess.run([npm, "view", PACKAGE, "version", "--prefer-online"], capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip().splitlines()[-1]
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def install_kind(lib=LIB):
    p = str(lib).replace("\\", "/")
    if "/node_modules/" in p:
        return "npm"
    if "/pipx/venvs/" in p:
        return "pipx"
    if (Path(lib).parent / ".git").exists():
        return "source"
    return "other"


def upgrade_command(kind):
    if kind == "npm" and npm_bin():
        return [npm_bin(), "install", "-g", f"{PACKAGE}@latest", "--prefer-online", "--no-fund", "--no-audit"]
    if kind == "pipx" and shutil.which("pipx"):
        return [shutil.which("pipx"), "upgrade", PACKAGE]
    return None


def _new_code(*argv):
    """Run the freshly installed version (same location, new files) and return its JSON result."""
    r = subprocess.run([sys.executable, "-m", "glab_teleport", *argv, "--json"], capture_output=True, text=True,
                       env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {}


def _show_skills(skills):
    changed = [s for s in skills if s["changed"]]
    if changed:
        term.kv([(t("skill updated", "อัปเดต skill"), s["path"]) for s in changed])
    elif skills:
        term.out(term.style("  " + t("Agent skill is up to date.", "skill ของ AI agent เป็นเวอร์ชันล่าสุดแล้ว"), "dim"))


def run(args):
    from . import agents
    kind = install_kind()
    with term.Status(t("Checking for a new version…", "กำลังตรวจหาเวอร์ชันใหม่…")):
        new = latest()
    if not new:
        raise SystemExit(t("Could not reach the npm registry. Check your network and try again.",
                           "เชื่อมต่อ npm registry ไม่ได้ ตรวจสอบเครือข่ายแล้วลองใหม่"))
    info = {"current": __version__, "latest": new, "install": kind}
    look_only = args.check or (term.JSON and not args.yes)
    if parse(new) <= parse(__version__):
        skills = [] if look_only else agents.refresh(Path(__file__).resolve().parent / "skill")
        term.emit({"ok": True, **info, "updated": False, "skills": skills})
        term.out(term.style("✓ ", "green") + t("glab-teleport {v} is the latest version.", "glab-teleport {v} เป็นเวอร์ชันล่าสุดแล้ว", v=__version__))
        _show_skills(skills)
        return 0
    if look_only:
        term.emit({"ok": True, **info, "updated": False, "available": True,
                   **({"next": "glab-teleport update --json --yes"} if term.JSON else {})})
        term.out(t("glab-teleport {n} is available (you have {v}).", "มี glab-teleport {n} แล้ว (เครื่องนี้เป็น {v})", n=new, v=__version__))
        term.out(term.style("  glab-teleport update", "cyan"))
        return 0
    if kind == "source":
        raise SystemExit(t("This copy runs from a source checkout. Update it with: git -C {d} pull",
                           "เครื่องนี้รันจาก source code โดยตรง อัปเดตด้วย: git -C {d} pull", d=LIB.parent))
    cmd = upgrade_command(kind)
    if not cmd:
        raise SystemExit(t("Update with: npm install -g glab-teleport@latest", "อัปเดตด้วย: npm install -g glab-teleport@latest"))
    with term.Status(t("Installing glab-teleport {n}…", "กำลังติดตั้ง glab-teleport {n}…", n=new)):
        r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        out = (r.stderr or r.stdout or "").strip()
        hint = ""
        if "EACCES" in out or "permission denied" in out.lower():
            hint = "\n" + t("npm cannot write its global folder. Use a Node version manager (nvm, fnm) or fix npm's prefix "
                            "instead of sudo: https://docs.npmjs.com/resolving-eacces-permissions-errors-when-installing-packages-globally",
                            "npm เขียนโฟลเดอร์ global ไม่ได้ แนะนำให้ใช้ตัวจัดการเวอร์ชัน Node (nvm, fnm) หรือแก้ prefix ของ npm แทนการใช้ sudo: "
                            "https://docs.npmjs.com/resolving-eacces-permissions-errors-when-installing-packages-globally")
        raise SystemExit(t("Update failed:", "อัปเดตไม่สำเร็จ:") + "\n  " + "\n  ".join(out.splitlines()[-8:]) + hint)
    now = _new_code("--version").get("version") or new
    skills = _new_code("skill", "install", "--refresh").get("installed") or []
    term.emit({"ok": True, **info, "updated": True, "version": now, "skills": skills, "changelog": CHANGELOG})
    term.out(term.style("✓ ", "green") + t("Updated glab-teleport {a} → {b}", "อัปเดต glab-teleport {a} → {b} แล้ว", a=__version__, b=now))
    _show_skills(skills)
    term.out(term.style("  " + t("What's new: {u}", "มีอะไรใหม่: {u}", u=CHANGELOG), "dim"))
    return 0
