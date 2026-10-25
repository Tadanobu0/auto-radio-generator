"""Mechanical check that an image prompt keeps the placement given by a scene's "draw" (014).

The script writer's SceneVisual.draw is Japanese; the image prompt is English. Two
mistakes were seen in the Wi-Fi run (radio_director-019):
- a height word of the place became a size word of the object
  (腰の高さの棚の上のルーター -> "one waist-high white router on a low shelf")
- an object that the script wants off the floor ended up low or on the floor

find_placement_problem() only flags those two patterns. It never requires a word to be
present, so a prompt that simply leaves the height out still passes.
"""
import re
from typing import Optional

# Height words of the place in "draw" -> the English body part that names that height
HEIGHT_WORDS = {
    "腰の高さ": "waist",
    "腰くらいの高さ": "waist",
    "腰ほどの高さ": "waist",
    "腰高": "waist",
    "胸の高さ": "chest",
    "胸くらいの高さ": "chest",
    "目の高さ": "eye",
    "目線の高さ": "eye",
    "肩の高さ": "shoulder",
}

# Other words in "draw" that keep the object up off the floor
ELEVATED_WORDS = ("高い位置", "高い棚", "高めの", "高い台", "床から離", "壁掛け", "壁に掛", "天井")

# Words that, in "draw" or "avoid", mean "not on the floor" (so 床 itself is not a request to draw a floor)
FLOOR_NEGATIONS = ("床から離", "床に置かない", "床に直置きしない", "床置きしない", "床に直接置かない")

# The furniture or spot that a height word should describe
FURNITURE = (
    "shelf|shelves|table|stand|cabinet|counter|countertop|desk|sideboard|bookcase|bookshelf|"
    "dresser|console|rack|ledge|chest of drawers|cupboard|unit|plinth|pedestal|bench|bracket|mount|surface"
)

_LOW_PLACEMENT = [
    re.compile(
        r"\b(?:on|onto|near|at|against|along|across|upon)\s+(?:the\s+|a\s+)?"
        r"(?:bare\s+|wooden\s+|wood\s+|tiled\s+|carpeted\s+|concrete\s+|stone\s+|polished\s+)?"
        r"(?:floor|ground|floorboards)\b"
    ),
    re.compile(r"\bfloor[- ](?:level|standing)\b"),
    re.compile(
        r"\blow(?:[- ](?:slung|lying|rise))?\s+(?:[a-z-]+\s+){0,2}?"
        r"(?:shelf|shelves|table|stand|stool|platform|cabinet|bench|base|plinth|pedestal|riser|ledge|"
        r"sideboard|console|chest|dresser|rack|cupboard|unit|bookcase|bookshelf|step|box|crate)\b"
    ),
]

_FURNITURE_RE = re.compile(rf"\b(?:{FURNITURE})\b")


def draw_height_parts(draw: str) -> list[str]:
    """English body parts for the height words in draw (e.g. ["waist"])."""
    parts = []
    for word, part in HEIGHT_WORDS.items():
        if word in draw and part not in parts:
            parts.append(part)
    return parts


def wants_off_floor(draw: str, avoid: Optional[list[str]] = None) -> bool:
    """True if draw (or avoid) keeps the object off the floor and draw does not itself put something on it."""
    avoid_text = " ".join(avoid or [])
    if "床" in draw and not any(neg in draw for neg in FLOOR_NEGATIONS):
        return False  # draw itself asks for something on the floor
    if draw_height_parts(draw) or any(word in draw for word in ELEVATED_WORDS):
        return True
    return "床" in avoid_text or "低い" in avoid_text


def find_placement_problem(draw: str, avoid: Optional[list[str]], prompt_body: str) -> Optional[str]:
    """A short English reason if the prompt breaks the placement in draw, else None."""
    text = prompt_body.lower()
    if wants_off_floor(draw, avoid):
        for pattern in _LOW_PLACEMENT:
            match = pattern.search(text)
            if match:
                return f"draw keeps it up off the floor, but the prompt says '{match.group(0)}'"
    for part in draw_height_parts(draw):
        for match in re.finditer(rf"\b{part}[- ](?:high|height|level)\b", text):
            before = text[:match.start()].rstrip()
            if before.endswith(" at") or before == "at":
                continue  # "at waist height"
            following = " ".join(text[match.end():].split()[:4])
            if _FURNITURE_RE.search(following):
                continue  # "waist-height wooden shelf"
            return (
                f"draw's height word ({part} height) describes the place, but the prompt uses "
                f"'{match.group(0)}' for the object"
            )
    return None
