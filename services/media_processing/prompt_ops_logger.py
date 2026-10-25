"""PromptOps telemetry logger for FLUX.1 image generation

Provides dual-format logging:
- JSONL (machine-readable): generation_metadata.jsonl
- Markdown (human-readable): prompt_report.md

Designed with fail-safe error handling to never disrupt main workflow.
"""
import json
import logging
from pathlib import Path
from typing import Optional

from rich.console import Console

from core.models.generation_metadata import GenerationMetadata

logger = logging.getLogger(__name__)
console = Console()


class PromptOpsLogger:
    """PromptOps telemetry logger with dual-format output
    
    Logs FLUX.1 image generation metadata in both machine-readable (JSONL)
    and human-readable (Markdown) formats for analysis and review.
    
    All logging operations are fail-safe and will never raise exceptions
    that could disrupt the main video generation workflow.
    """
    
    def __init__(self, output_dir: Path):
        """Initialize PromptOps logger
        
        Args:
            output_dir: Output directory for log files
        """
        self.output_dir = Path(output_dir)
        self.jsonl_path = self.output_dir / "generation_metadata.jsonl"
        self.markdown_path = self.output_dir / "prompt_report.md"
        
        # Ensure output directory exists
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning(f"Failed to create PromptOps output directory (non-fatal): {e}")
    
    def log_generation(self, metadata: GenerationMetadata) -> None:
        """Log generation metadata in dual format (fail-safe)
        
        This method will never raise exceptions. All errors are logged
        as warnings but do not disrupt the main workflow.
        
        Args:
            metadata: Generation metadata to log
        """
        try:
            self._append_jsonl(metadata)
        except Exception as e:
            logger.warning(f"PromptOps JSONL logging failed (non-fatal): {e}")
        
        try:
            self._update_markdown(metadata)
        except Exception as e:
            logger.warning(f"PromptOps Markdown logging failed (non-fatal): {e}")
    
    def _append_jsonl(self, metadata: GenerationMetadata) -> None:
        """Append single JSON line to JSONL file
        
        Args:
            metadata: Generation metadata
        """
        # Convert to dict and serialize to single line
        data = metadata.to_dict()
        json_line = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
        
        # Append to JSONL file (create if not exists)
        with open(self.jsonl_path, 'a', encoding='utf-8') as f:
            f.write(json_line + '\n')
        
        logger.debug(f"Appended metadata to {self.jsonl_path}")
    
    def _update_markdown(self, metadata: GenerationMetadata) -> None:
        """Update Markdown visual catalog with new generation
        
        Args:
            metadata: Generation metadata
        """
        # Read existing content if file exists
        if self.markdown_path.exists():
            with open(self.markdown_path, 'r', encoding='utf-8') as f:
                existing_content = f.read()
        else:
            # Initialize with header
            existing_content = "# FLUX.1 Generation Report\n\n"
        
        # Build new entry
        entry = self._build_markdown_entry(metadata)
        
        # Append new entry
        updated_content = existing_content + entry + "\n---\n\n"
        
        # Write back
        with open(self.markdown_path, 'w', encoding='utf-8') as f:
            f.write(updated_content)
        
        logger.debug(f"Updated Markdown report: {self.markdown_path}")
    
    def _build_markdown_entry(self, metadata: GenerationMetadata) -> str:
        """Build Markdown entry for a single generation
        
        Args:
            metadata: Generation metadata
        
        Returns:
            str: Markdown formatted entry
        """
        # Determine title
        if metadata.context_type == "segment":
            title = f"{metadata.segment_id} ({metadata.segment_type})"
        else:
            title = "thumbnail"
        
        # Build visual identity summary
        if metadata.visual_identity:
            vi = metadata.visual_identity
            primary = vi.get("primary_color", "N/A")
            secondary = vi.get("secondary_color", "N/A")
            aesthetic = vi.get("aesthetic", "N/A")
            vi_summary = f"{primary} + {secondary}, {aesthetic}"
        else:
            vi_summary = "N/A"
        
        # Build entry
        lines = [
            f"## Generation: {title}",
            "",
            f"![{title}]({metadata.image_path})",
            "",
            f"**Timestamp**: {metadata.timestamp}  ",
            f"**Context**: {metadata.context_type}",
        ]
        
        if metadata.segment_id:
            lines.append(f"**Segment**: `{metadata.segment_id}` (type: {metadata.segment_type})")

        # Scene direction from the script's visuals (013); "scene_splitter" when there was none
        scene_visual = getattr(metadata, "scene_visual", None)
        if metadata.context_type == "segment":
            if scene_visual:
                avoid = scene_visual.get("avoid") or []
                lines.extend([
                    f"**Scene source**: visuals (start_turn {scene_visual.get('start_turn')})  ",
                    f"**Focus**: {scene_visual.get('focus', '')}  ",
                    f"**Draw**: {scene_visual.get('draw', '')}  ",
                    f"**Avoid**: {'; '.join(avoid) if avoid else '(none)'}  ",
                ])
            else:
                lines.append("**Scene source**: scene_splitter  ")
        if getattr(metadata, "cached", False):
            lines.append("**Cached**: yes (image reused from output/.image_cache)  ")

        lines.extend([
            f"**Visual Identity**: {vi_summary}  ",
            f"**Seed**: {metadata.seed}  ",
            f"**Generation Time**: {metadata.generation_time_sec:.1f}s  ",
            f"**Resolution**: {metadata.resolution} ({metadata.steps} steps, {metadata.sampler}, CFG={metadata.cfg_scale})",
            "",
            "**Prompt**:",
            "```",
            metadata.prompt,
            "```",
            "",
        ])
        
        return "\n".join(lines)
    
    def initialize_report(self) -> None:
        """Initialize Markdown report with header (optional, called manually)
        
        This method can be called at the start of a workflow to create
        a fresh report with timestamp.
        """
        try:
            header = f"# FLUX.1 Generation Report\n\nGenerated: {metadata.timestamp}\n\n---\n\n"
            with open(self.markdown_path, 'w', encoding='utf-8') as f:
                f.write(header)
            logger.info(f"Initialized PromptOps report: {self.markdown_path}")
        except Exception as e:
            logger.warning(f"Failed to initialize PromptOps report (non-fatal): {e}")
