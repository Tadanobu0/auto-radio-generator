"""自動ラジオ動画生成システム - 共通ワークフロー関数

このモジュールは、CLIとWeb UI両方から呼び出せる
動画生成ワークフローを提供します。
"""
import asyncio
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, List
from urllib.parse import parse_qs, urlparse

# プロジェクトルートをパスに追加
PROJECT_ROOT = Path(__file__).parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.models import (
    load_config, Script, AppConfig,
    TotalUsage, PerplexityUsage, LLMUsage, VoicevoxUsage, CostBreakdown,
    ExecutionLogEntry, PromptRecord, ConfigSnapshot, CostLogEntry
)
from core.models.script import DialogueTurn  # 追加インポート
from core.models.visual import VisualIdentity  # Issue #7 fix: Proper type import
from core.models.execution_context import ExecutionContext
from core.interfaces import ResearchMode, ChapterMarker, ResearchResult
from services.script_generation.orchestrator import ScriptOrchestrator
from services.research import PerplexityResearcher
from services.audio_synthesis import VoicevoxClient
from services.video_rendering import FfmpegRenderer
from services.media_processing import ThumbnailGenerator, ThumbnailBackgroundGenerator
from services.cost_calculator import CostCalculator
from services.publishing import YouTubeClient, build_video_description
from services.publishing.metadata_builder import (
    MEDICAL_DISCLAIMER,
    format_reference_text_lines,
    normalize_hashtags,
    strip_urls,
)
from services.publishing.text_sanitizer import validate_url, normalize_url
from core.models.research import ResearchSource
from services.execution_logger import ExecutionLogger

# アプリケーションバージョン
APP_VERSION = "v3.6.1"


ReferenceEntry = str | ResearchSource


@dataclass
class UIOverrides:
    """UIから渡されるパラメータのオーバーライド設定"""
    research_mode: Optional[ResearchMode] = None  # "debate", "voices", "trivia"
    enable_research: bool = True                   # リサーチを有効化
    llm_provider: Optional[str] = None             # LLMプロバイダー ("gemini" | "openai" | "anthropic")
    bgm_volume: Optional[float] = None             # 0.0 - 0.5
    fade_in_sec: Optional[float] = None            # 1.0 - 10.0
    fade_out_sec: Optional[float] = None           # 1.0 - 10.0
    enable_spectrum: Optional[bool] = None         # スペクトラム表示
    speed_scale: Optional[float] = None            # 音声スピード (0.8 - 1.5)
    # 素材選択
    background_image: Optional[str] = None         # 背景画像ファイル名
    bgm_file: Optional[str] = None                 # BGMファイル名


@dataclass
class ThumbnailRegenerationState:
    """サムネイル再作成に必要なコンテキスト"""
    theme: str                          # 元のテーマ
    script_summary: str                 # 台本要約（Geminiプロンプト用）
    output_dir: str                     # 出力先ディレクトリ
    background_path: str                 # 背景画像パス
    base_title: str                     # 元の動画タイトル
    generation_count: int = 0           # 再生成回数（管理用）


@dataclass
class WorkflowResult:
    """ワークフロー実行結果"""
    success: bool
    video_path: Optional[Path] = None
    script: Optional[Script] = None
    audio_path: Optional[Path] = None
    subtitle_path: Optional[Path] = None
    duration_sec: float = 0.0
    file_size_mb: float = 0.0
    error_message: Optional[str] = None
    # 使用量・コスト情報
    usage: Optional[TotalUsage] = None
    cost: Optional[CostBreakdown] = None
    cost_report: str = ""
    # メタデータ情報
    metadata_content: str = ""
    formatted_title: str = ""  # 日付入りタイトル（コピー用）
    formatted_description: str = ""  # 概要欄・チャプター結合版（コピー用）
    uploaded_video_url: Optional[str] = None  # YouTubeアップロードURL（成功時）
    # サムネイル再作成用
    theme: str = ""  # 元のテーマ
    output_dir: Optional[Path] = None  # 出力先ディレクトリ
    # 画像生成時間
    segment_bg_generation_time: float = 0.0  # セグメント背景生成時間（秒）
    thumbnail_bg_generation_time: float = 0.0  # サムネイル背景生成時間（秒）
    # Visual identity
    # Issue #7 fix: Proper type annotation instead of Any
    visual_identity: Optional[VisualIdentity] = None  # VisualIdentity for brand consistency


@dataclass
class ScriptingPhaseResult:
    """台本作成フェーズの実行結果"""
    script: Script
    research_content: Optional[str] = None
    research_sources: Optional[list[ResearchSource]] = None
    perplexity_usage: Optional[PerplexityUsage] = None
    gemini_usage: Optional[LLMUsage] = None
    research_duration_sec: float = 0.0
    script_duration_sec: float = 0.0
    segments: Optional[list] = None  # ScriptSegment list for segment-based rendering
    # Issue #7 fix: Proper type annotation instead of Any
    visual_identity: Optional[VisualIdentity] = None  # VisualIdentity for brand consistency


@dataclass
class ProductionPhaseResult:
    """制作フェーズの実行結果"""
    video_path: Path
    audio_path: Path
    subtitle_path: Path
    duration_sec: float
    file_size_mb: float
    chapters: list[ChapterMarker]
    voicevox_usage: VoicevoxUsage
    audio_duration_sec: float = 0.0
    render_duration_sec: float = 0.0
    segment_bg_generation_time: float = 0.0  # セグメント背景生成時間（秒）


