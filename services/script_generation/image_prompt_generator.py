"""Image prompt generator for FLUX.1 background images"""
import logging
import re
from typing import List, Optional

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
    # Kept short and led by positive wording ("unpopulated, wordless"): FLUX reads negated nouns
    # as nouns, so every "no X" risks inviting X (e.g. "screen" was dropped for that reason).
    SEGMENT_PROMPT_PREFIX = (
        "An unpopulated, wordless scene with no people, no hands, no faces, "
        "no text, signage or labels: "
    )

    # Upper bound for the script excerpt handed to the LLM (same budget as the former
    # "first 800 chars" excerpt, now spread over the whole segment).
    MAX_CONTEXT_CHARS = 800

    # Number of evenly spaced excerpt points when a segment has no key-claim anchors.
    EVEN_EXCERPT_POINTS = 4

    # Citation tags such as [src=3] / [src=1, 2] in verified_script turns.
    _CITATION_TAG = re.compile(r"\[src=(\d+(?:\s*,\s*\d+)*)\]")

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
   - A SMALL DEVICE whose display is part of what it is (a desk sensor, a meter, a thermostat) may show its display window, but ONLY as a dark glass window or a plain colored glow - never digits, letters, or icons
3. NEVER TRANSCRIBE THE SCRIPT:
   - Do NOT copy names, product names, figures, statistics, or quotes from the script into the prompt
   - Translate the idea into a physical scene instead

PRIMARY FOCUS:
1. IDENTIFY THE CORE IDEA of the segment, then choose ONE physical scene that symbolizes it:
   - Prefer the actual object the script talks about, shown plainly and recognizably, over a symbolic substitute
   - A place: a room, workshop, studio, kitchen, street, landscape
   - Tangible objects: everyday things, tools, machines, materials, natural elements
   - Do NOT default to a bedroom, a hospital, or medical equipment unless the script is actually about them
2. NAME THE HERO OBJECT PRECISELY - every prompt MUST state all five:
   - WHAT: the exact main object (never a generic category such as "technology" or "research")
   - SIZE: its real-world size (palm-sized, knee-high, room-sized ...)
   - MATERIAL: what it is made of and its finish or condition
   - PLACE: what it rests on and where in the room or landscape it stands
   - FRAME SHARE: how much of the frame it fills - 40 to 60 percent, written out (e.g. "filling about half of the frame")
3. THEN THE ATMOSPHERE:
   - Let LIGHT carry the mood: direction, color, contrast, reflections, haze
   - Let COMPOSITION carry the meaning: scale, distance, symmetry, emptiness, depth
   - Keep supporting objects few and secondary so the hero object stays recognizable
4. START YOUR PROMPT WITH THE MAIN OBJECT OR PLACE:
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
- ALWAYS state the hero object's size, material, place, and frame share (40-60 percent)
- Do NOT write "no text" or "no people" yourself — that constraint is added automatically
- Describe only what IS in the frame. Do not name excluded things even to negate them
  (never write words such as screen, face, person, or sign) — naming them invites them
- Keep the prompt to ONE paragraph of 60-110 words

OUTPUT FORMAT:
Return ONLY the English prompt text, with no explanations and no surrounding quotation marks.
DO NOT include any Japanese characters in your output.

EXAMPLE OUTPUTS (Scene-Driven, unpopulated and wordless; the topics are unrelated to yours - copy the structure, never the objects or places):

Example 1 (Everyday Life Topic):
A small grapefruit-sized cast-iron kettle with a worn black enamel finish and a wooden handle, sitting on a pale stone countertop beside an open kitchen window, filling about half of the frame, a folded linen towel and a small bowl of loose tea leaves behind it, thin steam curling into cool morning air, bathed in soft teal and warm coral neon glow, Clean Minimalist Modern aesthetic, calm and homely atmosphere, close still-life framing, shot on Kodak Portra 400 film, subtle film grain, highly detailed

Example 2 (Technology Topic):
A long aisle of tall black steel server racks receding into darkness in an empty data hall, the racks filling about half of the frame along both sides, bundled fiber cables arcing overhead and thin haze catching beams of light between the cabinets, a single office chair pushed back from the aisle, bathed in golden amber and deep violet neon glow, Clean Minimalist Modern aesthetic, quiet and focused atmosphere, symmetrical wide shot with strong depth, shot on Kodak Portra 400 film, subtle film grain, highly detailed

Example 3 (Environmental Topic):
A row of arm-length cylindrical ice core samples laid on a brushed steel workbench inside a cold research hut, the ice filling the lower half of the frame, frost on the window and a pair of insulated gloves left beside a thermos, pale daylight spilling across the melting ice, bathed in emerald green and ice blue neon glow, Clean Minimalist Modern aesthetic, urgent and contemplative atmosphere, close still-life framing, shot on Kodak Portra 400 film, subtle film grain, highly detailed
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

Script Excerpts:
{segment_context}

