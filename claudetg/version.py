"""The single place the version number lives.

build.py hands this to PyInstaller and to Inno Setup, and the updater compares
releases against it, so a bump here is the only edit a release needs.
"""

__version__ = "1.0.5"

# Where the updater looks for releases. A fork only has to change this.
REPO = "bonuxq/claude-telegram-bridge"


def parts(text):
    """A version as comparable integers. Anything unparsable sorts lowest, so
    a malformed tag can never look newer than what is installed."""
    numbers = []
    for chunk in str(text or "").strip().lstrip("vV").split("."):
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        numbers.append(int(digits))
    return tuple(numbers) or (0,)


def is_newer(candidate, current=__version__):
    return parts(candidate) > parts(current)
