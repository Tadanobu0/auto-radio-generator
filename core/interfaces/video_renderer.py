"""動画生成インターフェース"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from core.models import AppConfig
from core.interfaces.audio_synthesizer import ChapterMarker, SynthesisResult


@dataclass
class RenderResult:
    """動画生成の結果"""
    video_path: Path          # 生成された動画ファイルパス
    duration_sec: float       # 動画の長さ（秒）
    file_size_mb: float       # ファイルサイズ（MB）
    segment_bg_generation_time: float = 0.0  # セグメント背景生成時間（秒）
    # タイムライン計算での警告（visuals を捨てた等）。result.json の warnings に載せる（013）
    warnings: list[str] = field(default_factory=list)


class IVideoRenderer(ABC):
    """動画生成の抽象インターフェース
    
    将来的なレンダリング方法の変更に備えて
    抽象クラスとして定義。
    """
    
    def __init__(self, config: AppConfig):
        """
        Args:
            config: アプリケーション設定
        """
        self.config = config
    
    @abstractmethod
    async def render(
        self,
        synthesis_result: SynthesisResult,
        background_image: Path,
        bgm_file: Path,
        output_path: Path,
        subtitle_path: Path | None = None,
        chapters: list[ChapterMarker] | None = None,
    ) -> RenderResult:
        """動画を生成する
        
        Args:
            synthesis_result: 音声合成の結果
            background_image: 背景画像パス
            bgm_file: BGMファイルパス
            output_path: 出力動画パス
            subtitle_path: 字幕ファイルパス（オプショナル）
            chapters: チャプターマーカー（オプショナル）
        
        Returns:
            RenderResult: 生成結果
        
        Raises:
            VideoRenderError: 生成に失敗した場合
        """
        pass
    
    @abstractmethod
    def check_ffmpeg_available(self) -> bool:
        """FFmpegが利用可能か確認する
        
        Returns:
            bool: FFmpegが利用可能な場合True
        """
        pass
