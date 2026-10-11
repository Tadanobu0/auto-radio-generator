"""VOICEVOX APIを使用した音声合成クライアント"""
import asyncio
import re
import shutil
import wave
from io import BytesIO
from pathlib import Path
from datetime import timedelta
from typing import Optional

import httpx
from pydub import AudioSegment
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from core.interfaces import IAudioSynthesizer, SynthesisResult, ChapterMarker
from core.models import Script, AppConfig
from services.media_processing import JingleProvider
from .voicevox_segment_timing import calculate_segment_timings
from .subtitle_layout import layout_turn

console = Console()

# verified_script.json の turn テキストに残る引用タグ ([src=3] 等) は
# fact_checker / research 用の出典参照であり、VOICEVOX に読ませると
# 「ブラケットエスアールイコール…」と読み上げてしまう。TTS 直前で除去する。
# verified_script.json 本体は出典トレーサビリティのため変更しない。
_CITATION_TAG_PATTERN = re.compile(r"\s*\[src=\d+\]")


def strip_citation_tags(text: str) -> str:
    """[src=N] 形式の引用タグを (前後空白ごと) 除去して返す。

    Args:
        text: 元テキスト (verified_script.json 由来の turn テキスト等)

    Returns:
        引用タグを除去し、前後の余分な空白を trim した文字列。
    """
    return _CITATION_TAG_PATTERN.sub("", text).strip()


