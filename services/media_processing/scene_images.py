"""Scene images: images-only run, regenerating chosen scenes, reusing a previous run's images (016).

Flow on the Mac side (radio_director): run with images_only=True, look at the pictures,
run images_only again with reuse_images_from=<that run> and regen_scenes=[...] for the
bad ones, and finally run the full render with reuse_images_from=<the last images run>.

Hand-over (same in both jobs):
- An images-only run copies every scene image to <run dir>/scene_images/NN_<segment_id>.png
  and writes <run dir>/scene_images.json (the list below, image paths as on this machine).
- result.json gets "scene_images": a list of
  {segment_id, scene_index, image_path, scene_visual, prompt, seed, cached, source}
  scene_index is 0-based within the chapter (the N of "_sceneN" minus 1; 0 for a one-scene chapter).
  source is "generated" / "cache" / "reused" / "static".
- reuse_images_from=<run dir> reads that run's scene_images.json and matches by segment_id.
"""
import hashlib
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

SCENE_IMAGES_DIR = "scene_images"
SCENE_IMAGES_MANIFEST = "scene_images.json"
DEFAULT_REGEN_SEED_OFFSET = 1

# Speech-rate guess for cutting scenes without audio (images-only run, chapters without visuals)
CHARS_PER_SEC_AT_SPEED_1 = 8.0

_SCENE_SUFFIX = re.compile(r"_scene(\d+)$")
_CITATION_TAG = re.compile(r"\[src=[\d,\s]+\]")


def scene_index_of(segment_id: str) -> int:
    """0-based scene index within the chapter, from the "_sceneN" suffix (0 if there is none)."""
    match = _SCENE_SUFFIX.search(segment_id or "")
    return int(match.group(1)) - 1 if match else 0


def base_seed_for(segment_id: str) -> int:
    """A fixed seed for a scene whose earlier seed is unknown (0 .. 2**31-1)."""
    return int(hashlib.sha256((segment_id or "").encode("utf-8")).hexdigest()[:8], 16) % (2 ** 31)


