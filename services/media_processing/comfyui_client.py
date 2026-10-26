"""ComfyUI image generation client for async API (polling-based)"""
import asyncio
import copy
import json
import logging
import random
import tempfile
from pathlib import Path
from typing import Optional, Tuple

import httpx
from rich.console import Console

from core.models import AppConfig
from core.models.generation_metadata import GenerationMetadata

logger = logging.getLogger(__name__)
console = Console()


class ComfyUIClient:
    """FLUX.1 [schnell] image generation via ComfyUI async API
    
    Connects to local ComfyUI instance and generates cinematic background images
    using FLUX.1 [schnell] model with polling-based async API.
    """
    
    # Workflow node IDs (configurable for different workflow structures)
    NODE_IDS = {
        "ksampler": "3",
        "checkpoint": "4",
        "empty_latent": "5",
        "clip_text_pos": "6",
        "clip_text_neg": "7",
        "vae_decode": "8",
        "save_image": "9",
    }
    
    def __init__(self, config: AppConfig):
        """Initialize ComfyUI client
        
        Args:
            config: Application configuration
        """
        self.config = config
        comfyui_config = getattr(config.yaml, "comfyui", None)
        
        if comfyui_config:
            self.base_url = comfyui_config.base_url
            self.workflow_path = comfyui_config.workflow_path
            self.timeout = comfyui_config.timeout
            self.steps = comfyui_config.steps
            self.width = comfyui_config.width
            self.height = comfyui_config.height
            self.cfg = comfyui_config.cfg
            self.sampler_name = comfyui_config.sampler_name
            self.scheduler = comfyui_config.scheduler
        else:
            # Fallback defaults
            self.base_url = "http://127.0.0.1:8188"
            self.workflow_path = "config/workflow_api.json"
            self.timeout = 600
            self.steps = 4
            self.width = 768
            self.height = 432
            self.cfg = 1.0
            self.sampler_name = "euler"
            self.scheduler = "normal"
        
        # Convert workflow_path to absolute path (relative to project root)
        workflow_path = Path(self.workflow_path)
        if not workflow_path.is_absolute():
            # Resolve relative to project root (services/media_processing/ -> project root)
            project_root = Path(__file__).parent.parent.parent
            workflow_path = project_root / workflow_path
        
        # Validate workflow path exists
        if not workflow_path.exists():
            raise FileNotFoundError(
                f"Workflow file not found: {workflow_path}. "
                f"Ensure config/workflow_api.json exists in the project root."
            )
        
        self.workflow_path = str(workflow_path)
        
        # Load workflow JSON
        self.workflow = self._load_workflow()
        
        logger.info(f"ComfyUIClient initialized: {self.base_url}")
    
    def _load_workflow(self) -> dict:
        """Load workflow JSON from file
        
        Returns:
            dict: Workflow dictionary
        """
        workflow_path = Path(self.workflow_path)
        if not workflow_path.exists():
            raise FileNotFoundError(f"Workflow file not found: {workflow_path}")
        
        with open(workflow_path, 'r', encoding='utf-8') as f:
            workflow = json.load(f)
        
        logger.info(f"Workflow loaded from {workflow_path}")
        return workflow
    
    async def check_api_status(self) -> bool:
        """Check if ComfyUI API is available
        
        Returns:
            bool: True if API is accessible, False otherwise
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.base_url}/system_stats")
                if response.status_code == 200:
                    console.print("[green]✓ ComfyUI API connected[/green]")
                    return True
        except Exception as e:
            logger.warning(f"ComfyUI API not available: {e}")
            console.print(f"[yellow]⚠ ComfyUI API not available: {e}[/yellow]")
        return False
    
    async def generate_image(
        self,
        prompt: str,
        output_path: Path,
        context_type: str = "segment",
        segment_id: str = None,
        segment_type: str = None,
        visual_identity: dict = None,
        seed: Optional[int] = None,
    ) -> Tuple[Path, GenerationMetadata]:
        """Generate image via ComfyUI async API and return metadata

        seed: fixed seed (a regenerated scene, 016); None = random as before.
        
        Args:
            prompt: English prompt for image generation
            output_path: Path to save generated image
            context_type: Context type ("segment" or "thumbnail")
            segment_id: Optional segment identifier
            segment_type: Optional segment type
            visual_identity: Optional visual identity dict
        
        Returns:
            Tuple[Path, GenerationMetadata]: Generated image path and metadata
        
        Raises:
            RuntimeError: If image generation fails
        """
        # Clone workflow and modify parameters
        workflow = copy.deepcopy(self.workflow)
        
        # Modify workflow parameters using node ID constants with validation
        try:
            workflow[self.NODE_IDS["clip_text_pos"]]["inputs"]["text"] = prompt  # Prompt
            workflow[self.NODE_IDS["empty_latent"]]["inputs"]["width"] = self.width
            workflow[self.NODE_IDS["empty_latent"]]["inputs"]["height"] = self.height
            workflow[self.NODE_IDS["ksampler"]]["inputs"]["seed"] = (
                random.randint(1, 10**15) if seed is None else int(seed)
            )
            workflow[self.NODE_IDS["ksampler"]]["inputs"]["steps"] = self.steps
            workflow[self.NODE_IDS["ksampler"]]["inputs"]["cfg"] = self.cfg
            workflow[self.NODE_IDS["ksampler"]]["inputs"]["sampler_name"] = self.sampler_name
            workflow[self.NODE_IDS["ksampler"]]["inputs"]["scheduler"] = self.scheduler
        except KeyError as e:
            raise RuntimeError(
                f"Workflow structure mismatch: {e}. "
                f"Ensure workflow_api.json node IDs match NODE_IDS constant: {self.NODE_IDS}"
            ) from e
        
        logger.info(f"Generating image via ComfyUI: {self.width}x{self.height}, {self.steps} steps")
        console.print(f"[cyan]Generating image via ComfyUI...[/cyan]")
        console.print(f"[dim]Prompt: {prompt[:80]}...[/dim]")
        
        import time
        gen_start = time.time()
        
        # Configure timeout
        timeout_config = httpx.Timeout(
            connect=10.0,
            read=self.timeout,
            write=10.0,
            pool=5.0
        )
        
        try:
            async with httpx.AsyncClient(timeout=timeout_config) as client:
                # Step 1: POST /prompt to submit workflow
                prompt_payload = {"prompt": workflow}
                response = await client.post(
                    f"{self.base_url}/prompt",
                    json=prompt_payload
                )
                response.raise_for_status()
                result = response.json()
                prompt_id = result.get("prompt_id")
                
                if not prompt_id:
                    raise RuntimeError("No prompt_id returned from ComfyUI")
                
                logger.info(f"Prompt submitted: {prompt_id}")
                
                # Step 2: Poll /history/{prompt_id} until completion
                max_polls = int(self.timeout / 2)  # 2-second intervals
                for poll_count in range(max_polls):
                    await asyncio.sleep(2)  # Wait 2 seconds between polls
                    
                    history_response = await client.get(
                        f"{self.base_url}/history/{prompt_id}"
                    )
                    history_response.raise_for_status()
                    history = history_response.json()
                    
                    if prompt_id in history:
                        # Check if execution is complete
                        history_data = history[prompt_id]
                        status = history_data.get("status", {})
                        
                        if status.get("completed", False):
                            logger.info(f"Generation completed after {poll_count * 2} seconds")
                            break
                        elif status.get("str", "") == "execution error":
                            raise RuntimeError(f"ComfyUI execution error: {history_data}")
                    else:
                        # History not yet available
                        pass
                else:
                    raise RuntimeError(
                        f"ComfyUI generation timeout after {self.timeout}s. "
                        f"The prompt was submitted but did not complete within the timeout period."
                    )
                
                # Step 3: Extract output filename from history
                history_data = history[prompt_id]
                outputs = history_data.get("outputs", {})
                
                save_image_node = self.NODE_IDS["save_image"]
                if save_image_node not in outputs:
                    raise RuntimeError(f"No output from node {save_image_node} (SaveImage)")
                
                save_image_outputs = outputs[save_image_node]
                images = save_image_outputs.get("images", [])
                
                if not images:
                    raise RuntimeError(f"No images in node {save_image_node} output")
                
                filename = images[0].get("filename")
                if not filename:
                    raise RuntimeError(f"No filename in node {save_image_node} output")
                
                logger.info(f"Output filename: {filename}")
                
                # Step 4: Download image from /view endpoint
                view_response = await client.get(
                    f"{self.base_url}/view",
                    params={"filename": filename, "subfolder": "", "type": "output"}
                )
                view_response.raise_for_status()
                image_data = view_response.content
                
                # Save to output path atomically
                output_path.parent.mkdir(parents=True, exist_ok=True)
                
                with tempfile.NamedTemporaryFile(
                    mode='wb',
                    dir=output_path.parent,
                    delete=False,
                    suffix='.tmp'
                ) as tmp_file:
                    tmp_file.write(image_data)
                    tmp_path = Path(tmp_file.name)
                
                # Atomic rename (moves the file, no cleanup needed)
                tmp_path.replace(output_path)
                
                gen_time = time.time() - gen_start
                
                logger.info(f"Image saved: {output_path}")
                console.print(f"[green]✓ Image generated: {output_path.name}[/green]")
                
                # Extract seed from workflow (randomly generated)
                actual_seed = workflow[self.NODE_IDS["ksampler"]]["inputs"]["seed"]
                
                # Build metadata
                resolution = f"{self.width}x{self.height}"
                metadata = self._build_metadata(
                    image_path=str(output_path),
                    context_type=context_type,
                    segment_id=segment_id,
                    segment_type=segment_type,
                    prompt=prompt,
                    visual_identity=visual_identity or {},
                    seed=actual_seed,
                    generation_time_sec=gen_time,
                    resolution=resolution,
                )
                
                return output_path, metadata
                
        except httpx.TimeoutException as e:
            error_msg = f"ComfyUI API timeout after {self.timeout}s"
            logger.error(error_msg)
            console.print(f"[red]✗ {error_msg}[/red]")
            raise RuntimeError(error_msg) from e
        except httpx.HTTPStatusError as e:
            error_msg = f"ComfyUI API error: {e.response.status_code}"
            logger.error(error_msg)
            console.print(f"[red]✗ {error_msg}[/red]")
            raise RuntimeError(error_msg) from e
        except Exception as e:
            error_msg = f"Image generation failed: {e}"
            logger.error(error_msg)
            console.print(f"[red]✗ {error_msg}[/red]")
            raise RuntimeError(error_msg) from e
    
    def _build_metadata(
        self,
        image_path: str,
        context_type: str,
        segment_id: str,
        segment_type: str,
        prompt: str,
        visual_identity: dict,
        seed: int,
        generation_time_sec: float,
        resolution: str = None,
    ) -> GenerationMetadata:
        """Build GenerationMetadata from generation parameters
        
        Args:
            image_path: Path to generated image
            context_type: Context type
            segment_id: Segment identifier
            segment_type: Segment type
            prompt: Full prompt
            visual_identity: Visual identity dict
            seed: Actual seed used
            generation_time_sec: Generation time
            resolution: Actual resolution used (optional)
        
        Returns:
            GenerationMetadata: Metadata instance
        """
        if resolution is None:
            resolution = f"{self.width}x{self.height}"
        
        if context_type == "segment":
            return GenerationMetadata.create_from_segment(
                image_path=image_path,
                segment_id=segment_id,
                segment_type=segment_type,
                prompt=prompt,
                visual_identity=visual_identity,
                seed=seed,
                generation_time_sec=generation_time_sec,
                resolution=resolution,
                steps=self.steps,
                sampler="comfyui",
                cfg_scale=self.cfg,
            )
        else:
            return GenerationMetadata.create_from_thumbnail(
                image_path=image_path,
                prompt=prompt,
                visual_identity=visual_identity,
                seed=seed,
                generation_time_sec=generation_time_sec,
                resolution=resolution,
                steps=self.steps,
                sampler="comfyui",
                cfg_scale=self.cfg,
            )