def _normalize_non_empty_strings(values: list[str]) -> list[str]:
    """空要素を除去しつつ順序を維持して重複排除する。"""
    seen = set()
    result: list[str] = []
    for value in values:
        text = (value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _to_json_safe(value: Any) -> Any:
    """Pydanticモデル等をJSONシリアライズ可能な形に再帰変換する。"""
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return _to_json_safe(value.model_dump())
    if isinstance(value, dict):
        return {k: _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_to_json_safe(item) for item in value]
    return value


# 責務 #4（チャプター整形）は services/workflow_chapters.py へ抽出。
# 既存の import 経路・シンボル名（workflow._format_chapter_lines /
# workflow._build_metadata_chapter_block）を維持するため再エクスポート。
from services.workflow_chapters import (  # noqa: E402
    _format_chapter_lines,
    _build_metadata_chapter_block,
)


def _resolve_script_for_metadata(output_base, fallback_script, log_fn=None):
    """metadata 生成に使う Script を解決する（FactFix 修正版を優先）。

    2026-05-06: FactChecker / FactFixAgent が script_fixed.json を生成したのに、
    metadata.txt は常に原文 script から生成されていたためハルシネーション内容が
    YouTube 説明文に載ってしまうバグへの対策。SessionManager.has_script_fixed /
    load_script_fixed で出力ディレクトリを参照し、存在すれば FactFix 後の Script を
    採用する。読み込み失敗時はフェイルオープン（fallback_script を返す）。

    Args:
        output_base: 当該実行の出力ディレクトリ（script_fixed.json があるならここ）
        fallback_script: script_fixed.json が無い / 読込失敗時に使う Script
        log_fn: 任意のログ関数（None ならログ出さない）

    Returns:
        Script: metadata 生成に使うべき Script オブジェクト
    """
    from core.session_manager import SessionManager

    log = log_fn or (lambda msg: None)
    try:
        # session_dir 直接指定で SessionManager を構築（既存ディレクトリの再利用、
        # __init__ の mkdir(exist_ok=True) は no-op）
        sm = SessionManager(project_root=PROJECT_ROOT, session_dir=output_base)
        if sm.has_script_fixed():
            try:
                fixed_script = sm.load_script_fixed()
                log(f"✓ FactFix 修正済み台本を metadata 生成に使用 (script_fixed.json)")
                return fixed_script
            except Exception as e:
                # 読込失敗（壊れた JSON 等）はフェイルオープン
                log(f"⚠ script_fixed.json 読込失敗、原文 script で続行: {e}")
                return fallback_script
        else:
            log(f"ℹ script_fixed.json なし、原文 script を metadata に使用")
            return fallback_script
    except Exception as e:
        # SessionManager 構築失敗もフェイルオープン
        log(f"⚠ SessionManager 構築失敗、原文 script で続行: {e}")
        return fallback_script


def _extract_urls(text: str) -> list[str]:
    """テキスト中のURLを抽出する。"""
    if not text:
        return []
    return re.findall(r"https?://[^\s\)\]]+", text)


def _resolve_dynamic_tags(script: Script, fallback_description: str) -> list[str]:
    """台本ハッシュタグを優先し、未設定時は説明文から抽出する。"""
    if script.hashtags:
        return _normalize_non_empty_strings(script.hashtags)
    extracted = re.findall(r"#\S+", fallback_description or "")
    return _normalize_non_empty_strings(extracted)


def _create_script_summary(script: Script, max_turns: int = 5) -> str:
    """台本の要約を作成する（コンテキスト引き継ぎ用）
    
    Args:
        script: 要約する台本
        max_turns: 要約に含める最大ターン数
    
    Returns:
        台本の要約文字列
    """
    # 対話のみを抽出
    dialogues = script.get_dialogue_only()
    
    # 主要なセクションを抽出
    sections = list(set(turn.section for turn in dialogues if turn.section))
    
    # キーワードを抽出（簡易実装）
    keywords = []
    for turn in dialogues[:max_turns]:
        # 簡単なキーワード抽出（名詞などを想定）
        words = turn.text.split()[:10]  # 最初の10語を候補とする
        keywords.extend(words)
    
    # 要約を構築
    summary_parts = []
    
    if sections:
        summary_parts.append(f"セクション: {', '.join(sections)}")
    
    if keywords:
        unique_keywords = list(set(keywords))[:20]  # 重複を除き最大20個
        summary_parts.append(f"キーワード: {', '.join(unique_keywords)}")
    
    # 最初の数ターンを追加
    initial_turns = []
    for turn in dialogues[:max_turns]:
        initial_turns.append(f"[{turn.speaker}] {turn.text[:50]}...")
    
    if initial_turns:
        summary_parts.append(f"初期発話: {' | '.join(initial_turns)}")
    
    return " | ".join(summary_parts)


def _resolve_references(
    script: Script,
    theme: str,
    fallback_description: str,
    research_sources: Optional[list[ResearchSource]] = None,
) -> list[ReferenceEntry]:
    """参考文献リストを解決し、可能ならタイトル付きソースを優先する。
    
    AI Editor方式: Geminiが選択したURLとResearchSourceを突き合わせて、
    選択されたURLのみをタイトル付きで表示する。
    """
    import logging
    logger = logging.getLogger(__name__)
    
    references: list[ReferenceEntry] = []

    # Geminiが選択したURLを取得（優先度最高）
    selected_urls = _normalize_non_empty_strings(list(script.references or []))
    
    if selected_urls and research_sources:
        # AI Editor方式: Geminiが選択したURLに対応するResearchSourceのみを抽出
        url_to_source = {}
        for source in research_sources:
            if source.url and source.url.strip():
                normalized_url = normalize_url(source.url.strip())
                if validate_url(normalized_url):
                    url_to_source[normalized_url] = source
        
        for url in selected_urls:
            normalized_url = normalize_url(url.strip())
            
            if not validate_url(normalized_url):
                logger.warning(f"Geminiから無効なURL: {url}")
                continue
                
            if normalized_url in url_to_source:
                # 選択されたURLに対応するResearchSourceを追加
                references.append(url_to_source[normalized_url])
            else:
                # 選択されたURLがResearchSourceにない場合は文字列として追加
                logger.info(f"Geminiが候補にないURLを選択: {url}")
                references.append(url)
    else:
        # 従来方式: 選択がない場合は全リサーチソースを使用
        for source in research_sources or []:
            url = (source.url or "").strip()
            if url and validate_url(url):
                references.append(source)

        # 台本に含まれるURL文字列を追加
        for url in _normalize_non_empty_strings(list(script.references or [])):
            if validate_url(url):
                references.append(url)

    # 生成概要文から抽出したURLを追加
    for url in _extract_urls(fallback_description or ""):
        if validate_url(url):
            references.append(url)

    # テーマ入力がURLなら追加
    stripped_theme = (theme or "").strip()
    if stripped_theme.startswith(("http://", "https://")) and validate_url(stripped_theme):
        references.append(stripped_theme)

    # URL基準で重複排除（ResearchSourceを優先保持）
    seen_urls: set[str] = set()
    deduped: list[ReferenceEntry] = []
    for ref in references:
        if isinstance(ref, ResearchSource):
            key = normalize_url((ref.url or "").strip())
        else:
            key = normalize_url((ref or "").strip())

        if not key or key in seen_urls:
            continue

        seen_urls.add(key)
        deduped.append(ref)

    return deduped


def _verified_refs_to_research_sources(references) -> list[ResearchSource]:
    """VerifiedScript.metadata.references (SourceRef) を ResearchSource へ変換する。

    外部台本モードで概要欄/metadata.txt の参考文献を
    「タイトル + 日付 + URL」表記にするための橋渡し (2026-07-17)。
    title 欠落は空文字にして metadata_builder 側の「参考文献N」
    フォールバックに委ねる。
    """
    sources: list[ResearchSource] = []
    for ref in references or []:
        url = str(getattr(ref, "url", "") or "").strip()
        if not url:
            continue
        sources.append(
            ResearchSource(
                title=(getattr(ref, "title", None) or "").strip(),
                url=url,
                published_date=getattr(ref, "published_date", None),
            )
        )
    return sources


def _capture_config_snapshot(config: AppConfig, overrides: UIOverrides) -> ConfigSnapshot:
    """設定スナップショットをキャプチャ（再現性確保用）
    
    Args:
        config: アプリケーション設定
        overrides: UI上書き設定
    
    Returns:
        ConfigSnapshot: 設定スナップショット
    """
    from dataclasses import asdict
    
    return ConfigSnapshot(
        yaml_config=config.yaml.model_dump(),
        ui_overrides=asdict(overrides),
        env_vars={
            "VOICEVOX_BASE_URL": config.env.voicevox_base_url,
            "GEMINI_API_KEY": "***MASKED***",
            "PERPLEXITY_API_KEY": "***MASKED***"
        }
    )


def _build_speaker_diagnostics(script: Script, label: str = "") -> tuple[list[str], bool]:
    """話者分布と口調ヒントの診断ログを作成する。"""
    dialogues = script.get_dialogue_only()
    count_a = sum(1 for line in dialogues if line.speaker == "A")
    count_b = sum(1 for line in dialogues if line.speaker == "B")

    a_nanoda = sum(1 for line in dialogues if line.speaker == "A" and "なのだ" in line.text)
    b_nanoda = sum(1 for line in dialogues if line.speaker == "B" and "なのだ" in line.text)
    a_wayo = sum(
        1 for line in dialogues if line.speaker == "A" and ("わよ" in line.text or "かしら" in line.text)
    )
    b_wayo = sum(
        1 for line in dialogues if line.speaker == "B" and ("わよ" in line.text or "かしら" in line.text)
    )

    prefix = f"[{label}] " if label else ""
    diagnostics = [
        f"[DEBUG] {prefix}話者分布診断:",
        f"  - A行数: {count_a}, B行数: {count_b}",
        f"  - 「なのだ」検出: A={a_nanoda}, B={b_nanoda}",
        f"  - 「わよ/かしら」検出: A={a_wayo}, B={b_wayo}",
    ]

    suspected_swap = (b_nanoda > a_nanoda) and (a_wayo > b_wayo)
    return diagnostics, suspected_swap


def _swap_script_speakers(script: Script) -> Script:
    """A/B話者を全行で入れ替えた新しいScriptを返す。"""
    swapped_sections = []
    for turn in script.sections:
        turn_data = turn.model_dump()
        if turn_data.get("speaker") == "A":
            turn_data["speaker"] = "B"
        elif turn_data.get("speaker") == "B":
            turn_data["speaker"] = "A"
        swapped_sections.append(DialogueTurn.model_validate(turn_data))

    return Script.model_validate({
        "title": script.title,
        "theme": script.theme,
        "sections": swapped_sections,
        "thumbnail_title": script.thumbnail_title,
        "description": script.description,
        "hashtags": script.hashtags,
        "references": script.references,
    })


# 責務 #10（ロギング/進捗）は services/workflow_logging.py へ抽出。
# 既存の import 経路・シンボル名（workflow.ProgressCallback /
# workflow._SessionLogFileHandler / workflow.LogFileWriter）を維持するため再エクスポート。
from services.workflow_logging import (  # noqa: E402
    ProgressCallback,
    _SessionLogFileHandler,
    LogFileWriter,
)


def create_script_generator(config: AppConfig, provider: Optional[str] = None):
    """台本生成エンジンを作成（プロバイダー選択対応）
    
    Args:
        config: アプリケーション設定
        provider: LLMプロバイダー名 ("gemini" | "openai" | "anthropic")
                 Noneの場合はconfig.yamlのdefault_providerを使用
    
    Returns:
        IScriptGenerator: プロバイダーに対応するクライアントインスタンス
    """
    from services.script_generation.llm_factory import create_script_generator as factory
    
    # デフォルトプロバイダーの決定
    if provider is None:
        provider = getattr(config.yaml.script_generator, 'default_provider', 'gemini')
    
    return factory(config, provider)


def create_researcher(config: AppConfig) -> PerplexityResearcher:
    """リサーチャー（Perplexity）を作成"""
    return PerplexityResearcher(config)


def apply_overrides(config: AppConfig, overrides: UIOverrides) -> AppConfig:
    """UIオーバーライドを設定に適用
    
    注意: Pydanticモデルはimmutableなので、新しい値で上書きする
    """
    # LLM provider override
    if overrides.llm_provider is not None:
        config.yaml.script_generator.default_provider = overrides.llm_provider
    
    if overrides.bgm_volume is not None:
        config.yaml.video_renderer.bgm_volume = overrides.bgm_volume
    
    if overrides.fade_in_sec is not None:
        config.yaml.video_renderer.bgm_fade_in_sec = overrides.fade_in_sec
    
    if overrides.fade_out_sec is not None:
        config.yaml.video_renderer.bgm_fade_out_sec = overrides.fade_out_sec
    
    if overrides.enable_spectrum is not None:
        config.yaml.video_renderer.enable_spectrum = overrides.enable_spectrum
    
    return config


# Step 4 v2 (2026-05-10): execute_planning_phase / _execute_gradio_scripting_phase
# は Gemini 自動台本生成経路の中核として削除済み。Perplexity リサーチは
# `services/pipeline/research_phase.py:execute_research_phase`、
# 台本生成は外部台本モード (services/pipeline/external_script_phase.py) を使う。


async def execute_production_phase(
    script: Script,
    config: AppConfig,
    output_dir: Path,
    project_root: Path,
    speed_scale: Optional[float] = None,
    segments: Optional[list] = None,
    visual_identity: Optional[VisualIdentity] = None,
    callbacks: Optional[ProgressCallback] = None
) -> ProductionPhaseResult:
    """制作フェーズ: 音声合成 → 動画生成
    
    Args:
        script: 台本データ（台本作成フェーズの出力）
        config: アプリケーション設定
        output_dir: 出力ディレクトリ
        project_root: プロジェクトルート
        speed_scale: 音声スピード倍率（オプション）
        segments: スクリプトセグメント（セグメント単位の背景切り替え用）
        visual_identity: ビジュアルアイデンティティ（色とスタイルの統一用）
        callbacks: 進捗コールバック
    
    Returns:
        ProductionPhaseResult: 動画ファイルパスと各種メタデータ
    """
    cb = callbacks or ProgressCallback()
    
    # ========== Step 1: 音声合成 ==========
    cb.log(f"\n== Phase 3-1: 音声合成 ==")
    cb.log(f"フレーズ数: {len(script.get_dialogue_only())}")
    cb.progress(0.70, "🗣️ 音声を合成中 (VOICEVOX)...")
    
    audio_start = time.time()
    audio_output_dir = output_dir / "audio"
    
    voicevox = VoicevoxClient(config)
    synthesis_result = await voicevox.synthesize(
        script, 
        audio_output_dir, 
        speed_scale_override=speed_scale,
        segments=segments
    )
    
    voicevox_usage = VoicevoxUsage(
        phrase_count=len(script.get_dialogue_only()),
        total_duration_sec=synthesis_result.total_duration_sec
    )
    audio_duration = time.time() - audio_start
    
    cb.log(f"✓ 音声合成完了: {synthesis_result.total_duration_sec:.1f}秒 ({audio_duration:.1f}秒)")
    cb.progress(0.85, "✅ 音声合成完了")
    
    # ========== Step 2: 動画生成 ==========
    cb.log(f"\n== Phase 3-2: 動画生成 ==")
    cb.log(f"BGM音量: {config.yaml.video_renderer.bgm_volume}")
    cb.log(f"フェードイン: {config.yaml.video_renderer.bgm_fade_in_sec}秒")
    cb.log(f"フェードアウト: {config.yaml.video_renderer.bgm_fade_out_sec}秒")
    cb.log(f"スペクトラム: {'ON' if config.yaml.video_renderer.enable_spectrum else 'OFF'}")
    cb.progress(0.90, "🎬 動画をレンダリング中 (FFmpeg)...")
    
    render_start = time.time()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    video_output_path = output_dir / "videos" / f"radio_{timestamp}.mp4"
    
    background_image = project_root / config.yaml.paths.background_image
    bgm_file = project_root / config.yaml.paths.bgm_file
    
    ffmpeg = FfmpegRenderer(config)
    render_result = await ffmpeg.render(
        synthesis_result=synthesis_result,
        background_image=background_image,
        bgm_file=bgm_file,
        output_path=video_output_path,
        subtitle_path=synthesis_result.subtitle_path,
        chapters=synthesis_result.chapters,
        segments=segments,  # セグメント単位の背景切り替え用
        visual_identity=visual_identity,  # ビジュアルアイデンティティを渡す
    )
    
    render_duration = time.time() - render_start
    
    cb.log(f"✓ 動画生成完了: {render_result.file_size_mb:.1f}MB ({render_duration:.1f}秒)")
    cb.progress(0.95, "✅ 動画生成完了")

    return ProductionPhaseResult(
        video_path=render_result.video_path,
        audio_path=synthesis_result.audio_path,
        subtitle_path=synthesis_result.subtitle_path,
        duration_sec=render_result.duration_sec,
        file_size_mb=render_result.file_size_mb,
        chapters=synthesis_result.chapters,
        voicevox_usage=voicevox_usage,
        audio_duration_sec=audio_duration,
        render_duration_sec=render_duration,
        segment_bg_generation_time=render_result.segment_bg_generation_time
    )


def _save_research_results(
    research_data,
    output_dir: Path,
    callbacks: ProgressCallback,
    *,
    theme: str,
    plan_queries: list[str],
    plan_angle: str = "自動生成",
) -> None:
    """リサーチ結果をファイルに保存

    Args:
        research_data: リサーチ結果
        output_dir: 出力ディレクトリ
        callbacks: 進捗コールバック
        theme: ResearchBrief.theme に書き込む元テーマ（連結クエリ全文ではなく）
        plan_queries: ResearchBrief.queries に書き込む実検索クエリリスト
        plan_angle: ResearchBrief.angle に書き込む切り口（plan が無い経路では既定値）
    """
    try:
        from dataclasses import asdict
        import json

        callbacks.log(f"[DEBUG] research.json保存処理開始")

        research_path = output_dir / "research.json"
        research_path.parent.mkdir(parents=True, exist_ok=True)

        research_dict = _to_json_safe(asdict(research_data))
        json_str = json.dumps(research_dict, ensure_ascii=False, indent=2)
        research_path.write_text(json_str, encoding="utf-8")

        callbacks.log(f"✓ リサーチ結果保存: {research_path}")

        # ResearchBrief も保存（インポート機能用）
        # 案1実装: 自動モードで生成したリサーチデータを再利用可能にする
        from core.models.artifacts import ResearchBrief

        # セッションIDを取得（output_dirのディレクトリ名から）
        session_id = output_dir.name

        research_brief = ResearchBrief(
            session_id=session_id,
            theme=theme,
            research_mode=research_data.mode,
            created_at=datetime.now().isoformat(),
            research_content=research_data.content,
            research_sources=[s.model_dump() for s in (research_data.sources or [])],
            queries=plan_queries,
            angle=plan_angle,
            curated_topics=None,
            perplexity_usage=asdict(research_data.usage) if research_data.usage else None,
            gemini_usage_planning=None
        )
        
        research_brief_path = output_dir / "research_brief.json"
        research_brief_json = json.dumps(research_brief.model_dump(), ensure_ascii=False, indent=2)
        research_brief_path.write_text(research_brief_json, encoding="utf-8")
        callbacks.log(f"✓ リサーチブリーフ保存（インポート用）: {research_brief_path}")
        
        # Markdownレポートも保存
        report_path = output_dir / "research_report.md"
        report_content = f"""# リサーチレポート

**テーマ**: {theme}
**モード**: {research_data.mode}
**生成日時**: {datetime.now().strftime('%Y年%m月%d日 %H:%M:%S')}

---

{research_data.content}
"""
        report_path.write_text(report_content, encoding="utf-8")
        callbacks.log(f"✓ リサーチレポート保存: {report_path}")
        
        # Perplexityの生データを全文保存
        full_report_path = output_dir / "full_research_report.md"
        full_report_path.write_text(research_data.content, encoding="utf-8")
        callbacks.log(f"✓ Perplexity全文レポート保存: {full_report_path}")
        
    except Exception as save_error:
        callbacks.log(f"⚠ リサーチ結果保存エラー: {save_error}")
        import traceback
        callbacks.log(f"[DEBUG] Traceback: {traceback.format_exc()}")


async def check_prerequisites(
    config: AppConfig,
    log_callback: Optional[Callable[[str], None]] = None
) -> tuple[bool, str]:
    """前提条件をチェック
    
    Returns:
        (success, error_message)
    """
    def log(msg: str):
        if log_callback:
            log_callback(msg)
    
    # VOICEVOX確認
    log("VOICEVOXエンジンの接続を確認中...")
    voicevox = VoicevoxClient(config)
    if not await voicevox.check_engine_status():
        return False, "VOICEVOXエンジンに接続できません。エンジンを起動してください。"
    log("✓ VOICEVOXエンジン接続OK")
    
    # FFmpeg確認
    log("FFmpegの利用可能性を確認中...")
    ffmpeg = FfmpegRenderer(config)
    if not ffmpeg.check_ffmpeg_available():
        return False, "FFmpegが見つかりません。インストールしてください。"
    log("✓ FFmpeg利用可能")
    
    # アセット確認
    background_image = PROJECT_ROOT / config.yaml.paths.background_image
    bgm_file = PROJECT_ROOT / config.yaml.paths.bgm_file
    
    if not background_image.exists():
        return False, f"背景画像が見つかりません: {background_image}"
    log(f"✓ 背景画像: {background_image.name}")
    
    if not bgm_file.exists():
        return False, f"BGMファイルが見つかりません: {bgm_file}"
    log(f"✓ BGMファイル: {bgm_file.name}")
    
    return True, ""


def _build_publish_fields(
    *,
    config: AppConfig,
    theme: str,
    script: Script,
    research_sources,
    generated_metadata: dict,
    chapters,
    total_usage,
    footer_text_override,
    creation_date: str,
) -> tuple[str, str, Any]:
    """YouTube 概要欄向けフィールド(タイトル/説明文/publishing_config)を構築する。

    Phase 4 後処理から抽出した純計算ヘルパー(I/O なし)。`creation_date` を引数で
    受け取ることで `datetime.now()` を排除し、単体テストの決定性を確保する。

    Args:
        config: アプリ設定(publishing_config の解決に使用)
        theme: 動画テーマ(references 解決に使用)
        script: 台本(タイトル/説明/タグ/参考文献のソース)
        research_sources: リサーチソース(references 解決に使用)
        generated_metadata: `_generate_youtube_metadata` の戻り(title/description 優先採用)
        chapters: ChapterMarker のリスト(概要欄チャプター行に整形)
        total_usage: LLM モデル情報の抽出に使用(読み取りのみ)
        footer_text_override: UI 入力のフッター(優先)。空なら config フォールバック
        creation_date: `YYYY.MM.DD` 形式の制作日文字列

    Returns:
        (formatted_title, formatted_description, publishing_config)
    """
    # 日付入りタイトルを生成（AI生成タイトル + 日付）
    ai_title = generated_metadata.get("title", script.title)
    formatted_title = f"{ai_title} ({creation_date}制作)"

    publishing_config = getattr(config.yaml, "publishing", None)
    chapter_lines = _format_chapter_lines(chapters)

    script_description = (
        generated_metadata.get("description")
        or script.description
        or ""
    )

    dynamic_tags = _resolve_dynamic_tags(
        script,
        fallback_description=script_description,
    )
    references = _resolve_references(
        script,
        theme=theme,
        fallback_description=script_description,
        research_sources=research_sources,
    )

    configured_tags = getattr(publishing_config, "default_tags", []) if publishing_config else []
    if not isinstance(configured_tags, list):
        configured_tags = []
    fixed_tags = _normalize_non_empty_strings([str(tag) for tag in configured_tags])

    configured_footer = (
        getattr(publishing_config, "footer_text", "") if publishing_config else ""
    )
    resolved_footer = (
        (footer_text_override or "").strip()
        if isinstance(footer_text_override, str) and footer_text_override.strip()
        else (configured_footer or "").strip()
    )

    # 使用モデル情報を生成
    llm_model_info = ""
    if total_usage.llm_usage:
        model_parts = []
        for provider, usage in total_usage.llm_usage.items():
            if usage.model_name:
                model_parts.append(f"{provider.upper()}: {usage.model_name}")
        if model_parts:
            llm_model_info = "■台本生成モデル\n" + "\n".join(model_parts)

    formatted_description = build_video_description(
        script_description=script_description,
        chapters=chapter_lines,
        references=references,
        dynamic_tags=dynamic_tags,
        fixed_tags=fixed_tags,
        footer_text=resolved_footer,
        llm_model_info=llm_model_info,
    )

    return formatted_title, formatted_description, publishing_config


def _run_metadata_phase(
    *,
    config: AppConfig,
    output_base: Path,
    script: Script,
    chapters,
    external_phase_result,
    theme: str,
    log_fn,
) -> tuple[Path, dict[str, str]]:
    """Phase 4 のメタデータ生成を実行し (metadata_path, generated_metadata) を返す。

    mock スキップ時は metadata.txt を空文字で書き出す。通常時は FactFix 修正済み
    台本を優先解決したうえで、外部台本モードの事前構築 metadata から
    YouTube 用メタデータを生成する。`external_phase_result.pre_built_metadata` へは
    通常経路（非スキップ）でのみアクセスするため、mock スキップ時に
    external_phase_result が None でも安全（原コードと厳密一致）。

    Args:
        config: アプリ設定(mock スキップ判定に使用)
        output_base: 出力ディレクトリ(metadata.txt の親)
        script: フォールバック台本(= scripting_result.script)
        chapters: ChapterMarker のリスト(= production_result.chapters)
        external_phase_result: 外部台本フェーズ結果(.pre_built_metadata を保持)
        theme: 動画テーマ
        log_fn: ログ出力関数(= callbacks.log)

    Returns:
        (metadata_path, generated_metadata)
    """
    skip_metadata_in_mock = bool(
        config.yaml.dev.mock_mode and getattr(config.yaml.dev, "mock_skip_metadata", False)
    )

    # YouTube用メタデータを生成
    metadata_path = output_base / "metadata.txt"
    generated_metadata: dict[str, str] = {}
    if skip_metadata_in_mock:
        metadata_path.write_text("", encoding="utf-8")
        log_fn("[INFO] Mockモード設定によりメタデータ生成をスキップしました")
    else:
        # 2026-07-17: 参考文献 (タイトル + 日付 + URL) と固定タグを metadata.txt
        # にも反映する。verified_script 属性を持たないテストスタブでも安全な
        # よう getattr で解決する。
        _vs_for_metadata = getattr(external_phase_result, "verified_script", None)
        vs_references = _verified_refs_to_research_sources(
            _vs_for_metadata.metadata.references if _vs_for_metadata is not None else []
        )
        _publishing_config = getattr(config.yaml, "publishing", None)
        _configured_tags = (
            getattr(_publishing_config, "default_tags", []) if _publishing_config else []
        )
        if not isinstance(_configured_tags, list):
            _configured_tags = []
        # FactFix 修正済み台本があればそちらを優先（ハルシネーションが概要欄に載るのを防ぐ）
        effective_script = _resolve_script_for_metadata(
            output_base, script, log_fn=log_fn
        )
        # 外部台本モード必須 (Step 4 v2): VerifiedScript.metadata から
        # 事前構築済みの dict をそのまま採用する。
        ext_metadata_for_packaging = external_phase_result.pre_built_metadata
        generated_metadata = _generate_youtube_metadata(
            script=effective_script,
            chapters=chapters,
            output_path=metadata_path,
            theme=theme,
            external_metadata=ext_metadata_for_packaging,
            references=vs_references,
            fixed_tags=[str(tag) for tag in _configured_tags],
        )
        log_fn(f"✓ YouTubeメタデータ生成 (外部台本モード, LLM ¥0): {metadata_path.name}")

    return metadata_path, generated_metadata


def _upload_to_youtube(
    *,
    config: AppConfig,
    use_mock: bool,
    upload_override,
    publishing_config,
    video_path,
    formatted_title: str,
    formatted_description: str,
    thumbnail_path,
    callbacks,
) -> Optional[str]:
    """YouTube へ動画をアップロードし、アップロード URL を返す（失敗時は None）。

    should_upload の判定（UI 優先フラグ / config 設定 / mock ガード）、アップロード、
    再生リスト追加までを担う。アップロード失敗・再生リスト追加失敗はいずれも非致命で、
    ログ出力のうえ動画生成結果は成功扱いとする（原コードと厳密一致）。

    Args:
        config: アプリ設定(YouTubeClient 生成に使用)
        use_mock: Mock 実行か（True の場合は強制的にアップロード無効）
        upload_override: UI 優先のアップロードフラグ（None なら config 設定に従う）
        publishing_config: publishing 設定(enable_upload / playlist_id)
        video_path: アップロード対象の動画ファイルパス
        formatted_title: 動画タイトル
        formatted_description: 概要欄
        thumbnail_path: サムネイル画像パス
        callbacks: ProgressCallback（ログ出力用）

    Returns:
        アップロード成功時は動画 URL、無効/失敗時は None
    """
    uploaded_video_url: Optional[str] = None
    should_upload = (
        upload_override
        if upload_override is not None
        else bool(publishing_config and getattr(publishing_config, "enable_upload", False))
    )

    # Safety guard: never upload during mock executions.
    if use_mock and should_upload:
        should_upload = False
        callbacks.log("[INFO] MockモードのためYouTubeアップロードを強制的に無効化しました")

    if should_upload:
        # 保険: 概要欄に URL が残っていたら落として (載せずに) 続行する。
        # 出典リンク先のドメイン分類で動画が削除されたため (2026-10-23)
        formatted_description, removed_urls = strip_urls(formatted_description)
        if removed_urls:
            callbacks.log(
                f"⚠ 概要欄に URL が {len(removed_urls)} 件残っていたため取り除いてアップロードします: "
                f"{removed_urls}"
            )
        callbacks.log("[INFO] YouTubeアップロードを開始します...")
        try:
            youtube_client = YouTubeClient(config)
            uploaded_video_url = youtube_client.upload_video(
                file_path=video_path,
                title=formatted_title,
                description=formatted_description,
                thumbnail_path=thumbnail_path,
            )
            callbacks.log(f"✓ YouTubeアップロード完了: {uploaded_video_url}")

            # 設定されている場合は再生リストへ追加（失敗しても非致命）
            playlist_id = getattr(publishing_config, "playlist_id", None)
            if isinstance(playlist_id, str):
                playlist_id = playlist_id.strip()

            if playlist_id:
                callbacks.log(f"[INFO] 再生リスト追加設定: {playlist_id}")
                try:
                    parsed_url = urlparse(uploaded_video_url)
                    video_id = parse_qs(parsed_url.query).get("v", [None])[0]
                    if video_id:
                        youtube_client.add_video_to_playlist(
                            video_id=video_id,
                            playlist_id=playlist_id,
                        )
                        callbacks.log(f"✓ 再生リストへ追加完了: {playlist_id}")
                    else:
                        callbacks.log(
                            "⚠ 再生リスト追加をスキップ: 動画IDの抽出に失敗しました"
                        )
                except Exception as playlist_error:
                    callbacks.log(
                        f"⚠ 再生リストへの追加に失敗しました（動画生成は成功）: {playlist_error}"
                    )
            else:
                callbacks.log("[INFO] 再生リスト追加設定: 未設定（playlist_id が空のためスキップ）")
        except Exception as upload_error:
            callbacks.log(
                f"⚠ YouTubeアップロードに失敗しました（動画生成は成功）: {upload_error}"
            )
    else:
        callbacks.log("[INFO] YouTubeアップロード設定: 無効（UI設定優先）")

    return uploaded_video_url


def _record_execution_logs(
    *,
    config: AppConfig,
    overrides_obj: UIOverrides,
    output_base: Path,
    project_root: Path,
    theme: str,
    production_result,
    thumbnail_path,
    metadata_path,
    total_usage,
    cost,
    callbacks,
) -> None:
    """実行ログ・コスト履歴を記録する（副作用のみ）。

    ExecutionLogEntry / CostLogEntry を構築し ExecutionLogger へ追記する。記録失敗は
    非致命で、ログ出力のうえ呼び出し元へ例外を伝播しない（原コードと厳密一致）。

    Args:
        config: アプリ設定
        overrides_obj: UI 上書き設定(config スナップショット用)
        output_base: 出力ディレクトリ
        project_root: プロジェクトルート(logs ディレクトリの親)
        theme: 動画テーマ
        production_result: 制作フェーズ結果(生成ファイルパス)
        thumbnail_path: サムネイルパス
        metadata_path: metadata.txt パス
        total_usage: 使用量集計
        cost: コスト計算結果
        callbacks: ProgressCallback（ログ出力用）
    """
    try:
        from uuid import uuid4
        execution_id = str(uuid4())

        # プロンプト記録を収集（実際に使用されたインスタンスから）
        all_prompt_records = []

        # Note: Mock モードではプロンプト記録が空の場合がある
        # 実際のAPI呼び出しがあった場合のみ記録される

        # 設定スナップショットをキャプチャ
        config_snapshot = _capture_config_snapshot(config, overrides_obj)

        # 生成ファイルパスを記録
        from pathlib import Path as PathLib
        generated_files = {
            "script": str(output_base / "script.json"),
            "video": str(production_result.video_path),
            "audio": str(production_result.audio_path),
            "subtitle": str(production_result.subtitle_path),
            "thumbnail": str(thumbnail_path),
            "metadata": str(metadata_path)
        }

        # ExecutionLogEntryを作成
        execution_log = ExecutionLogEntry(
            execution_id=execution_id,
            app_version=APP_VERSION,
            timestamp=datetime.now().isoformat(),
            output_directory=str(output_base),
            theme=theme,
            config_snapshot=config_snapshot,
            prompts=[PromptRecord(**rec) for rec in all_prompt_records],
            generated_files=generated_files,
            success=True,
            error_message=None,
            total_duration_sec=total_usage.total_duration_sec,
            perplexity_requests=total_usage.perplexity.request_count
        )

        # CostLogEntryを作成（後方互換性のためgeminiプロパティを使用）
        # total_usage.geminiは全LLM使用量の合計を返すプロパティ
        cost_log = CostLogEntry(
            execution_id=execution_id,
            timestamp=datetime.now().isoformat(),
            output_directory=str(output_base),
            perplexity_requests=total_usage.perplexity.request_count,
            perplexity_model_name=config.yaml.researcher.model,
            gemini_input_tokens=total_usage.gemini.input_tokens,
            gemini_output_tokens=total_usage.gemini.output_tokens,
            gemini_model_name=total_usage.gemini.model_name,
            voicevox_phrases=total_usage.voicevox.phrase_count,
            voicevox_duration_sec=total_usage.voicevox.total_duration_sec,
            perplexity_usd=cost.perplexity_usd,
            gemini_input_usd=cost.gemini_input_usd,
            gemini_output_usd=cost.gemini_output_usd,
            total_usd=cost.total_usd,
            total_jpy=cost.total_jpy,
            is_free_tier=cost.is_free_tier,
            research_duration_sec=total_usage.research_duration_sec,
            script_duration_sec=total_usage.script_duration_sec,
            audio_duration_sec=total_usage.audio_duration_sec,
            render_duration_sec=total_usage.render_duration_sec,
            total_duration_sec=total_usage.total_duration_sec
        )

        # ログを書き込み
        logger = ExecutionLogger(project_root / "logs")
        logger.append_execution_log(execution_log)
        logger.append_cost_log(cost_log)

        callbacks.log(f"✓ 実行ログ記録完了: execution_id={execution_id}")

    except Exception as log_error:
        import traceback
        callbacks.log(f"⚠ 実行ログ記録エラー（動画生成は成功）: {log_error}")
        callbacks.log(f"[DEBUG] Traceback: {traceback.format_exc()}")


async def _generate_thumbnail_assets(
    *,
    config: AppConfig,
    output_base: Path,
    project_root: Path,
    theme: str,
    script: Script,
    visual_identity,
    generated_metadata: dict,
    skip_thumbnail_in_mock: bool,
    callbacks,
) -> tuple[Path, float]:
    """サムネイル背景(FLUX.1 dynamic / static フォールバック)と thumbnail.png を生成する。

    dynamic モードで FLUX.1 生成に失敗した場合は静的背景へフォールバックする。例外の
    握り方・ログ出力・フォールバック条件は原コードと厳密一致（本運用 1 本目で踏んだ経路）。
    背景生成時間は戻り値で返し、呼び出し側で total_usage へ代入する。

    Args:
        config: アプリ設定
        output_base: 出力ディレクトリ(thumbnail_bg.png / thumbnail.png の親)
        project_root: プロジェクトルート(静的背景パスの基点)
        theme: 動画テーマ(FLUX.1 プロンプト / summary フォールバック)
        script: 台本(title / description のソース)
        visual_identity: ビジュアルアイデンティティ(= scripting_result.visual_identity)
        generated_metadata: メタデータ dict(title / thumbnail_title のソース)
        skip_thumbnail_in_mock: Mock 設定によるサムネイル生成スキップ
        callbacks: ProgressCallback（ログ出力用）

    Returns:
        (thumbnail_path, thumbnail_bg_generation_time)
    """
    # サムネイル背景を生成（FLUX.1 dynamic mode）
    video_config = getattr(config.yaml, "video_renderer", None)
    thumbnail_bg_mode = getattr(video_config, "thumbnail_background_mode", "static") if video_config else "static"
    thumbnail_bg_generation_time = 0.0

    if thumbnail_bg_mode == "dynamic":
        try:
            callbacks.log("[INFO] サムネイル背景を動的生成中（FLUX.1）...")
            thumbnail_bg_start = time.time()

            # Generate script summary for prompt
            script_summary = script.description[:300] if script.description else theme

            # Generate background via FLUX.1
            thumbnail_bg_generator = ThumbnailBackgroundGenerator(config, output_dir=output_base)
            thumbnail_bg_path = output_base / "thumbnail_bg.png"

            # Use await instead of asyncio.run() to avoid nested event loop error
            background_image = await thumbnail_bg_generator.generate(
                theme=theme,
                script_summary=script_summary,
                output_path=thumbnail_bg_path,
                visual_identity=visual_identity,  # ビジュアルアイデンティティを渡す
                topic_title=script.title
            )

            thumbnail_bg_generation_time = time.time() - thumbnail_bg_start
            callbacks.log(f"✓ サムネイル背景生成完了（FLUX.1）: {thumbnail_bg_path.name} ({thumbnail_bg_generation_time:.1f}秒)")
        except Exception as e:
            callbacks.log(f"⚠ サムネイル背景生成失敗、静的背景を使用: {e}")
            background_image = project_root / config.yaml.paths.background_image
    else:
        # Use static background
        background_image = project_root / config.yaml.paths.background_image

    # サムネイル画像を生成（AI生成のthumbnail_titleを使用）
    thumbnail_path = output_base / "thumbnail.png"
    if skip_thumbnail_in_mock:
        callbacks.log("[INFO] Mockモード設定によりサムネイル生成をスキップしました")
    else:
        thumbnail_generator = ThumbnailGenerator()
        thumbnail_generator.generate(
            title=generated_metadata.get("title", script.title),
            thumbnail_title=generated_metadata.get("thumbnail_title", ""),
            background_path=background_image,
            output_path=thumbnail_path
        )
        callbacks.log(f"✓ サムネイル画像生成: {thumbnail_path.name}")

    return thumbnail_path, thumbnail_bg_generation_time


def run_workflow_sync(
    theme: str,
    overrides: Optional[UIOverrides] = None,
    log_callback: Optional[Callable[[str], None]] = None,
    progress_callback: Optional[Callable[[float, str], None]] = None,
    use_mock: bool = False,
    avoid_topics: Optional[str] = None,
    upload_override: Optional[bool] = None,
    footer_text_override: Optional[str] = None,
    research_import_filepath: Optional[str] = None,
    external_script_path: Optional[str] = None,
) -> WorkflowResult:
    """同期版ワークフロー実行（Gradio から呼び出し用 / 外部台本モード前提）

    Step 4 v2 (2026-05-10): Phase 1 (planning) + Phase 2 (scripting) の自動経路は
    削除済み。`external_script_path` (VerifiedScript JSON) が必須。

    Args:
        theme: 動画のテーマ（外部台本モード時は VerifiedScript.metadata.title から復元）
        overrides: UIからのパラメータオーバーライド
        log_callback: ログ出力用コールバック関数
        progress_callback: 進捗コールバック (ratio, description)
        use_mock: Mockモードを使用するか（開発・テスト用）
        avoid_topics: research_import 経由で渡される除外要件メモ
        upload_override: YouTubeアップロード実行のUI優先フラグ
        footer_text_override: 概要欄フッター文（UI入力優先）
        research_import_filepath: research_brief.json インポート（参考保持用）
        external_script_path: VerifiedScript JSON のパス（必須）

    Returns:
        WorkflowResult: 実行結果（Usage/Cost情報含む）
    """
    async def _run_phases():
        # インポート経路で brief.theme を SSOT として再代入するため nonlocal 宣言。
        # 詳細は ResearchBrief ロード箇所のコメント参照（PR-I 同系統の修正）。
        nonlocal theme
        workflow_start = time.time()
        overrides_obj = overrides or UIOverrides()
        callbacks = ProgressCallback(log_callback, progress_callback)
        log_writer: Optional[LogFileWriter] = None
        original_mock_mode = None  # 元のmock_mode設定を保存
        
        try:
            # ========== Phase 0: 設定読み込み・前提条件チェック ==========
            callbacks.progress(0.0, "🚀 生成プロセスを開始します...")
            callbacks.log("設定を読み込み中...")
            
            config = load_config(PROJECT_ROOT)

            # MockモードはUIの実行ボタン種別を優先する
            # - 通常の「動画を生成する」: use_mock=False
            # - 「モックで動画を作成」: use_mock=True
            if not hasattr(config.yaml, 'dev'):
                from types import SimpleNamespace
                config.yaml.dev = SimpleNamespace(mock_mode=False, mock_data_path="tests/mock_data")

            original_mock_mode = bool(getattr(config.yaml.dev, "mock_mode", False))
            config.yaml.dev.mock_mode = bool(use_mock)

            if use_mock:
                callbacks.log("🔴 Mockモードが有効化されました")
            elif original_mock_mode:
                callbacks.log("[dim]INFO: UI通常実行のためMockモードを無効化して実行します[/dim]")
            
            config = apply_overrides(config, overrides_obj)
            
            # 素材パスのオーバーライド
            if overrides_obj.background_image:
                config.yaml.paths.background_image = f"assets/backgrounds/{overrides_obj.background_image}"
            if overrides_obj.bgm_file:
                config.yaml.paths.bgm_file = f"assets/bgm/{overrides_obj.bgm_file}"
            
            # 前提条件チェック
            callbacks.progress(0.02, "前提条件を確認中...")
            success, error = await check_prerequisites(config, log_callback)
            if not success:
                return WorkflowResult(success=False, error_message=error)
            callbacks.progress(0.05, "✅ 前提条件OK")
            
            # 出力ディレクトリを準備
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_base = PROJECT_ROOT / config.yaml.paths.output_dir / timestamp
            output_base.mkdir(parents=True, exist_ok=True)
            
            # ログファイルライターを初期化
            log_writer = LogFileWriter(output_base)
            # ProgressCallbackにlog_writerを設定
            callbacks.log_writer = log_writer
            callbacks.log(f"出力ディレクトリ: {output_base}")
            
            # Usage集約用
            total_usage = TotalUsage()
            
            # プロバイダーの決定
            callbacks.log(f"[DEBUG] overrides_obj.llm_provider = {overrides_obj.llm_provider}")
            callbacks.log(f"[DEBUG] config default_provider = {getattr(config.yaml.script_generator, 'default_provider', 'gemini')}")
            provider = overrides_obj.llm_provider or getattr(config.yaml.script_generator, 'default_provider', 'gemini')
            callbacks.log(f"[DEBUG] 選択されたプロバイダー: {provider}")
            
            # 実行ログ用: API クライアントインスタンスを保持
            api_clients = {
                'script_generator': None,
                'researcher': None
            }
            
            # ========== インポートリサーチデータのロード（オプション） ==========
            # research_import_filepathが指定された場合、Phase 1/2のリサーチAPIを完全スキップ
            preloaded_research: Optional[ResearchResult] = None
            
            if research_import_filepath and not config.yaml.dev.mock_mode:
                import json as _json
                from core.models.artifacts import ResearchBrief
                from core.models.research import ResearchSource as ModelResearchSource
                
                callbacks.log("\n== リサーチデータ インポート ==")
                callbacks.progress(0.05, "📂 リサーチデータを読み込み中...")
                
                try:
                    with open(research_import_filepath, 'r', encoding='utf-8') as _f:
                        _brief_data = _json.load(_f)
                    brief = ResearchBrief(**_brief_data)
                    
                    # ResearchBrief.research_sources (list[dict]) → list[ResearchSource] に変換
                    imported_sources = []
                    for _s in (brief.research_sources or []):
                        if isinstance(_s, dict):
                            try:
                                imported_sources.append(ModelResearchSource(**_s))
                            except Exception:
                                pass
                    
                    preloaded_research = ResearchResult(
                        topic=brief.theme,
                        mode=brief.research_mode,
                        content=brief.research_content,
                        sources=imported_sources,
                        usage=None,
                        # 2026-05-06: research_import 経路でも structured_facts を引き継ぐ。
                        # これが無いとリサーチ側で抽出された数値・固有名詞が ResearchResult に
                        # 載らず、Orchestrator Step 0.5 の FactSheet.from_structured_facts
                        # 分岐が常に skip → 全件 FactExtractor フォールバックになる。
                        structured_facts=brief.structured_facts,
                    )

                    # PR-I 同系統: インポートされた ResearchBrief を SSOT として
                    # workflow の theme を上書きする。app.py 側で theme 未入力時に
                    # "Imported Research" 等の placeholder が入っていた場合、
                    # それが ScriptOrchestrator / MetadataGenerator に流れて LLM が
                    # 「輸入された研究」と誤解釈するハルシネーション要因となる。
                    # brief.theme が非空ならインポート時は常にそちらを採用する。
                    if brief.theme and brief.theme.strip():
                        if theme != brief.theme:
                            callbacks.log(
                                f"   [INFO] テーマを ResearchBrief から復元: "
                                f"{theme!r} → {brief.theme!r}"
                            )
                        theme = brief.theme

                    # リサーチ（API課金）フェーズを無効化
                    overrides_obj.enable_research = False

                    callbacks.log(f"✅ リサーチデータのインポート完了（APIコスト: ¥0）")
                    callbacks.log(f"   テーマ: {brief.theme}")
                    callbacks.log(f"   モード: {brief.research_mode}")
                    callbacks.log(f"   コンテキスト: {len(brief.research_content)}文字")
                    callbacks.log(f"   ソース数: {len(imported_sources)}件")
                    callbacks.progress(0.10, "✅ リサーチデータ読み込み完了（Perplexity APIスキップ）")
                    
                except FileNotFoundError:
                    callbacks.log(f"❌ インポートファイルが見つかりません: {research_import_filepath}")
                    callbacks.log("通常のリサーチAPIを使用して続行します")
                except Exception as _e:
                    callbacks.log(f"❌ リサーチデータのインポート失敗: {_e}")
                    callbacks.log("通常のリサーチAPIを使用して続行します")

            # ========== 外部台本モード（VerifiedScript JSON）==========
            # Step 3 (2026-05-09): Mac 側 radio_director の VerifiedScript JSON を
            # 受け取り、Phase 1 (planning) + Phase 2 (scripting) を完全 bypass する。
            # external_script_path が指定された場合、execute_external_script_phase を
            # 経由して Script + segments + pre_built_metadata を構築し、Phase 3 にそのまま渡す。
            external_phase_result = None
            external_mode = bool(external_script_path)
            if external_mode and not config.yaml.dev.mock_mode:
                from pathlib import Path as _Path
                from core.session_manager import SessionManager as _SessionManager
                from services.pipeline import execute_external_script_phase

                callbacks.log("\n== 外部台本モード: VerifiedScript JSON ロード ==")
                callbacks.progress(0.05, "📄 VerifiedScript JSON を読み込み中...")

                try:
                    sm = _SessionManager(
                        project_root=PROJECT_ROOT,
                        session_dir=output_base,
                    )
                    external_phase_result = await execute_external_script_phase(
                        verified_script_path=_Path(external_script_path),
                        session_manager=sm,
                        config=config,
                        callbacks=callbacks,
                    )

                    # Phase 1 / Phase 2 を完全 bypass する (Gemini API ¥0)
                    overrides_obj.enable_research = False

                    # Theme を VerifiedScript.metadata.title から復元 (research_import と同パターン)
                    vs_title = external_phase_result.verified_script.metadata.title
                    if vs_title and vs_title.strip():
                        if theme != vs_title:
                            callbacks.log(
                                f"   [INFO] テーマを VerifiedScript から復元: {theme!r} → {vs_title!r}"
                            )
                        theme = vs_title

                    callbacks.log(f"✅ 外部台本ロード完了 (LLM コスト: ¥0)")
                    callbacks.log(f"   タイトル: {vs_title}")
                    callbacks.log(
                        f"   Sections: {len(external_phase_result.script.sections)}, "
                        f"Segments: {len(external_phase_result.segments)}"
                    )
                    callbacks.progress(0.10, "✅ VerifiedScript ロード完了（Phase 1/2 完全スキップ）")

                except FileNotFoundError as _e:
                    callbacks.log(f"❌ VerifiedScript ファイルが見つかりません: {external_script_path}")
                    return WorkflowResult(success=False, error_message=str(_e))
                except Exception as _e:
                    # silent fallback 禁止 (指示書 §3.4)
                    callbacks.log(f"❌ VerifiedScript ロード失敗: {_e}")
                    return WorkflowResult(success=False, error_message=str(_e))

            # ========== Phase 1+2 (deleted in Step 4 v2): 外部台本モード必須 ==========
            # Step 4 v2 (2026-05-10): Gemini 自動台本生成経路は削除済み。
            # external_script_path が指定されている場合のみ scripting_result を構築する。
            if external_mode and external_phase_result is not None:
                callbacks.log("[INFO] 外部台本モード: Phase 1 (planning) + Phase 2 (scripting) を完全スキップ")
                # ScriptingPhaseResult 互換のスタブを構築 (Phase 3 production にそのまま渡せるように)
                # Step 6 (2026-05-12): external_phase_result で生成済みの visual_identity を伝播。
                # production_phase → ffmpeg_renderer → ImageProvider に下流配線済み。
                # 2026-07-17: VerifiedScript.metadata.references (title/published_date)
                # を ResearchSource に変換して伝播し、概要欄の参考文献を
                # 「タイトル + 日付 + URL」表記にする (従来は URL のみ)。
                _vs_for_refs = getattr(external_phase_result, "verified_script", None)
                external_ref_sources = _verified_refs_to_research_sources(
                    _vs_for_refs.metadata.references if _vs_for_refs is not None else []
                )
                scripting_result = ScriptingPhaseResult(
                    script=external_phase_result.script,
                    research_content=None,
                    research_sources=external_ref_sources or None,
                    perplexity_usage=None,
                    gemini_usage=None,
                    research_duration_sec=0.0,
                    script_duration_sec=0.0,
                    segments=external_phase_result.segments,
                    visual_identity=external_phase_result.visual_identity,
                )
            else:
                # 旧 LLM 自動経路は削除済み。外部台本モードを必須とする。
                error_msg = (
                    "Step 4 v2 (2026-05-10): 旧 Gemini 自動台本生成経路は削除されました。"
                    "external_script_path (VerifiedScript JSON) を指定してください。"
                    "Mac 側 radio_director の出力を `output/imports/<run_id>/verified_script.json` に配置し、"
                    "UI の 🎬 外部台本モード または CLI `--phase external --verified-script <path>` で実行してください。"
                )
                callbacks.log(f"❌ {error_msg}")
                return WorkflowResult(success=False, error_message=error_msg)

            # Usage記録
            if scripting_result.perplexity_usage:
                total_usage.perplexity = scripting_result.perplexity_usage
            if scripting_result.gemini_usage:
                # プロバイダー別に集約
                provider = scripting_result.gemini_usage.provider
                if provider in total_usage.llm_usage:
                    existing = total_usage.llm_usage[provider]
                    total_usage.llm_usage[provider] = LLMUsage(
                        provider=provider,
                        model_name=scripting_result.gemini_usage.model_name,
                        input_tokens=existing.input_tokens + scripting_result.gemini_usage.input_tokens,
                        output_tokens=existing.output_tokens + scripting_result.gemini_usage.output_tokens,
                        request_count=existing.request_count + scripting_result.gemini_usage.request_count
                    )
                else:
                    total_usage.llm_usage[provider] = scripting_result.gemini_usage
            total_usage.research_duration_sec = scripting_result.research_duration_sec
            total_usage.script_duration_sec = scripting_result.script_duration_sec
            
            # ========== Phase 3: 制作（音声合成 → 動画生成） ==========
            production_result = await execute_production_phase(
                script=scripting_result.script,
                config=config,
                output_dir=output_base,
                project_root=PROJECT_ROOT,
                speed_scale=overrides_obj.speed_scale,
                segments=scripting_result.segments,  # セグメント情報を渡す
                visual_identity=scripting_result.visual_identity,  # ビジュアルアイデンティティを渡す
                callbacks=callbacks
            )
            
            # Usage記録
            total_usage.voicevox = production_result.voicevox_usage
            total_usage.audio_duration_sec = production_result.audio_duration_sec
            total_usage.render_duration_sec = production_result.render_duration_sec
            
            # 画像生成時間を記録
            segment_bg_time = production_result.segment_bg_generation_time
            total_usage.segment_bg_generation_time = segment_bg_time
            
            # ========== Phase 4: 後処理（メタデータ生成） ==========
            callbacks.progress(0.97, "📦 後処理中...")
            skip_thumbnail_in_mock = bool(
                config.yaml.dev.mock_mode and getattr(config.yaml.dev, "mock_skip_thumbnail", False)
            )

            # YouTube用メタデータを生成
            metadata_path, generated_metadata = _run_metadata_phase(
                config=config,
                output_base=output_base,
                script=scripting_result.script,
                chapters=production_result.chapters,
                external_phase_result=external_phase_result,
                theme=theme,
                log_fn=callbacks.log,
            )

            # サムネイル生成（背景: FLUX.1 dynamic / static フォールバック）
            thumbnail_path, thumbnail_bg_generation_time = await _generate_thumbnail_assets(
                config=config,
                output_base=output_base,
                project_root=PROJECT_ROOT,
                theme=theme,
                script=scripting_result.script,
                visual_identity=scripting_result.visual_identity,
                generated_metadata=generated_metadata,
                skip_thumbnail_in_mock=skip_thumbnail_in_mock,
                callbacks=callbacks,
            )
            total_usage.thumbnail_bg_generation_time = thumbnail_bg_generation_time

            # ログファイルを完了
            if log_writer:
                log_writer.finalize()
                callbacks.log(f"✓ 処理ログ保存: {log_writer.log_path.name}")
            
            # メタデータの内容を読み込んでUIへ渡す
            metadata_content = metadata_path.read_text(encoding="utf-8")
            
            # 日付入りタイトルを生成（AI生成タイトル + 日付）
            creation_date = datetime.now().strftime("%Y.%m.%d")
            formatted_title, formatted_description, publishing_config = _build_publish_fields(
                config=config,
                theme=theme,
                script=scripting_result.script,
                research_sources=scripting_result.research_sources,
                generated_metadata=generated_metadata,
                chapters=production_result.chapters,
                total_usage=total_usage,
                footer_text_override=footer_text_override,
                creation_date=creation_date,
            )

            # 総所要時間
            total_usage.total_duration_sec = time.time() - workflow_start
            
            # コスト計算
            cost_calculator = CostCalculator(config)
            cost = cost_calculator.calculate(total_usage)
            cost_report = cost_calculator.format_cost_report(total_usage, cost)

            # YouTubeアップロード（失敗しても動画生成結果は成功扱い）
            uploaded_video_url = _upload_to_youtube(
                config=config,
                use_mock=use_mock,
                upload_override=upload_override,
                publishing_config=publishing_config,
                video_path=production_result.video_path,
                formatted_title=formatted_title,
                formatted_description=formatted_description,
                thumbnail_path=thumbnail_path,
                callbacks=callbacks,
            )

            # ========== 実行ログ・コスト履歴の記録 ==========
            _record_execution_logs(
                config=config,
                overrides_obj=overrides_obj,
                output_base=output_base,
                project_root=PROJECT_ROOT,
                theme=theme,
                production_result=production_result,
                thumbnail_path=thumbnail_path,
                metadata_path=metadata_path,
                total_usage=total_usage,
                cost=cost,
                callbacks=callbacks,
            )

            callbacks.log(f"\n== 完了 ==")
            callbacks.log(f"動画: {production_result.video_path}")
            callbacks.log(f"総所要時間: {total_usage.total_duration_sec:.1f}秒")
            callbacks.progress(1.0, "✨ すべて完了しました！")
            
            return WorkflowResult(
                success=True,
                video_path=production_result.video_path,
                script=scripting_result.script,
                audio_path=production_result.audio_path,
                subtitle_path=production_result.subtitle_path,
                duration_sec=production_result.duration_sec,
                file_size_mb=production_result.file_size_mb,
                usage=total_usage,
                cost=cost,
                cost_report=cost_report,
                metadata_content=metadata_content,
                formatted_title=formatted_title,
                formatted_description=formatted_description,
                uploaded_video_url=uploaded_video_url,
                theme=theme,
                output_dir=output_base,
                segment_bg_generation_time=segment_bg_time,
                thumbnail_bg_generation_time=thumbnail_bg_generation_time,
                visual_identity=scripting_result.visual_identity,  # ビジュアルアイデンティティを格納
            )
            
        except Exception as e:
            error_msg = f"エラーが発生しました: {str(e)}"
            callbacks.log(f"\n❌ {error_msg}")
            
            # エラー時もログファイルを完了
            if log_writer:
                log_writer.finalize()
            
            # エラー時も実行ログを記録（失敗として）
            try:
                from uuid import uuid4
                execution_id = str(uuid4())
                
                config_snapshot = _capture_config_snapshot(config, overrides_obj)
                
                execution_log = ExecutionLogEntry(
                    execution_id=execution_id,
                    app_version=APP_VERSION,
                    timestamp=datetime.now().isoformat(),
                    output_directory=str(output_base) if 'output_base' in locals() else "",
                    theme=theme,
                    config_snapshot=config_snapshot,
                    prompts=[],
                    generated_files={},
                    success=False,
                    error_message=error_msg,
                    total_duration_sec=time.time() - workflow_start,
                    perplexity_requests=total_usage.perplexity.request_count if 'total_usage' in locals() else 0
                )
                
                logger = ExecutionLogger(PROJECT_ROOT / "logs")
                logger.append_execution_log(execution_log)
                
                callbacks.log(f"✓ エラーログ記録完了: execution_id={execution_id}")
            
            except Exception as log_error:
                callbacks.log(f"⚠ エラーログ記録失敗: {log_error}")
            
            return WorkflowResult(success=False, error_message=error_msg)
        
        finally:
            # Mockモード設定を元に戻す
            if original_mock_mode is not None:
                config.yaml.dev.mock_mode = original_mock_mode
                if use_mock:
                    callbacks.log("🔴 Mockモード設定を元に戻しました")
    
    return asyncio.run(_run_phases())


def scan_assets(project_root: Optional[Path] = None) -> dict[str, list[str]]:
    """アセットフォルダをスキャンしてファイル一覧を取得
    
    Args:
        project_root: プロジェクトルートパス
    
    Returns:
        dict: {"backgrounds": [...], "bgm": [...]}
    """
    root = project_root or PROJECT_ROOT
    
    backgrounds_dir = root / "assets" / "backgrounds"
    bgm_dir = root / "assets" / "bgm"
    
    # 背景画像 (png, jpg, jpeg)
    backgrounds = []
    if backgrounds_dir.exists():
        for ext in ["*.png", "*.jpg", "*.jpeg", "*.webp"]:
            backgrounds.extend([f.name for f in backgrounds_dir.glob(ext)])
    backgrounds.sort()
    
    # BGM (mp3, wav, m4a)
    bgm_files = []
    if bgm_dir.exists():
        for ext in ["*.mp3", "*.wav", "*.m4a", "*.ogg"]:
            bgm_files.extend([f.name for f in bgm_dir.glob(ext)])
    bgm_files.sort()
    
    return {
        "backgrounds": backgrounds,
        "bgm": bgm_files
    }


def _generate_youtube_metadata(
    script: Script,
    chapters: list[ChapterMarker],
    output_path: Path,
    theme: str = "",
    provider: str = "ollama",
    external_metadata: Optional[dict] = None,
    references: Optional[list[ResearchSource]] = None,
    fixed_tags: Optional[list[str]] = None,
) -> dict:
    """YouTube 投稿用メタデータファイルを生成（外部台本モード前提）

    Step 4 v2 (2026-05-10): Gemini packaging prompt 経路は削除済み。
    外部台本モードで VerifiedScript.metadata から事前構築済みの `external_metadata`
    を必須引数として受け取る。

    Args:
        script: 台本データ（タイトル等のフォールバック用）
        chapters: チャプターマーカーリスト
        output_path: 出力パス（metadata.txt）
        theme: 元のテーマ（script.title が空の場合に使用）
        provider: 互換性のため残置（未使用）
        external_metadata: 外部台本モードの事前構築済み dict (必須)。
                          {title, thumbnail_title, description, hashtags} を持つ。
        references: 参考文献 (ResearchSource)。概要欄用テキストに
                    「タイトル(ドメイン名、日付)」表記で出力する (URL は載せない)
        fixed_tags: 固定タグ (config publishing.default_tags)。テーマ由来タグの
                    後ろに結合し、# 付与・60 個上限の正規化をかける

    Returns:
        生成されたメタデータ辞書 {"title": str, "thumbnail_title": str, "description": str}
    """
    if external_metadata is None:
        raise ValueError(
            "Step 4 v2: _generate_youtube_metadata は external_metadata が必須です。"
            "Gemini packaging prompt 経路は削除されました。外部台本モードを使用してください。"
        )

    import json as _json

    metadata = {
        "title": external_metadata.get("title", script.title or theme or ""),
        "thumbnail_title": external_metadata.get("thumbnail_title", ""),
        "description": (external_metadata.get("description", "") or "").strip(),
    }
    hashtags = list(external_metadata.get("hashtags", []) or [])
    # 2026-07-17: テーマ由来タグに # を付与し先頭へ、固定タグを末尾へ、60 個上限
    normalized_hashtags = normalize_hashtags(hashtags, list(fixed_tags or []))
    # 2026-07-17: 参考文献を概要欄用テキストに載せる
    # (2026-10-24 から URL は載せず「タイトル(ドメイン名、日付)」表記)
    reference_lines = format_reference_text_lines(list(references or []))
    lines = [
        "=" * 50,
        "YouTube 投稿用メタデータ (外部台本モード / VerifiedScript)",
        "=" * 50,
        "",
        "【タイトル】",
        metadata["title"],
        "",
        "【サムネイル文字】",
        metadata["thumbnail_title"],
        "",
        "【説明文】",
        metadata["description"],
        "",
        "【ハッシュタグ候補】",
        " ".join(normalized_hashtags),
        "",
    ]
    chapter_block_lines = _build_metadata_chapter_block(chapters)
    if chapter_block_lines:
        lines.extend(chapter_block_lines)
    lines.extend([
        "【概要欄用テキスト】",
        "※ 以下をYouTubeの概要欄にコピー＆ペーストしてください",
        "",
        # 概要欄へ貼る前提のブロックなので URL を落とす (2026-10-24)
        strip_urls(metadata["description"])[0],
        "",
    ])
    if reference_lines:
        lines.append("【参考文献】")
        lines.extend(reference_lines)
        lines.append("")
    lines.extend([
        MEDICAL_DISCLAIMER,
        "",
        "-----------------------------------",
        "■使用音声",
        "VOICEVOX:ずんだもん",
        "VOICEVOX:四国めたん",
        "-----------------------------------",
        "",
        "=" * 50,
    ])
    output_path.write_text("\n".join(lines), encoding="utf-8")
    metadata_json_path = output_path.parent / "video_metadata.json"
    metadata_json_path.write_text(
        _json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return metadata