@dataclass
class SceneImageOptions:
    """What the run does with scene images; the defaults change nothing."""

    regen_scenes: set[str] = field(default_factory=set)
    regen_seed_offset: int = DEFAULT_REGEN_SEED_OFFSET
    reuse_dir: Optional[Path] = None
    # segment_id -> entries of the previous run, in order (duplicate ids are matched in order)
    reuse_entries: dict[str, list[dict]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    reused_ids: list[str] = field(default_factory=list)
    unmatched_ids: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return bool(self.regen_scenes or self.reuse_dir)

    def is_regen(self, segment_id: str) -> bool:
        return segment_id in self.regen_scenes

    def take_reuse(self, segment_id: str) -> Optional[dict]:
        """The previous run's entry for this scene (consumed), or None."""
        entries = self.reuse_entries.get(segment_id)
        if not entries:
            return None
        return entries.pop(0)

    def regen_seed(self, segment_id: str, previous: Optional[dict]) -> int:
        """Seed for a regenerated scene: the previous seed (or a fixed one) plus the offset."""
        seed = previous.get("seed") if previous else None
        if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
            seed = base_seed_for(segment_id)
        return seed + int(self.regen_seed_offset)

    def finish(self) -> list[str]:
        """Summary warnings once all scenes are done (reused / unmatched / left over)."""
        out = list(self.warnings)
        if self.reuse_dir is not None:
            out.append(
                f"reused {len(self.reused_ids)} scene image(s) from {self.reuse_dir}"
                + (f": {', '.join(self.reused_ids)}" if self.reused_ids else "")
            )
            if self.unmatched_ids:
                out.append(
                    f"no reusable image in {self.reuse_dir} for {len(self.unmatched_ids)} scene(s), "
                    f"generated as usual: {', '.join(self.unmatched_ids)}"
                )
            left = [sid for sid, entries in self.reuse_entries.items() for _ in entries]
            if left:
                out.append(
                    f"{len(left)} scene image(s) of {self.reuse_dir} matched no scene of this run: {', '.join(left)}"
                )
        self.warnings.clear()
        self.reused_ids.clear()
        self.unmatched_ids.clear()
        self.reuse_entries.clear()
        return out


def build_scene_image_options(
    regen_scenes: Optional[Iterable[str]] = None,
    regen_seed_offset: Optional[int] = None,
    reuse_images_from: Optional[str | Path] = None,
) -> SceneImageOptions:
    """Options from run_workflow_sync's arguments. A missing or broken manifest is a ValueError."""
    options = SceneImageOptions(
        regen_scenes={str(s).strip() for s in (regen_scenes or []) if str(s).strip()},
        regen_seed_offset=DEFAULT_REGEN_SEED_OFFSET if regen_seed_offset is None else int(regen_seed_offset),
    )
    if reuse_images_from:
        run_dir = Path(reuse_images_from)
        options.reuse_dir = run_dir
        for entry in load_scene_images_manifest(run_dir):
            options.reuse_entries.setdefault(entry["segment_id"], []).append(entry)
    return options


def load_scene_images_manifest(run_dir: Path) -> list[dict]:
    """Entries of <run_dir>/scene_images.json; image_path is resolved to an existing file.

    A path written on this machine is used as is; if it is gone, the same file name under
    <run_dir>/scene_images/ is tried (the run directory may have been moved).
    """
    manifest = Path(run_dir) / SCENE_IMAGES_MANIFEST
    if not manifest.is_file():
        raise ValueError(f"reuse_images_from: {manifest} not found (not an images_only run directory?)")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(f"reuse_images_from: cannot read {manifest} ({type(e).__name__}: {e})") from e
    entries = data.get("scene_images") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ValueError(f"reuse_images_from: {manifest} has no scene_images list")
    out = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("segment_id"):
            continue
        entry = dict(entry)
        entry["image_path"] = _resolve_image(Path(run_dir), entry.get("image_path"))
        out.append(entry)
    return out


def _resolve_image(run_dir: Path, image_path: Any) -> Optional[str]:
    if not image_path:
        return None
    path = Path(str(image_path))
    if path.is_file():
        return str(path)
    moved = run_dir / SCENE_IMAGES_DIR / path.name
    return str(moved) if moved.is_file() else None


def scene_record(
    segment,
    image_path: Path,
    prompt: Optional[str],
    seed: Optional[int],
    cached: bool,
    source: str,
) -> dict:
    """One entry of the scene list (image_path absolute)."""
    visual = getattr(segment, "scene_visual", None)
    return {
        "segment_id": segment.segment_id,
        "scene_index": scene_index_of(segment.segment_id),
        "image_path": str(Path(image_path).resolve()),
        "scene_visual": visual.model_dump() if visual is not None else None,
        "prompt": prompt,
        "seed": seed if isinstance(seed, int) and not isinstance(seed, bool) and seed >= 0 else None,
        "cached": bool(cached),
        "source": source,
    }


def export_scene_images(records: list[dict], run_dir: Path) -> list[dict]:
    """Copy the images to <run_dir>/scene_images/ and write <run_dir>/scene_images.json.

    Returns the records with image_path pointing at the copies (absolute paths).
    """
    run_dir = Path(run_dir)
    target_dir = run_dir / SCENE_IMAGES_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    exported = []
    for order, record in enumerate(records, start=1):
        record = dict(record)
        source = Path(record["image_path"])
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", record["segment_id"])
        target = target_dir / f"{order:02d}_{safe_id}{source.suffix or '.png'}"
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        record["image_path"] = str(target.resolve())
        exported.append(record)
    (run_dir / SCENE_IMAGES_MANIFEST).write_text(
        json.dumps({"scene_images": exported}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return exported


def estimate_turn_times(
    turns: list[dict], speed_scale: float = 1.0, pause_sec: float = 0.5
) -> tuple[list[float], float]:
    """Guess each turn's start and the end of the last one (seconds from the chapter start).

    From the text length only; for cutting scenes without audio. Chapters whose script
    gives visuals do not depend on it (their cuts are the visuals' start_turns).
    """
    rate = CHARS_PER_SEC_AT_SPEED_1 * (speed_scale if speed_scale and speed_scale > 0 else 1.0)
    starts, t, end = [], 0.0, 0.0
    for turn in turns:
        starts.append(t)
        text = _CITATION_TAG.sub("", (turn or {}).get("text") or "")
        end = t + len(text.strip()) / rate
        t = end + pause_sec
    return starts, end