class VoicevoxClient(IAudioSynthesizer):
    """VOICEVOX Local APIを使用した音声合成
    
    ローカルで起動しているVOICEVOXエンジンに接続します。
    """
    
    def __init__(self, config: AppConfig):
        super().__init__(config)
        self.base_url = config.env.voicevox_base_url
        self.speakers = config.yaml.audio_synthesizer.speakers
        self.audio_config = config.yaml.audio_synthesizer
        
        # Jingle provider for dynamic pause calculation
        self.jingle_provider = JingleProvider()
        video_config = getattr(config.yaml, "video_renderer", None)
        self.enable_jingles = getattr(video_config, "enable_jingles", True) if video_config else True
        self.jingle_overlap_sec = getattr(video_config, "jingle_overlap_sec", 1.0) if video_config else 1.0
        self.pre_jingle_pause_sec = getattr(video_config, "pre_jingle_pause_sec", 0.5) if video_config else 0.5
    
    async def check_engine_status(self) -> bool:
        """VOICEVOXエンジンの状態を確認"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(f"{self.base_url}/version", timeout=5.0)
                if response.status_code == 200:
                    version = response.text.strip('"')
                    console.print(f"[green]✓ VOICEVOX エンジン接続成功[/green] v{version}")
                    return True
        except Exception as e:
            console.print(f"[red]✗ VOICEVOX エンジンに接続できません: {e}[/red]")
            console.print(f"[yellow]  → {self.base_url} でエンジンが起動しているか確認してください[/yellow]")
        return False
    
    async def synthesize(
        self,
        script: Script,
        output_dir: Path,
        speed_scale_override: Optional[float] = None,
        segments: Optional[list] = None
    ) -> SynthesisResult:
        """台本から音声を合成
        
        Args:
            script: 台本データ
            output_dir: 出力ディレクトリ
            speed_scale_override: UIからの話速指定（優先される）
            segments: スクリプトセグメント（セグメント単位のタイミング計算用）
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Mock Mode Check
        mock_mode = self.config.yaml.dev.mock_mode if hasattr(self.config.yaml, 'dev') else False
        if mock_mode:
            mock_data_path = self.config.yaml.dev.mock_data_path if hasattr(self.config.yaml.dev, 'mock_data_path') else "tests/mock_data"
            mock_audio_file = Path(mock_data_path) / "audio" / "combined_audio.wav"
            mock_subtitle_file = Path(mock_data_path) / "audio" / "subtitles.ass"
            output_audio_path = output_dir / "combined_audio.wav"
            
            if mock_audio_file.exists():
                console.print(f"[yellow]⚠ MOCK MODE: Using audio from {mock_audio_file}[/yellow]")
                # Mockファイルを出力先にコピー
                shutil.copy(mock_audio_file, output_audio_path)
                
                # Mockファイルの情報を取得
                audio_segment = AudioSegment.from_wav(str(mock_audio_file))
                duration_sec = len(audio_segment) / 1000.0
                mock_chapters = self._build_mock_chapters(script, duration_sec, segments=segments)
                subtitle_path = output_dir / "subtitles.ass"

                if mock_subtitle_file.exists():
                    shutil.copy(mock_subtitle_file, subtitle_path)
                    console.print(f"[yellow]  ▶ Mock字幕をコピー: {mock_subtitle_file}[/yellow]")
                else:
                    mock_phrase_data = self._build_mock_phrase_data(script, duration_sec)
                    self._generate_ass(mock_phrase_data, subtitle_path)
                    console.print("[yellow]  ▶ Mock字幕を推定生成しました[/yellow]")
                
                console.print(f"[green]✓ Mock音声を使用しました[/green] ({duration_sec:.1f}秒)")
                console.print(f"[yellow]  ▶ Mockチャプター生成: {len(mock_chapters)}件[/yellow]")
                
                # SynthesisResultを返す（Mockでもチャプターを付与）
                return SynthesisResult(
                    audio_path=output_audio_path,
                    subtitle_path=subtitle_path,
                    total_duration_sec=duration_sec,  # 正しいフィールド名
                    chapters=mock_chapters
                )
            else:
                console.print(f"[red]✗ Mock audio not found at {mock_audio_file}[/red]")
                console.print(f"[yellow]  Falling back to normal VOICEVOX synthesis...[/yellow]")
        
        # UIからの指定がconfigより優先
        speed_scale = speed_scale_override if speed_scale_override is not None else self.audio_config.speed_scale
        
        # 一時ディレクトリ
        temp_dir = output_dir / "temp_phrases"
        temp_dir.mkdir(exist_ok=True)
        
        phrase_data = []  # (音声セグメント, 開始時間, 終了時間, テキスト, 話者ID)
        chapters: list[ChapterMarker] = []  # YouTubeチャプター情報
        current_time_ms = 0
        pause_ms = self.audio_config.pause_between_phrases_ms

        # 2026-05-06: dialogue index → ScriptSegment マップを構築。
        # deep_dive_N の topic_title 引き当て + segment_id ベースの chapter dedup に使う。
        # 不一致 / segments=None なら [] が返り、従来の line.section ベース動作になる。
        seg_for_idx = self._build_segment_index_map(script, segments)
        last_segment_id: Optional[str] = None

        console.print(f"[cyan]音声合成中...[/cyan] {len(script.get_dialogue_only())} フレーズ")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console
        ) as progress:
            task = progress.add_task("合成中...", total=len(script.get_dialogue_only()))

            async with httpx.AsyncClient(timeout=60.0) as client:
                for i, line in enumerate(script.get_dialogue_only()):
                    # セクション開始を検出してチャプターを記録
                    # Note: 冒頭に2秒の無音が追加されるため、チャプタータイムスタンプを2秒オフセット
                    if line.section:
                        # segment アクセス（segments が無い経路では None）
                        current_seg = seg_for_idx[i] if i < len(seg_for_idx) else None
                        topic_title = getattr(current_seg, "topic_title", None) if current_seg else None
                        # dedup キー: segment_id 優先（同一 section が複数 deep_dive を跨ぐ問題対策）
                        dedup_key = (
                            getattr(current_seg, "segment_id", None) if current_seg else None
                        ) or line.section

                        if dedup_key != last_segment_id:
                            chapter_title = self._get_chapter_title(
                                line.section,
                                line.text,
                                line.chapter_title,
                                topic_title=topic_title,
                            )
                            pre_roll_offset_sec = 2.0  # 冒頭の無音時間（秒）
                            adjusted_time_sec = (current_time_ms / 1000.0) + pre_roll_offset_sec
                            chapters.append(ChapterMarker(
                                start_time_sec=adjusted_time_sec,
                                title=chapter_title,
                                section_id=line.section
                            ))
                            console.print(f"[yellow]  ▶ チャプター: {chapter_title} @ {adjusted_time_sec:.1f}s[/yellow]")
                            last_segment_id = dedup_key
                    
                    # 話者IDからVOICEVOX speaker_idを取得
                    # line.speaker は "A" または "B"
                    voicevox_speaker_id = self.speakers.main if line.speaker == "A" else self.speakers.sub
                    
                    # デバッグ: 話者情報を表示
                    console.print(f"[dim]  [{i+1}] {line.speaker} → VOICEVOX ID: {voicevox_speaker_id}[/dim]")
                    
                    # 音声合成
                    audio_data = await self._synthesize_phrase(
                        client, line.text, voicevox_speaker_id, speed_scale
                    )
                    
                    # AudioSegmentに変換
                    audio_segment = AudioSegment.from_wav(BytesIO(audio_data))
                    duration_ms = len(audio_segment)
                    
                    # タイミング情報を記録（話者IDも含める）
                    start_time = current_time_ms
                    end_time = current_time_ms + duration_ms
                    phrase_data.append((audio_segment, start_time, end_time, line.text, line.speaker))
                    
                    current_time_ms = end_time + pause_ms
                    progress.update(task, advance=1, description=f"合成中... {i+1}/{len(script.get_dialogue_only())}")
        
        # セグメント間ポーズを計算
        segment_pauses = self._calculate_segment_pauses(segments)
        
        # 音声を結合（セグメント間にポーズを挿入し、タイムスタンプを調整）
        console.print("[cyan]音声ファイルを結合中...[/cyan]")
        combined_audio, adjusted_phrase_data = self._combine_audio_with_pauses(
            phrase_data, pause_ms, segment_pauses, segments
        )
        
        # 冒頭と末尾に無音を追加（演出強化）
        console.print("[cyan]冒頭・末尾に無音を追加中...[/cyan]")
        pre_roll = AudioSegment.silent(duration=2000)   # 冒頭2秒の無音
        post_roll = AudioSegment.silent(duration=5000)  # 末尾5秒の無音
        combined_audio = pre_roll + combined_audio + post_roll
        
        # 音声ファイルを保存
        audio_path = output_dir / "combined_audio.wav"
        combined_audio.export(str(audio_path), format="wav")
        
        # ASS字幕を生成（調整済みタイムスタンプを使用）
        subtitle_path = output_dir / "subtitles.ass"
        self._generate_ass(adjusted_phrase_data, subtitle_path)
        
        # チャプターマーカーを生成（調整済みタイムスタンプを使用）
        chapters = self._build_chapters(adjusted_phrase_data, script, segments=segments)
        
        # セグメント単位のタイミング情報を計算（調整済みタイムスタンプを使用）
        # segment_pausesを渡してジングル情報をSegmentTimingに含める
        segment_timings = calculate_segment_timings(script, segments, adjusted_phrase_data, segment_pauses) if segments else []
        
        total_duration_sec = len(combined_audio) / 1000.0
        
        console.print(f"[green]✓ 音声合成完了[/green] {audio_path.name}")
        console.print(f"  → 長さ: {total_duration_sec:.1f}秒, フレーズ数: {len(phrase_data)}")
        if segment_timings:
            console.print(f"  → セグメント数: {len(segment_timings)}")
        
        return SynthesisResult(
            audio_path=audio_path,
            subtitle_path=subtitle_path,
            total_duration_sec=total_duration_sec,
            chapters=chapters,
            segment_timings=segment_timings
        )
    
    async def _synthesize_phrase(
        self,
        client: httpx.AsyncClient,
        text: str,
        speaker_id: int,
        speed_scale: float
    ) -> bytes:
        """１フレーズの音声を合成"""
        # verified_script.json の [src=N] 引用タグは音読対象外。VOICEVOX 直前で除去。
        text = strip_citation_tags(text)

        # 音声クエリを生成
        query_response = await client.post(
            f"{self.base_url}/audio_query",
            params={"text": text, "speaker": speaker_id}
        )
        query_response.raise_for_status()
        query = query_response.json()
        
        # 音声パラメータを設定（speed_scaleは引数から受け取る）
        query["speedScale"] = speed_scale
        query["pitchScale"] = self.audio_config.pitch_scale
        query["intonationScale"] = self.audio_config.intonation_scale
        query["volumeScale"] = self.audio_config.volume_scale
        
        # 音声を合成
        synthesis_response = await client.post(
            f"{self.base_url}/synthesis",
            params={"speaker": speaker_id},
            json=query
        )
        synthesis_response.raise_for_status()
        
        return synthesis_response.content
    
    def _calculate_segment_pauses(self, segments: Optional[list]) -> list[tuple[float, Optional[Path], Optional[float]]]:
        """Calculate pause duration after each segment based on jingle length
        
        Pause structure: [pre-jingle pause] + [jingle duration]
        - Pre-jingle pause: A brief breathing space before jingle starts (e.g., 0.5s)
        - Jingle duration: Full jingle playback time
        
        Note: Returns a list indexed by segment position (not segment_id) to correctly
        handle duplicate segment_ids (e.g., multiple "deep_dive" sections from Gemini).
        
        Args:
            segments: List of ScriptSegment objects
        
        Returns:
            list[(pause_sec, jingle_path, jingle_duration)]: One entry per segment.
            The last segment always has (0.0, None, None) since no jingle plays after it.
            Length equals len(segments).
        """
        # Return empty list for invalid inputs
        if not segments:
            return []
        
        # Initialize with (0.0, None, None) for all segments
        pauses: list[tuple[float, Optional[Path], Optional[float]]] = [
            (0.0, None, None) for _ in segments
        ]
        
        if not self.enable_jingles or not self.jingle_provider.is_available():
            return pauses
        
        # Calculate pause for each segment (except the last one).
        # Skip empty segments (num_turns == 0) defensively: there is no spoken
        # content before which the jingle would play, so injecting a jingle here
        # would desynchronize audio and visual timelines.
        for i, segment in enumerate(segments[:-1]):
            if len(segment.turns) == 0:
                console.print(
                    f"[dim]  Segment[{i}] {segment.segment_id}: empty (num_turns=0), skip jingle[/dim]"
                )
                continue
            # Select random jingle and get its duration
            jingle_path = self.jingle_provider.get_random_jingle()
            if jingle_path:
                jingle_duration = self.jingle_provider.get_jingle_duration(jingle_path)
                # Pause = pre-jingle pause + full jingle duration (no overlap, perfect sync)
                pause_sec = self.pre_jingle_pause_sec + jingle_duration
                pauses[i] = (pause_sec, jingle_path, jingle_duration)
                console.print(
                    f"[dim]  Segment[{i}] {segment.segment_id}: jingle={jingle_path.name} "
                    f"({jingle_duration:.2f}s), pre-pause={self.pre_jingle_pause_sec:.2f}s, "
                    f"total_pause={pause_sec:.2f}s[/dim]"
                )
        
        return pauses
    
    def _combine_audio_with_pauses(
        self,
        phrase_data: list,
        pause_ms: int,
        segment_pauses: list[tuple[float, Optional[Path], Optional[float]]],
        segments: Optional[list]
    ) -> tuple[AudioSegment, list]:
        """Combine audio with dynamic segment pauses and adjust phrase timestamps
        
        Args:
            phrase_data: List of (audio_segment, start_ms, end_ms, text, speaker) tuples
            pause_ms: Pause between individual phrases (250ms)
            segment_pauses: List of (pause_sec, jingle_path, jingle_duration) indexed by segment position
            segments: List of ScriptSegment objects
        
        Returns:
            tuple: (combined_audio, adjusted_phrase_data with offset timestamps)
        """
        if not phrase_data:
            return AudioSegment.silent(duration=1000), []
        
        if not segments:
            # Legacy mode: no segments, use original _combine_audio logic
            pause = AudioSegment.silent(duration=pause_ms)
            combined = phrase_data[0][0]
            for item in phrase_data[1:]:
                segment = item[0]
                combined += pause + segment
            return combined, phrase_data
        
        # New mode: segment-based with dynamic pauses
        combined = AudioSegment.silent(duration=0)
        adjusted_phrase_data = []
        phrase_index = 0
        cumulative_offset_ms = 0  # Cumulative offset from inserted pauses
        
        pause = AudioSegment.silent(duration=pause_ms)
        
        for seg_idx, segment in enumerate(segments):
            # Combine phrases for this segment
            num_turns = len(segment.turns)
            for _ in range(num_turns):
                if phrase_index >= len(phrase_data):
                    break
                
                audio_seg, orig_start_ms, orig_end_ms, text, speaker = phrase_data[phrase_index]
                
                # Adjust timestamps with cumulative offset
                adjusted_start_ms = orig_start_ms + cumulative_offset_ms
                adjusted_end_ms = orig_end_ms + cumulative_offset_ms
                
                adjusted_phrase_data.append((
                    audio_seg,
                    adjusted_start_ms,
                    adjusted_end_ms,
                    text,
                    speaker
                ))
                
                combined += audio_seg + pause
                phrase_index += 1
            
            # Insert pause after segment (if not last segment)
            # Use index-based access to handle duplicate segment_ids correctly
            if seg_idx < len(segment_pauses):
                pause_sec, jingle_path, jingle_duration = segment_pauses[seg_idx]
                if pause_sec > 0:
                    pause_ms_seg = int(pause_sec * 1000)
                    combined += AudioSegment.silent(duration=pause_ms_seg)
                    cumulative_offset_ms += pause_ms_seg
                    
                    jingle_name = jingle_path.name if jingle_path is not None else 'None'
                    jingle_dur_str = f"{jingle_duration:.2f}s" if jingle_duration is not None else "0s"
                    console.print(
                        f"[yellow]  → Inserted {pause_sec:.2f}s pause after segment[{seg_idx}] {segment.segment_id} "
                        f"(jingle: {jingle_name}, duration: {jingle_dur_str}), "
                        f"cumulative offset: {cumulative_offset_ms/1000:.2f}s[/yellow]"
                    )
        
        return combined, adjusted_phrase_data
    
    def _combine_audio(
        self,
        phrase_data: list,
        pause_ms: int
    ) -> AudioSegment:
        """音声セグメントを結合（レガシーモード用）"""
        if not phrase_data:
            return AudioSegment.silent(duration=1000)
        
        pause = AudioSegment.silent(duration=pause_ms)
        combined = phrase_data[0][0]
        for item in phrase_data[1:]:
            segment = item[0]
            combined += pause + segment
        
        return combined
    
    def _generate_ass(self, phrase_data: list, output_path: Path) -> None:
        """ASS形式の字幕ファイルを生成（話者ごとに色分け）
        
        Note: 冒頭に2秒の無音が追加されているため、字幕タイミングを2秒オフセット
        """
        pre_roll_offset_ms = 2000  # 冒頭の無音時間
        
        # ASSヘッダー
        header = """[Script Info]
Title: Auto Radio Subtitles
ScriptType: v4.00+
WrapStyle: 2
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Main,Meiryo,105,&H0055FF55,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,6,2,2,80,80,30,1
Style: Sub,Meiryo,105,&H00FFFFFF,&H000000FF,&H00CC99FF,&H80000000,0,0,0,0,100,100,0,0,1,6,2,2,80,80,30,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        
        lines = [header]
        
        for _, start_ms, end_ms, text, speaker in phrase_data:
            # 冒頭の無音分をオフセット
            adjusted_start_ms = start_ms + pre_roll_offset_ms
            adjusted_end_ms = end_ms + pre_roll_offset_ms
            
            # 話者IDに応じてスタイルを選択
            style = "Main" if speaker == "A" else "Sub"
            
            # 表示層: [src=N] を除去し、実フォントの描画幅で折り返す。安全域と行数上限に
            # 収まらないターンは連続キューに分け、発話区間を文字数比で配る。
            for cue in layout_turn(text, adjusted_start_ms, adjusted_end_ms):
                start_time = self._ms_to_ass_time(cue.start_ms)
                end_time = self._ms_to_ass_time(cue.end_ms)
                wrapped_text = "\\N".join(cue.lines)
                lines.append(f"Dialogue: 0,{start_time},{end_time},{style},,0,0,0,,{wrapped_text}\n")
        
        output_path.write_text("".join(lines), encoding="utf-8")
    
    @staticmethod
    def _wrap_subtitle_budoux(chunks: list[str], max_chars: int = 28) -> str:
        """文節区切りされたチャンクを指定文字数で折り返す
        
        Args:
            chunks: BudouXで分割された文節リスト
            max_chars: 1行あたりの最大文字数
        
        Returns:
            str: ASS形式の改行コード（\\N）で区切られたテキスト
        """
        lines = []
        current_line = ""
        
        for chunk in chunks:
            test_line = current_line + chunk
            if len(test_line) <= max_chars or not current_line:
                current_line = test_line
            else:
                lines.append(current_line)
                current_line = chunk
        
        if current_line:
            lines.append(current_line)
        
        return "\\N".join(lines)
    
    @staticmethod
    def _ms_to_ass_time(ms: int) -> str:
        """ミリ秒をASS形式の時間文字列に変換"""
        td = timedelta(milliseconds=ms)
        total_seconds = int(td.total_seconds())
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60
        seconds = total_seconds % 60
        centiseconds = (ms % 1000) // 10
        return f"{hours:01d}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"

    def _build_mock_chapters(
        self,
        script: Script,
        total_duration_sec: float,
        segments: Optional[list] = None,
    ) -> list[ChapterMarker]:
        """Mockモード用に台本のsection情報からチャプター時刻を推定する

        2026-05-06: segments があれば topic_title をチャプター名に反映する。
        """
        dialogue = script.get_dialogue_only()
        if not dialogue:
            return []

        pre_roll_offset_sec = 2.0
        total_lines = len(dialogue)
        timeline_span_sec = max(1.0, float(total_duration_sec) - pre_roll_offset_sec)
        mock_chapters: list[ChapterMarker] = []
        seg_for_idx = self._build_segment_index_map(script, segments)

        for index, line in enumerate(dialogue):
            if not line.section:
                continue

            current_seg = seg_for_idx[index] if index < len(seg_for_idx) else None
            topic_title = getattr(current_seg, "topic_title", None) if current_seg else None

            chapter_title = self._get_chapter_title(
                line.section,
                line.text,
                line.chapter_title,
                topic_title=topic_title,
            )
            position_ratio = index / max(1, total_lines - 1)
            estimated_start = pre_roll_offset_sec + (timeline_span_sec * position_ratio)
            estimated_start = min(max(0.0, estimated_start), max(0.0, total_duration_sec - 0.1))

            mock_chapters.append(
                ChapterMarker(
                    start_time_sec=estimated_start,
                    title=chapter_title,
                    section_id=line.section,
                )
            )

        return mock_chapters

    def _build_mock_phrase_data(self, script: Script, total_duration_sec: float) -> list[tuple]:
        """Mockモード用にASS字幕生成のための擬似タイミングを作成する"""
        dialogue = script.get_dialogue_only()
        if not dialogue:
            return []

        pre_roll_ms = 2000
        post_roll_ms = 5000
        total_ms = max(1000, int(total_duration_sec * 1000))
        timeline_ms = max(1000, total_ms - pre_roll_ms - post_roll_ms)
        total_weight = sum(max(1, len((line.text or "").strip())) for line in dialogue)

        phrase_data: list[tuple] = []
        current_ms = 0
        pause_ms = 250

        for idx, line in enumerate(dialogue):
            remaining_weight = sum(
                max(1, len((item.text or "").strip())) for item in dialogue[idx:]
            )
            if idx == len(dialogue) - 1:
                duration_ms = max(600, timeline_ms - current_ms)
            else:
                weight = max(1, len((line.text or "").strip()))
                duration_ms = max(600, int((timeline_ms * weight) / max(1, total_weight)))
                max_allowed = max(600, timeline_ms - current_ms - (len(dialogue) - idx - 1) * 600)
                duration_ms = min(duration_ms, max_allowed)

            start_ms = max(0, current_ms)
            end_ms = min(timeline_ms, start_ms + duration_ms)
            if end_ms <= start_ms:
                end_ms = min(timeline_ms, start_ms + 600)

            phrase_data.append(
                (
                    AudioSegment.silent(duration=max(1, end_ms - start_ms)),
                    start_ms,
                    end_ms,
                    line.text,
                    line.speaker,
                )
            )

            current_ms = end_ms + pause_ms
            if current_ms >= timeline_ms:
                break

        return phrase_data
    
    def _build_chapters(
        self,
        phrase_data: list,
        script: Script,
        segments: Optional[list] = None,
    ) -> list[ChapterMarker]:
        """Build chapter markers from phrase data and script sections

        2026-05-06: segments を受け取り、deep_dive_N の各セグメントに紐づく
        topic_title をチャプタータイトルとして使う。同一 section 値（例:
        "deep_dive"）が複数 deep_dive 区間で使われる本運用構成では、
        line.section 単位の dedup だと全 deep_dive がひとつのチャプターに
        畳み込まれてしまうため、segment_id 単位で dedup する。

        Args:
            phrase_data: List of (audio_segment, start_ms, end_ms, text, speaker) tuples
            script: Script object with section information
            segments: Optional ScriptSegment list（topic_title 引き当て用）。
                      None / 不一致時は line.section ベースの従来動作にフォールバック。

        Returns:
            List of ChapterMarker objects
        """
        if not phrase_data or not script:
            return []

        pre_roll_offset_ms = 2000  # Pre-roll silence added to audio
        chapters: list[ChapterMarker] = []
        dialogue = script.get_dialogue_only()
        seg_for_idx = self._build_segment_index_map(script, segments)

        # Map dialogue lines to phrase data by index
        last_dedup_key: Optional[str] = None
        for idx, line in enumerate(dialogue):
            if not line.section or idx >= len(phrase_data):
                continue

            # Get timing from phrase_data
            _, start_ms, _, text, _ = phrase_data[idx]
            start_sec = (start_ms + pre_roll_offset_ms) / 1000.0

            # segment（あれば）から topic_title と segment_id を取得
            current_seg = seg_for_idx[idx] if idx < len(seg_for_idx) else None
            topic_title = getattr(current_seg, "topic_title", None) if current_seg else None
            dedup_key = (
                getattr(current_seg, "segment_id", None) if current_seg else None
            ) or line.section

            # Generate chapter title（topic_title を渡して deep_dive を実タイトルに）
            chapter_title = self._get_chapter_title(
                line.section,
                line.text,
                line.chapter_title,
                topic_title=topic_title,
            )

            # 新しいセグメント / セクションに入った時のみチャプター追加
            if dedup_key != last_dedup_key:
                chapters.append(
                    ChapterMarker(
                        start_time_sec=start_sec,
                        title=chapter_title,
                        section_id=line.section,
                    )
                )
                last_dedup_key = dedup_key

        return chapters
    
    def _get_chapter_title(
        self,
        section_id: str,
        text: str,
        chapter_title: Optional[str] = None,
        topic_title: Optional[str] = None,
    ) -> str:
        """セクションIDからチャプタータイトルを生成

        2026-05-06: 内部 segment_type 名（"deep_dive" 等）が視聴者向けチャプター
        にそのまま漏れていた問題への対策。優先順位を以下に整理:
          1. AI 生成の DialogueTurn.chapter_title（個別タイトル）
          2. 視聴者向け変換ルール（intro→「はじめに」/ deep_dive→トピック名 / conclusion→「まとめ」）
          3. 固定マッピング（後方互換）
          4. section_id そのまま

        Args:
            section_id: セクションID (例: 'intro', 'deep_dive', 'deep_dive_1')
            text: セリフテキスト
            chapter_title: AI生成のチャプタータイトル（最優先）
            topic_title: deep_dive 系セグメントに紐づくトピックタイトル
                         （ScriptSegment.topic_title / CuratedTopic.title 由来）

        Returns:
            チャプタータイトル文字列
        """
        # 優先度1: AI生成のchapter_titleがあれば使用（個別タイトルが付いている場合）
        if chapter_title and chapter_title.strip():
            return chapter_title.strip()

        sid = (section_id or "").strip().lower()

        # 優先度2: 視聴者向け変換ルール
        # intro / オープニング系 → 「はじめに」
        if sid == "intro" or sid.startswith("intro_") or sid in ("opening", "オープニング"):
            return "はじめに"
        # conclusion / まとめ / ending 系 → 「まとめ」
        if (
            sid == "conclusion"
            or sid.startswith("conclusion_")
            or sid in ("ending", "まとめ", "closing")
        ):
            return "まとめ"
        # deep_dive 系 → トピックタイトル（取得できれば）
        if sid == "deep_dive" or sid.startswith("deep_dive"):
            if topic_title and topic_title.strip():
                return topic_title.strip()
            # topic_title が無い場合は legacy 固定マッピングへフォールスルー

        # 優先度3: 固定マッピング（後方互換性のため）
        section_titles = {
            "intro": "オープニング",
            "definition": "基礎解説",
            "metaphor": "たとえで理解",
            "example": "活用例",
            "main": "本題",
            "deep_dive_1": "深掘り1",
            "deep_dive_2": "深掘り2",
            "deep_dive_3": "深掘り3",
            "news_1": "ニュース1",
            "news_2": "ニュース2",
            "news_3": "ニュース3",
            "listener_mail": "リスナーメール",
            "conclusion": "まとめ",
            "ending": "エンディング",
        }

        base_title = section_titles.get(section_id, section_id)

        # news_N の場合、セリフから見出しを抽出（最初の30文字まで）
        if section_id.startswith("news_"):
            headline = text[:30].replace('\n', ' ')
            if len(text) > 30:
                headline += "..."
            return f"{base_title}: {headline}"

        # 優先度4: section_id そのまま（base_title）
        return base_title

    @staticmethod
    def _build_segment_index_map(script: Script, segments: Optional[list]) -> list:
        """dialogue_only の各 index に対応する ScriptSegment を引けるリストを構築

        ScriptSegment.turns を順に消費し、dialogue 配列の i 番目の line がどの
        segment に属するかを O(N) で決める。segments が None / 空 / 総ターン数が
        合わない場合は空リストを返し、呼び出し側は従来動作（line.section 単位の
        chapter 生成）にフォールバックする。

        Args:
            script: 音声合成対象の Script
            segments: ScriptSegment のリスト（orchestrator 出力）

        Returns:
            list: index → ScriptSegment のマップ。サイズ不一致なら []
        """
        if not segments:
            return []
        dialogue = script.get_dialogue_only()
        seg_for_idx: list = []
        for seg in segments:
            turns_attr = getattr(seg, "turns", None) or []
            for _ in turns_attr:
                seg_for_idx.append(seg)
        # 不一致なら捨てる（フェイルセーフ: 従来動作にフォールバック）
        if len(seg_for_idx) != len(dialogue):
            return []
        return seg_for_idx
