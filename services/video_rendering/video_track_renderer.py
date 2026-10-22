"""Video track renderer for Phase B of 3-phase pipeline

Renders silent video track with:
- Segment-based background image switching
- Date overlay (top-right corner)
- Topic title overlay (center, per segment)
- Subtitles (.ass file)
"""
import asyncio
import logging
import shutil
from datetime import datetime
from pathlib import Path

from rich.console import Console

from core.models import AppConfig, VideoTimeline
from core.interfaces import ChapterMarker
from .topic_overlay import build_topic_overlay_filter

logger = logging.getLogger(__name__)
console = Console()


class VideoTrackRenderer:
    """Video track renderer
    
    Generates silent video track with segment-based background switching.
    This is Phase B (Video) of the 3-phase rendering pipeline.
    """
    
    def __init__(self, config: AppConfig):
        """Initialize video track renderer
        
        Args:
            config: Application configuration
        """
        self.config = config
        self.video_config = config.yaml.video_renderer
        self.ffmpeg_path = self._find_ffmpeg()
    
    def _find_ffmpeg(self) -> str:
        """Find FFmpeg executable path"""
        # Check project root
        project_ffmpeg = self.config.project_root.parent / "ffmpeg.exe"
        if project_ffmpeg.exists():
            return str(project_ffmpeg)
        
        # Check PATH
        ffmpeg_in_path = shutil.which("ffmpeg")
        if ffmpeg_in_path:
            return ffmpeg_in_path
        
        return "ffmpeg"
    
    async def render_video_track(
        self,
        timeline: VideoTimeline,
        output_path: Path,
        chapters: list[ChapterMarker],
    ) -> Path:
        """Render silent video track
        
        Args:
            timeline: Video timeline with segment and image information
            output_path: Output video file path (e.g., temp_video_track.mp4)
            chapters: Chapter markers for topic overlay
        
        Returns:
            Path: Generated video track path
        """
        console.print("[cyan]Rendering video track...[/cyan]")
        
        # Build FFmpeg command
        cmd = self._build_ffmpeg_command(timeline, output_path, chapters)
        
        # Debug output
        console.print(f"[dim]Video track: {len(timeline.segments)} segments with background switching[/dim]")
        
        # Execute FFmpeg
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            error_msg = stderr.decode('utf-8', errors='replace')
            logger.error(f"Video track rendering failed: {error_msg}")
            console.print(f"[red]✗ Video track rendering failed[/red]")
            raise RuntimeError(f"FFmpeg video rendering failed with code {process.returncode}")
        
        console.print(f"[green]✓ Video track rendered: {output_path.name}[/green]")
        return output_path
    
    def _build_ffmpeg_command(
        self,
        timeline: VideoTimeline,
        output_path: Path,
        chapters: list[ChapterMarker]
    ) -> list[str]:
        """Build FFmpeg command for video track rendering
        
        Args:
            timeline: Video timeline
            output_path: Output file path
            chapters: Chapter markers
        
        Returns:
            list[str]: FFmpeg command arguments
        """
        width, height = timeline.resolution.split('x')
        
        # Input files: one image per segment
        # Use video_duration_sec (includes jingle duration) for jingle-synchronized cuts
        input_args = []
        for seg in timeline.segments:
            input_args.extend([
                "-loop", "1",
                "-t", str(seg.video_duration_sec),
                "-i", str(seg.background_image_path)
            ])
        
        # Build filter_complex
        filter_parts = []
        
        # 1. Concatenate images (segment-based switching)
        concat_filter = self._build_concat_filter(timeline, width, height)
        filter_parts.append(concat_filter)
        
        # 2. Add overlays (date, topic titles, subtitles)
        overlay_filter = self._build_overlay_filter(timeline, chapters)
        if overlay_filter:
            filter_parts.append(overlay_filter)
        
        filter_complex = ",".join(filter_parts)
        
        cmd = [
            self.ffmpeg_path,
            "-y",  # Overwrite output
            *input_args,
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-c:v", "libx264",
            "-preset", "ultrafast",  # Fast encoding for intermediate file
            "-crf", "23",
            "-r", str(timeline.fps),
            "-pix_fmt", "yuv420p",
            "-t", str(timeline.total_duration_sec),  # Explicit duration to match audio
            "-an",  # No audio
            str(output_path)
        ]
        
        return cmd
    
    def _build_concat_filter(self, timeline: VideoTimeline, width: str, height: str) -> str:
        """Build concatenation filter for segment images
        
        Args:
            timeline: Video timeline
            width: Video width
            height: Video height
        
        Returns:
            str: Concat filter string
        """
        n = len(timeline.segments)
        
        # Scale each input to target resolution
        scale_filters = []
        for i in range(n):
            scale_filters.append(f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2[v{i}]")
        
        # Concatenate all scaled inputs
        concat_inputs = "".join(f"[v{i}]" for i in range(n))
        concat_filter = f"{concat_inputs}concat=n={n}:v=1:a=0[vbase]"
        
        return ";".join(scale_filters) + ";" + concat_filter
    
    def _build_overlay_filter(self, timeline: VideoTimeline, chapters: list[ChapterMarker]) -> str:
        """Build overlay filters (date, topic titles, subtitles)
        
        Args:
            timeline: Video timeline
            chapters: Chapter markers
        
        Returns:
            str: Overlay filter string
        """
        filters = []
        
        # 1. Date overlay (top-right corner)
        date_filter = self._build_date_filter()
        filters.append(date_filter)
        
        # 2. Topic title overlay (center, per segment)
        show_topic_overlay = getattr(
            getattr(self.config.yaml, "video", None),
            "show_topic_overlay",
            True
        )
        if show_topic_overlay:
            topic_filter = self._build_topic_overlay_filter(chapters, timeline.total_duration_sec)
            if topic_filter:
                filters.append(topic_filter)
        
        # 3. Subtitle overlay (.ass file)
        if timeline.subtitle_path and timeline.subtitle_path.exists():
            subtitle_filter = self._build_subtitle_filter(timeline.subtitle_path)
            filters.append(subtitle_filter)
        
        if not filters:
            # No overlays, just rename output
            return "[vbase]copy[vout]"
        
        # Chain filters: [vbase] -> filter1 -> filter2 -> ... -> [vout]
        # Properly chain with input/output labels
        filter_chain = f"[vbase]{','.join(filters)}[vout]"
        return filter_chain
    
    def _build_date_filter(self) -> str:
        """Build date overlay filter (top-right corner)
        
        Returns:
            str: Date filter string
        """
        creation_date = datetime.now().strftime("%Y/%m/%d")
        fontfile = self._get_drawtext_fontfile()
        
        return (
            f"drawtext=text='{creation_date}':"
            f"fontfile='{fontfile}':"
            f"fontsize=28:fontcolor=white@0.7:"
            f"x=w-tw-20:y=20:"
            f"shadowcolor=black@0.5:shadowx=2:shadowy=2"
        )
    
    def _build_topic_overlay_filter(
        self,
        chapters: list[ChapterMarker],
        total_duration_sec: float
    ) -> str:
        """Build topic title overlay filter (center, per segment)

        Titles are never truncated: they wrap to up to 2 lines and shrink if needed
        (see topic_overlay).

        Args:
            chapters: Chapter markers
            total_duration_sec: Total video duration

        Returns:
            str: Topic overlay filter string
        """
        return build_topic_overlay_filter(
            chapters,
            total_duration_sec,
            self._get_drawtext_fontfile(),
            self._escape_drawtext_text,
        )
    
    def _build_subtitle_filter(self, subtitle_path: Path) -> str:
        """Build subtitle overlay filter (.ass file)
        
        Args:
            subtitle_path: Path to .ass subtitle file
        
        Returns:
            str: Subtitle filter string
        """
        abs_path = str(subtitle_path.resolve())
        safe_path = self._escape_windows_path(abs_path)
        return f"subtitles='{safe_path}'"
    
    def _get_drawtext_fontfile(self) -> str:
        """Get Japanese font file path for drawtext filter
        
        Returns:
            str: Escaped font file path
        """
        # Meiryo first so the chapter title matches the subtitle font
        candidates = [
            "C:/Windows/Fonts/meiryo.ttc",
            "C:/Windows/Fonts/msgothic.ttc",
            "C:/Windows/Fonts/yuGothM.ttc",
            "C:/Windows/Fonts/arial.ttf",
        ]
        
        for candidate in candidates:
            if Path(candidate).exists():
                return self._escape_windows_path(candidate)
        
        return self._escape_windows_path(candidates[0])
    
    def _escape_windows_path(self, path: str) -> str:
        """Escape Windows path for FFmpeg filters
        
        Args:
            path: Windows path
        
        Returns:
            str: Escaped path
        """
        return path.replace("\\", "/").replace(":", "\\:")
    
    def _escape_drawtext_text(self, text: str) -> str:
        """Escape text for FFmpeg drawtext filter
        
        Args:
            text: Text to escape
        
        Returns:
            str: Escaped text
        """
        escaped = text.replace("\\", "\\\\")
        escaped = escaped.replace(":", "\\:")
        escaped = escaped.replace("'", "\\'")
        escaped = escaped.replace(",", "\\,")
        return escaped
