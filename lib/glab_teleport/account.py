"""login / logout / doctor / config."""
import base64
import os
import re
import shutil
import sys
import time
import urllib.parse
from pathlib import Path

from . import __version__, config, term
from .gitlab import OAUTH_REDIRECT, ApiError, GitError, GitLab, ls_remote, oauth_exchange
from .i18n import set_lang, t

SIDE_LABEL = {"source": ("Source", "ต้นทาง"), "target": ("Target", "ปลายทาง")}


def side_label(side):
    return t(*SIDE_LABEL[side])


def open_browser(url):
    import webbrowser
    try:
        return webbrowser.open(url)
    except Exception:  # noqa — headless machines: the link is printed anyway
        return False


def glab_client_id(host):
    """Reuse the OAuth Application ID configured for glab, if any."""
    paths = [Path(os.environ["GLAB_CONFIG_DIR"]) / "config.yml"] if os.environ.get("GLAB_CONFIG_DIR") else []
    paths += [Path.home() / ".config/glab-cli/config.yml", Path.home() / "Library/Application Support/glab-cli/config.yml"]
    for p in paths:
        try:
            lines = p.read_text().splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines):
            if line.strip().rstrip(":").strip("'\"") == host:
                ind = len(line) - len(line.lstrip())
                for sub in lines[i + 1:]:
                    if sub.strip() and len(sub) - len(sub.lstrip()) <= ind:
                        break
                    m = re.match(r"\s*client_id:\s*['\"]?([\w-]{16,})", sub)
                    if m:
                        return m.group(1)
    return None


def oauth_login(url, client_id, scopes, insecure=False, timeout=300):
    """OAuth 2.0 authorization code + PKCE in the browser (like `glab auth login --web`)."""
    import hashlib
    import html
    import secrets
    from http.server import BaseHTTPRequestHandler, HTTPServer
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state, result = secrets.token_urlsafe(16), {}
    redirect = urllib.parse.urlsplit(OAUTH_REDIRECT)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            if u.path != redirect.path:
                self.send_response(404)
                self.end_headers()
                return
            qs = dict(urllib.parse.parse_qsl(u.query))
            result.update(qs)
            ok = qs.get("state") == state and "code" in qs
            msg = (f"<h2>✓ {html.escape(t('Signed in to glab-teleport', 'เข้าสู่ระบบ glab-teleport สำเร็จ'))}</h2>"
                   f"<p>{html.escape(t('You can close this tab and return to the terminal.', 'ปิดหน้านี้แล้วกลับไปที่ terminal ได้เลย'))}</p>") if ok else \
                f"<h2>✗ {html.escape(t('Sign-in failed', 'เข้าสู่ระบบไม่สำเร็จ'))}</h2><p>{html.escape(qs.get('error_description') or qs.get('error') or 'state mismatch')}</p>"
            body = f"<!doctype html><meta charset=utf-8><body style='font-family:system-ui,sans-serif;padding:3em'>{msg}".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    try:
        srv = HTTPServer(("127.0.0.1", redirect.port), Handler)
    except OSError:
        raise SystemExit(t("Port {p} is in use (is another login running?).", "port {p} ถูกใช้งานอยู่ (มีการ login ค้างอยู่หรือไม่)", p=redirect.port))
    srv.timeout = 1
    auth_url = f"{url}/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": OAUTH_REDIRECT, "response_type": "code", "state": state,
        "scope": " ".join(scopes), "code_challenge": challenge, "code_challenge_method": "S256"})
    term.out(term.style("  " + t("Opening your browser. If it doesn't open, visit:", "กำลังเปิด browser หากไม่เปิดเอง ให้เปิดลิงก์นี้:"), "dim"))
    term.out("  " + auth_url)
    open_browser(auth_url)
    deadline = time.time() + timeout
    try:
        while not ({"code", "error"} & result.keys()) and time.time() < deadline:
            srv.handle_request()
    finally:
        srv.server_close()
    if result.get("state") != state or "code" not in result:
        raise SystemExit(t("Sign-in failed: {e}", "เข้าสู่ระบบไม่สำเร็จ: {e}", e=result.get("error_description") or result.get("error") or "timeout"))
    return oauth_exchange(url, {"client_id": client_id, "code_verifier": verifier, "redirect_uri": OAUTH_REDIRECT}, {
        "client_id": client_id, "code": result["code"], "grant_type": "authorization_code",
        "redirect_uri": OAUTH_REDIRECT, "code_verifier": verifier}, insecure)


