"""Video track renderer for Phase B of 3-phase pipeline

Renders silent video track with:
- Segment-based background image switching
- Date overlay (top-right corner)
- Topic title overlay (center, per segment)
- Subtitles (.ass file)
"""
import asyncio
import logging
import math
import shutil
from datetime import datetime
from pathlib import Path

from rich.console import Console

from core.models import AppConfig, VideoTimeline
from core.interfaces import ChapterMarker
from .topic_overlay import DATE_FONT_SIZE, DATE_MARGIN_X, DATE_MARGIN_Y, build_topic_overlay_filter

logger = logging.getLogger(__name__)
console = Console()


def _even(value: float) -> int:
    """Round up to an even pixel count (yuv420p needs even sizes)."""
    n = math.ceil(value)
    return n + (n % 2)


def build_pan_filter(width: int, height: int, duration_sec: float, pan_ratio: float, scene_index: int) -> str:
    """Filter chain that slowly pans one still image across the frame (no zoom).

    The image is scaled to (1 + pan_ratio) of the frame (cropped to that exact size
    from the centre if its aspect differs), then a frame-sized crop window moves
    horizontally by the slack (pan_ratio of the width) over the scene, using the
    input time t. Scenes alternate left-to-right and right-to-left; the window stays
    vertically centred. Zero-length scenes get a fixed centred window.
    """
    big_w = _even(width * (1 + pan_ratio))
    big_h = _even(height * (1 + pan_ratio))
    dx = big_w - width
    dy = big_h - height
    if duration_sec > 0:
        progress = f"t/{duration_sec:.3f}"
        x = f"{dx}*{progress}" if scene_index % 2 == 0 else f"{dx}-{dx}*{progress}"
    else:
        x = str(dx // 2)
    return (
        f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase,"
        f"crop={big_w}:{big_h},"
        f"crop={width}:{height}:x={x}:y={dy // 2},"
        f"setsar=1"
    )


def smooth_pan_window(
    width: int,
    duration_sec: float,
    scene_index: int,
    zoom_min: float,
    zoom_max: float,
) -> tuple[float, float, float, float]:
    """(z0, dz, o0, do) of a smooth pan: zoom z = z0 + dz*p, image-centre offset o = o0 + do*p.

    p runs from 0 at the first frame to min(1, duration/full_range_sec) at the last (see
    build_smooth_pan_filter), so every scene moves at the same speed and only scenes of
    full_range_sec or longer use the whole range (longer ones move slower). o is how far the
    image centre sits from the frame centre
    in output pixels; it travels width*(zoom_min - 1), which keeps the window inside the
    image at every zoom >= zoom_min. Even scenes zoom in moving right, odd scenes zoom out
    moving left. Zero-length scenes stay still at (zoom_min, centre).
    """
    zoom_min = max(1.0, zoom_min)
    zoom_max = max(zoom_min, zoom_max)
    if duration_sec <= 0:
        return zoom_min, 0.0, 0.0, 0.0
    half_travel = width * (zoom_min - 1) / 2
    z_from, z_to, o_from, o_to = zoom_min, zoom_max, -half_travel, half_travel
    if scene_index % 2 == 1:
        z_from, z_to, o_from, o_to = z_to, z_from, o_to, o_from
    return z_from, z_to - z_from, o_from, o_to - o_from


def build_smooth_pan_filter(
    width: int,
    height: int,
    duration_sec: float,
    fps: int,
    scene_index: int,
    zoom_min: float,
    zoom_max: float,
    full_range_sec: float,
    work_width: int = 0,
) -> str:
    """Filter chain that slowly pans and zooms one still image with sub-pixel steps.

    crop (build_pan_filter) can only move by whole pixels (by 2 on yuv420p), so a slow
    pan stands still for dozens of frames and then jumps. Here the image is scaled to a
    working size and perspective (sense=source) maps a moving window of it onto the whole
    working frame, resampling with 1/256-pixel precision every frame; the result is then
    scaled to the output. perspective recomputes its per-pixel map every frame on one
    thread, so a working size below the output (work_width; 0 = output width) keeps it
    cheap. The window is a plain rectangle (zoom + translation only); see
    smooth_pan_window for the motion. perspective has no time variable, so progress uses
    the input frame number `in` (1 for the first frame).
    """
    if work_width <= 0 or work_width >= width:
        work_w, work_h = width, height
    else:
        work_w = _even(work_width)
        work_h = _even(work_width * height / width)
    z0, dz, o0, do = smooth_pan_window(work_w, duration_sec, scene_index, zoom_min, zoom_max)
    half_w = work_w / 2
    half_h = work_h / 2
    if dz == 0 and do == 0:
        z, o, eval_mode = f"{z0:.6f}", f"{o0:.4f}", "init"
    else:
        frames = fps * max(duration_sec, full_range_sec)
        p = f"(in-1)/{frames:.1f}"
        z = f"({z0:.6f}{dz:+.6f}*{p})"
        o = f"({o0:.4f}{do:+.4f}*{p})"
        eval_mode = "frame"
    # Window in the frame-sized image: output x = (image x - left) * z, image centre at half_w + o
    left = f"{half_w:g}-({o}+{half_w:g})/{z}"
    right = f"{half_w:g}-({o}-{half_w:g})/{z}"
    top = f"{half_h:g}-{half_h:g}/{z}"
    bottom = f"{half_h:g}+{half_h:g}/{z}"
    upscale = f"scale={width}:{height}," if (work_w, work_h) != (width, height) else ""
    return (
        f"scale={work_w}:{work_h}:force_original_aspect_ratio=increase,"
        f"crop={work_w}:{work_h},"
        f"format=yuv420p,"
        f"perspective=x0={left}:y0={top}:x1={right}:y1={top}:"
        f"x2={left}:y2={bottom}:x3={right}:y3={bottom}:"
        f"interpolation=linear:sense=source:eval={eval_mode},"
        f"{upscale}"
        f"setsar=1"
    )


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
        console.print(
            f"[dim]Video track: {len(timeline.segments)} segments, "
            f"{len(timeline.video_scenes())} backgrounds (pan: {self._pan_method() or 'off'})[/dim]"
        )
        
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

        # Input files: one image per scene (segments are split into scenes; a segment's
        # scenes together last its video_duration_sec, which ends at the next segment's
        # first utterance where the chapter title also switches; see
        # align_cuts_to_segment_starts / assign_scene_times)
        # -framerate makes the looped image produce output-rate frames, so a pan moves
        # every output frame instead of every 25 fps input frame.
        input_args = []
        for image_path, duration_sec in timeline.video_scenes():
            input_args.extend([
                "-loop", "1",
                "-framerate", str(timeline.fps),
                "-t", str(duration_sec),
                "-i", str(image_path)
            ])

        # Build filter_complex
        filter_parts = []

        # 1. Concatenate images (scene-based switching, optionally panning)
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
            *self._video_codec_args(),
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
        scenes = timeline.video_scenes()
        n = len(scenes)
        pan_method = self._pan_method()
        pan_ratio = self._pan_ratio()
        vc = self.video_config

        # Scale each input to target resolution (and pan it when enabled)
        scale_filters = []
        for i, (_image, duration_sec) in enumerate(scenes):
            if pan_method == "perspective":
                chain = build_smooth_pan_filter(
                    int(width), int(height), duration_sec, timeline.fps, i,
                    float(getattr(vc, "pan_zoom_min", 1.12)),
                    float(getattr(vc, "pan_zoom_max", 1.22)),
                    float(getattr(vc, "pan_full_range_sec", 90.0)),
                    int(getattr(vc, "pan_work_width", 0)),
                )
            elif pan_method == "crop" and pan_ratio > 0:
                chain = build_pan_filter(int(width), int(height), duration_sec, pan_ratio, i)
            else:
                chain = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
            scale_filters.append(f"[{i}:v]{chain}[v{i}]")

        # Concatenate all scaled inputs
        concat_inputs = "".join(f"[v{i}]" for i in range(n))
        concat_filter = f"{concat_inputs}concat=n={n}:v=1:a=0[vbase]"

        return ";".join(scale_filters) + ";" + concat_filter

    def _pan_method(self) -> str:
        """"perspective" (smooth, default), "crop" (009, whole-pixel steps) or "" (no pan)."""
        if not getattr(self.video_config, "enable_pan", False):
            return ""
        method = str(getattr(self.video_config, "pan_method", "perspective")).strip().lower()
        if method not in ("perspective", "crop"):
            logger.warning(f"Unknown pan_method {method!r}; using perspective")
            return "perspective"
        return method

    def _pan_ratio(self) -> float:
        """crop method: share of the screen width a scene moves over its length (0 = no pan)."""
        if not getattr(self.video_config, "enable_pan", False):
            return 0.0
        return max(0.0, float(getattr(self.video_config, "pan_ratio", 0.05)))

    def _video_codec_args(self) -> list[str]:
        """Encoder arguments for the video track.

        libx264 (preset video_track_x264_preset) by default. video_track_use_gpu switches
        to NVENC with the same quality settings as the single-image renderer (use_gpu there).
        """
        if getattr(self.video_config, "video_track_use_gpu", False):
            return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0"]
        return [
            "-c:v", "libx264",
            "-preset", str(getattr(self.video_config, "video_track_x264_preset", "ultrafast")),
            "-crf", "23",
        ]
    
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

        # Position/size shared with topic_overlay, which keeps the title band clear of it
        return (
            f"drawtext=text='{creation_date}':"
            f"fontfile='{fontfile}':"
            f"fontsize={DATE_FONT_SIZE}:fontcolor=white@0.7:"
            f"x=w-tw-{DATE_MARGIN_X}:y={DATE_MARGIN_Y}:"
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
