"""Two-language UI (English default, Thai optional).

Every user-facing string is written as ``t(english, thai, **fields)`` right where it is used,
so a translation can never drift away from the code that prints it.
"""
import os

LANGS = ("en", "th")
_lang = "en"
explicit = False  # True when the user chose a language (flag, env or config) — the wizard then won't ask


def set_lang(code):
    global _lang
    code = (code or "").strip().lower()[:2]
    _lang = code if code in LANGS else "en"
    return _lang


def lang():
    return _lang


def t(en, th=None, **fields):
    text = th if _lang == "th" and th else en
    return text.format(**fields) if fields else text


def resolve_lang(argv, config):
    """--lang flag > GLAB_TELEPORT_LANG > config file > English."""
    global explicit
    for i, a in enumerate(argv):
        if a == "--lang" and i + 1 < len(argv):
            explicit = True
            return argv[i + 1]
        if a.startswith("--lang="):
            explicit = True
            return a.split("=", 1)[1]
    chosen = os.environ.get("GLAB_TELEPORT_LANG") or config.get("lang")
    explicit = bool(chosen)
    return chosen or "en"