def _login_side(args, side, cfg):
    term.heading(t("{s} GitLab", "GitLab {s}", s=side_label(side)))
    url = config.side_url(side, getattr(args, f"{side}_url", None))
    if sys.stdin.isatty() and not getattr(args, f"{side}_url", None) and not args.token_stdin:
        hint = "" if url else term.style(" " + t("(e.g. https://gitlab.example.com)", "(เช่น https://gitlab.example.com)"), "dim")
        url = config.normalize_url(term.prompt("  URL" + hint, url)) or url
    if not url:
        return None
    cfg[side] = url
    config.save_config(cfg)
    method = "stdin" if args.token_stdin else args.method
    if not method:
        method = term.ask(t("How do you want to sign in to {u}?", "เลือกวิธีเข้าสู่ระบบ {u}", u=url), [
            term.item("web-token", t("Create a token in the browser", "สร้าง token ผ่านเว็บ"),
                      t("recommended · works on any GitLab", "แนะนำ · ใช้ได้กับ GitLab ทุกเครื่อง")),
            term.item("oauth", t("Sign in with OAuth", "เข้าสู่ระบบด้วย OAuth"),
                      t("needs an OAuth application on this GitLab", "ต้องมี OAuth application บน GitLab นี้")),
            term.item("paste", t("Paste an existing token", "ใช้ token ที่มีอยู่แล้ว"),
                      t("personal, group or project access token", "personal, group หรือ project access token"))],
            subtitle=t("required scopes: {s}", "scope ที่ต้องใช้: {s}", s=", ".join(config.SCOPES[side])))
        if method is None:
            return None
    if method == "stdin":
        auth = {"type": "pat", "token": sys.stdin.readline().strip()}
    elif method == "oauth":
        cid = args.client_id or (config.get_cred(url) or {}).get("client_id") or glab_client_id(config.host_key(url))
        if not cid:
            apps = f"{url}/-/user_settings/applications"
            term.out("  " + t("Create an OAuth application once (any user can):", "สร้าง OAuth application หนึ่งครั้ง (ผู้ใช้ทั่วไปสร้างได้):"))
            term.kv([("1", apps), ("2", f"Name: glab-teleport · Redirect URI: {OAUTH_REDIRECT}"),
                     ("3", t("Untick “Confidential”; scopes: api, read_api, read_repository, write_repository",
                             "ไม่ต้องเลือก “Confidential”; scope: api, read_api, read_repository, write_repository")),
                     ("4", t("Save, then paste the Application ID below", "บันทึกแล้วนำ Application ID มาวางด้านล่าง"))], indent=4)
            open_browser(apps)
            cid = term.prompt("  Application ID")
        if not cid:
            return None
        auth = oauth_login(url, cid, config.SCOPES[side], args.insecure)
    else:
        if method == "web-token":
            link = f"{url}/-/user_settings/personal_access_tokens?" + urllib.parse.urlencode(
                {"name": f"glab-teleport ({side})", "scopes": ",".join(config.SCOPES[side])})
            term.out(term.style("  " + t("Opening the token page with name and scopes pre-filled. Set an expiry date and click Create.",
                                         "กำลังเปิดหน้าสร้าง token ที่กรอกชื่อและ scope ไว้แล้ว กรุณาตั้งวันหมดอายุแล้วกด Create"), "dim"))
            term.out("  " + link)
            open_browser(link)
        auth = {"type": "pat", "token": term.secret("  " + t("Token (input hidden)", "Token (ไม่แสดงบนหน้าจอ)"))}
    if not (auth.get("token") or auth.get("access_token")):
        return None
    gl = GitLab(side, url, auth, args.insecure)
    try:
        me = gl.get("/user")
    except ApiError as e:
        term.out(term.style("  ✗ " + t("Sign-in failed (HTTP {c}). Nothing was saved.", "เข้าสู่ระบบไม่สำเร็จ (HTTP {c}) ไม่มีการบันทึก",
                                       c=e.code or t("network", "เครือข่าย")), "red"))
        return False
    if auth["type"] == "pat":
        info = gl.try_get("/personal_access_tokens/self") or {}
        scopes, exp = info.get("scopes"), info.get("expires_at") or "—"
    else:
        scopes, exp = (auth.get("scope") or "").split(), t("auto-refresh", "ต่ออายุอัตโนมัติ")
    config.put_cred(url, gl.auth)
    term.out("  " + term.style("✓ ", "green") + t("Signed in as {u}", "เข้าสู่ระบบในชื่อ {u}", u=me["username"])
             + term.style(f"  ·  {'OAuth' if auth['type'] == 'oauth' else 'token'} · {t('expires', 'หมดอายุ')} {exp}", "dim"))
    miss = config.missing_scopes(scopes, config.SCOPES[side]) if scopes is not None else []
    if miss:
        term.out(term.style("  ! " + t("Missing scopes: {s}. Run glab-teleport doctor for details.",
                                       "ขาด scope: {s} ดูรายละเอียดด้วย glab-teleport doctor", s=", ".join(miss)), "yellow"))
    return True


