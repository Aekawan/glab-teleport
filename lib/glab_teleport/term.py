"""Terminal UI: colors, aligned tables, a single live status line, prompts and a fuzzy picker.

No curses: every frame is redrawn with plain ANSI so Thai (and other combining scripts) render correctly.
"""
import codecs
import getpass
import os
import select
import sys
import threading
import time
import unicodedata

from .i18n import t

COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
STYLES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "blue": "34", "magenta": "35", "cyan": "36",
          "title": "1;36", "ok": "32", "warn": "33", "fail": "31", "new": "36", "key": "1"}
SYM = {"ok": "✓", "fail": "✗", "warn": "!", "new": "+", "update": "↻", "move": "↪", "skip": "–", "info": "·", "manual": "☞"}


def set_color(enabled):
    global COLOR
    COLOR = enabled


def style(text, *names):
    if not COLOR or not names or text == "":
        return str(text)
    codes = ";".join(STYLES.get(n, n) for n in names)
    return f"\x1b[{codes}m{text}\x1b[0m"


def strip_ansi(s):
    out, i = [], 0
    while i < len(s):
        if s[i] == "\x1b":
            j = i + 1
            if j < len(s) and s[j] == "[":
                j += 1
                while j < len(s) and not s[j].isalpha():
                    j += 1
            i = j + 1
            continue
        out.append(s[i])
        i += 1
    return "".join(out)


def width(s):
    """Display width: combining marks (Thai vowels/tones) take 0 columns, wide CJK take 2."""
    s = strip_ansi(str(s))
    return sum(0 if unicodedata.combining(c) or unicodedata.category(c) in ("Mn", "Me", "Cf")
               else 2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def fit(s, cols):
    """Truncate plain text to `cols` display columns with an ellipsis."""
    s = str(s)
    if width(s) <= cols:
        return s
    out, w = "", 0
    for c in s:
        cw = width(c)
        if w + cw > max(0, cols - 1):
            break
        out, w = out + c, w + cw
    return out + "…"


def fit_path(s, cols):
    """Shorten a path in the middle, keeping the first segment and the project name: a/…/project."""
    s = str(s)
    if width(s) <= cols:
        return s
    parts = s.split("/")
    for keep_head in (1, 0):
        head = "/".join(parts[:keep_head]) + "/…/" if keep_head else "…/"
        for k in range(len(parts) - 1, 0, -1):
            cand = head + "/".join(parts[-k:])
            if width(cand) <= cols and k < len(parts) - keep_head:
                return cand
    return "…" + s[-(cols - 1):] if cols > 1 else "…"


def pad(s, cols, align="<"):
    gap = max(0, cols - width(s))
    return (" " * gap + s) if align == ">" else (s + " " * gap)


def term_cols(default=100):
    try:
        return os.get_terminal_size().columns
    except OSError:
        return default


# ── output ──
_out_lock = threading.Lock()


def out(line=""):
    with _out_lock:
        if Status.active:
            Status.active.clear()
        print(line, flush=True)
        if Status.active:
            Status.active.draw()


def err(line):
    with _out_lock:
        print(line, file=sys.stderr, flush=True)


def heading(text, sub=""):
    out("")
    out(style(text, "bold") + (("  " + style(sub, "dim")) if sub else ""))


def kv(rows, indent=2):
    """Aligned key/value lines."""
    w = max((width(k) for k, _ in rows), default=0)
    for k, v in rows:
        out(" " * indent + style(pad(k, w), "dim") + "  " + v)


def table(headers, rows, indent=2, aligns=None, max_cols=None, paths=(), shrink_last=True):
    """Aligned table. Cells may contain ANSI styles. On a terminal, long cells shrink to fit
    (path columns shorten in the middle, the last column first); piped output is never truncated."""
    cols = len(headers)
    aligns = aligns or ["<"] * cols
    widths = [max([width(h)] + [width(r[i]) for r in rows]) for i, h in enumerate(headers)]
    if sys.stdout.isatty() or max_cols:
        limit = (max_cols or term_cols()) - indent - 2 * (cols - 1) - 1
        floor = [max(width(h), 16 if i in paths else 8) for i, h in enumerate(headers)]
        if shrink_last:  # notes column gives way first
            last = cols - 1
            widths[last] -= max(0, min(sum(widths) - limit, widths[last] - floor[last]))
        while sum(widths) > limit:  # then shave the widest column one step at a time (keeps columns balanced)
            i = max(range(cols), key=lambda k: widths[k] - floor[k])
            if widths[i] <= floor[i]:
                break
            widths[i] -= 1

    def cell(v, i):
        plain = strip_ansi(str(v))
        if width(plain) > widths[i]:
            v = fit_path(plain, widths[i]) if i in paths else fit(plain, widths[i])
        return pad(v, widths[i], aligns[i])
    if any(headers):
        out(" " * indent + style("  ".join(pad(h, widths[i], aligns[i]) for i, h in enumerate(headers)).rstrip(), "dim"))
    for r in rows:
        out(" " * indent + "  ".join(cell(r[i], i) for i in range(cols)).rstrip())


# ── live status line (one line, bottom of output) ──
class Status:
    active = None
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, text=""):
        self.text, self.i, self.tty = text, 0, sys.stdout.isatty()
        self._stop = threading.Event()

    def __enter__(self):
        if self.tty:
            Status.active = self
            self._th = threading.Thread(target=self._spin, daemon=True)
            self._th.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self.tty:
            with _out_lock:
                self.clear()
                Status.active = None

    def update(self, text):
        self.text = text

    def _spin(self):
        while not self._stop.wait(0.1):
            with _out_lock:
                if Status.active is self:
                    self.i += 1
                    self.draw()

    def draw(self):
        frame = style(self.FRAMES[self.i % len(self.FRAMES)], "cyan")
        sys.stdout.write("\r\x1b[K  " + frame + " " + fit(self.text, term_cols() - 6))
        sys.stdout.flush()

    def clear(self):
        sys.stdout.write("\r\x1b[K")
        sys.stdout.flush()


