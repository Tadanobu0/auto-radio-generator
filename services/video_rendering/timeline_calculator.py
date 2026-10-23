"""Timeline calculator for segment-based video rendering

Calculates detailed timeline information from script segments and audio synthesis results.
This is Phase A of the 3-phase rendering pipeline.
"""
import logging
from pathlib import Path

from rich.console import Console

from core.models import AppConfig, ScriptSegment, SegmentTimelineEntry, VideoTimeline
from core.interfaces import SynthesisResult, SegmentTiming
from services.media_processing import ImageProvider, JingleProvider

logger = logging.getLogger(__name__)
console = Console()


def align_cuts_to_segment_starts(
    entries: list[SegmentTimelineEntry],
    segments: list[ScriptSegment],
    total_duration_sec: float,
) -> None:
    """Make each background cut land on the next segment's first utterance.

    The chapter title switches at the next segment's first utterance
    (VoicevoxClient._build_chapters), so the background must switch there too.
    Summing per-segment durations (first utterance .. jingle end) left out the
    2s pre-roll and the inter-phrase pause after each segment's last utterance,
    so every cut came 2s + 0.25s * k too early (radio_director-013: the next
    chapter's background with the previous chapter's title).

    The video track concatenates segment images back to back, so the first image
    starts at 0 and the last one runs to the end of the audio. Segments without
    turns get no screen time (as before: they have no audio and no jingle).
    """
    shown = [i for i, seg in enumerate(segments[:len(entries)]) if seg.turns]
    if not shown:
        return
    prev_cut = 0.0
    for i, entry in enumerate(entries):
        if i not in shown:
            entry.video_duration_sec = 0.0
            entry.video_cut_time_sec = prev_cut
            continue
        k = shown.index(i)
        cut = entries[shown[k + 1]].audio_start_sec if k + 1 < len(shown) else float(total_duration_sec)
        cut = max(cut, prev_cut)
        entry.video_duration_sec = cut - prev_cut
        entry.video_cut_time_sec = cut
        prev_cut = cut