def cmd_login(args):
    if term.JSON and not args.token_stdin:
        raise SystemExit(t("login is interactive. Ask the user to run `glab-teleport login` in their own terminal — never handle tokens yourself.",
                           "login ต้องทำแบบโต้ตอบ ให้ผู้ใช้รัน `glab-teleport login` ใน terminal ของตัวเอง — ห้ามจัดการ token แทน"))
    if not sys.stdin.isatty() and not args.token_stdin:
        raise SystemExit(t("login needs an interactive terminal (or use --side source --token-stdin).",
                           "login ต้องรันใน terminal (หรือใช้ --side source --token-stdin)"))
    if args.token_stdin and args.side == "both":
        raise SystemExit(t("--token-stdin works for one side at a time: --side source|target",
                           "--token-stdin ใช้ได้ทีละฝั่ง: --side source|target"))
    cfg = config.load_config()
    for side in (config.SIDES if args.side == "both" else [args.side]):
        for _ in range(3):
            r = _login_side(args, side, cfg)
            if r is not False or args.token_stdin or not term.confirm("  " + t("Try again?", "ลองอีกครั้งหรือไม่"), True):
                break
    term.out("")
    term.out(term.style(t("Saved to {d}. Next: glab-teleport doctor", "บันทึกที่ {d} ขั้นต่อไป: glab-teleport doctor", d=config.CONFIG_DIR), "dim"))


def cmd_logout(args):
    for side in (config.SIDES if args.side == "both" else [args.side]):
        url = config.side_url(side, getattr(args, f"{side}_url", None))
        if url and config.get_cred(url):
            config.put_cred(url, None)
            term.out(t("Signed out of {u}", "ออกจากระบบ {u} แล้ว", u=url))


