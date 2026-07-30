"""Runtime localisation: Java-style .properties bundles in UTF-8.

lang/messages.properties is English and the complete reference; a
messages_XX.properties overlays it, so a missing key silently falls back to
English and never crashes a message. The language comes from config.json
("language") or, when unset, from the Windows UI language / POSIX locale.
"""

import locale
import os

LANG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "lang")
LANGUAGES = ("en", "ru", "uk", "ko", "zh", "vi", "el", "pt", "es")
NATIVE = {"en": "English", "ru": "Русский", "uk": "Українська", "ko": "한국어",
          "zh": "中文", "vi": "Tiếng Việt", "el": "Ελληνικά",
          "pt": "Português", "es": "Español"}

# Windows primary LANGIDs for the shipped languages.
_WIN_LANGS = {0x09: "en", 0x19: "ru", 0x22: "uk", 0x12: "ko", 0x04: "zh",
              0x2A: "vi", 0x08: "el", 0x16: "pt", 0x0A: "es"}

_base = {}
_active = {}
_lang = "en"


def _parse(path):
    table = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith(("#", "!")) or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                table[key.strip()] = value.strip().replace("\\n", "\n")
    except OSError:
        pass
    return table


def detect():
    """Best-effort OS language, limited to the shipped set."""
    try:
        import ctypes
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
        if langid in _WIN_LANGS:
            return _WIN_LANGS[langid]
    except (OSError, AttributeError):
        pass
    try:
        code = (locale.getlocale()[0] or "")[:2].lower()
        if code in LANGUAGES:
            return code
    except (ValueError, TypeError):
        pass
    return "en"


def set_language(lang=None):
    """None means auto-detect. Unknown codes fall back to English."""
    global _lang, _base, _active
    _lang = lang if lang in LANGUAGES else detect()
    if _lang not in LANGUAGES:
        _lang = "en"
    _base = _parse(os.path.join(LANG_DIR, "messages.properties"))
    _active = dict(_base)
    if _lang != "en":
        _active.update(_parse(os.path.join(LANG_DIR, f"messages_{_lang}.properties")))


def current():
    return _lang


def t(key, **kwargs):
    """Translate; a missing key returns the key itself so nothing ever dies."""
    text = _active.get(key) or _base.get(key) or key
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            pass
    return text


set_language(None)
