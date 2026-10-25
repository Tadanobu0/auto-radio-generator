"""Generation metadata model for PromptOps telemetry

Captures all relevant information about a FLUX.1 image generation for:
- Machine-readable analysis (JSONL format)
- Human-readable review (Markdown catalog)
"""
from dataclasses import dataclass, asdict
from typing import Optional
from datetime import datetime


@dataclass
class GenerationMetadata:
    """Metadata for a single FLUX.1 image generation
    
    This data model captures the complete context of an image generation,
    including prompt, visual identity, actual seed used, and performance metrics.
    """
    
    # Temporal information
    timestamp: str  # ISO 8601 format (e.g., "2026-03-31T20:30:15Z")
    
    # Output information
    image_path: str  # Relative path from project root
    
    # Context information
    context_type: str  # "segment" | "thumbnail"
    segment_id: Optional[str] = None  # e.g., "intro", "deep_dive_1"
    segment_type: Optional[str] = None  # e.g., "intro", "deep_dive", "conclusion"
    
    # Generation parameters
    prompt: str = ""  # Full prompt sent to FLUX.1
    visual_identity: dict = None  # {primary_color, secondary_color, aesthetic, ...}
    seed: int = -1  # Actual seed used (extracted from API response)
    
    # Performance metrics
    generation_time_sec: float = 0.0  # Wall-clock time for this generation
    
    # Technical parameters
    resolution: str = ""  # e.g., "1024x576"
    steps: int = 0  # Inference steps
    sampler: str = ""  # Sampler name
    cfg_scale: float = 1.0  # CFG scale

    # Scene direction from the script (SceneVisual as a dict: start_turn / focus / draw / avoid).
    # None when the scene was cut by scene_splitter (no visuals). 013
    scene_visual: Optional[dict] = None
    # True when the image came from output/.image_cache (no generation in this run). 013
    cached: bool = False

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization
        
        Returns:
            dict: Dictionary representation
        """
        return asdict(self)
    
    @classmethod
    def create_from_segment(
        cls,
        image_path: str,
        segment_id: str,
        segment_type: str,
        prompt: str,
        visual_identity: dict,
        seed: int,
        generation_time_sec: float,
        resolution: str,
        steps: int,
        sampler: str,
        cfg_scale: float,
    ) -> "GenerationMetadata":
        """Factory method for segment-based generation
        
        Args:
            image_path: Relative path to generated image
            segment_id: Segment identifier
            segment_type: Segment type
            prompt: Full prompt
            visual_identity: Visual identity dict
            seed: Actual seed used
            generation_time_sec: Generation time
            resolution: Image resolution
            steps: Inference steps
            sampler: Sampler name
            cfg_scale: CFG scale
        
        Returns:
            GenerationMetadata: Metadata instance
        """
        return cls(
            timestamp=datetime.utcnow().isoformat() + "Z",
            image_path=image_path,
            context_type="segment",
            segment_id=segment_id,
            segment_type=segment_type,
            prompt=prompt,
            visual_identity=visual_identity,
            seed=seed,
            generation_time_sec=generation_time_sec,
            resolution=resolution,
            steps=steps,
            sampler=sampler,
            cfg_scale=cfg_scale,
        )
    
    @classmethod
    def create_from_thumbnail(
        cls,
        image_path: str,
        prompt: str,
        visual_identity: dict,
        seed: int,
        generation_time_sec: float,
        resolution: str,
        steps: int,
        sampler: str,
        cfg_scale: float,
    ) -> "GenerationMetadata":
        """Factory method for thumbnail generation
        
        Args:
            image_path: Relative path to generated image
            prompt: Full prompt
            visual_identity: Visual identity dict
            seed: Actual seed used
            generation_time_sec: Generation time
            resolution: Image resolution
            steps: Inference steps
            sampler: Sampler name
            cfg_scale: CFG scale
        
        Returns:
            GenerationMetadata: Metadata instance
        """
        return cls(
            timestamp=datetime.utcnow().isoformat() + "Z",
            image_path=image_path,
            context_type="thumbnail",
            segment_id=None,
            segment_type=None,
            prompt=prompt,
            visual_identity=visual_identity,
            seed=seed,
            generation_time_sec=generation_time_sec,
            resolution=resolution,
            steps=steps,
            sampler=sampler,
            cfg_scale=cfg_scale,
        )
