"""User configuration and per-host credentials.

~/.config/glab-teleport/config.json        source/target URLs, language
~/.config/glab-teleport/credentials.json   tokens keyed by host (mode 600)
~/.glab-teleport/                          run reports, plans, runner tokens, caches
"""
import json
import os
import threading
import urllib.parse
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("GLAB_TELEPORT_CONFIG_DIR")
                  or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "glab-teleport")
CONFIG_FILE = CONFIG_DIR / "config.json"
CRED_FILE = CONFIG_DIR / "credentials.json"
WORK_DIR = Path(os.environ.get("GLAB_TELEPORT_HOME") or Path.home() / ".glab-teleport")

SIDES = ("source", "target")
SCOPES = {"source": ["read_api", "read_repository"], "target": ["api", "write_repository"]}
SCOPE_IMPLIED = {"read_api": {"api"}, "read_repository": {"write_repository"}}
_lock = threading.Lock()


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path, data, secret=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if secret else 0o644)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def load_config():
    return read_json(CONFIG_FILE, {})


def save_config(cfg):
    write_json(CONFIG_FILE, cfg)


def normalize_url(url):
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = "https://" + url
    return url


def host_key(url):
    return urllib.parse.urlsplit(url).netloc.lower()


def side_url(side, override=None):
    """--source-url/--target-url > env > config file. Nothing is built in."""
    env = os.environ.get(f"GLAB_TELEPORT_{side.upper()}_URL")
    return normalize_url(override or env or load_config().get(side))


def get_cred(url):
    return read_json(CRED_FILE, {}).get(host_key(url))


def put_cred(url, cred):
    with _lock:
        creds = read_json(CRED_FILE, {})
        if cred is None:
            creds.pop(host_key(url), None)
        else:
            creds[host_key(url)] = cred
        write_json(CRED_FILE, creds, secret=True)


def resolve_auth(url, side):
    """Environment token (for CI) > saved credentials for the host."""
    tok = (os.environ.get(f"GLAB_TELEPORT_{side.upper()}_TOKEN") or "").strip()
    return {"type": "pat", "token": tok} if tok else get_cred(url)


def missing_scopes(have, need):
    have = set(have or [])
    return [n for n in need if n not in have and not (SCOPE_IMPLIED.get(n, set()) & have)]
