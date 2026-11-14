"""ComfyUI image generation client for async API (polling-based)"""
import asyncio
import copy
import json
import logging
import math
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
    
    # Workflow node IDs (configurable for different workflow structures).
    # Kept for compatibility; when an ID does not fit (missing node or another class_type),
    # the node is found by class_type and by the KSampler's links (022).
    NODE_IDS = {
        "ksampler": "3",
        "checkpoint": "4",
        "empty_latent": "5",
        "clip_text_pos": "6",
        "clip_text_neg": "7",
        "vae_decode": "8",
        "save_image": "9",
    }

    # class_types accepted for each role that is written to (022)
    NODE_CLASSES = {
        "ksampler": ("KSampler",),
        "empty_latent": ("EmptyLatentImage", "EmptySD3LatentImage"),
        "clip_text_pos": ("CLIPTextEncode",),
        "clip_text_neg": ("CLIPTextEncode",),
        "save_image": ("SaveImage",),
    }
    # KSampler input that links to the role
    _KSAMPLER_LINKS = {
        "empty_latent": "latent_image",
        "clip_text_pos": "positive",
        "clip_text_neg": "negative",
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
            self.poll_interval_sec = getattr(comfyui_config, "poll_interval_sec", 2.0)
            self.node_ids = dict(self.NODE_IDS)
            self.node_ids.update(getattr(comfyui_config, "node_ids", None) or {})
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
            self.poll_interval_sec = 2.0
            self.node_ids = dict(self.NODE_IDS)
        try:
            self.poll_interval_sec = float(self.poll_interval_sec)
        except (TypeError, ValueError):
            self.poll_interval_sec = 2.0
        if not self.poll_interval_sec > 0:
            self.poll_interval_sec = 2.0

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

    @staticmethod
    def _error_text(history_data: dict) -> str:
        """The execution_error message of a failed run, short (022); the whole entry if there is none."""
        messages = ((history_data or {}).get("status") or {}).get("messages") or []
        for message in messages:
            if isinstance(message, (list, tuple)) and len(message) == 2 and message[0] == "execution_error":
                info = message[1] if isinstance(message[1], dict) else {}
                return (
                    f"node {info.get('node_id')} ({info.get('node_type')}): "
                    f"{info.get('exception_type', '')} {info.get('exception_message', '')}".strip()
                )
        return str(history_data)[:500]

    def cache_identity(self) -> str:
        """What makes an image of this backend differ for the same prompt and seed (022).

        Used in the image cache key so images of another server or size are not mixed in.
        """
        return f"comfyui|{str(self.base_url).rstrip('/')}|{self.width}x{self.height}"

    def resolve_nodes(self, workflow: dict) -> dict:
        """Node ID of each role written to (022): ksampler / empty_latent / clip_text_pos /
        clip_text_neg / save_image.

        The fixed ID (NODE_IDS, or comfyui.node_ids) is used when that node has a fitting
        class_type. Otherwise the KSampler is the first KSampler node, the prompts and the
        latent image are found through the KSampler's links, and SaveImage by class_type.
        Raises RuntimeError when a role cannot be found (the caller falls back to a static image).
        """
        def fits(node_id, role) -> bool:
            node = workflow.get(str(node_id)) if node_id is not None else None
            return isinstance(node, dict) and node.get("class_type") in self.NODE_CLASSES[role]

        def first_of(role):
            for node_id, node in workflow.items():
                if isinstance(node, dict) and node.get("class_type") in self.NODE_CLASSES[role]:
                    return str(node_id)
            return None

        found = {}
        ksampler = self.node_ids.get("ksampler")
        found["ksampler"] = str(ksampler) if fits(ksampler, "ksampler") else first_of("ksampler")
        if found["ksampler"] is None:
            raise RuntimeError(f"Workflow structure mismatch: no KSampler node (node IDs {self.node_ids})")
        inputs = workflow[found["ksampler"]].get("inputs") or {}
        for role, link_name in self._KSAMPLER_LINKS.items():
            link = inputs.get(link_name)
            linked = str(link[0]) if isinstance(link, (list, tuple)) and link else None
            fixed = self.node_ids.get(role)
            if fits(linked, role):
                found[role] = linked
            elif fits(fixed, role):
                found[role] = str(fixed)
            else:
                raise RuntimeError(
                    f"Workflow structure mismatch: no {'/'.join(self.NODE_CLASSES[role])} node for "
                    f"KSampler.{link_name} (node IDs {self.node_ids})"
                )
        if found["clip_text_pos"] == found["clip_text_neg"]:
            raise RuntimeError("Workflow structure mismatch: positive and negative prompts are the same node")
        save = self.node_ids.get("save_image")
        found["save_image"] = str(save) if fits(save, "save_image") else first_of("save_image")
        if found["save_image"] is None:
            raise RuntimeError(f"Workflow structure mismatch: no SaveImage node (node IDs {self.node_ids})")
        return found

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
        
        # Modify workflow parameters (nodes by fixed ID, or by class_type and links: 022)
        nodes = self.resolve_nodes(workflow)
        try:
            workflow[nodes["clip_text_pos"]]["inputs"]["text"] = prompt  # Prompt
            workflow[nodes["empty_latent"]]["inputs"]["width"] = self.width
            workflow[nodes["empty_latent"]]["inputs"]["height"] = self.height
            ksampler_inputs = workflow[nodes["ksampler"]]["inputs"]
            ksampler_inputs["seed"] = random.randint(1, 10**15) if seed is None else int(seed)
            ksampler_inputs["steps"] = self.steps
            ksampler_inputs["cfg"] = self.cfg
            ksampler_inputs["sampler_name"] = self.sampler_name
            ksampler_inputs["scheduler"] = self.scheduler
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
                # Sender name for the shared b70 ComfyUI queue / history (and b70-gate): 026
                prompt_payload.setdefault("extra_data", {})["sender"] = "radio"
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
                # (interval: comfyui.poll_interval_sec, 2 seconds by default; 022)
                interval = self.poll_interval_sec
                max_polls = max(1, math.ceil(self.timeout / interval))
                for poll_count in range(max_polls):
                    await asyncio.sleep(interval)

                    history_response = await client.get(
                        f"{self.base_url}/history/{prompt_id}"
                    )
                    history_response.raise_for_status()
                    history = history_response.json()

                    if prompt_id in history:
                        # Check if execution is complete
                        history_data = history[prompt_id]
                        status = history_data.get("status", {}) or {}

                        # ComfyUI reports a failed run as status_str "error" (022);
                        # "str" / "execution error" is kept from before
                        if status.get("status_str") == "error" or status.get("str", "") == "execution error":
                            raise RuntimeError(f"ComfyUI execution error: {self._error_text(history_data)}")
                        if status.get("completed", False):
                            logger.info(f"Generation completed after {(poll_count + 1) * interval:.1f} seconds")
                            break
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

                save_image_node = nodes["save_image"]
                if save_image_node not in outputs:
                    raise RuntimeError(f"No output from node {save_image_node} (SaveImage)")

                save_image_outputs = outputs[save_image_node]
                images = save_image_outputs.get("images", [])

                if not images:
                    raise RuntimeError(f"No images in node {save_image_node} output")

                filename = images[0].get("filename")
                if not filename:
                    raise RuntimeError(f"No filename in node {save_image_node} output")
                # where ComfyUI put it (022): a filename_prefix with a folder gives a subfolder
                subfolder = images[0].get("subfolder") or ""
                image_type = images[0].get("type") or "output"

                logger.info(f"Output filename: {filename} (subfolder '{subfolder}', type {image_type})")

                # Step 4: Download image from /view endpoint
                view_response = await client.get(
                    f"{self.base_url}/view",
                    params={"filename": filename, "subfolder": subfolder, "type": image_type}
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
                actual_seed = workflow[nodes["ksampler"]]["inputs"]["seed"]
                
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
