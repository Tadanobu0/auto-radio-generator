"""YouTube thumbnail background generator using FLUX.1"""
import logging
from pathlib import Path
from typing import Optional

from rich.console import Console

from core.models import AppConfig
from core.models.visual import VisualIdentity, VisualPalette
from services.media_processing.flux_client import FluxClient
from services.script_generation.image_prompt_generator import ImagePromptGenerator
from services.media_processing.prompt_ops_logger import PromptOpsLogger

logger = logging.getLogger(__name__)
console = Console()


class ThumbnailBackgroundGenerator:
    """FLUX.1-based thumbnail background generator
    
    Generates eye-catching background images for YouTube thumbnails
    based on video theme and script content.
    """
    
    def __init__(self, config: AppConfig, output_dir: Optional[Path] = None):
        """Initialize thumbnail background generator
        
        Args:
            config: Application configuration
            output_dir: Optional output directory for PromptOps logging
        """
        self.config = config
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
            logger.info("Thumbnail generator using ComfyUI")
            console.print("[cyan]Thumbnail generator using ComfyUI[/cyan]")
        else:  # forge
            from services.media_processing.flux_client import FluxClient
            self.image_client = FluxClient(config)
            logger.info("Thumbnail generator using FLUX.1 Forge")
            console.print("[cyan]Thumbnail generator using FLUX.1 Forge[/cyan]")
        
        # Legacy support: flux_client attribute for backward compatibility
        self.flux_client = self.image_client
        
        # Initialize PromptOps logger (optional, fail-safe)
        self.logger = None
        if output_dir:
            try:
                self.logger = PromptOpsLogger(output_dir)
            except Exception as e:
                logger.warning(f"Failed to initialize PromptOps logger (non-fatal): {e}")
    
    async def generate(
        self,
        theme: str,
        script_summary: str,
        output_path: Path,
        visual_identity: Optional[VisualIdentity] = None,
        topic_title: Optional[str] = None,
        prompt: Optional[str] = None,
        seed: Optional[int] = None,
    ) -> Path:
        """Generate thumbnail background image

        Args:
            theme: Video theme
            script_summary: Script summary (200-300 chars)
            output_path: Output path for generated image
            visual_identity: Optional visual identity for brand consistency
            topic_title: Optional topic title
            prompt: ready-made prompt (candidates, 021); None = made here as before
            seed: fixed seed (candidates, 021); None = random as before

        Returns:
            Path: Path to generated image
        
        Raises:
            RuntimeError: If generation fails
        """
        # Issue #2, #6 fix: Single parameter for clarity
        identity = visual_identity
        
        console.print("[cyan]Generating YouTube thumbnail background via FLUX.1...[/cyan]")
        console.print(f"[dim]Theme: {theme}[/dim]")
        if identity:
            console.print(f"[dim]Visual Identity: {identity}[/dim]")
        
        # 1. Generate prompt with visual identity
        # Issue #6 fix: Pass only visual_identity
        if prompt is None:
            prompt = await self.build_prompt(theme, script_summary, identity, topic_title)

        console.print(f"[dim]Prompt: {prompt[:80]}...[/dim]")
        
        # Prepare visual identity dict for metadata
        vi_dict = {}
        if identity:
            vi_dict = {
                "primary_color": identity.primary_color,
                "secondary_color": identity.secondary_color,
                "aesthetic": identity.aesthetic,
            }
        
        # 2. Generate image via image client with metadata
        image_path, metadata = await self.image_client.generate_image(
            prompt=prompt,
            output_path=output_path,
            context_type="thumbnail",
            visual_identity=vi_dict,
            **({"seed": seed} if seed is not None else {}),
        )
        
        console.print(f"[green]✓ Thumbnail background generated: {image_path.name}[/green]")
        logger.info(f"Thumbnail background generated: {image_path}")
        
        # Log to PromptOps (fail-safe)
        if self.logger:
            self.logger.log_generation(metadata)
        
        return image_path
    
    async def build_prompt(
        self,
        theme: str,
        script_summary: str,
        visual_identity: Optional[VisualIdentity] = None,
        topic_title: Optional[str] = None,
    ) -> str:
        """The thumbnail background prompt (made once for all candidates, 021)."""
        return await self.prompt_generator.generate_thumbnail_prompt(
            theme=theme,
            script_summary=script_summary,
            topic_title=topic_title,
            visual_identity=visual_identity,
        )

    async def check_availability(self) -> bool:
        """Check if image generation API is available
        
        Returns:
            bool: True if available, False otherwise
        """
        return await self.image_client.check_api_status()