# ── prompts ──
def confirm(question, default=False):
    if not sys.stdin.isatty():
        return default
    hint = "[Y/n]" if default else "[y/N]"
    ans = input(f"{question} {style(hint, 'dim')} ").strip().lower()
    return default if not ans else ans in ("y", "yes", "ใช่", "ช")


def prompt(question, default=""):
    ans = input(f"{question}{style(' [' + default + ']', 'dim') if default else ''}: ").strip()
    return ans or default


def secret(question):
    return getpass.getpass(f"{question}: ").strip()


# ── fuzzy picker ──
def item(value, label, meta="", prefix="", tag="", help=""):
    """help: a longer explanation shown under the list while this row is highlighted."""
    return {"value": value, "label": label, "meta": meta, "prefix": prefix, "tag": tag, "help": help,
            "search": f"{prefix}{label} {meta} {tag}".lower()}


def wrap(text, cols):
    """Word-wrap by display width (Thai has no spaces between words, so long runs are cut by width)."""
    lines, cur = [], ""
    for word in text.split(" "):
        cand = (cur + " " + word) if cur else word
        if width(cand) <= cols:
            cur = cand
            continue
        if cur:
            lines.append(cur)
        while width(word) > cols:
            cut = fit(word, cols + 1)[:-1]
            lines.append(cut)
            word = word[len(cut):]
        cur = word
    return lines + ([cur] if cur else [])


