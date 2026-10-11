"""Image prompt generator for FLUX.1 background images"""
import logging
from typing import Optional

from rich.console import Console

from core.interfaces.llm_port import LLMRequest
from core.models import AppConfig
from core.models.curation import ScriptSegment
from core.models.visual import (
    VisualIdentity,
    VisualPalette,
    DEFAULT_PRIMARY_COLOR,
    DEFAULT_SECONDARY_COLOR,
    DEFAULT_AESTHETIC,
)
from services.script_generation.adapters.factory import LLMAdapterFactory

logger = logging.getLogger(__name__)
console = Console()


class ImagePromptGenerator:
    """LLM-based image prompt generator

    Step 5 (2026-05-10): Mac Studio Proxy (vLLM / Qwen3 系) 経由で
    FLUX.1 用の英語プロンプトを生成する。Gemini 直叩きから移行済み。
    """
    
    # Default color palette when VisualIdentity is not provided
    # Issue #4 fix: Derive from centralized constants
    DEFAULT_COLOR_PALETTE = f"{DEFAULT_PRIMARY_COLOR} and {DEFAULT_SECONDARY_COLOR} neon lighting"
    
    # Prepended by code to every segment prompt (LLM output and fallbacks alike), so the
    # constraint sits at the head of the prompt instead of relying on the LLM to end with it.
    SEGMENT_PROMPT_PREFIX = (
        "An unpopulated scene with no people, no hands, no faces, "
        "and no text, lettering, signage, labels or screen interfaces: "
    )

    # Upper bound for the final segment prompt (prefix included). FLUX.1 [schnell] is trained
    # with 256 T5 tokens; ComfyUI pads to 256 but does not truncate, so longer prompts run
    # out of distribution. Measured with ComfyUI's T5 tokenizer on 501 past segment prompts:
    # every prompt up to 1,135 chars stayed within 256 tokens (max 233), every prompt from
    # 1,398 chars up exceeded it (median 3.95 chars/token). Longer output is discarded in
    # favour of the fixed fallback, never truncated.
    MAX_SEGMENT_PROMPT_CHARS = 1000

    SYSTEM_PROMPT_TEMPLATE = """You are a professional cinematographer creating SCENE-DRIVEN, text-free still-life and environment shots for AI image generation.

Your task is to generate a detailed English prompt for FLUX.1 that evokes what the radio script segment is about through a PLACE, PHYSICAL OBJECTS, LIGHT, and COMPOSITION - with no people and no text anywhere in the frame.

ABSOLUTE RULES (MANDATORY - these override everything else):
1. NO PEOPLE:
   - NO people, hands, fingers, faces, silhouettes, or body parts of any kind
   - Imply human presence only through objects and light: an empty chair, a tool left on a bench, a lamp left on, a door ajar
2. NO TEXT OR TEXT-BEARING SURFACES:
   - NO text, letters, numbers, captions, signage, labels, logos, or watermarks
   - NO screens, monitors, phone or tablet displays, apps, or user interfaces
   - NO documents, books with visible pages, papers, charts, graphs, diagrams, maps, code, or data visualizations
   - Devices may appear only switched off or seen from behind/side, with no visible display content
3. NEVER TRANSCRIBE THE SCRIPT:
   - Do NOT copy names, product names, figures, statistics, or quotes from the script into the prompt
   - Translate the idea into a physical scene instead

PRIMARY FOCUS:
1. IDENTIFY THE CORE IDEA of the segment, then choose ONE physical scene that symbolizes it:
   - A place: a room, workshop, studio, laboratory, street, landscape
   - Tangible objects: instruments, tools, machines, materials, natural elements
2. DESCRIBE THE SCENE WITH PRECISION:
   - Be SPECIFIC about objects, materials, and surfaces: name the exact object, what it is made of, its condition, and what it rests on - never a generic category such as "technology" or "research"
   - Let LIGHT carry the mood: direction, color, contrast, reflections, haze
   - Let COMPOSITION carry the meaning: scale, distance, symmetry, emptiness, depth
3. START YOUR PROMPT WITH THE MAIN OBJECT OR PLACE:
   - Make it the hero of the composition

UNIFIED VISUAL BRAND (MANDATORY - Apply consistently):
- Color Palette: {color_palette}
- Aesthetic: {aesthetic}
- Film Quality: shot on Kodak Portra 400 film, subtle film grain, highly detailed

NARRATIVE GUIDANCE:
{composition_guidance}

CREATIVE FREEDOM:
- Camera angles, distances, and framing: YOUR CHOICE to best showcase the scene
- Lighting style and mood: YOUR CHOICE to support the narrative
- Supporting objects and setting: YOUR CHOICE to enrich the scene

CONSTRAINTS (MANDATORY):
- ALWAYS start with a SPECIFIC physical object or place
- ALWAYS incorporate the color palette and aesthetic
- Use ONLY the Color Palette given above — NEVER reuse colors from the EXAMPLE
  OUTPUTS below (their colors are placeholders illustrating structure only)
- ALWAYS include film quality keywords
- Do NOT write "no text" or "no people" yourself — that constraint is added automatically
- Keep the prompt to ONE paragraph of 60-110 words

OUTPUT FORMAT:
Return ONLY the English prompt text, with no explanations and no surrounding quotation marks.
DO NOT include any Japanese characters in your output.

EXAMPLE OUTPUTS (Scene-Driven, no people, no text):

Example 1 (Medical Topic):
A small glucose sensor patch and a capped insulin pen resting on a folded linen cloth beside a glass of water on a bedside table, an unmade bed and a softly glowing lamp in the background suggesting someone has just left the room, bathed in soft teal and warm coral neon glow, Clean Minimalist Modern aesthetic, calm and intimate atmosphere, shallow depth of field, shot on Kodak Portra 400 film, subtle film grain, highly detailed

Example 2 (Technology Topic):
A long aisle of server racks receding into darkness in an empty data hall, bundled fiber cables arcing overhead and thin haze catching beams of light between the cabinets, a single office chair pushed back from the aisle, bathed in golden amber and deep violet neon glow, Clean Minimalist Modern aesthetic, quiet and focused atmosphere, symmetrical wide shot with strong depth, shot on Kodak Portra 400 film, subtle film grain, highly detailed

Example 3 (Environmental Topic):
A row of cylindrical ice core samples laid on a steel workbench inside a cold research hut, frost on the window and a pair of insulated gloves left beside a thermos, pale daylight spilling across the melting ice, bathed in emerald green and ice blue neon glow, Clean Minimalist Modern aesthetic, urgent and contemplative atmosphere, close still-life framing, shot on Kodak Portra 400 film, subtle film grain, highly detailed
"""
    
    THUMBNAIL_SYSTEM_PROMPT_TEMPLATE = """You are a professional cinematographer specializing in creating ABSTRACT, METAPHORICAL, and ATMOSPHERIC YouTube thumbnail backgrounds for radio content.

Your task is to generate a detailed English prompt for FLUX.1 that creates a visually striking background featuring ABSTRACT LANDSCAPES, ATMOSPHERIC SCENERY, or METAPHORICAL IMAGERY - NOT specific objects or people. The imagery style MUST follow the Aesthetic specified in the UNIFIED VISUAL BRAND below (do not default to any particular genre).

PRIMARY FOCUS (MANDATORY - Radio Background Suitability):
1. CREATE ABSTRACT OR METAPHORICAL VISUALS:
   - Landscapes: Expansive cityscapes, natural scenery, or dreamlike environments that fit the Aesthetic below
   - Abstract art: Flowing energy patterns, geometric shapes, light and color compositions
   - Stylized elements: Light patterns, textures, and environmental details drawn from the Aesthetic below (no text or signage)
   - Metaphorical imagery: Visual metaphors that evoke the theme's essence without literal representation

2. AVOID CONCRETE SUBJECTS:
   - NO specific devices, tools, or equipment
   - NO people, hands, or human figures
   - NO text, charts, graphs, or data displays
   - NO literal representations of the topic

3. OPTIMIZE FOR BACKGROUND USE:
   - The image should complement text overlay (thumbnail title will be added later)
   - High visual impact through color, lighting, and atmosphere
   - Clean composition that doesn't compete with foreground text

UNIFIED VISUAL BRAND (MANDATORY):
- Color Palette: {color_palette}
- Aesthetic: {aesthetic}
- Film Quality: shot on Kodak Portra 400 film, subtle film grain, highly detailed

REQUIREMENTS:
- Focus: Create ABSTRACT, ATMOSPHERIC, or METAPHORICAL imagery (NOT concrete objects)
- Impact: Maximum visual impact through color, lighting, and mood
- Composition: Clean, uncluttered background suitable for text overlay
- Colors: Use ONLY the Color Palette given above — NEVER reuse colors from the
  EXAMPLE OUTPUTS below (their colors are placeholders illustrating structure only)
- ALWAYS end with: "no text, no writing, no watermarks" (MANDATORY)

OUTPUT FORMAT:
Return ONLY the English prompt text, no explanations.
DO NOT include any Japanese characters in your output.

EXAMPLE OUTPUTS (Abstract/Metaphorical Backgrounds):

Example 1 (Medical/Health Topic):
"An abstract flowing energy field with pulsing waves of light representing vitality and health, transitioning from deep violet to gentle lavender, with soft particle effects and ethereal glow, bathed in gentle lavender and pale gold lighting, Clean Minimalist Modern aesthetic, serene and hopeful atmosphere, shot on Kodak Portra 400 film, subtle film grain, highly detailed, no text"

Example 2 (Technology Topic):
"A futuristic cityscape at dusk with towering illuminated skyscrapers and light beams cutting through misty atmosphere, geometric patterns of light floating in the foreground, bathed in crimson red and steel blue glow, Clean Minimalist Modern aesthetic, innovative and dynamic atmosphere, shot on Kodak Portra 400 film, subtle film grain, highly detailed, no text"

Example 3 (Environmental Topic):
"An abstract representation of Earth's atmosphere with swirling aurora-like energy patterns in vivid greens and blues, cosmic background with distant stars, flowing light trails suggesting movement and change, bathed in sunset orange and dusk purple accents, Clean Minimalist Modern aesthetic, majestic and contemplative atmosphere, shot on Kodak Portra 400 film, subtle film grain, highly detailed, no text"
"""
    
    def __init__(self, config: AppConfig):
        """Initialize prompt generator

        Args:
            config: Application configuration
        """
        self.config = config

        # Step 5 (2026-05-10): Gemini → Mac Studio Proxy (vLLM Qwen3 系)。
        # orchestrator.curator_model を再利用 (ThumbnailGenerator.regenerate_with_new_title
        # と同じ選択基準で、軽量タスク用のスロットを共有する)。
        self.model_name = config.yaml.script_generator.orchestrator.curator_model
        self._llm_port = LLMAdapterFactory.create(
            config, "ollama", model_override=self.model_name
        )

        logger.info(f"ImagePromptGenerator initialized with model: {self.model_name}")
    
    async def generate_prompt(
        self,
        segment: ScriptSegment,
        visual_identity: Optional[VisualIdentity] = None
    ) -> str:
        """Generate English image prompt from segment content
        
        Args:
            segment: Script segment
            visual_identity: Optional visual identity for brand consistency
        
        Returns:
            str: English prompt for FLUX.1
        """
        # Issue #6 fix: Single parameter for clarity
        identity = visual_identity
        
        # Extract segment context
        segment_context = self._build_segment_context(segment)
        
        # Get composition guidance based on segment type
        composition_guidance = self._get_composition_guidance(segment.segment_type)
        
        # Build color palette and aesthetic descriptions
        # Issue #1 fix: No isinstance check needed (VisualPalette is now an alias)
        if identity:
            color_palette = identity.to_color_fragment()
            aesthetic = identity.to_aesthetic_fragment()
        else:
            # Fallback to default cyberpunk colors
            color_palette = self.DEFAULT_COLOR_PALETTE
            aesthetic = f"{DEFAULT_AESTHETIC} aesthetic"
        
        # Build dynamic system prompt
        system_prompt = self.SYSTEM_PROMPT_TEMPLATE.format(
            color_palette=color_palette,
            aesthetic=aesthetic,
            composition_guidance=composition_guidance
        )
        
        # Build user message with scene-translation emphasis
        user_message = f"""Generate a SCENE-DRIVEN cinematic image prompt for this radio segment.

Segment Type: {segment.segment_type}
Topic: {segment.topic_title or "General discussion"}

Script Content:
{segment_context}

INSTRUCTIONS:
1. Read the script and identify the CORE IDEA of the segment
2. Choose ONE physical place or object arrangement that symbolizes that idea
3. Describe it with MAXIMUM SPECIFICITY at the start of your prompt, expressing the idea through objects, light, and composition
4. Add the unified visual brand (colors, aesthetic, film quality)
5. No people, hands, or faces; no text, screens, documents, charts, or numbers; never copy names or figures from the script

Generate the prompt now:"""
        
        logger.info(f"Generating prompt for segment: {segment.segment_id}")
        console.print(f"[cyan]Generating image prompt for {segment.segment_id}...[/cyan]")
        
        try:
            llm_request = LLMRequest(
                system_prompt=system_prompt,
                user_prompt=user_message,
                model=self.model_name,
                max_tokens=512,
                temperature=0.8,
                response_format="text",
            )
            llm_response = await self._llm_port.generate(llm_request)
            body = llm_response.content.strip().strip('"\'').strip()
            if not body:
                raise ValueError("LLM returned an empty image prompt")

            # Put the no-people / no-text constraint at the head of the prompt
            prompt = self.SEGMENT_PROMPT_PREFIX + body

            if len(prompt) > self.MAX_SEGMENT_PROMPT_CHARS:
                # Runaway output: use the fixed fallback rather than a truncated prompt
                logger.warning(
                    f"Generated prompt too long for {segment.segment_id}: "
                    f"{len(prompt)} chars > {self.MAX_SEGMENT_PROMPT_CHARS}, using fallback prompt"
                )
                console.print(f"[yellow]⚠ Prompt too long ({len(prompt)} chars), using fallback prompt[/yellow]")
                return self._get_fallback_prompt(segment, identity)

            logger.info(f"Generated prompt: {prompt[:100]}...")
            console.print(f"[dim]Prompt: {prompt[:80]}...[/dim]")

            return prompt

        except Exception as e:
            logger.error(f"Prompt generation failed: {e}")
            console.print(f"[red]✗ Prompt generation failed: {e}[/red]")

            # Fallback to generic prompt
            fallback = self._get_fallback_prompt(segment, identity)
            console.print(f"[yellow]Using fallback prompt[/yellow]")
            return fallback
    
    def _build_segment_context(self, segment: ScriptSegment) -> str:
        """Build rich context string from segment for subject-driven prompt generation
        
        Provides comprehensive script information to enable LLM to identify
        concrete subjects, objects, and actions discussed in the segment.
        
        Args:
            segment: Script segment
        
        Returns:
            str: Rich context description with full narrative content
        """
        context_parts = []
        
        # Add topic if available
        if segment.topic_title:
            context_parts.append(f"Topic: {segment.topic_title}")
        
        # Add comprehensive dialogue context (expanded from 3 turns to 10+ turns)
        if segment.turns and len(segment.turns) > 0:
            # Strategy: Capture enough turns to convey the segment's core narrative
            # - For short segments (<5 turns): Include all turns
            # - For medium segments (5-15 turns): Include first 10 turns
            # - For long segments (>15 turns): Include first 12 turns + strategic sampling
            
            total_turns = len(segment.turns)
            
            if total_turns <= 5:
                # Short segment: include everything
                sample_turns = segment.turns
            elif total_turns <= 15:
                # Medium segment: first 10 turns capture intro + core discussion
                sample_turns = segment.turns[:10]
            else:
                # Long segment: first 12 turns + sample from middle/end
                # This ensures we capture intro, core points, and conclusion hints
                sample_turns = segment.turns[:12].copy()  # Explicit copy for clarity
                mid_point = total_turns // 2
                # Add 2-3 turns from middle only if they're not already included in first 12
                if mid_point >= 12:
                    sample_turns.extend(segment.turns[mid_point:mid_point+2])
                # Add last 2 turns only if segment is long enough to avoid overlap
                # (total_turns > 14 ensures last 2 turns are beyond first 12)
                if total_turns > 14:
                    sample_turns.extend(segment.turns[-2:])
            
            # Extract dialogue text with generous character limit (800 chars)
            # This allows LLM to grasp concrete subjects and narrative flow
            dialogue_sample = " ".join([
                turn.get("text", "") for turn in sample_turns 
                if turn.get("text")
            ])
            
            if dialogue_sample:
                # Expand character limit from 200 to 800 to preserve narrative richness
                # Truncate gracefully at sentence boundary if needed
                if len(dialogue_sample) > 800:
                    truncated = dialogue_sample[:800]
                    # Try to end at last complete sentence
                    last_period = truncated.rfind('。')
                    if last_period == -1:
                        last_period = truncated.rfind('.')
                    # Use sentence boundary if found and reasonable (not too early)
                    if last_period > 600:
                        truncated = truncated[:last_period + 1]
                    # If no good sentence boundary found, keep full 800 chars (already set)
                    context_parts.append(f"Discussion:\n{truncated}...")
                else:
                    context_parts.append(f"Discussion:\n{dialogue_sample}")
        
        return "\n\n".join(context_parts) if context_parts else "General radio discussion"
    
    async def generate_thumbnail_prompt(
        self,
        theme: str,
        script_summary: str,
        topic_title: Optional[str] = None,
        visual_identity: Optional[VisualIdentity] = None
    ) -> str:
        """Generate eye-catching thumbnail background prompt
        
        Args:
            theme: Video theme
            script_summary: Script summary (200-300 chars)
            topic_title: Optional topic title
            visual_identity: Optional visual identity for brand consistency
        
        Returns:
            str: English prompt for FLUX.1 thumbnail background
        """
        # Issue #6 fix: Single parameter for clarity
        identity = visual_identity
        
        # Build color palette and aesthetic descriptions
        # Issue #1 fix: No isinstance check needed (VisualPalette is now an alias)
        if identity:
            color_palette = identity.to_color_fragment()
            aesthetic = identity.to_aesthetic_fragment()
        else:
            # Fallback to default cyberpunk colors
            color_palette = self.DEFAULT_COLOR_PALETTE
            aesthetic = f"{DEFAULT_AESTHETIC} aesthetic"
        
        # Build dynamic system prompt
        system_prompt = self.THUMBNAIL_SYSTEM_PROMPT_TEMPLATE.format(
            color_palette=color_palette,
            aesthetic=aesthetic
        )
        
        # Sanitize Japanese input to prevent mixing
        theme_sanitized = self._extract_english_keywords(theme)
        summary_sanitized = self._extract_english_keywords(script_summary[:300])
        
        # Build user message with sanitized input
        user_message = f"""Generate a visually striking ABSTRACT/METAPHORICAL thumbnail background prompt for this radio content:

Theme keywords: {theme_sanitized}
Topic keywords: {self._extract_english_keywords(topic_title) if topic_title else theme_sanitized}

Content essence:
{summary_sanitized}

Create an ABSTRACT, ATMOSPHERIC, or METAPHORICAL background (NOT concrete objects/people) that evokes the theme's essence through color, lighting, and mood."""
        
        logger.info(f"Generating thumbnail prompt for theme: {theme}")
        console.print(f"[cyan]Generating thumbnail background prompt...[/cyan]")
        
        try:
            llm_request = LLMRequest(
                system_prompt=system_prompt,
                user_prompt=user_message,
                model=self.model_name,
                max_tokens=512,
                temperature=0.9,  # Higher creativity for thumbnails
                response_format="text",
            )
            llm_response = await self._llm_port.generate(llm_request)
            prompt = llm_response.content.strip()

            # Sanitize to remove any Japanese characters
            prompt = self._sanitize_prompt(prompt)

            # Enforce mandatory quality keywords only
            prompt = self._enforce_quality_keywords(prompt)

            logger.info(f"Generated thumbnail prompt: {prompt[:100]}...")
            console.print(f"[dim]Thumbnail prompt: {prompt[:80]}...[/dim]")

            return prompt

        except Exception as e:
            logger.error(f"Thumbnail prompt generation failed: {e}")
            console.print(f"[red]✗ Thumbnail prompt generation failed: {e}[/red]")

            # Fallback to generic thumbnail prompt
            fallback = self._get_fallback_thumbnail_prompt(theme, identity)
            console.print(f"[yellow]Using fallback thumbnail prompt[/yellow]")
            return fallback
    
    def _extract_english_keywords(self, text: str) -> str:
        """Extract English keywords from mixed Japanese/English text
        
        Args:
            text: Input text (may contain Japanese)
        
        Returns:
            str: English keywords only, or generic description if none found
        """
        import re
        
        # Extract English words (2+ characters)
        english_words = re.findall(r'\b[a-zA-Z]{2,}\b', text)
        
        if english_words:
            # Return unique keywords, max 10 words
            unique_words = []
            seen = set()
            for word in english_words:
                word_lower = word.lower()
                if word_lower not in seen and word_lower not in {'the', 'and', 'for', 'with', 'about'}:
                    unique_words.append(word)
                    seen.add(word_lower)
                if len(unique_words) >= 10:
                    break
            return ' '.join(unique_words)
        else:
            # No English found, return generic description
            return "abstract concept, modern technology, innovative ideas"
    
    def _sanitize_prompt(self, prompt: str) -> str:
        """Remove any Japanese characters from prompt
        
        Args:
            prompt: Generated prompt
        
        Returns:
            str: Sanitized prompt (English only)
        """
        import re
        
        # Remove Japanese characters (Hiragana, Katakana, Kanji)
        sanitized = re.sub(r'[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]+', '', prompt)
        
        # Remove extra spaces
        sanitized = re.sub(r'\s+', ' ', sanitized).strip()
        
        # Log if sanitization occurred
        if sanitized != prompt:
            logger.warning(f"Prompt sanitized: removed non-English characters")
            logger.debug(f"Original length: {len(prompt)}, Sanitized length: {len(sanitized)}")
        
        return sanitized
    
    def _get_composition_guidance(self, segment_type: str) -> str:
        """Get composition guidance based on segment type
        
        Provides concrete, cinematographer-level directives about which specific
        visual details and subjects to focus on, moving beyond abstract mood to
        actionable visual storytelling instructions.
        
        Args:
            segment_type: Segment type (intro, deep_dive, conclusion)
        
        Returns:
            str: Concrete visual directives for LLM
        """
        guidance_map = {
            "intro": """- Narrative Role: Scene-setting and topic introduction
- Scene Focus: Show the PLACE and the KEY OBJECTS that define this topic, waiting and unattended
  * For medical topics: an empty consultation room, instruments laid out on a tray, a made bed by a window
  * For technology topics: the hardware itself - machines, cables, components, an empty workshop or server hall
  * For social topics: the everyday place where the issue lives - an empty street, classroom, kitchen, or station
- Visual Details to Emphasize:
  * The main object or place should be clearly identifiable and occupy 40-60% of frame
  * Include surrounding objects that establish the domain
  * A state of readiness: everything in place, nobody present
- Suggested Composition: Medium or establishing shot that clearly shows where the story takes place""",
            
            "deep_dive": """- Narrative Role: Detailed investigation and analysis
- Scene Focus: Show the MECHANISM or MATERIAL at the heart of the topic, up close
  * For medical topics: instruments, samples, vials, or equipment in fine physical detail
  * For technology topics: internal components, circuitry, lenses, wiring, or machinery mid-process
  * For social topics: a telling object or trace that embodies the phenomenon
- Visual Details to Emphasize:
  * Close-up or detail shots that reveal texture, material, and construction
  * Signs of work in progress: tools set down, parts disassembled, light catching dust or haze
  * Several related objects can be arranged to convey complexity
- Suggested Composition: Close-up or medium-close still life emphasizing physical detail""",
            
            "conclusion": """- Narrative Role: Synthesis and forward-looking perspective
- Scene Focus: Show the topic's objects or place in a WIDER CONTEXT that suggests outcome and future
  * For medical topics: the equipment at rest in a bright, calm room; a window opening onto morning light
  * For technology topics: the finished object in its setting; a horizon, skyline, or open road beyond it
  * For social topics: the same place at a hopeful hour - dawn light, open doors, a path leading onward
- Visual Details to Emphasize:
  * A sense of completion or calm: things put away, light changing, space opening up
  * Forward-looking elements: an open doorway, a path, a horizon, first light
  * Wider framing that places the object within its larger environment
- Suggested Composition: Medium or wide shot that shows the scene's place in the larger picture"""
        }
        return guidance_map.get(segment_type, guidance_map["deep_dive"])
    
    def _enforce_quality_keywords(self, prompt: str) -> str:
        """Enforce mandatory quality keywords only (minimal constraints)
        
        Args:
            prompt: Generated prompt
        
        Returns:
            str: Prompt with quality keywords enforced
        """
        # Only enforce film quality keywords, not composition/lighting
        mandatory_quality_keywords = [
            "shot on Kodak Portra 400 film",
            "subtle film grain",
            "highly detailed"
        ]
        
        # Add missing quality keywords
        for keyword in mandatory_quality_keywords:
            if keyword.lower() not in prompt.lower():
                prompt += f", {keyword}"
        
        # Ensure "no text, no writing, no watermarks" is at the end
        if "no text" not in prompt.lower():
            prompt += ", no text, no writing, no watermarks"
        
        return prompt
    
    def _get_fallback_thumbnail_prompt(
        self,
        theme: str,
        visual_identity: Optional[VisualIdentity] = None
    ) -> str:
        """Get fallback thumbnail prompt if generation fails
        
        Args:
            theme: Video theme (may contain Japanese)
            visual_identity: Optional visual identity
        
        Returns:
            str: Fallback thumbnail prompt (abstract/metaphorical, English only)
        """
        # Extract English keywords from theme
        theme_keywords = self._extract_english_keywords(theme)
        
        # Issue #1 fix: No isinstance check needed
        if visual_identity:
            color_desc = visual_identity.to_color_fragment()
            aesthetic_desc = visual_identity.to_aesthetic_fragment()
        else:
            color_desc = self.DEFAULT_COLOR_PALETTE
            aesthetic_desc = f"{DEFAULT_AESTHETIC} aesthetic"
        
        # Return abstract/metaphorical fallback (no concrete objects)
        return (
            f"An abstract futuristic cityscape with neon-lit skyscrapers and holographic light beams, "
            f"flowing energy patterns and geometric shapes in the atmosphere, "
            f"bathed in {color_desc}, "
            f"{aesthetic_desc}, "
            f"dynamic composition with depth and atmospheric perspective, "
            f"shot on Kodak Portra 400 film, subtle film grain, highly detailed, no text, no writing, no watermarks"
        )
    
    def _get_fallback_prompt(
        self,
        segment: ScriptSegment,
        visual_identity: Optional[VisualIdentity] = None
    ) -> str:
        """Get fallback prompt if generation fails
        
        Args:
            segment: Script segment
            visual_identity: Optional visual identity
        
        Returns:
            str: Fallback prompt
        """
        # Map segment type to a generic scene with no people and no text-bearing objects
        scene_map = {
            "intro": "An empty radio studio with a vintage microphone on a desk stand and dark acoustic panels",
            "deep_dive": "A quiet workshop bench with precision tools and metal components laid out in neat rows",
            "conclusion": "A wide city skyline at dusk seen from an empty rooftop under an open sky",
        }
        
        scene = scene_map.get(segment.segment_type, scene_map["deep_dive"])
        
        # Issue #1 fix: No isinstance check needed
        if visual_identity:
            color_desc = visual_identity.to_color_fragment()
            aesthetic_desc = visual_identity.to_aesthetic_fragment()
        else:
            color_desc = self.DEFAULT_COLOR_PALETTE
            aesthetic_desc = f"{DEFAULT_AESTHETIC} aesthetic"
        
        return (
            f"{self.SEGMENT_PROMPT_PREFIX}{scene}, bathed in {color_desc}, "
            f"{aesthetic_desc}, "
            f"shot on Kodak Portra 400 film, subtle film grain, highly detailed"
        )
