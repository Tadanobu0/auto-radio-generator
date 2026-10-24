"""音声合成インターフェース"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from core.models import Script, AppConfig


@dataclass
class ChapterMarker:
    """YouTubeチャプター用のマーカー"""
    start_time_sec: float     # 開始時間（秒）
    title: str                # チャプタータイトル
    section_id: str           # セクションID（例: 'intro', 'news_1'）


@dataclass
class SegmentTiming:
    """Segment-level audio timing information
    
    Used to track when each script segment starts/ends in the synthesized audio.
    This enables segment-based background image switching and jingle insertion.
    """
    segment_id: str                    # Segment ID (e.g., "intro", "deep_dive_1")
    segment_type: str                  # "intro" | "deep_dive" | "conclusion"
    topic_title: Optional[str]         # Topic title for deep_dive segments
    start_sec: float                   # Segment start time in audio
    end_sec: float                     # Segment end time in audio
    duration_sec: float                # Segment duration
    jingle_path: Optional[Path] = None # Jingle file selected by VoicevoxClient (single source of truth)
    jingle_duration: Optional[float] = None  # Jingle duration in seconds
    # Start time (sec, same clock as start_sec) of each turn in the segment, in turn order.
    # Empty when unknown (estimated timing, empty segment). Used to cut scenes at turn starts.
    turn_start_secs: list[float] = field(default_factory=list)


@dataclass
class SynthesisResult:
    """音声合成の結果"""
    audio_path: Path          # 結合された音声ファイルパス
    subtitle_path: Path       # SRT字幕ファイルパス
    total_duration_sec: float # 総再生時間（秒）
    chapters: list[ChapterMarker] = field(default_factory=list)  # YouTubeチャプター情報
    segment_timings: list[SegmentTiming] = field(default_factory=list)  # セグメント単位のタイミング情報


class IAudioSynthesizer(ABC):
    """音声合成の抽象インターフェース
    
    将来的なエンジン変更（ElevenLabs, Azure TTS等）に備えて
    抽象クラスとして定義。
    """
    
    def __init__(self, config: AppConfig):
        """
        Args:
            config: アプリケーション設定
        """
        self.config = config
    
    @abstractmethod
    async def synthesize(self, script: Script, output_dir: Path) -> SynthesisResult:
        """台本から音声を合成する
        
        Args:
            script: 台本データ
            output_dir: 出力ディレクトリ
        
        Returns:
            SynthesisResult: 合成結果（音声パス、字幕パス、総時間）
        
        Raises:
            AudioSynthesisError: 合成に失敗した場合
        """
        pass
    
    @abstractmethod
    async def check_engine_status(self) -> bool:
        """音声合成エンジンの状態を確認する
        
        Returns:
            bool: エンジンが利用可能な場合True
        """
        pass
