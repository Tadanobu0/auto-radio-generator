"""Reading fixes: replace words in the text sent to VOICEVOX only (019).

The Mac render (radio_director) passes its config/reading_fixes.json (word -> kana).
Only the text given to /audio_query changes; subtitles keep the script's text.

Accepted JSON shapes (keys starting with "_" are comments and skipped):
- {"語": "かな", ...}
- {"fixes": {...}} / {"reading_fixes": {...}} / {"replacements": {...}} (same mapping inside)
- [{"word": "語", "reading": "かな"}, ...] (also from/to, surface/kana, src/dst),
  or the same list under "fixes" / "reading_fixes" / "replacements" / "entries"

Replacement is one pass, longest word first, so a replaced reading is never replaced again.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_WRAPPER_KEYS = ("fixes", "reading_fixes", "replacements", "entries")
_PAIR_KEYS = (("word", "reading"), ("from", "to"), ("surface", "kana"), ("src", "dst"))


class ReadingFixesError(ValueError):
    """The reading fixes file is missing or not in an accepted shape."""


@dataclass
class ReadingFixes:
    """Word -> reading table; apply() returns the text for synthesis."""

    table: dict[str, str] = field(default_factory=dict)
    source: Optional[Path] = None
    applied_count: int = 0

    def __post_init__(self) -> None:
        words = sorted((w for w in self.table if w), key=len, reverse=True)
        self._pattern = re.compile("|".join(re.escape(w) for w in words)) if words else None

    def __len__(self) -> int:
        return len(self.table)

    def apply(self, text: str) -> str:
        if not self._pattern or not text:
            return text

        def _sub(match: re.Match) -> str:
            self.applied_count += 1
            return self.table[match.group(0)]

        return self._pattern.sub(_sub, text)


def parse_reading_fixes(data: Any, source: Optional[Path] = None) -> ReadingFixes:
    """ReadingFixes from loaded JSON (see the module doc for the shapes)."""
    if isinstance(data, dict):
        for key in _WRAPPER_KEYS:
            if key in data and isinstance(data[key], (dict, list)):
                data = data[key]
                break
    table: dict[str, str] = {}
    if isinstance(data, dict):
        for word, reading in data.items():
            if str(word).startswith("_"):
                continue
            _add(table, word, reading, source)
    elif isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                raise ReadingFixesError(f"reading_fixes: {source}: list entries must be objects: {entry!r}")
            pair = next(((entry[a], entry[b]) for a, b in _PAIR_KEYS if a in entry and b in entry), None)
            if pair is None:
                raise ReadingFixesError(
                    f"reading_fixes: {source}: entry needs word/reading (or from/to): {entry!r}"
                )
            _add(table, *pair, source)
    else:
        raise ReadingFixesError(f"reading_fixes: {source}: expected an object or a list")
    return ReadingFixes(table=table, source=source)


def load_reading_fixes(path: str | Path) -> ReadingFixes:
    """Read the table from a JSON file. A missing or broken file is a ReadingFixesError."""
    path = Path(path)
    if not path.is_file():
        raise ReadingFixesError(f"reading_fixes: {path} not found")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as e:
        raise ReadingFixesError(f"reading_fixes: cannot read {path} ({type(e).__name__}: {e})") from e
    return parse_reading_fixes(data, source=path)


def _add(table: dict[str, str], word: Any, reading: Any, source: Optional[Path]) -> None:
    if not isinstance(word, str) or not isinstance(reading, str):
        raise ReadingFixesError(f"reading_fixes: {source}: word and reading must be strings: {word!r} -> {reading!r}")
    if not word or not reading.strip():
        raise ReadingFixesError(f"reading_fixes: {source}: empty word or reading: {word!r} -> {reading!r}")
    table[word] = reading