class TimelineCalculator:
    """Timeline calculation engine
    
    Constructs detailed video timeline from script segments and audio timing information.
    This is the foundation for Phase B (Independent Rendering).
    """
    
    def __init__(self, config: AppConfig):
        """Initialize timeline calculator
        
        Args:
            config: Application configuration
        """
        self.config = config
        
        # Get jingle settings from config
        video_config = getattr(config.yaml, "video_renderer", None)
        self.jingle_overlap_sec = getattr(video_config, "jingle_overlap_sec", 3.0) if video_config else 3.0
        self.enable_jingles = getattr(video_config, "enable_jingles", True) if video_config else True
        self.pre_jingle_pause_sec = getattr(video_config, "pre_jingle_pause_sec", 0.5) if video_config else 0.5
    
    async def calculate_timeline(
        self,
        segments: list[ScriptSegment],
        synthesis_result: SynthesisResult,
        image_provider: ImageProvider,
        jingle_provider: JingleProvider,
        bgm_path: Path,
    ) -> VideoTimeline:
        """Calculate complete video timeline
        
        Args:
            segments: Script segments from TopicCurator/SegmentGenerator
            synthesis_result: Audio synthesis result with segment timings
            image_provider: Background image provider
            jingle_provider: Jingle audio provider
            bgm_path: BGM file path
        
        Returns:
            VideoTimeline: Complete timeline with all segment information
        """
        console.print("[cyan]Calculating video timeline...[/cyan]")
        
        timeline_entries = []
        
        for i, segment in enumerate(segments):
            # Index-based correspondence: segments[i] <-> segment_timings[i]
            # This correctly handles duplicate segment_ids (e.g., multiple "deep_dive")
            if i < len(synthesis_result.segment_timings):
                timing = synthesis_result.segment_timings[i]
                # Sanity check: warn if IDs diverge (does not block processing)
                if timing.segment_id != segment.segment_id:
                    logger.warning(
                        f"segment_id mismatch at index {i}: "
                        f"script='{segment.segment_id}' vs timing='{timing.segment_id}'. "
                        f"Using timing by index (safe)."
                    )
            else:
                logger.warning(
                    f"No timing at index {i} for segment {segment.segment_id}, using estimated timing"
                )
                timing = self._estimate_timing(segment, i, len(segments), synthesis_result.total_duration_sec)
            
            # Get background image for this segment
            bg_image = await image_provider.get_image_for_segment(segment)
            
            # Get jingle information from SegmentTiming (single source of truth from VoicevoxClient)
            # No random selection here - use what VoicevoxClient already selected
            jingle_path = timing.jingle_path
            jingle_duration = timing.jingle_duration
            jingle_start = None
            
            if jingle_path and jingle_duration:
                # Start jingle after pre-jingle pause (breathing space before jingle)
                # Pause structure: [segment end] + [pre-pause] + [jingle]
                jingle_start = timing.end_sec + self.pre_jingle_pause_sec
                logger.debug(
                    f"Jingle for segment {segment.segment_id}: {jingle_path.name} "
                    f"at {jingle_start:.2f}s (pre-pause: {self.pre_jingle_pause_sec:.2f}s, "
                    f"duration: {jingle_duration:.2f}s) [from VoicevoxClient]"
                )
            
            # Calculate video timing (for jingle-synchronized transitions)
            # If jingle exists, extend video display until jingle ends (including pre-pause)
            # (provisional: align_cuts_to_segment_starts below sets the final cut times)
            if jingle_path and jingle_duration and jingle_duration > 0:
                total_pause_duration = self.pre_jingle_pause_sec + jingle_duration
                video_duration = timing.duration_sec + total_pause_duration
                video_cut_time = timing.end_sec + total_pause_duration
            else:
                video_duration = timing.duration_sec
                video_cut_time = timing.end_sec
            
            timeline_entries.append(SegmentTimelineEntry(
                segment_id=segment.segment_id,
                segment_type=segment.segment_type,
                topic_title=segment.topic_title,
                audio_start_sec=timing.start_sec,
                audio_end_sec=timing.end_sec,
                duration_sec=timing.duration_sec,
                video_duration_sec=video_duration,
                video_cut_time_sec=video_cut_time,
                background_image_path=bg_image,
                jingle_path=jingle_path,
                jingle_start_sec=jingle_start,
                jingle_duration_sec=jingle_duration,
            ))
            
            console.print(
                f"[dim]  Segment {i+1}/{len(segments)}: {segment.segment_id} "
                f"({timing.start_sec:.1f}s - {timing.end_sec:.1f}s, "
                f"image: {bg_image.name})[/dim]"
            )
        
        # Total duration equals the synthesized audio duration.
        # Pre-jingle pauses and jingle silences are already baked into combined_audio
        # by VoicevoxClient._combine_audio_with_pauses; adding them again would
        # double-count and cause the video timeline to exceed the audio track.
        jingle_count = sum(1 for entry in timeline_entries if entry.jingle_path is not None)
        total_duration_with_pauses = synthesis_result.total_duration_sec

        align_cuts_to_segment_starts(timeline_entries, segments, total_duration_with_pauses)
        
        logger.debug(
            f"Timeline duration: audio={synthesis_result.total_duration_sec:.2f}s, "
            f"jingles={jingle_count}, total={total_duration_with_pauses:.2f}s"
        )
        
        # Get video settings from config
        video_config = self.config.yaml.video_renderer
        
        timeline = VideoTimeline(
            segments=timeline_entries,
            total_duration_sec=total_duration_with_pauses,  # Include pre-jingle pauses
            main_audio_path=synthesis_result.audio_path,
            bgm_path=bgm_path,
            bgm_volume=video_config.bgm_volume,
            fade_in_sec=video_config.bgm_fade_in_sec,
            fade_out_sec=video_config.bgm_fade_out_sec,
            resolution=video_config.output_resolution,
            fps=video_config.output_fps,
            subtitle_path=synthesis_result.subtitle_path,
        )
        
        console.print(f"[green]✓ Timeline calculated: {len(timeline_entries)} segments[/green]")
        
        return timeline
    
    def _estimate_timing(
        self,
        segment: ScriptSegment,
        index: int,
        total_segments: int,
        total_duration: float
    ) -> SegmentTiming:
        """Estimate timing for a segment (fallback)
        
        Args:
            segment: Script segment
            index: Segment index
            total_segments: Total number of segments
            total_duration: Total audio duration
        
        Returns:
            SegmentTiming: Estimated timing
        """
        # Simple linear estimation
        segment_duration = total_duration / total_segments
        start_sec = index * segment_duration
        end_sec = start_sec + segment_duration
        
        logger.warning(
            f"Using estimated timing for segment {segment.segment_id}: "
            f"{start_sec:.1f}s - {end_sec:.1f}s"
        )
        
        return SegmentTiming(
            segment_id=segment.segment_id,
            segment_type=segment.segment_type,
            topic_title=segment.topic_title,
            start_sec=start_sec,
            end_sec=end_sec,
            duration_sec=segment_duration,
        )