INSTRUCTIONS:
1. Read the excerpts (taken from across the whole segment) and identify the CORE IDEA of the segment
2. Choose ONE physical place or object arrangement that shows that idea - preferably the actual object the script discusses
3. Start your prompt with the hero object and state its size, material, where it is placed, and that it fills 40-60 percent of the frame
4. Add light, composition, and the unified visual brand (colors, aesthetic, film quality)
5. Keep the scene unpopulated and wordless, describing only what is in it; never copy names or figures from the script

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
        
        Provides excerpts from across the whole segment (not only its opening) so the
        LLM sees the objects and places discussed in the later key points as well.

        Args:
            segment: Script segment

        Returns:
            str: Topic plus dialogue excerpts, at most MAX_CONTEXT_CHARS of dialogue
        """
        context_parts = []

        # Add topic if available
        if segment.topic_title:
            context_parts.append(f"Topic: {segment.topic_title}")

        dialogue = self._select_dialogue_excerpts(segment)
        if dialogue:
            context_parts.append(f"Discussion:\n{dialogue}")

        return "\n\n".join(context_parts) if context_parts else "General radio discussion"

    def _select_dialogue_excerpts(self, segment: ScriptSegment) -> str:
        """Pick dialogue excerpts spread over the segment, within MAX_CONTEXT_CHARS.

        - Whole dialogue fits the budget: return it all.
        - Otherwise take the turns around (one before, the turn, one after) each anchor turn:
          the first turn citing each key claim's source ([src=N]), or, when there are no
          such turns, evenly spaced turns. The budget is split equally between the excerpts,
          which are joined by newlines.
        """
        turns = segment.turns or []
        texts = [self._CITATION_TAG.sub("", turn.get("text") or "").strip() for turn in turns]
        whole = " ".join(text for text in texts if text)
        if len(whole) <= self.MAX_CONTEXT_CHARS:
            return whole

        anchors = self._key_claim_anchor_turns(turns, segment.key_claim_source_idxs)
        if not anchors:
            anchors = self._even_anchor_turns(len(turns))

        # Merge overlapping / adjacent windows so no turn is quoted twice
        windows: List[List[int]] = []
        for anchor in anchors:
            start, end = max(anchor - 1, 0), min(anchor + 1, len(turns) - 1)
            if windows and start <= windows[-1][1] + 1:
                windows[-1][1] = max(windows[-1][1], end)
            else:
                windows.append([start, end])

        separators = len(windows) - 1
        budget = (self.MAX_CONTEXT_CHARS - separators) // len(windows)
        excerpts = []
        for start, end in windows:
            excerpt = " ".join(text for text in texts[start:end + 1] if text)
            if excerpt:
                excerpts.append(self._truncate_excerpt(excerpt, budget))
        return "\n".join(excerpts)

    def _key_claim_anchor_turns(self, turns: List[dict], source_idxs: List[int]) -> List[int]:
        """Index of the first turn citing each key claim's source, sorted and deduplicated."""
        anchors = set()
        for source_idx in source_idxs:
            for i, turn in enumerate(turns):
                cited = {
                    int(n)
                    for tag in self._CITATION_TAG.findall(turn.get("text") or "")
                    for n in tag.split(",")
                }
                if source_idx in cited:
                    anchors.add(i)
                    break
        return sorted(anchors)

    def _even_anchor_turns(self, total_turns: int) -> List[int]:
        """Evenly spaced turn indices (starting at the first turn)."""
        points = min(self.EVEN_EXCERPT_POINTS, total_turns)
        return sorted({k * total_turns // points for k in range(points)})

    @staticmethod
    def _truncate_excerpt(excerpt: str, budget: int) -> str:
        """Fit an excerpt into budget chars, preferring a sentence end, else ending with '…'."""
        if len(excerpt) <= budget:
            return excerpt
        head = excerpt[:budget]
        # Japanese sentence ends only: '.' also appears inside figures such as "4.5"
        last_period = max(head.rfind(mark) for mark in "。！？")
        if last_period >= budget // 2:
            return head[:last_period + 1]
        return excerpt[:budget - 1] + "…"
    
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
  * For health and lifestyle topics: the very thing the topic is about (a food, a household device, a piece of exercise gear) in the ordinary place where it is used - not hospital equipment unless the script is about a hospital
  * For technology topics: the hardware itself - machines, cables, components, an empty workshop or server hall
  * For social topics: the everyday place where the issue lives - an empty street, classroom, kitchen, or station
- Visual Details to Emphasize:
  * The main object or place should be clearly identifiable and occupy 40-60% of frame
  * Include surrounding objects that establish the domain
  * A state of readiness: everything in place, nobody present
- Suggested Composition: Medium or establishing shot that clearly shows where the story takes place""",
            
            "deep_dive": """- Narrative Role: Detailed investigation and analysis
- Scene Focus: Show the MECHANISM or MATERIAL at the heart of the topic, up close
  * For health and lifestyle topics: the topic's own object itself in fine physical detail (its shape, material, and finish), shown as it really looks - not as medical instruments or lab equipment
  * For technology topics: internal components, circuitry, lenses, wiring, or machinery mid-process
  * For social topics: a telling object or trace that embodies the phenomenon
- Visual Details to Emphasize:
  * The main object should be clearly identifiable and occupy 40-60% of frame
  * Close-up or detail shots that reveal texture, material, and construction
  * Signs of work in progress: tools set down, parts disassembled, light catching dust or haze
  * Several related objects can be arranged to convey complexity
- Suggested Composition: Close-up or medium-close still life emphasizing physical detail""",
            
            "conclusion": """- Narrative Role: Synthesis and forward-looking perspective
- Scene Focus: Show the topic's objects or place in a WIDER CONTEXT that suggests outcome and future
  * For health and lifestyle topics: the topic's own object in daily use at rest in a bright, calm room; a window opening onto morning light
  * For technology topics: the finished object in its setting; a horizon, skyline, or open road beyond it
  * For social topics: the same place at a hopeful hour - dawn light, open doors, a path leading onward
- Visual Details to Emphasize:
  * The main object or place should still be clearly identifiable and occupy 40-60% of frame
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
