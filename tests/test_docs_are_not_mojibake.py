"""No shipped text may be UTF-8 that was decoded as cp1252 and written back out.

This is the encoding hazard this project already knows about from the other direction — the Windows
console is cp1252 and dies on characters outside it — and on 2026-09-07 the same hazard was found
to have gone the other way in a **shipped** document: `docs/10_sre_app.md` had 41 lines where `→`
had become `â†'` and `│` had become `â”‚`. It reads as line noise, it is in the public package, and
nothing noticed for as long as it had been there.

It was found by accident, while scanning for Vietnamese text — `â` is a Vietnamese letter, and the
corruption manufactures them. That is not a detection strategy anyone should rely on twice.

**The test is the repair, run as a question.** Take each line, encode it back to cp1252, decode
those bytes as UTF-8. If that round trip succeeds *and returns something different*, the line is
mojibake and the result is what it originally said. If the round trip raises, the line was never
corrupted. No list of known-bad sequences to keep up to date, and no character the check does not
cover.
"""

import re
from pathlib import Path

import pytest


#: Everything a reader or an installer receives. `audits/` is excluded because it is not shipped
#: and because an audit must not be edited after the fact — a corrupted one is reported, not fixed.
SHIPPED_ROOTS = ("README.md", "CHANGELOG.md", "CONTRIBUTING.md", "docs", "data", "db_ops",
                 "examples")

TEXT_SUFFIXES = {".md", ".py", ".json", ".yml", ".yaml", ".sql", ".ps1", ".html", ".toml", ".txt",
                 ".j2", ".cfg", ".ini"}

REPO_ROOT = Path(__file__).resolve().parent.parent


def _shipped_text_files() -> list[Path]:
    files: list[Path] = []
    for name in SHIPPED_ROOTS:
        root = REPO_ROOT / name
        if not root.exists():
            continue
        if root.is_file():
            files.append(root)
            continue
        files.extend(f for f in root.rglob("*")
                     if f.is_file() and f.suffix.lower() in TEXT_SUFFIXES)
    return files


def _original_if_mojibake(line: str) -> str | None:
    """What the line said before it was corrupted, or None if it was never corrupted.

    The whole line first; then each run of non-ASCII characters on its own. A line that also holds
    a character cp1252 has but UTF-8 cannot start with - a genuine `—` - cannot round-trip as a
    whole, and three lines of `docs/10_sre_app.md` kept `â†’` past this check for exactly that
    reason until 0.24.0.
    """
    try:
        repaired = line.encode("cp1252").decode("utf-8")
        return repaired if repaired != line else None
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    changed = False

    def repair(match: "re.Match[str]") -> str:
        nonlocal changed
        run = match.group(0)
        try:
            fixed = run.encode("cp1252").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return run
        changed = changed or fixed != run
        return fixed

    repaired = re.sub(r"[^\x00-\x7f]+", repair, line)
    return repaired if changed else None


def test_no_shipped_file_holds_mojibake() -> None:
    """A `→` that reads as `â†'` is not a typo, it is a file that went through the wrong codec.

    Reported with what each line *should* say, because the corrupted form is unreadable and a
    failure that only says "line 1923 is wrong" leaves the reader doing this by hand.
    """
    findings: list[str] = []
    for path in _shipped_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(f"{path.relative_to(REPO_ROOT).as_posix()}: not valid UTF-8 at all")
            continue
        for number, line in enumerate(text.splitlines(), 1):
            original = _original_if_mojibake(line)
            if original is not None:
                findings.append(
                    f"{path.relative_to(REPO_ROOT).as_posix()}:{number}\n"
                    f"     is: {line.strip()[:88]}\n"
                    f"  means: {original.strip()[:88]}")

    assert not findings, (
        f"{len(findings)} shipped line(s) are UTF-8 that was decoded as cp1252 and written back "
        f"out. Repair each by `line.encode('cp1252').decode('utf-8')` — the round trip is exact, "
        f"which is why this test can name what the line should say:\n\n" + "\n".join(findings[:20]))


def test_the_check_recognises_the_corruption_it_exists_for() -> None:
    """The detector, held to the real case. `→` (U+2192) is `E2 86 92` in UTF-8; read as cp1252
    those three bytes are `â`, `†`, `'`, and that is exactly what was in the file."""
    corrupted = "Windows host â†’ bastion-01"

    assert _original_if_mojibake(corrupted) == "Windows host → bastion-01"


def test_the_corruption_is_found_beside_a_character_that_was_meant():
    """The line that slipped through: a genuine em dash made the whole-line round trip fail, so the
    arrow beside it was never looked at."""
    corrupted = "| — | IP discovery | ARP scan â†’ MAC map |"

    assert _original_if_mojibake(corrupted) == "| — | IP discovery | ARP scan → MAC map |"


@pytest.mark.parametrize("line", [
    "plain ascii only",
    "an em dash — and a curly quote ’ that are genuinely meant",
    "→ a real arrow, already correct",
    "中文 characters outside cp1252 entirely",
])
def test_correct_text_is_not_reported(line: str) -> None:
    """The half that stops this being noise. Text that is already right either cannot be encoded
    back to cp1252 at all, or round-trips to itself — neither is a finding."""
    assert _original_if_mojibake(line) is None
