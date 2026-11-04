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

Candidates (021). Only with candidates >= 2 / picks / prompt_overrides; the defaults add nothing.
- candidates=N (images_only): every generated scene gets N images, seed = start + 0 .. N-1
  (start = base_seed_for(segment_id); for a scene in regen_scenes the highest seed of the
  previous run's candidates + regen_seed_offset). Copies go to scene_images/NN_<id>_cK.png,
  a contact sheet to scene_images/NN_<id>_sheet.jpg. The entry gets
  "candidates": [{index, image_path, seed, cached, generation_time_sec[, error]}] and
  "contact_sheet"; image_path stays the scene image (candidate 1 unless picked).
- picks={segment_id: K, "thumbnail": K} (with reuse_images_from): the reused scene takes the
  previous run's candidate K; the entry gets "picked": K. No pick = candidate 1 = as before.
- prompt_overrides={segment_id: text}: a generated scene uses "text" as the image prompt
  (the fixed no-people prefix is put in front); a dict {focus/draw/avoid} replaces the
  scene direction and the prompt is made from it as usual. The entry gets "prompt_override".
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
# Candidates per scene (021): 1 = as before
DEFAULT_CANDIDATES = 1
MAX_CANDIDATES = 16
THUMBNAIL_KEY = "thumbnail"  # key for the thumbnail background in picks / prompt_overrides / regen_scenes
THUMBNAIL_CANDIDATES_DIR = "thumbnail_bg_candidates"
CONTACT_SHEET_SUFFIX = "_sheet.jpg"

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


class ReuseStrictError(RuntimeError):
    """reuse_strict (019): a scene has no reusable image, so the run stops instead of generating one.

    stage is "before_synthesis" (the check before VOICEVOX) or "render" (a scene id that only
    showed up with the real audio timing; no image has been generated at that point either).
    """

    def __init__(self, missing_ids: list[str], reuse_dir: Optional[Path], stage: str):
        self.missing_ids = list(missing_ids)
        self.reuse_dir = reuse_dir
        self.stage = stage
        super().__init__(
            f"reuse_strict: no reusable image in {reuse_dir} for {len(self.missing_ids)} scene(s)"
            f" ({stage}): {', '.join(self.missing_ids)}"
        )


@dataclass
class SceneImageOptions:
    """What the run does with scene images; the defaults change nothing."""

    regen_scenes: set[str] = field(default_factory=set)
    regen_seed_offset: int = DEFAULT_REGEN_SEED_OFFSET
    reuse_dir: Optional[Path] = None
    # reuse_strict (019): a scene that is not in regen_scenes and has no reusable image
    # fails the run (ReuseStrictError) instead of being generated
    strict: bool = False
    # segment_id -> entries of the previous run, in order (duplicate ids are matched in order)
    reuse_entries: dict[str, list[dict]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    reused_ids: list[str] = field(default_factory=list)
    unmatched_ids: list[str] = field(default_factory=list)
    # Candidates (021): images per generated scene; picks: segment_id / "thumbnail" -> 1-based
    # candidate number; prompt_overrides: segment_id / "thumbnail" -> prompt text (or a dict
    # of focus / draw / avoid for a scene)
    candidates: int = DEFAULT_CANDIDATES
    picks: dict[str, int] = field(default_factory=dict)
    prompt_overrides: dict[str, Any] = field(default_factory=dict)
    # thumbnail background of the previous run (scene_images.json "thumbnail_bg"), or None
    reuse_thumbnail: Optional[dict] = None
    used_pick_ids: set[str] = field(default_factory=set)
    used_override_ids: set[str] = field(default_factory=set)
    skipped_override_ids: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return bool(
            self.regen_scenes or self.reuse_dir or self.candidates > 1 or self.picks or self.prompt_overrides
        )

    def is_regen(self, segment_id: str) -> bool:
        return segment_id in self.regen_scenes

    def take_reuse(self, segment_id: str) -> Optional[dict]:
        """The previous run's entry for this scene (consumed), or None."""
        entries = self.reuse_entries.get(segment_id)
        if not entries:
            return None
        return entries.pop(0)

    def pick_for(self, segment_id: str) -> Optional[int]:
        """The picked candidate number of this scene (021), or None."""
        pick = self.picks.get(segment_id)
        if pick is not None:
            self.used_pick_ids.add(segment_id)
        return pick

    def override_for(self, segment_id: str) -> Any:
        """The prompt override of this scene (021), or None."""
        override = self.prompt_overrides.get(segment_id)
        if override is not None:
            self.used_override_ids.add(segment_id)
        return override

    def candidate_seeds(self, segment_id: str, previous: Optional[dict], regen: bool) -> list[int]:
        """Seeds of the N candidates (021), consecutive from a start seed.

        A regenerated scene starts after the highest seed the previous run used for it
        (its candidates, or its seed) plus regen_seed_offset, so no earlier picture comes back.
        Otherwise the start is the fixed seed of the segment_id (the same seeds every run).
        """
        start = self.regen_seed(segment_id, previous) if regen else base_seed_for(segment_id)
        return [start + k for k in range(max(1, int(self.candidates)))]

    def missing_for(self, segment_ids: Iterable[str]) -> list[str]:
        """Scenes (in order) that would not get a reusable image; nothing is consumed (019).

        Scenes in regen_scenes are not missing (they are generated on purpose).
        A duplicate id needs as many usable entries as it appears.
        """
        taken: dict[str, int] = {}
        missing = []
        for sid in segment_ids:
            if self.is_regen(sid):
                continue
            entries = self.reuse_entries.get(sid) or []
            k = taken.get(sid, 0)
            taken[sid] = k + 1
            # the k-th appearance takes the k-th entry, as take_reuse does
            if k >= len(entries) or not entries[k].get("image_path"):
                missing.append(sid)
        return missing

    def regen_seed(self, segment_id: str, previous: Optional[dict]) -> int:
        """Seed for a regenerated scene: the previous seed (or a fixed one) plus the offset."""
        seed = previous.get("seed") if previous else None
        # with candidates (021) the highest of them, so a regenerated scene gets new seeds
        cand_seeds = [
            c.get("seed") for c in ((previous or {}).get("candidates") or []) if isinstance(c, dict)
        ]
        cand_seeds = [s for s in cand_seeds if _valid_seed(s)]
        if cand_seeds:
            seed = max(cand_seeds + ([seed] if _valid_seed(seed) else []))
        if not _valid_seed(seed):
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
        # picks / prompt overrides (021) that did nothing
        unused_picks = [sid for sid in self.picks if sid != THUMBNAIL_KEY and sid not in self.used_pick_ids]
        if unused_picks:
            out.append(f"picks matched no reused scene of this run: {', '.join(unused_picks)}")
        if self.skipped_override_ids:
            out.append(
                "prompt override not used because the scene image was reused "
                f"(add the scene to regen_scenes to make it from the new prompt): {', '.join(self.skipped_override_ids)}"
            )
        unused_overrides = [
            sid for sid in self.prompt_overrides
            if sid != THUMBNAIL_KEY and sid not in self.used_override_ids and sid not in self.skipped_override_ids
        ]
        if unused_overrides:
            out.append(f"prompt overrides matched no generated scene of this run: {', '.join(unused_overrides)}")
        self.used_pick_ids.clear()
        self.used_override_ids.clear()
        self.skipped_override_ids.clear()
        self.warnings.clear()
        self.reused_ids.clear()
        self.unmatched_ids.clear()
        self.reuse_entries.clear()
        return out


def build_scene_image_options(
    regen_scenes: Optional[Iterable[str]] = None,
    regen_seed_offset: Optional[int] = None,
    reuse_images_from: Optional[str | Path] = None,
    reuse_strict: bool = False,
    candidates: Optional[int] = None,
    picks: Any = None,
    prompt_overrides: Any = None,
) -> SceneImageOptions:
    """Options from run_workflow_sync's arguments. A missing or broken manifest is a ValueError.

    reuse_strict (019) needs reuse_images_from (ValueError otherwise).
    candidates / picks / prompt_overrides (021): see parse_candidates / parse_picks /
    parse_prompt_overrides. picks need reuse_images_from, and a picked number must exist
    among that run's candidates of the scene (ValueError otherwise).
    """
    if reuse_strict and not reuse_images_from:
        raise ValueError("reuse_strict: reuse_images_from is required (no previous run to take images from)")
    pick_map = parse_picks(picks)
    if pick_map and not reuse_images_from:
        raise ValueError("picks: reuse_images_from is required (the candidates to pick from are in that run)")
    options = SceneImageOptions(
        regen_scenes={str(s).strip() for s in (regen_scenes or []) if str(s).strip()},
        regen_seed_offset=DEFAULT_REGEN_SEED_OFFSET if regen_seed_offset is None else int(regen_seed_offset),
        strict=bool(reuse_strict),
        candidates=parse_candidates(candidates),
        picks=pick_map,
        prompt_overrides=parse_prompt_overrides(prompt_overrides),
    )
    if reuse_images_from:
        run_dir = Path(reuse_images_from)
        options.reuse_dir = run_dir
        for entry in load_scene_images_manifest(run_dir):
            options.reuse_entries.setdefault(entry["segment_id"], []).append(entry)
        options.reuse_thumbnail = load_thumbnail_manifest(run_dir)
        _check_picks(options)
    return options


def _valid_seed(seed: Any) -> bool:
    return isinstance(seed, int) and not isinstance(seed, bool) and seed >= 0


def parse_candidates(candidates: Any) -> int:
    """Candidates per scene (021): None = 1; an int 1 .. MAX_CANDIDATES (ValueError otherwise)."""
    if candidates is None:
        return DEFAULT_CANDIDATES
    if isinstance(candidates, bool):
        raise ValueError(f"candidates: must be an integer 1-{MAX_CANDIDATES}, got {candidates!r}")
    try:
        value = int(str(candidates).strip())
    except (TypeError, ValueError):
        raise ValueError(f"candidates: must be an integer 1-{MAX_CANDIDATES}, got {candidates!r}") from None
    if not 1 <= value <= MAX_CANDIDATES:
        raise ValueError(f"candidates: must be an integer 1-{MAX_CANDIDATES}, got {value}")
    return value


def _load_json_arg(value: Any, name: str) -> Any:
    """A dict as is, or the JSON in the file at that path (str / Path)."""
    if value is None or isinstance(value, dict):
        return value
    if isinstance(value, (str, Path)):
        path = Path(value)
        if not path.is_file():
            raise ValueError(f"{name}: {path} not found")
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as e:
            raise ValueError(f"{name}: cannot read {path} ({type(e).__name__}: {e})") from e
    raise ValueError(f"{name}: expected a dict or the path of a JSON file, got {type(value).__name__}")


def parse_picks(picks: Any) -> dict[str, int]:
    """Picked candidates (021): {segment_id: K, "thumbnail": K} (K from 1), or a JSON file of it.

    The file may also be {"picks": {...}}. Empty / None = no picks.
    """
    data = _load_json_arg(picks, "picks")
    if data is None:
        return {}
    if isinstance(data, dict) and isinstance(data.get("picks"), dict):
        data = data["picks"]
    if not isinstance(data, dict):
        raise ValueError("picks: expected an object {segment_id: candidate number}")
    out: dict[str, int] = {}
    for key, value in data.items():
        sid = str(key).strip()
        if not sid:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError(f"picks: candidate number of {sid} must be an integer from 1, got {value!r}")
        try:
            number = int(str(value).strip())
        except ValueError:
            raise ValueError(f"picks: candidate number of {sid} must be an integer from 1, got {value!r}") from None
        if number < 1:
            raise ValueError(f"picks: candidate number of {sid} must be an integer from 1, got {number}")
        out[sid] = number
    return out


_OVERRIDE_FIELDS = ("focus", "draw", "avoid")


def parse_prompt_overrides(overrides: Any) -> dict[str, Any]:
    """Prompt overrides (021): {segment_id: "prompt text" | {focus, draw, avoid}}, or a JSON file of it.

    The file may also be {"prompt_overrides": {...}}. The thumbnail ("thumbnail") takes text only.
    """
    data = _load_json_arg(overrides, "prompt_overrides")
    if data is None:
        return {}
    if isinstance(data, dict) and isinstance(data.get("prompt_overrides"), dict):
        data = data["prompt_overrides"]
    if not isinstance(data, dict):
        raise ValueError("prompt_overrides: expected an object {segment_id: prompt}")
    out: dict[str, Any] = {}
    for key, value in data.items():
        sid = str(key).strip()
        if not sid:
            continue
        if isinstance(value, str):
            if not value.strip():
                raise ValueError(f"prompt_overrides: the prompt of {sid} is empty")
            out[sid] = value.strip()
            continue
        if isinstance(value, dict) and sid != THUMBNAIL_KEY:
            unknown = sorted(set(value) - set(_OVERRIDE_FIELDS))
            if unknown or not value:
                raise ValueError(
                    f"prompt_overrides: {sid} takes a prompt text or an object of {'/'.join(_OVERRIDE_FIELDS)}"
                    + (f" (unknown: {', '.join(unknown)})" if unknown else "")
                )
            for name in ("focus", "draw"):
                if name in value and not (isinstance(value[name], str) and value[name].strip()):
                    raise ValueError(f"prompt_overrides: {sid}.{name} must be a non-empty string")
            avoid = value.get("avoid")
            if avoid is not None and not (
                isinstance(avoid, str) or (isinstance(avoid, list) and all(isinstance(a, str) for a in avoid))
            ):
                raise ValueError(f"prompt_overrides: {sid}.avoid must be a string or a list of strings")
            out[sid] = dict(value)
            continue
        raise ValueError(
            f"prompt_overrides: {sid} takes a prompt text"
            + ("" if sid == THUMBNAIL_KEY else f" or an object of {'/'.join(_OVERRIDE_FIELDS)}")
        )
    return out


def _check_picks(options: SceneImageOptions) -> None:
    """Each pick of a scene in the previous run must name one of its candidates (ValueError)."""
    for sid, number in options.picks.items():
        if sid == THUMBNAIL_KEY:
            pick_thumbnail_path(options, number)  # raises when the candidate is not there
            continue
        entries = options.reuse_entries.get(sid)
        if not entries:
            continue  # no such scene in that run: a warning at the end (finish)
        for entry in entries:
            if candidate_path(entry, number) is None:
                raise ValueError(
                    f"picks: {sid} has no candidate {number} with an image in {options.reuse_dir}"
                    f" ({len(entry.get('candidates') or []) or 1} candidate(s))"
                )


def candidate_path(entry: dict, number: int) -> Optional[str]:
    """Image of candidate `number` (from 1) of a manifest entry; an entry without candidates has only 1."""
    candidates = entry.get("candidates") or []
    if not candidates:
        return entry.get("image_path") if number == 1 else None
    for cand in candidates:
        if isinstance(cand, dict) and cand.get("index") == number:
            return cand.get("image_path") or None
    return None


def pick_thumbnail_path(options: SceneImageOptions, number: int) -> Path:
    """The previous run's thumbnail background candidate `number` (ValueError if it is not there).

    A run without thumbnail candidates has only candidate 1 (its thumbnail_bg.png).
    """
    run_dir = options.reuse_dir
    info = options.reuse_thumbnail
    path = candidate_path(info, number) if info else None
    if path is None and not info and number == 1 and run_dir is not None:
        plain = Path(run_dir) / "thumbnail_bg.png"
        path = str(plain) if plain.is_file() else None
    if path is None:
        raise ValueError(f"picks: no thumbnail background candidate {number} in {run_dir}")
    return Path(path)


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
        if entry.get("candidates"):  # 021
            entry["candidates"] = _resolve_candidates(Path(run_dir), entry["candidates"], SCENE_IMAGES_DIR)
        out.append(entry)
    return out


def load_thumbnail_manifest(run_dir: Path) -> Optional[dict]:
    """The "thumbnail_bg" part of <run_dir>/scene_images.json (021), paths resolved; None if absent."""
    manifest = Path(run_dir) / SCENE_IMAGES_MANIFEST
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return None
    info = data.get("thumbnail_bg") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        return None
    info = dict(info)
    info["image_path"] = _resolve_image(Path(run_dir), info.get("image_path"), "")
    info["candidates"] = _resolve_candidates(Path(run_dir), info.get("candidates") or [], THUMBNAIL_CANDIDATES_DIR)
    return info


def _resolve_candidates(run_dir: Path, candidates: Any, subdir: str) -> list[dict]:
    out = []
    for cand in candidates if isinstance(candidates, list) else []:
        if isinstance(cand, dict):
            cand = dict(cand)
            cand["image_path"] = _resolve_image(run_dir, cand.get("image_path"), subdir)
            out.append(cand)
    return out


def _resolve_image(run_dir: Path, image_path: Any, subdir: str = SCENE_IMAGES_DIR) -> Optional[str]:
    if not image_path:
        return None
    path = Path(str(image_path))
    if path.is_file():
        return str(path)
    moved = (run_dir / subdir / path.name) if subdir else (run_dir / path.name)
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


def export_scene_images(
    records: list[dict], run_dir: Path, warnings: Optional[list[str]] = None,
) -> list[dict]:
    """Copy the images to <run_dir>/scene_images/ and write <run_dir>/scene_images.json.

    Returns the records with image_path pointing at the copies (absolute paths).
    A record with candidates (021) also gets its candidates copied to NN_<id>_cK.png and a
    contact sheet NN_<id>_sheet.jpg ("contact_sheet"; None if it could not be made, with a
    line in `warnings`).
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
        if record.get("candidates"):
            record["candidates"], record["contact_sheet"] = export_candidates(
                record["candidates"], target_dir, f"{order:02d}_{safe_id}", record["segment_id"], warnings,
            )
        exported.append(record)
    (run_dir / SCENE_IMAGES_MANIFEST).write_text(
        json.dumps({"scene_images": exported}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return exported


def export_candidates(
    candidates: list[dict], target_dir: Path, stem: str, label: str, warnings: Optional[list[str]] = None,
) -> tuple[list[dict], Optional[str]]:
    """Copy candidates to <target_dir>/<stem>_cK.png and make <stem>_sheet.jpg (021).

    Returns (candidates with image_path at the copies, contact sheet path or None).
    A candidate without an image (failed) keeps image_path None and gets an empty tile.
    """
    from services.media_processing.contact_sheet import make_contact_sheet

    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for cand in candidates:
        cand = dict(cand)
        if cand.get("image_path"):
            source = Path(cand["image_path"])
            target = target_dir / f"{stem}_c{cand['index']}{source.suffix or '.png'}"
            try:
                if source.resolve() != target.resolve():
                    shutil.copy2(source, target)
                cand["image_path"] = str(target.resolve())
            except Exception as e:
                cand["image_path"] = None
                cand.setdefault("error", f"copy failed ({type(e).__name__}: {e})")
        out.append(cand)
    sheet_path = target_dir / f"{stem}{CONTACT_SHEET_SUFFIX}"
    try:
        make_contact_sheet([(c["index"], c.get("image_path")) for c in out], sheet_path)
        sheet = str(sheet_path.resolve())
    except Exception as e:
        sheet = None
        message = f"contact sheet for {label} could not be made ({type(e).__name__}: {e}); the candidates are kept"
        logger.warning(message)
        if warnings is not None:
            warnings.append(message)
    return out, sheet


def add_thumbnail_to_manifest(run_dir: Path, info: dict) -> None:
    """Put the thumbnail background candidates into <run_dir>/scene_images.json as "thumbnail_bg" (021)."""
    manifest = Path(run_dir) / SCENE_IMAGES_MANIFEST
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        data = {"scene_images": []}
    if not isinstance(data, dict):
        data = {"scene_images": data if isinstance(data, list) else []}
    data["thumbnail_bg"] = info
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


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
