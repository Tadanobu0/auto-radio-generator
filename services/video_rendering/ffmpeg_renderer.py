"""FFmpegを使用した動画レンダリング（3フェーズパイプライン版）

Phase A: Timeline Calculation - セグメント情報から映像・音声のタイムラインを計算
Phase B: Independent Rendering - 映像トラックと音声トラックを独立して生成
Phase C: Muxing - 再エンコードなしで高速結合
"""
import asyncio
import subprocess
import shutil
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from rich.console import Console

from core.interfaces import ChapterMarker, IVideoRenderer, RenderResult, SynthesisResult
from core.models import AppConfig, ScriptSegment
from core.models.visual import VisualIdentity  # Issue #7 fix: Proper type import
from services.media_processing import ImageProvider, JingleProvider
from .timeline_calculator import TimelineCalculator
from .topic_overlay import build_topic_overlay_filter
from .audio_track_renderer import AudioTrackRenderer
from .video_track_renderer import VideoTrackRenderer
from .ffmpeg_renderer_single_image import render_single_image
from .ffmpeg_renderer_mux import mux_tracks

console = Console()


class FfmpegRenderer(IVideoRenderer):
    """FFmpegを使用した動画生成
    
    背景画像、音声、BGM、字幕を合成してMP4動画を生成します。
    """
    
    def __init__(self, config: AppConfig):
        super().__init__(config)
        self.video_config = config.yaml.video_renderer
        
        # FFmpegのパス（環境変数PATH、またはプロジェクト内のffmpeg.exe）
        self.ffmpeg_path = self._find_ffmpeg()
        
        # 3-phase pipeline components
        self.timeline_calculator = TimelineCalculator(config)
        self.video_track_renderer = VideoTrackRenderer(config)
        self.audio_track_renderer = AudioTrackRenderer(config)
    
    def _find_ffmpeg(self) -> str:
        """FFmpegの実行パスを探す"""
        # まずプロジェクトルートを確認
        project_ffmpeg = self.config.project_root.parent / "ffmpeg.exe"
        if project_ffmpeg.exists():
            return str(project_ffmpeg)
        
        # PATHから探す
        ffmpeg_in_path = shutil.which("ffmpeg")
        if ffmpeg_in_path:
            return ffmpeg_in_path
        
        # デフォルト
        return "ffmpeg"
    
    def _escape_windows_path(self, path: str) -> str:
        """Windows絶対パスをFFmpeg subtitlesフィルタ向けにエスケープ
        
        libass（subtitlesフィルタの内部ライブラリ）は独自のパス解析を行うため、
        以下の処理が必須:
          1. バックスラッシュ \\ → スラッシュ / に統一
          2. ドライブレターのコロン : → \\: にエスケープ
        
        Args:
            path: エスケープ対象のパス文字列
        
        Returns:
            str: FFmpeg subtitlesフィルタ用にエスケープされたパス
        """
        return path.replace("\\", "/").replace(":", "\\:")

    def _escape_drawtext_text(self, text: str) -> str:
        """FFmpeg drawtext用にテキストをエスケープ"""
        escaped = text.replace("\\", "\\\\")
        escaped = escaped.replace(":", "\\:")
        escaped = escaped.replace("'", "\\'")
        escaped = escaped.replace(",", "\\,")
        return escaped

    def _get_drawtext_fontfile(self) -> str:
        """Windowsで利用可能な日本語フォントのfontfile値を返す(字幕と揃えて Meiryo を優先)"""
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

    def _build_topic_overlay_filter(
        self,
        chapters: list[ChapterMarker] | None,
        total_duration_sec: float,
    ) -> str | None:
        """チャプター単位のトピックオーバーレイ drawtext を構築(タイトルは切らずに折り返す)"""
        overlay = build_topic_overlay_filter(
            chapters,
            total_duration_sec,
            self._get_drawtext_fontfile(),
            self._escape_drawtext_text,
        )
        return overlay or None
    
    def check_ffmpeg_available(self) -> bool:
        """FFmpegが利用可能か確認"""
        try:
            result = subprocess.run(
                [self.ffmpeg_path, "-version"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                # バージョン情報の最初の行を取得
                version_line = result.stdout.split('\n')[0]
                console.print(f"[green]OK FFmpeg 利用可能[/green] {version_line}")
                return True
        except Exception as e:
            console.print(f"[red]ERROR FFmpeg が見つかりません: {e}[/red]")
        return False
    
    async def render(
        self,
        synthesis_result: SynthesisResult,
        background_image: Path,
        bgm_file: Path,
        output_path: Path,
        subtitle_path: Path | None = None,
        chapters: list[ChapterMarker] | None = None,
        segments: Optional[list[ScriptSegment]] = None,
        visual_identity: Optional[VisualIdentity] = None,
    ) -> RenderResult:
        """動画を生成（3フェーズパイプライン）
        
        Phase A: Timeline Calculation
        Phase B: Independent Rendering (Video Track + Audio Track)
        Phase C: Muxing (再エンコードなし)
        
        Args:
            synthesis_result: 音声合成の結果
            background_image: 背景画像パス（後方互換性のため残す、segmentsがある場合は無視）
            bgm_file: BGMファイルパス
            output_path: 出力動画パス
            subtitle_path: 字幕ファイルパス（オプショナル）
            chapters: チャプターマーカー（オプショナル）
            segments: スクリプトセグメント（新規、セグメント単位の背景切り替え用）
        """
        # 出力ディレクトリを確保
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 字幕パスとチャプターの決定
        final_subtitle_path = subtitle_path if subtitle_path is not None else synthesis_result.subtitle_path
        final_chapters = chapters if chapters is not None else synthesis_result.chapters
        
        # セグメント情報がない場合は従来の単一背景画像モードで動作（後方互換性）
        if not segments:
            console.print("[yellow]⚠ No segments provided, using single-image fallback mode[/yellow]")
            return await self._render_single_image(
                synthesis_result=synthesis_result,
                background_image=background_image,
                bgm_file=bgm_file,
                output_path=output_path,
                subtitle_path=final_subtitle_path,
                chapters=final_chapters,
            )
        
        # 3-phase pipeline mode
        console.print("[cyan]動画を生成中（3フェーズパイプライン）...[/cyan]")
        
        # 一時ファイル用ディレクトリ
        temp_dir = output_path.parent / ".temp"
        temp_dir.mkdir(exist_ok=True)
        
        try:
            # ========== Phase A: Timeline Calculation ==========
            console.print("[cyan]Phase A: タイムライン計算中...[/cyan]")
            
            # Extract output_dir from output_path for PromptOps logging
            output_dir = output_path.parent
            image_provider = ImageProvider(self.config, visual_identity=visual_identity, output_dir=output_dir)
            jingle_provider = JingleProvider()
            
            timeline = await self.timeline_calculator.calculate_timeline(
                segments=segments,
                synthesis_result=synthesis_result,
                image_provider=image_provider,
                jingle_provider=jingle_provider,
                bgm_path=bgm_file,
            )
            
            console.print(f"[green]✓ タイムライン計算完了: {len(timeline.segments)}セグメント[/green]")
            
            # Get wall-clock time for image generation
            segment_bg_generation_time = image_provider.get_total_generation_time()
            if segment_bg_generation_time > 0:
                console.print(f"[dim]セグメント背景生成時間（実測）: {segment_bg_generation_time:.1f}秒[/dim]")
            
            # ========== Phase B: Independent Rendering ==========
            console.print("[cyan]Phase B: 映像・音声トラック生成中...[/cyan]")
            
            video_track_path = temp_dir / "video_track.mp4"
            audio_track_path = temp_dir / "audio_track.wav"
            
            # 並列実行（映像と音声は完全に独立）
            video_task = self.video_track_renderer.render_video_track(
                timeline=timeline,
                output_path=video_track_path,
                chapters=final_chapters,
            )
            audio_task = self.audio_track_renderer.render_audio_track(
                timeline=timeline,
                output_path=audio_track_path,
            )
            
            await asyncio.gather(video_task, audio_task)
            
            console.print(f"[green]✓ 映像トラック生成完了: {video_track_path.name}[/green]")
            console.print(f"[green]✓ 音声トラック生成完了: {audio_track_path.name}[/green]")
            
            # ========== Phase C: Muxing (再エンコードなし) ==========
            console.print("[cyan]Phase C: 最終結合中...[/cyan]")
            
            await self._mux_tracks(
                video_track=video_track_path,
                audio_track=audio_track_path,
                output_path=output_path,
            )
            
            # 中間ファイル削除（設定で保持も可能）
            keep_temp = getattr(getattr(self.config.yaml, "dev", None), "keep_temp_files", False)
            if not keep_temp:
                shutil.rmtree(temp_dir)
            else:
                console.print(f"[dim]中間ファイルを保持: {temp_dir}[/dim]")
            
            # 結果を返す
            file_size_mb = output_path.stat().st_size / (1024 * 1024)
            total_duration = synthesis_result.total_duration_sec
            
            console.print(f"[green]OK 動画生成完了[/green] {output_path.name}")
            console.print(f"  → サイズ: {file_size_mb:.1f} MB, 長さ: {total_duration:.1f}秒")
            console.print(f"  → セグメント数: {len(timeline.segments)}")
            
            return RenderResult(
                video_path=output_path,
                duration_sec=total_duration,
                file_size_mb=file_size_mb,
                segment_bg_generation_time=segment_bg_generation_time
            )
        
        except Exception as e:
            # エラー時も中間ファイルを保持（デバッグ用）
            console.print(f"[red]✗ 動画生成エラー: {e}[/red]")
            console.print(f"[yellow]中間ファイルを保持（デバッグ用）: {temp_dir}[/yellow]")
            raise
    
    async def _mux_tracks(self, video_track: Path, audio_track: Path, output_path: Path) -> None:
        """Mux video and audio tracks (Phase C)
        
        Args:
            video_track: Video track file path
            audio_track: Audio track file path
            output_path: Output video file path
        """
        await mux_tracks(self.ffmpeg_path, video_track, audio_track, output_path)
    
    async def _render_single_image(
        self,
        synthesis_result: SynthesisResult,
        background_image: Path,
        bgm_file: Path,
        output_path: Path,
        subtitle_path: Path,
        chapters: list[ChapterMarker],
    ) -> RenderResult:
        """Single-image fallback rendering (used when no segments are provided).

        Args:
            synthesis_result: Audio synthesis result
            background_image: Background image path
            bgm_file: BGM file path
            output_path: Output video path
            subtitle_path: Subtitle file path
            chapters: Chapter markers

        Returns:
            RenderResult: Rendering result
        """
        return await render_single_image(
            self,
            synthesis_result,
            background_image,
            bgm_file,
            output_path,
            subtitle_path,
            chapters,
        )
    
    def _build_ffmpeg_command(
        self,
        background_image: Path,
        audio_file: Path,
        bgm_file: Path,
        subtitle_file: Path,
        output_path: Path,
        resolution: str,
        fps: int,
        bgm_volume: float,
        fade_in_sec: float,
        fade_out_sec: float,
        total_duration_sec: float,
        chapters: list[ChapterMarker] | None = None,
    ) -> list[str]:
        """FFmpegコマンドを構築"""
        width, height = resolution.split('x')

        if chapters:
            console.print(f"[dim]DEBUG: chapter markers received: {len(chapters)}[/dim]")
        
        # BGMフェードアウト開始時間
        fade_out_start = max(0, total_duration_sec - fade_out_sec)
        
        # スペクトラム設定
        enable_spectrum = self.video_config.enable_spectrum
        spectrum_color = self.video_config.spectrum_color
        spectrum_mode = self.video_config.spectrum_mode
        
        # フィルター複合構築
        # BGM: ループ再生、音量調整、フェードイン/アウト
        bgm_filter = (
            f"[2:a]volume={bgm_volume},"
            f"afade=t=in:st=0:d={fade_in_sec},"
            f"afade=t=out:st={fade_out_start}:d={fade_out_sec}[bgm]"
        )
        
        # 音声ミックス → ラウドネスノーマライゼーション（YouTube推奨: -14 LUFS）
        audio_mix_filter = (
            "[1:a][bgm]amix=inputs=2:duration=first:dropout_transition=2[mixed];"
            "[mixed]loudnorm=I=-14:TP=-1:LRA=11[aout]"
        )
        
        # 字幕ファイル (.ass) の適用
        if subtitle_file and subtitle_file.exists():
            # 【重要】Windows絶対パスのFFmpeg subtitlesフィルタ向けエスケープ
            # libass（subtitlesフィルタの内部ライブラリ）は独自のパス解析を行うため、
            # 以下の処理が必須:
            #   1. バックスラッシュ \ → スラッシュ / に統一
            #   2. ドライブレターのコロン : → \: にエスケープ
            #   3. シングルクォートで囲む（スペース等の対策）
            # 相対パスでも同様の問題が起きるため、絶対パスに統一してエスケープする
            abs_path = str(subtitle_file.resolve())
            safe_path = self._escape_windows_path(abs_path)

            console.print(f"[dim]DEBUG: subtitle original: {subtitle_file}[/dim]")
            console.print(f"[dim]DEBUG: subtitle escaped:  {safe_path}[/dim]")
            
            subtitle_filter = f"subtitles='{safe_path}'"
        else:
            console.print("[yellow]WARNING: Subtitle path not provided or file not found.[/yellow]")
            subtitle_filter = None
        
        # 日付表示フィルター（右上に透かし文字で表示）
        creation_date = datetime.now().strftime("%Y/%m/%d")
        # drawtextフィルター: Windowsの場合はフォントファイルを直接指定
        # 日本語フォントを優先して豆腐（文字化け）を回避
        drawtext_fontfile = self._get_drawtext_fontfile()
        date_filter = (
            f"drawtext=text='{creation_date}':"
            f"fontfile='{drawtext_fontfile}':"
            f"fontsize=28:fontcolor=white@0.7:"
            f"x=w-tw-20:y=20:"
            f"shadowcolor=black@0.5:shadowx=2:shadowy=2"
        )

        show_topic_overlay = getattr(
            getattr(self.config.yaml, "video", None),
            "show_topic_overlay",
            True,
        )
        topic_overlay_filter = None
        if show_topic_overlay:
            topic_overlay_filter = self._build_topic_overlay_filter(
                chapters=chapters,
                total_duration_sec=total_duration_sec,
            )

        composed_video_filters = [date_filter]
        if topic_overlay_filter:
            composed_video_filters.append(topic_overlay_filter)
        if subtitle_filter:
            composed_video_filters.append(subtitle_filter)
        composed_video_filter_chain = ",".join(composed_video_filters)
        
        # ビデオフィルター構築
        # TASK-7: 波形表示を完全に削除（視認性向上のため）
        # 日付 → トピックオーバーレイ → 字幕のみを表示
        video_filter = (
            f"[0:v]scale={width}:{height},"
            f"{composed_video_filter_chain}[vout]"
        )
        
        # フィルター全体
        filter_complex = f"{bgm_filter};{audio_mix_filter};{video_filter}"
        
        # GPU加速設定の取得
        use_gpu = getattr(self.video_config, 'use_gpu', False)
        
        # エンコーダー設定を切り替え
        if use_gpu:
            # GPU加速（NVENC）を使用
            console.print("[green]🚀 Using GPU Acceleration (RTX 4070/NVENC)[/green]")
            video_codec = "h264_nvenc"
            codec_params = [
                "-preset", "p4",      # RTXシリーズ向け推奨プリセット
                "-rc", "vbr",         # 可変ビットレート
                "-cq", "23",          # Constant Quality (23 = 高品質)
                "-b:v", "0",          # ビットレート制限なし（CQ優先）
            ]
        else:
            # CPU エンコーディング（libx264）
            console.print("[yellow]🐢 Using CPU Encoding (libx264)[/yellow]")
            video_codec = "libx264"
            codec_params = [
                "-preset", "medium",  # CPUプリセット
                "-crf", "23",         # Constant Rate Factor
            ]
        
        cmd = [
            self.ffmpeg_path,
            "-y",  # 上書き確認なし
            
            # 入力1: 背景画像（ループ）
            "-loop", "1",
            "-i", str(background_image),
            
            # 入力2: メイン音声
            "-i", str(audio_file),
            
            # 入力3: BGM（無限ループ）
            "-stream_loop", "-1",
            "-i", str(bgm_file),
            
            # フィルター
            "-filter_complex", filter_complex,
            
            # 出力マッピング
            "-map", "[vout]",
            "-map", "[aout]",
            
            # ビデオコーデック設定（GPU/CPU切り替え）
            "-c:v", video_codec,
            *codec_params,  # プリセットと品質設定を展開
            "-r", str(fps),
            "-pix_fmt", "yuv420p",  # 互換性のため必須
            
            # オーディオコーデック設定
            "-c:a", self.video_config.output_audio_codec,
            "-b:a", self.video_config.output_audio_bitrate,
            
            # 音声トラック終了で停止
            "-shortest",
            
            # 出力ファイル
            str(output_path)
        ]
        
        return cmd