def cmd_doctor(args):
    import socket
    from datetime import date
    bad = [0]

    checks = []

    def line(good, text, hint="", warn=False):
        bad[0] += 0 if good or warn else 1
        checks.append({"ok": bool(good), "level": "ok" if good else "warn" if warn else "fail",
                       "check": term.strip_ansi(text), "hint": hint if not good else ""})
        sym = term.style("✓", "green") if good else term.style("!", "yellow") if warn else term.style("✗", "red")
        term.out(f"  {sym} {text}" + (term.style(f"  → {hint}", "yellow") if hint and not good else ""))

    term.out(term.style("glab-teleport doctor", "bold") + term.style(f"  {__version__}", "dim"))
    from .update import latest, parse
    new = latest(timeout=3, npm_fallback=False)
    if new and parse(new) > parse(__version__):
        line(False, t("glab-teleport {v} · {n} is available", "glab-teleport {v} · มีเวอร์ชัน {n} แล้ว", v=__version__, n=new),
             "glab-teleport update", warn=True)
    elif new:
        line(True, t("glab-teleport {v} (latest)", "glab-teleport {v} (ล่าสุด)", v=__version__))
    line(sys.version_info >= (3, 9), f"Python {sys.version.split()[0]}", t("Python 3.9+ required", "ต้องใช้ Python 3.9 ขึ้นไป"))
    line(bool(shutil.which("git")), "git", t("install git", "ติดตั้ง git"))
    line(bool(shutil.which("git-lfs")), "git-lfs", t("only needed for repositories that use Git LFS", "จำเป็นเฉพาะ repository ที่ใช้ Git LFS"), warn=True)
    line(True, t("config {c}", "การตั้งค่า {c}", c=config.CONFIG_DIR))
    urls = {}
    for side in config.SIDES:
        url = urls[side] = config.side_url(side, getattr(args, f"{side}_url", None))
        term.heading(t("{s} GitLab", "GitLab {s}", s=side_label(side)), url or "")
        if not url:
            line(False, t("not configured", "ยังไม่ได้ตั้งค่า"), "glab-teleport login")
            continue
        host = urllib.parse.urlsplit(url).hostname
        try:
            socket.getaddrinfo(host, None)
            line(True, f"DNS {host}")
        except OSError:
            line(False, f"DNS {host}", t("check your network or VPN", "ตรวจสอบเครือข่ายหรือ VPN"))
            continue
        auth = config.resolve_auth(url, side)
        if not auth:
            line(False, t("not signed in", "ยังไม่ได้เข้าสู่ระบบ"), "glab-teleport login")
            continue
        gl = GitLab(side, url, auth, args.insecure)
        try:
            me = gl.get("/user")
        except ApiError as e:
            line(False, t("credentials rejected (HTTP {c})", "ข้อมูลเข้าสู่ระบบใช้ไม่ได้ (HTTP {c})", c=e.code), "glab-teleport login")
            continue
        line(True, t("signed in as {u}", "เข้าสู่ระบบในชื่อ {u}", u=me["username"]) + (" (admin)" if me.get("is_admin") else "")
             + term.style(f"  {'OAuth' if gl.auth.get('type') == 'oauth' else 'token'}", "dim"))
        if gl.auth.get("type") == "oauth":
            scopes = (gl.auth.get("scope") or "").split()
            line(True, t("OAuth token refreshes automatically", "OAuth token ต่ออายุอัตโนมัติ"))
        else:
            info = gl.try_get("/personal_access_tokens/self") or {}
            scopes, exp = info.get("scopes") or [], info.get("expires_at")
            if exp:
                days = (date.fromisoformat(exp) - date.today()).days
                line(days > 7, t("token expires {d} (in {n} days)", "token หมดอายุ {d} (อีก {n} วัน)", d=exp, n=days),
                     t("create a new token and run glab-teleport login", "สร้าง token ใหม่แล้วรัน glab-teleport login"), warn=days > 0)
        miss = config.missing_scopes(scopes, config.SCOPES[side])
        soft = side == "source" and miss == ["read_repository"]  # some GitLab versions allow git reads with read_api
        line(not miss, "scopes: " + (", ".join(sorted(scopes)) or "?"), t("missing {m}", "ขาด {m}", m=", ".join(miss)), warn=soft)
        try:
            p = (gl.get("/projects", membership="true", per_page=1, simple="true", order_by="last_activity_at") or [None])[0]
            if p:
                line(True, t("git access works ({p})", "เข้าถึง git ได้ ({p})", p=p["path_with_namespace"]) + term.style(f"  {len(ls_remote(gl, p['http_url_to_repo']))} refs", "dim"))
        except GitError as e:
            line(False, t("git access failed", "เข้าถึง git ไม่ได้"), str(e)[:80])
        if side == "target":
            gs = gl.get_all("/groups", min_access_level=40, top_level_only="true")
            line(bool(gs), t("Maintainer or Owner of: {g}", "เป็น Maintainer หรือ Owner ของ: {g}", g=", ".join(g["full_path"] for g in gs[:6]) or "—"),
                 t("Maintainer of the target group is required", "ต้องเป็น Maintainer ของ group ปลายทาง"))
    if urls.get("source") and urls.get("source") == urls.get("target"):
        term.out(term.style("\n  ! " + t("Source and target are the same GitLab. For moves inside one instance, GitLab's own Transfer is faster.",
                                         "ต้นทางและปลายทางเป็น GitLab เดียวกัน หากย้ายภายในระบบเดียวกัน ใช้ Transfer ของ GitLab จะเร็วกว่า"), "yellow"))
    term.emit({"ok": not bad[0], "problems": bad[0], "source": urls.get("source"), "target": urls.get("target"), "checks": checks})
    term.out("")
    term.out(term.style(t("All set. Run: glab-teleport", "พร้อมใช้งาน เริ่มได้ด้วยคำสั่ง: glab-teleport"), "green") if not bad[0]
             else term.style(t("{n} problem(s) to fix (see → above).", "พบ {n} รายการที่ต้องแก้ไข (ดู → ด้านบน)", n=bad[0]), "red"))
    raise SystemExit(1 if bad[0] else 0)


def cmd_config(args):
    cfg = config.load_config()
    if not args.key:
        term.emit({"ok": True, **{k: cfg.get(k) for k in ("lang", "source", "target", "read_only")}, "file": str(config.CONFIG_FILE)})
        term.kv([(k, str(cfg.get(k, "—"))) for k in ("lang", "source", "target", "read_only")])
        term.out(term.style(f"\n  {config.CONFIG_FILE}", "dim"))
        return
    key = args.key.lower()
    if key not in ("lang", "source", "target", "read_only"):
        raise SystemExit(t("Unknown key '{k}'. Use: lang, source, target, read_only", "ไม่รู้จัก '{k}' ใช้ได้: lang, source, target, read_only", k=key))
    if args.value is None:
        return term.out(str(cfg.get(key, "")))
    value = args.value.strip()
    if key == "read_only":
        value = value.lower() in ("1", "true", "on", "yes")
    elif key == "lang":
        if value not in ("en", "th"):
            raise SystemExit(t("Language must be en or th", "ภาษาต้องเป็น en หรือ th"))
        set_lang(value)
    else:
        value = config.normalize_url(value)
    cfg[key] = value
    config.save_config(cfg)
    term.out(term.style("✓ ", "green") + f"{key} = {value}")
