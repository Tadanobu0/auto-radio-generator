"""Background image provider for segment-based video rendering

Provides background images for each segment, either by:
1. Static mode: Selecting from local assets (assets/backgrounds/)
2. Dynamic mode: Generating via FLUX.1 (Forge or ComfyUI backend)

Static mode uses segment type to select appropriate images:
- intro_*.png for intro segments
- topic_*.png for deep_dive segments
- conclusion_*.png for conclusion segments
"""
import asyncio
import hashlib
import logging
import random
from datetime import datetime
from pathlib import Path
from typing import Optional

from rich.console import Console

from core.models import AppConfig
from core.models.curation import ScriptSegment
from core.models.generation_metadata import GenerationMetadata
from core.models.visual import VisualIdentity, VisualPalette
from services.media_processing.prompt_ops_logger import PromptOpsLogger
from services.media_processing.scene_images import (
    ReuseStrictError,
    SceneImageOptions,
    candidate_path,
    scene_record,
)

logger = logging.getLogger(__name__)
console = Console()


class ImageProvider:
    """Background image provider
    
    Provides background images for each segment based on configuration.
    Supports static (local assets) and dynamic (DALL-E 3) modes.
    """
    
    # Class-level semaphore: Limit concurrent FLUX.1 generations to 1
    # This prevents VRAM exhaustion when multiple segments are processed in parallel
    _generation_semaphore = asyncio.Semaphore(1)
    
    def __init__(self, config: AppConfig, visual_identity: Optional[VisualIdentity] = None, output_dir: Optional[Path] = None):
        """Initialize image provider
        
        Args:
            config: Application configuration
            visual_identity: Optional visual identity for brand consistency
            output_dir: Optional output directory for PromptOps logging
        """
        self.config = config
        # Issue #2, #6 fix: Single parameter for clarity
        self.visual_identity = visual_identity
        
        # Get mode from config (default to static)
        self.mode = config.yaml.video_renderer.background_mode
        
        self.static_images_dir = Path("assets/backgrounds")
        self.cache_dir = Path("output/.image_cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize PromptOps logger (optional, fail-safe)
        self.logger = None
        if output_dir:
            try:
                self.logger = PromptOpsLogger(output_dir)
            except Exception as e:
                logger.warning(f"Failed to initialize PromptOps logger (non-fatal): {e}")
        
        # Track wall-clock time for all image generation
        self._generation_start_time: Optional[float] = None
        self._generation_end_time: Optional[float] = None

        # Fallbacks (generic / short prompt, static image) per scene, for result.json (014)
        self.warnings: list[str] = []

        # Scene list (segment_id, image, prompt, seed, ...) and reuse / regen options (016).
        # scene_options is set by the caller after construction (None = as before).
        self.scene_records: list[dict] = []
        self.scene_options: Optional[SceneImageOptions] = None

        # Scan available static images (always scan for fallback support)
        self._static_images: dict[str, list[Path]] = {}
        self._scan_static_images()
        
        # Initialize dynamic generation components
        if self.mode == "dynamic":
            from services.script_generation.image_prompt_generator import ImagePromptGenerator
            
            self.prompt_generator = ImagePromptGenerator(config)
            
            # Get image provider from config with defensive fallback
            image_provider = getattr(config.yaml, "image_provider", "forge")
            if image_provider not in {"forge", "comfyui"}:
                logger.warning(
                    f"Invalid image_provider '{image_provider}' in config, defaulting to 'forge'. "
                    f"Valid options: forge, comfyui"
                )
                image_provider = "forge"
            
            if image_provider == "comfyui":
                from services.media_processing.comfyui_client import ComfyUIClient
                self.image_client = ComfyUIClient(config)
                logger.info("Dynamic image generation mode enabled (ComfyUI)")
                console.print("[cyan]Dynamic image generation mode enabled (ComfyUI)[/cyan]")
            else:  # forge
                from services.media_processing.flux_client import FluxClient
                self.image_client = FluxClient(config)
                logger.info("Dynamic image generation mode enabled (FLUX.1 Forge)")
                console.print("[cyan]Dynamic image generation mode enabled (FLUX.1 Forge)[/cyan]")
            
            # Legacy support: flux_client attribute for backward compatibility
            self.flux_client = self.image_client
    
    def _scan_static_images(self):
        """Scan assets/backgrounds/ and categorize by segment type"""
        if not self.static_images_dir.exists():
            logger.warning(f"Backgrounds directory not found: {self.static_images_dir}")
            console.print(f"[yellow]⚠ Backgrounds directory not found: {self.static_images_dir}[/yellow]")
            return
        
        # Scan for images by segment type
        segment_types = ["intro", "deep_dive", "conclusion", "default"]
        
        for seg_type in segment_types:
            pattern = f"{seg_type}_*.png"
            images = list(self.static_images_dir.glob(pattern))
            if images:
                self._static_images[seg_type] = images
                logger.info(f"Found {len(images)} images for segment type '{seg_type}'")
        
        # Also scan for generic images (Abstract_*.png, etc.)
        generic_images = [
            p for p in self.static_images_dir.glob("*.png")
            if not any(p.name.startswith(f"{st}_") for st in segment_types)
        ]
        if generic_images:
            self._static_images["generic"] = generic_images
            logger.info(f"Found {len(generic_images)} generic images")
        
        total_images = sum(len(imgs) for imgs in self._static_images.values())
        console.print(f"[dim]Loaded {total_images} background images from {self.static_images_dir}[/dim]")
    
    async def get_image_for_segment(self, segment: ScriptSegment) -> Path:
        """Get background image for a segment
        
        Args:
            segment: Script segment
        
        Returns:
            Path: Background image path
        """
        reused = self._reused_image(segment)
        if reused is not None:
            return reused
        if self.mode == "dynamic":
            try:
                return await self._generate_dynamic_image(segment)
            except Exception as e:
                logger.error(f"Dynamic image generation failed: {e}")
                logger.info("Falling back to static image selection")
                console.print(f"[yellow]⚠ Dynamic generation failed, using static fallback[/yellow]")
                self._take_prompt_warnings()
                self._add_static_fallback_warning(segment, e)
                return self._record_scene(segment, self._select_static_image(segment), None, None, False, "static")
            finally:
                self._take_prompt_warnings()
        else:
            return self._record_scene(segment, self._select_static_image(segment), None, None, False, "static")

    def _record_scene(
        self, segment: ScriptSegment, path: Path, prompt: Optional[str], seed: Optional[int],
        cached: bool, source: str, extra: Optional[dict] = None,
    ) -> Path:
        """Keep the scene in self.scene_records (016) and return path; never fails the image.

        extra (021): candidates / contact_sheet / picked / prompt_override, only when used.
        """
        try:
            if not isinstance(getattr(self, "scene_records", None), list):
                self.scene_records = []
            record = scene_record(segment, path, prompt, seed, cached, source)
            if extra:
                record.update({k: v for k, v in extra.items() if v is not None})
            self.scene_records.append(record)
        except Exception as e:  # the record is a report only
            logger.warning(f"Scene record for {segment.segment_id} failed (non-fatal): {e}")
        return path

    def _reused_image(self, segment: ScriptSegment) -> Optional[Path]:
        """The previous run's image for this scene (reuse_images_from), unless it is to be regenerated."""
        options = getattr(self, "scene_options", None)
        if options is None or options.reuse_dir is None or options.is_regen(segment.segment_id):
            return None
        entry = options.take_reuse(segment.segment_id)
        if entry is None or not entry.get("image_path"):
            if getattr(options, "strict", False):
                # reuse_strict (019): stop here, before any image of this run is generated
                raise ReuseStrictError([segment.segment_id], options.reuse_dir, "render")
            if entry is not None:
                options.warnings.append(
                    f"scene image for {segment.segment_id} in {options.reuse_dir} is missing; generated as usual"
                )
            options.unmatched_ids.append(segment.segment_id)
            return None
        path = Path(entry["image_path"])
        seed = entry.get("seed")
        # candidates of the previous run (021): carried over, and the picked one is the image
        extra = None
        pick = options.pick_for(segment.segment_id) if getattr(options, "picks", None) else None
        if entry.get("candidates") or pick is not None or entry.get("prompt_override"):
            extra = {
                "candidates": entry.get("candidates") or None,
                "contact_sheet": entry.get("contact_sheet"),
                "prompt_override": entry.get("prompt_override"),
            }
        if pick is not None:
            picked = candidate_path(entry, pick)
            if picked:
                path = Path(picked)
                for cand in entry.get("candidates") or []:
                    if isinstance(cand, dict) and cand.get("index") == pick:
                        seed = cand.get("seed")
                extra["picked"] = pick
            else:
                options.warnings.append(
                    f"picked candidate {pick} of {segment.segment_id} has no image in {options.reuse_dir}; "
                    "used the scene image (candidate 1)"
                )
        if getattr(options, "prompt_overrides", None) and segment.segment_id in options.prompt_overrides:
            options.skipped_override_ids.append(segment.segment_id)
        options.reused_ids.append(segment.segment_id)
        logger.info(f"Reusing scene image for {segment.segment_id}: {path}")
        console.print(f"[dim]Reusing image of the previous run: {path.name}[/dim]")
        return self._record_scene(segment, path, entry.get("prompt"), seed, False, "reused", extra)

    def finish_scene_images(self) -> None:
        """Move the reuse summary (reused / unmatched scenes) into self.warnings (016)."""
        options = getattr(self, "scene_options", None)
        if options is not None and options.active:
            self._warning_list().extend(options.finish())

    def _warning_list(self) -> list[str]:
        if not isinstance(getattr(self, "warnings", None), list):
            self.warnings = []
        return self.warnings

    def _take_prompt_warnings(self) -> None:
        """Move the prompt generator's fallback warnings into self.warnings (in order)."""
        generator = getattr(self, "prompt_generator", None)
        pending = getattr(generator, "warnings", None)
        if isinstance(pending, list) and pending:
            self._warning_list().extend(pending)
            pending.clear()

    def _add_static_fallback_warning(self, segment: ScriptSegment, error: Exception) -> None:
        """Keep a static-image fallback in self.warnings (for result.json)."""
        text = " ".join(str(error).split())[:160]
        self._warning_list().append(
            f"background image for {segment.segment_id}: image generation failed "
            f"({type(error).__name__}: {text}); used a static image"
        )

    async def _generate_dynamic_image(self, segment: ScriptSegment) -> Path:
        """Generate background image via FLUX.1 with concurrency control
        
        Uses class-level semaphore to ensure only 1 image is generated at a time,
        preventing VRAM exhaustion from parallel generation requests.
        
        Args:
            segment: Script segment
        
        Returns:
            Path: Generated image path
        """
        import time
        
        # Acquire semaphore lock (wait if another generation is in progress)
        async with self._generation_semaphore:
            console.print(f"[dim]🔒 Acquired generation lock for {segment.segment_id}[/dim]")
            logger.debug(f"Semaphore acquired for segment {segment.segment_id}")
            
            # Track wall-clock time for first generation
            if self._generation_start_time is None:
                self._generation_start_time = time.time()
            
            # 1. Generate prompt from segment with visual identity
            # Issue #6 fix: Pass only visual_identity
            # (a prompt override of the scene (021) replaces it)
            prompt, override = await self._scene_prompt(segment)
            override_extra = {"prompt_override": override} if override is not None else None

            # 2. Check cache (prompt-based key)
            cache_key = self._get_prompt_cache_key(prompt)
            cache_path = self.cache_dir / f"{cache_key}.png"

            # A scene in regen_scenes (016) skips the cache and gets a shifted seed;
            # its file name carries the seed so the cached image is kept as it was
            seed = None
            options = getattr(self, "scene_options", None)
            regen = options is not None and options.is_regen(segment.segment_id)
            previous = options.take_reuse(segment.segment_id) if regen else None
            if options is not None and int(getattr(options, "candidates", 1) or 1) > 1:
                # N candidates with consecutive seeds (021)
                return await self._generate_candidates(segment, prompt, override, regen, previous)
            if regen:
                seed = options.regen_seed(segment.segment_id, previous)
                cache_path = self.cache_dir / f"{cache_key}_seed{seed}.png"
                console.print(f"[cyan]Regenerating {segment.segment_id} with seed {seed}[/cyan]")
                logger.info(f"Regenerating scene {segment.segment_id} (cache skipped, seed {seed})")

            if seed is None and cache_path.exists():
                console.print(f"[dim]Using cached image: {cache_path.name}[/dim]")
                logger.info(f"Cache hit for segment {segment.segment_id}")
                # Record the prompt even on a cache hit, so the run's report lists every scene (013)
                if self.logger:
                    self.logger.log_generation(GenerationMetadata(
                        timestamp=datetime.utcnow().isoformat() + "Z",
                        image_path=str(cache_path),
                        context_type="segment",
                        segment_id=segment.segment_id,
                        segment_type=segment.segment_type,
                        prompt=prompt,
                        visual_identity=self._visual_identity_dict(),
                        scene_visual=self._scene_visual_dict(segment),
                        cached=True,
                    ))
                return self._record_scene(segment, cache_path, prompt, None, True, "cache", override_extra)
            
            # 3. Generate image via FluxClient
            try:
                console.print(f"[cyan]Generating image for {segment.segment_id}...[/cyan]")
                
                # Prepare visual identity dict for metadata
                vi_dict = self._visual_identity_dict()

                # Generate image with metadata (seed only for a regenerated scene: 016)
                seed_kwargs = {"seed": seed} if seed is not None else {}
                image_path, metadata = await self.image_client.generate_image(
                    prompt=prompt,
                    output_path=cache_path,
                    context_type="segment",
                    segment_id=segment.segment_id,
                    segment_type=segment.segment_type,
                    visual_identity=vi_dict,
                    **seed_kwargs,
                )
                
                console.print(f"[green]✓ Generated: {image_path.name} ({metadata.generation_time_sec:.1f}s)[/green]")
                logger.info(f"Image generated for segment {segment.segment_id}: {image_path} ({metadata.generation_time_sec:.1f}s)")
                
                # Log to PromptOps (fail-safe), with the script's scene direction if any (013)
                if self.logger:
                    try:
                        metadata.scene_visual = self._scene_visual_dict(segment)
                    except Exception:
                        pass
                    self.logger.log_generation(metadata)
                
                # Update end time for wall-clock tracking
                self._generation_end_time = time.time()

                return self._record_scene(
                    segment, image_path, prompt, getattr(metadata, "seed", seed), False, "generated", override_extra
                )

            except Exception as e:
                # Track time even on failure
                self._generation_end_time = time.time()

                logger.error(f"Dynamic image generation failed: {e}, falling back to static mode")
                console.print(f"[yellow]⚠ Generation failed, using static fallback[/yellow]")
                self._take_prompt_warnings()
                self._add_static_fallback_warning(segment, e)
                return self._record_scene(
                    segment, self._select_static_image(segment), prompt, None, False, "static", override_extra
                )

    async def _scene_prompt(self, segment: ScriptSegment) -> tuple[str, Optional[dict]]:
        """The scene's image prompt and, when a prompt override (021) was used, what it was.

        A text override is the prompt (the fixed no-people prefix is put in front unless it is
        there already). A focus / draw / avoid override replaces the scene direction for the
        prompt generator only (the script itself is not changed).
        """
        options = getattr(self, "scene_options", None)
        override = None
        if options is not None and getattr(options, "prompt_overrides", None):
            override = options.override_for(segment.segment_id)
        if override is None:
            prompt = await self.prompt_generator.generate_prompt(segment, visual_identity=self.visual_identity)
            return prompt, None
        if isinstance(override, str):
            from services.script_generation.image_prompt_generator import ImagePromptGenerator

            prefix = ImagePromptGenerator.SEGMENT_PROMPT_PREFIX
            prompt = override if override.startswith(prefix) else prefix + override
            console.print(f"[cyan]Prompt override for {segment.segment_id}[/cyan]")
            logger.info(f"Prompt override for {segment.segment_id}: {override[:100]}")
            return prompt, {"kind": "prompt", "text": override}

        from core.models.curation import SceneVisual

        current = getattr(segment, "scene_visual", None)
        base = current.model_dump() if current is not None else {"start_turn": 0, "focus": "", "draw": "", "avoid": []}
        base.update(override)
        try:
            visual = SceneVisual(**base)
            changed = segment.model_copy(update={"scene_visual": visual})
        except Exception as e:
            self._warning_list().append(
                f"prompt override for {segment.segment_id} was not usable ({type(e).__name__}: "
                f"{' '.join(str(e).split())[:160]}); made the prompt from the script as usual"
            )
            prompt = await self.prompt_generator.generate_prompt(segment, visual_identity=self.visual_identity)
            return prompt, None
        logger.info(f"Scene direction override for {segment.segment_id}: {override}")
        prompt = await self.prompt_generator.generate_prompt(changed, visual_identity=self.visual_identity)
        recorded = visual.model_dump()
        recorded.pop("start_turn", None)
        return prompt, {"kind": "scene_visual", "scene_visual": recorded}

    async def _generate_candidates(
        self, segment: ScriptSegment, prompt: str, override: Optional[dict], regen: bool,
        previous: Optional[dict],
    ) -> Path:
        """N images of one scene with consecutive seeds (021); called under the generation lock.

        Each candidate is cached as <prompt hash>_seed<seed>.png, so the same prompt and seed
        is not drawn twice (except for a scene in regen_scenes, which is always drawn).
        The scene image is the first candidate that has an image; if none has, a static image
        is used as when a single image fails.
        """
        import time

        options = self.scene_options
        seeds = options.candidate_seeds(segment.segment_id, previous, regen)
        cache_key = self._get_prompt_cache_key(prompt)
        vi_dict = self._visual_identity_dict()
        candidates: list[dict] = []
        console.print(
            f"[cyan]Generating {len(seeds)} candidates for {segment.segment_id} "
            f"(seeds {seeds[0]}..{seeds[-1]})[/cyan]"
        )
        for index, seed in enumerate(seeds, start=1):
            cand = {"index": index, "image_path": None, "seed": seed, "cached": False, "generation_time_sec": 0.0}
            cache_path = self.cache_dir / f"{cache_key}_seed{seed}.png"
            if not regen and cache_path.exists():
                cand.update(image_path=str(cache_path), cached=True)
                candidates.append(cand)
                continue
            start = time.time()
            try:
                image_path, metadata = await self.image_client.generate_image(
                    prompt=prompt,
                    output_path=cache_path,
                    context_type="segment",
                    segment_id=segment.segment_id,
                    segment_type=segment.segment_type,
                    visual_identity=vi_dict,
                    seed=seed,
                )
                elapsed = time.time() - start
                used_seed = getattr(metadata, "seed", seed)
                cand.update(
                    image_path=str(Path(image_path)),
                    seed=used_seed if isinstance(used_seed, int) and not isinstance(used_seed, bool)
                    and used_seed >= 0 else seed,
                    generation_time_sec=round(elapsed, 3),
                )
                console.print(f"[green]✓ {segment.segment_id} candidate {index}: {elapsed:.1f}s[/green]")
                if self.logger:
                    try:
                        metadata.scene_visual = self._scene_visual_dict(segment)
                        self.logger.log_generation(metadata)
                    except Exception as log_error:
                        logger.warning(f"PromptOps log failed (non-fatal): {log_error}")
            except Exception as e:
                cand["generation_time_sec"] = round(time.time() - start, 3)
                cand["error"] = f"{type(e).__name__}: {' '.join(str(e).split())[:160]}"
                logger.error(f"Candidate {index} of {segment.segment_id} failed: {e}")
                console.print(f"[yellow]⚠ {segment.segment_id} candidate {index} failed[/yellow]")
            self._generation_end_time = time.time()
            candidates.append(cand)

        extra = {"candidates": candidates, "prompt_override": override}
        usable = [c for c in candidates if c["image_path"]]
        if not usable:
            self._take_prompt_warnings()
            self._add_static_fallback_warning(
                segment, RuntimeError(f"all {len(candidates)} candidates failed ({candidates[0].get('error')})")
            )
            return self._record_scene(segment, self._select_static_image(segment), prompt, None, False, "static", extra)
        failed = [c["index"] for c in candidates if not c["image_path"]]
        if failed:
            self._warning_list().append(
                f"{len(failed)} of {len(candidates)} candidates of {segment.segment_id} failed "
                f"(candidate {', '.join(map(str, failed))}); the others are kept"
            )
        first = usable[0]
        source = "cache" if all(c["cached"] for c in usable) else "generated"
        return self._record_scene(
            segment, Path(first["image_path"]), prompt, first["seed"], first["cached"], source, extra
        )

    def _visual_identity_dict(self) -> dict:
        """Colors and aesthetic of the visual identity, for the prompt records."""
        if not self.visual_identity:
            return {}
        return {
            "primary_color": self.visual_identity.primary_color,
            "secondary_color": self.visual_identity.secondary_color,
            "aesthetic": self.visual_identity.aesthetic,
        }

    @staticmethod
    def _scene_visual_dict(segment: ScriptSegment) -> Optional[dict]:
        """The script's SceneVisual of this scene as a dict (None if the scene has none)."""
        visual = getattr(segment, "scene_visual", None)
        return visual.model_dump() if visual is not None else None

    def _select_static_image(self, segment: ScriptSegment) -> Path:
        """Select background image from local assets
        
        Selection strategy:
        1. Try segment type specific images (intro_*.png, etc.)
        2. Fall back to generic images
        3. Use deterministic selection based on segment_id hash
        
        Args:
            segment: Script segment
        
        Returns:
            Path: Selected image path
        
        Raises:
            FileNotFoundError: If no background images are available
        """
        # Map segment type to image category
        segment_type = segment.segment_type
        
        # Try segment-type specific images first
        candidates = self._static_images.get(segment_type, [])
        
        # Fall back to generic images if no type-specific images found
        if not candidates:
            candidates = self._static_images.get("generic", [])
        
        # Fall back to any available images
        if not candidates:
            all_images = []
            for images in self._static_images.values():
                all_images.extend(images)
            candidates = all_images
        
        if not candidates:
            raise FileNotFoundError(
                f"No background images found in {self.static_images_dir}. "
                f"Please add images with naming pattern: {segment_type}_*.png or default_*.png"
            )
        
        # Deterministic selection based on segment_id hash
        # This ensures the same segment always gets the same image
        segment_hash = int(hashlib.md5(segment.segment_id.encode()).hexdigest(), 16)
        selected = candidates[segment_hash % len(candidates)]
        
        logger.debug(f"Selected image for segment '{segment.segment_id}': {selected.name}")
        return selected
    
    def _get_prompt_cache_key(self, prompt: str) -> str:
        """Generate cache key from prompt hash
        
        Uses SHA-256 full hash to prevent collision risks.
        
        Args:
            prompt: Image generation prompt
        
        Returns:
            str: Cache key (64-char hex, SHA-256)
        """
        return hashlib.sha256(prompt.encode()).hexdigest()
    
    def get_total_generation_time(self) -> float:
        """Get total wall-clock time for all image generation
        
        Returns actual elapsed time (wall-clock), not sum of individual generation times.
        This is more accurate for parallel/async operations.
        
        Returns:
            float: Total generation time in seconds (0.0 if no generation occurred)
        """
        if self._generation_start_time is None or self._generation_end_time is None:
            return 0.0
        return self._generation_end_time - self._generation_start_time
