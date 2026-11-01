"""Reading fixes: replace words in the text sent to VOICEVOX only (019).

The Mac render (radio_director) passes its config/reading_fixes.json.
Only the text given to /audio_query changes; subtitles keep the script's text.

radio_director's shape (regex rules, applied with re.sub in list order):
    {"version": 1, "note": "...", "fixes": [
        {"id": "...", "pattern": "<regex>", "replace": "<hiragana>", "reading": "<katakana, for people>", "note": "..."},
    ]}
  pattern must compile and must not match the empty string; replace must not use
  backreferences (\\1, \\g<name>). reading is only a description and is not used.

Also accepted (word -> kana, replaced as plain text; keys starting with "_" are comments):
- {"語": "かな", ...}
- {"fixes": {...}} / {"reading_fixes": {...}} / {"replacements": {...}} (same mapping inside)
- [{"word": "語", "reading": "かな"}, ...] (also from/to, surface/kana, src/dst),
  or the same list under "fixes" / "reading_fixes" / "replacements" / "entries"
A list may mix both kinds: an entry with "pattern" is a regex rule.

Order: the regex rules first (in list order, one re.sub each), then the plain words in one
pass, longest word first (a plain replacement is never replaced again).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_WRAPPER_KEYS = ("fixes", "reading_fixes", "replacements", "entries")
_PAIR_KEYS = (("word", "reading"), ("from", "to"), ("surface", "kana"), ("src", "dst"))
# backreferences in a re.sub replacement: \1 .. \99 and \g<...>
_BACKREF = re.compile(r"\\(?:\d|g<)")


class ReadingFixesError(ValueError):
    """The reading fixes file is missing or not in an accepted shape."""


@dataclass
class RegexFix:
    """One regex rule of radio_director's reading_fixes.json."""

    pattern: re.Pattern
    replace: str
    id: Optional[str] = None


@dataclass
class ReadingFixes:
    """Regex rules and word -> reading table; apply() returns the text for synthesis."""

    table: dict[str, str] = field(default_factory=dict)
    source: Optional[Path] = None
    applied_count: int = 0
    rules: list[RegexFix] = field(default_factory=list)

    def __post_init__(self) -> None:
        words = sorted((w for w in self.table if w), key=len, reverse=True)
        self._pattern = re.compile("|".join(re.escape(w) for w in words)) if words else None

    def __len__(self) -> int:
        return len(self.rules) + len(self.table)

    def apply(self, text: str) -> str:
        if not text:
            return text
        for rule in self.rules:
            text, n = rule.pattern.subn(rule.replace, text)
            self.applied_count += n
        if not self._pattern:
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
    rules: list[RegexFix] = []
    if isinstance(data, dict):
        for word, reading in data.items():
            if str(word).startswith("_"):
                continue
            _add(table, word, reading, source)
    elif isinstance(data, list):
        for index, entry in enumerate(data):
            if not isinstance(entry, dict):
                raise ReadingFixesError(f"reading_fixes: {source}: list entries must be objects: {entry!r}")
            if "pattern" in entry:
                rules.append(_regex_rule(entry, index, source))
                continue
            pair = next(((entry[a], entry[b]) for a, b in _PAIR_KEYS if a in entry and b in entry), None)
            if pair is None:
                raise ReadingFixesError(
                    f"reading_fixes: {source}: entry needs pattern/replace (or word/reading): {entry!r}"
                )
            _add(table, *pair, source)
    else:
        raise ReadingFixesError(f"reading_fixes: {source}: expected an object or a list")
    return ReadingFixes(table=table, source=source, rules=rules)


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


def _regex_rule(entry: dict, index: int, source: Optional[Path]) -> RegexFix:
    """A regex rule; same checks as radio_director (valid regex, no empty match, no backreference)."""
    name = entry.get("id") or f"#{index}"
    pattern, replace = entry.get("pattern"), entry.get("replace")
    if not isinstance(pattern, str) or not pattern:
        raise ReadingFixesError(f"reading_fixes: {source}: {name}: pattern must be a non-empty string")
    if not isinstance(replace, str):
        raise ReadingFixesError(f"reading_fixes: {source}: {name}: replace must be a string")
    try:
        compiled = re.compile(pattern)
    except re.error as e:
        raise ReadingFixesError(f"reading_fixes: {source}: {name}: invalid pattern {pattern!r} ({e})") from e
    if compiled.search("") is not None:
        raise ReadingFixesError(f"reading_fixes: {source}: {name}: pattern {pattern!r} matches the empty string")
    if _BACKREF.search(replace):
        raise ReadingFixesError(f"reading_fixes: {source}: {name}: replace {replace!r} uses a backreference")
    try:
        compiled.sub(replace, "")  # a bad escape in replace fails here, not in the middle of synthesis
    except re.error as e:
        raise ReadingFixesError(f"reading_fixes: {source}: {name}: invalid replace {replace!r} ({e})") from e
    return RegexFix(pattern=compiled, replace=replace, id=entry.get("id"))


def _add(table: dict[str, str], word: Any, reading: Any, source: Optional[Path]) -> None:
    if not isinstance(word, str) or not isinstance(reading, str):
        raise ReadingFixesError(f"reading_fixes: {source}: word and reading must be strings: {word!r} -> {reading!r}")
    if not word or not reading.strip():
        raise ReadingFixesError(f"reading_fixes: {source}: empty word or reading: {word!r} -> {reading!r}")
    table[word] = reading
