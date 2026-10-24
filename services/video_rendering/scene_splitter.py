"""Split a chapter (script segment) into scenes for background images.

A chapter used to show one image for 2-5 minutes. Now it is cut into scenes of
about `target_sec` each (1-`max_scenes` per chapter); every scene gets its own
image whose prompt comes from that scene's turns.

Cut rule (006 report, plan 1 + 2):
1. n = round(chapter length / target_sec), clamped to 1..max_scenes
2. each cut goes to the turn whose start is nearest to the evenly spaced position
3. if a turn within +-`snap_turns` turns first cites a new key claim source
   ([src=N] with N in key_claim_source_idxs), the cut moves there (nearest one)

Cuts always land on a turn start, never inside a turn.
"""
import math
import re
from typing import Optional, Sequence

_CITATION_TAG = re.compile(r"\[src=([\d,\s]+)\]")


def scene_count(duration_sec: float, target_sec: float = 90.0, max_scenes: int = 4) -> int:
    """Number of scenes for a chapter of duration_sec (1..max_scenes)."""
    if target_sec <= 0 or duration_sec <= 0:
        return 1
    n = math.floor(duration_sec / target_sec + 0.5)
    return max(1, min(max(1, max_scenes), n))


def key_claim_first_turns(turns: Sequence[dict], source_idxs: Sequence[int]) -> set[int]:
    """Turn indices where a key claim source is cited for the first time in the chapter."""
    wanted = set(source_idxs)
    seen: set[int] = set()
    firsts: set[int] = set()
    for i, turn in enumerate(turns):
        cited = {
            int(n)
            for tag in _CITATION_TAG.findall(turn.get("text") or "")
            for n in tag.split(",")
            if n.strip()
        }
        new = (cited & wanted) - seen
        if new:
            firsts.add(i)
            seen |= new
    return firsts


def split_scene_starts(
    turn_start_secs: Sequence[float],
    end_sec: float,
    turns: Optional[Sequence[dict]] = None,
    key_claim_source_idxs: Sequence[int] = (),
    target_sec: float = 90.0,
    max_scenes: int = 4,
    snap_turns: int = 2,
) -> list[int]:
    """First turn index of each scene (always starts with 0).

    Args:
        turn_start_secs: Start time of each turn of the chapter (ascending)
        end_sec: End of the chapter's speech (end of its last turn)
        turns: The chapter's turns (for [src=N] snapping); None = no snapping
        key_claim_source_idxs: The chapter's key claim sources
        target_sec: About how long one scene should be
        max_scenes: Upper limit of scenes per chapter
        snap_turns: How far (in turns) a cut may move to a new key claim
    """
    num_turns = len(turn_start_secs)
    if num_turns <= 1:
        return [0]
    start_sec = turn_start_secs[0]
    n = scene_count(end_sec - start_sec, target_sec, max_scenes)
    if n <= 1:
        return [0]

    anchors = key_claim_first_turns(turns, key_claim_source_idxs) if turns else set()
    anchors.discard(0)

    starts = [0]
    span = end_sec - start_sec
    for k in range(1, n):
        target = start_sec + span * k / n
        # Nearest turn start (turn 0 is the chapter start, never a cut)
        cut = min(range(1, num_turns), key=lambda i: (abs(turn_start_secs[i] - target), i))
        nearby = [a for a in anchors if abs(a - cut) <= snap_turns and a > starts[-1]]
        if nearby:
            cut = min(nearby, key=lambda a: (abs(turn_start_secs[a] - target), a))
        if cut > starts[-1]:
            starts.append(cut)
    return starts