class Picker:
    """fzf-style list: type to filter (all words must match), ↑↓ move, Enter select,
    Tab toggle (multi), Ctrl-A toggle all visible, Esc clear/back, Ctrl-C quit."""

    KEYS = {b"[A": "UP", b"OA": "UP", b"[B": "DOWN", b"OB": "DOWN", b"[5~": "PGUP", b"[6~": "PGDN",
            b"[H": "HOME", b"OH": "HOME", b"[1~": "HOME", b"[F": "END", b"OF": "END", b"[4~": "END"}

    def __init__(self, title, items, multi=False, preselect=(), custom=None, subtitle="", empty_ok=False):
        self.title, self.items, self.multi, self.subtitle, self.empty_ok = title, items, multi, subtitle, empty_ok
        self.custom = custom
        self.chosen = {i["value"] for i in items if i["value"] in set(preselect)}
        self.query, self.cur, self.top, self.size = "", 0, 0, None

    def visible(self):
        terms = self.query.lower().split()
        rows = [i for i in self.items if all(x in i["search"] for x in terms)]
        if self.custom and self.query.strip():
            rows.append({"value": ("custom", self.query.strip()), "label": f"↳ {self.custom}: {self.query.strip()}",
                         "meta": "", "prefix": "", "tag": "", "search": ""})
        return rows

    @staticmethod
    def _line(segs, cols, fill=None):
        parts, w = [], 0
        for text, code in segs:
            if not text or w >= cols:
                continue
            s = text if width(text) <= cols - w else fit(text, cols - w)
            parts.append(f"\x1b[{code}m{s}\x1b[0m" if code else s)
            w += width(s)
        if fill is not None and w < cols:
            parts.append(f"\x1b[{fill}m{' ' * (cols - w)}\x1b[0m" if fill else " " * (cols - w))
        return "".join(parts), w

    def _label(self, it, base):
        j = lambda *c: ";".join(x for x in (base, *c) if x)
        segs = [(it["prefix"], j("2"))] if it["prefix"] else []
        label, marks = it["label"], [False] * len(it["label"])
        for term in self.query.lower().split():
            p = label.lower().find(term)
            if p >= 0:
                marks[p:p + len(term)] = [True] * len(term)
        i = 0
        while i < len(label):
            k = i
            while k < len(label) and marks[k] == marks[i]:
                k += 1
            segs.append((label[i:k], j("1", "33") if marks[i] else j()))
            i = k
        if it["tag"]:
            segs.append(("  " + it["tag"], j("36")))
        return segs

    def _draw(self, rows):
        cols, height = os.get_terminal_size()
        self.size, page = (cols, height), max(1, height - 5)
        self.cur = max(0, min(self.cur, len(rows) - 1))
        self.top = min(max(self.top, self.cur - page + 1), self.cur) if rows else 0
        W = cols - 1
        lines = [self._line([(" " + self.title, "1"), ("   " + self.subtitle, "2")], W)[0]]
        real = sum(1 for r in rows if not isinstance(r["value"], tuple))
        count = f"{real}/{len(self.items)}" + (t("  {n} selected", "  เลือก {n}", n=len(self.chosen)) if self.multi else "") + " "
        left, lw = self._line([(" › ", "1;36"), (self.query, "1")], W - width(count) - 1)
        lines.append(left + " " * (W - lw - width(count)) + f"\x1b[2m{count}\x1b[0m")
        lines.append(f"\x1b[2m {'─' * (W - 2)}\x1b[0m")
        shown = rows[self.top:self.top + page]
        lead = 3 + (2 if self.multi else 0)
        lab_w = max((width(i["prefix"] + i["label"] + ("  " + i["tag"] if i["tag"] else "")) for i in shown), default=0)
        meta_col = lead + lab_w + 3          # descriptions line up in one column when labels are short
        aligned = meta_col <= W * 0.55
        for n, it in enumerate(shown):
            on = self.top + n == self.cur
            base = "7" if on else ""
            j = lambda *c: ";".join(x for x in (base, *c) if x)
            segs = [(" › " if on else "   ", j("1", "36"))]
            if self.multi and not isinstance(it["value"], tuple):
                tick = it["value"] in self.chosen
                segs.append(("◉ " if tick else "○ ", j("32") if tick else j("2")))
            meta = it["meta"]
            if meta and aligned:
                body, _ = self._line(segs + self._label(it, base), meta_col, fill=base)
                lines.append(body + self._line([(meta, j("2"))], W - meta_col, fill=base)[0])
                continue
            lw = 5 + width(it["prefix"] + it["label"] + ("  " + it["tag"] if it["tag"] else ""))
            mw = min(width(meta) + 2, max(0, W - lw - 2)) if meta else 0   # the label wins; the description gets what is left
            if mw < 14:
                mw = 0                                                        # too narrow to be useful: hide it
            body, _ = self._line(segs + self._label(it, base), W - mw, fill=base)
            tail = (self._line([(meta, j("2"))], mw - 1, fill=base)[0] + (f"\x1b[{base}m \x1b[0m" if base else " ")) if meta else ""
            lines.append(body + tail)
        if not rows:
            lines.append("\x1b[2m   " + t("No matches — keep typing or press Esc to clear",
                                          "ไม่พบรายการที่ตรงกัน — พิมพ์คำอื่นหรือกด Esc เพื่อล้าง") + "\x1b[0m")
        more = len(rows) - (self.top + len(shown))
        lines.append("\x1b[2m   " + (t("↓ {n} more", "↓ อีก {n} รายการ", n=more) if more > 0 else "") + "\x1b[0m")
        current = rows[self.cur] if rows else None
        if current and current.get("help"):
            for ln in wrap(current["help"], W - 6)[:4]:
                lines.append("\x1b[36m   │ \x1b[0m" + ln)
            lines.append("")
        if self.multi:  # keys right under the list, where the eye already is
            keys = [("Tab", t("select / unselect", "เลือก / ยกเลิก")), ("Enter", t("continue", "ดำเนินการต่อ")),
                    ("Ctrl-A", t("all", "ทั้งหมด")), ("Esc", t("back", "ย้อนกลับ"))]
        else:
            keys = [("↑↓", t("move", "เลื่อน")), ("Enter", t("select", "เลือก")), (t("type", "พิมพ์"), t("to filter", "เพื่อค้นหา")),
                    ("Esc", t("back", "ย้อนกลับ"))]
        segs = [(" ", "")]
        for k, (key, what) in enumerate(keys):
            segs += [(("   " if k else "  ") + key, "1"), (" " + what, "2")]
        lines.append(self._line(segs, W)[0])
        lines += [""] * (height - len(lines))
        sys.stdout.write("\x1b[?25l\x1b[H" + "\r\n".join(l + "\x1b[K" for l in lines[:height])
                         + f"\x1b[2;{4 + width(self.query)}H\x1b[?25h")
        sys.stdout.flush()

    def _key(self, fd):
        while True:
            if not select.select([fd], [], [], 0.25)[0]:
                if os.get_terminal_size() != self.size:
                    return "RESIZE"
                continue
            b = os.read(fd, 1)
            if b == b"\x1b":
                seq = b""
                while select.select([fd], [], [], 0.03)[0]:
                    seq += os.read(fd, 1)
                    if len(seq) >= 2 and (seq[-1:].isalpha() or seq[-1:] == b"~"):
                        break
                return self.KEYS.get(seq, "") if seq else "ESC"
            ch = self._dec.decode(b)
            if ch:
                return ch

    def run(self):
        import termios
        import tty
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="ignore")
        sys.stdout.write("\x1b[?1049h")
        try:
            tty.setraw(fd)
            while True:
                rows = self.visible()
                self._draw(rows)
                k = self._key(fd)
                page = max(1, self.size[1] - 5)
                if k == "\x03":
                    raise KeyboardInterrupt
                if k in ("UP", "\x10"):
                    self.cur -= 1
                elif k in ("DOWN", "\x0e"):
                    self.cur += 1
                elif k == "PGUP":
                    self.cur -= page
                elif k == "PGDN":
                    self.cur += page
                elif k == "HOME":
                    self.cur = 0
                elif k == "END":
                    self.cur = len(rows) - 1
                elif k in ("\r", "\n"):
                    if not rows:
                        continue
                    pick = rows[self.cur]["value"]
                    if isinstance(pick, tuple):
                        return [pick] if self.multi else pick
                    if self.multi:
                        picked = [i["value"] for i in self.items if i["value"] in self.chosen]
                        return picked if picked or self.empty_ok else [pick]
                    return pick
                elif k == "\t" and self.multi and rows and not isinstance(rows[self.cur]["value"], tuple):
                    self.chosen ^= {rows[self.cur]["value"]}
                    self.cur += 1
                elif k == "\x01" and self.multi:
                    vals = {r["value"] for r in rows if not isinstance(r["value"], tuple)}
                    self.chosen = self.chosen - vals if vals <= self.chosen else self.chosen | vals
                elif k in ("\x7f", "\x08"):
                    self.query, self.cur, self.top = self.query[:-1], 0, 0
                elif k == "\x15":
                    self.query, self.cur, self.top = "", 0, 0
                elif k == "ESC":
                    if not self.query:
                        return None
                    self.query, self.cur, self.top = "", 0, 0
                elif len(k) == 1 and k.isprintable():
                    self.query, self.cur, self.top = self.query + k, 0, 0
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            sys.stdout.write("\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()


def ask(title, items, **kw):
    return Picker(title, items, **kw).run() if items else None


def elapsed(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    return f"{m}m {s:02d}s" if m < 60 else f"{m // 60}h {m % 60:02d}m"


def now():
    return time.time()
