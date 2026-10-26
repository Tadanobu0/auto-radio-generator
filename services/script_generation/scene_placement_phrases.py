"""Positive placement phrases that code adds to the short scene prompt (016).

The short prompt (ImagePromptGenerator._short_scene_prompt) is the last resort for a scene
with a SceneVisual. In the humidifier run (radio_director-022) all four pictures whose
placement was the opposite of the advice (right under the TV, a low stand within a child's
reach, by the window, at the bedside) came from this prompt: the LLM sentence had dropped
"avoid" and the placement words.

So the code adds them itself, after the LLM sentence:
- each "avoid" item as its opposite state, stated positively
  (窓際に置かない -> away from the window, toward the middle of the room)
- the height word of "draw" (腰の高さの棚 -> at waist height)
- a distance in "draw" (壁から30cm離す -> with about 30 centimeters of open space around it)
A phrase is skipped when the sentence already says it.
"""
import re
from typing import Optional

from services.script_generation.scene_placement_check import draw_height_parts

HIGH_PLACE = "up on a high shelf, well above the floor"
# When draw gives the height itself (腰の高さ), "high shelf" would contradict it
RAISED_ONLY = "raised up off the floor"

# (Japanese cues in an avoid item, positive English phrase, English cues meaning "already said")
AVOID_OPPOSITES: list[tuple[tuple[str, ...], str, tuple[str, ...]]] = [
    (("窓",), "away from the window, toward the middle of the room",
     ("away from the window", "middle of the room", "center of the room", "centre of the room")),
    (("テレビ", "家電", "パソコン", "電子レンジ", "電子機器", "オーディオ"),
     "on its own stand with open space around it, apart from other appliances",
     ("apart from other appliances", "away from other appliances")),
    (("手が届", "低い台", "低い棚", "低い位置", "低い場所"), HIGH_PLACE,
     ("high shelf", "above the floor")),
    (("床", "直置き"), "raised up off the floor", ("off the floor", "above the floor")),
    (("枕元", "ベッド", "布団", "寝具"), "a few steps away from the bed, across the room",
     ("away from the bed", "across the room")),
    (("壁際", "壁に近", "壁にぴったり", "壁に密着", "壁に付け", "壁につけ"),
     "a little away from the wall, with open space behind it",
     ("away from the wall", "space behind")),
    (("カーテン", "家具", "本棚", "物陰", "隙間"), "with clear open space around it",
     ("clear open space", "open space around")),
    (("エアコン", "風が直接", "吹き出し"), "out of the direct airflow", ("airflow",)),
    (("直射日光", "日なた", "日差し", "西日"), "in soft shade, out of direct sunlight",
     ("shade", "out of direct sunlight")),
    (("部屋の隅", "隅に"), "toward the middle of the room",
     ("middle of the room", "center of the room", "centre of the room")),
]

_DISTANCE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(cm|ｃｍ|センチメートル|センチ|m|ｍ|メートル)\s*"
    r"(?:以上|ほど|くらい|ぐらい|程度|前後)?\s*(?:は)?\s*(?:離|あけ|空け|あい|空い)"
)
_UNITS = {"cm": "centimeters", "ｃｍ": "centimeters", "センチ": "centimeters", "センチメートル": "centimeters",
          "m": "meters", "ｍ": "meters", "メートル": "meters"}


def _already_said(sentence: str, cues: tuple[str, ...]) -> bool:
    lower = sentence.lower()
    return any(cue in lower for cue in cues)


def avoid_opposite_phrases(avoid: Optional[list[str]], sentence: str = "", draw: str = "") -> list[str]:
    """Positive opposite of each avoid item (in table order), minus those the sentence already has."""
    text = " ".join(item for item in (avoid or []) if item)
    draw_gives_height = bool(draw_height_parts(draw))
    phrases: list[str] = []
    for cues, phrase, said in AVOID_OPPOSITES:
        if not any(cue in text for cue in cues):
            continue
        if phrase == HIGH_PLACE and draw_gives_height:
            phrase, said = RAISED_ONLY, ("off the floor", "above the floor")
        if phrase not in phrases and not _already_said(sentence, said):
            phrases.append(phrase)
    return phrases


def draw_placement_phrases(draw: str, sentence: str = "") -> list[str]:
    """Height and distance words of draw, in English, that the sentence does not have yet."""
    lower = sentence.lower()
    phrases = []
    for part in draw_height_parts(draw):
        if not re.search(rf"\b{part}[- ](?:height|high|level)\b", lower):
            phrases.append(f"at {part} height")
    for number, unit in _DISTANCE_RE.findall(draw):
        if re.search(rf"(?<![\d.]){re.escape(number)}(?![\d.])", lower):
            continue
        english = _UNITS[unit]
        if number in ("1", "1.0"):
            english = english[:-1]
        phrase = f"with about {number} {english} of open space around it"
        if phrase not in phrases:
            phrases.append(phrase)
    return phrases


def short_prompt_placement_phrases(draw: str, avoid: Optional[list[str]], sentence: str) -> list[str]:
    """All phrases to add after the short prompt's sentence: draw's placement, then avoid's opposites."""
    phrases = draw_placement_phrases(draw, sentence)
    for phrase in avoid_opposite_phrases(avoid, sentence, draw):
        if phrase not in phrases:
            phrases.append(phrase)
    return phrases
