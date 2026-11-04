"""自動ラジオ動画生成システム - 共通ワークフロー関数

このモジュールは、CLIとWeb UI両方から呼び出せる
動画生成ワークフローを提供します。
"""
import asyncio
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
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
from services.publishing.text_sanitizer import strip_angle_brackets
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
    upload_skipped: bool = False  # アップロードしない実行で公開を飛ばしたか（012）
    # 警告（不正な visuals を捨てて scene_splitter に戻した章など）。result.json の warnings へ（013）
    warnings: list[str] = field(default_factory=list)
    # 画像プロンプトの記録のパス（prompt_report.md / generation_metadata.jsonl。013）
    prompt_report_path: Optional[Path] = None
    generation_metadata_path: Optional[Path] = None
    # 画像だけを先に作る実行か（016）。True のとき動画・音声・字幕は作っていない
    images_only: bool = False
    # 場面ごとの背景画像の一覧（016）。result.json の scene_images へ（build_scene_images_result_fields）
    scene_images: list[dict] = field(default_factory=list)
    # 画像だけを作る実行で作ったサムネイル背景（018）。<output_dir>/thumbnail_bg.png。作らなかった・失敗したら None
    thumbnail_bg_path: Optional[Path] = None
    # 候補（021）。build_image_candidates_result_fields で result.json へ
    image_candidates: int = 1  # 1 場面あたりの候補数（images_only の candidates）
    thumbnail_bg_candidates: list[dict] = field(default_factory=list)  # サムネイル背景の候補
    thumbnail_bg_contact_sheet: Optional[Path] = None  # サムネイル背景の一覧画像
    thumbnail_bg_pick: Optional[int] = None  # picks の thumbnail で使った候補番号
    # Mac 書き出しの受け口（019）。build_mac_render_result_fields で result.json へ
    reuse_strict: bool = False  # 使い回しの画像が足りなければ止める実行だったか
    reuse_strict_failed: bool = False  # 足りなくて止めたか（このとき success=False）
    reuse_strict_stage: Optional[str] = None  # "before_synthesis"（合成の前）/ "render"（合成の後・画像を作る前）
    missing_scene_images: list[str] = field(default_factory=list)  # 使い回しの画像が無かった場面の segment_id
    thumbnail_bg_source: Optional[str] = None  # 渡した画像を使ったときのそのパス（thumbnail_bg）
    reading_fixes_path: Optional[Path] = None  # 読みの置き換え表のパス
    reading_fixes_applied: int = 0  # 合成に渡す文で置き換えた回数
    upload_error: Optional[str] = None  # アップロードに失敗したときの理由（youtube_dir の token 無しなど）
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
    warnings: list[str] = field(default_factory=list)  # タイムライン計算での警告（013）
    scene_images: list[dict] = field(default_factory=list)  # 場面ごとの背景画像の一覧（016）


@dataclass
class ImagesOnlyPhaseResult:
    """画像だけを先に作る実行（016）の結果"""
    scene_images: list[dict]            # run の下にコピーした後の一覧（image_path はコピー先）
    warnings: list[str] = field(default_factory=list)
    segment_bg_generation_time: float = 0.0


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
    callbacks: Optional[ProgressCallback] = None,
    scene_options=None,
    reading_fixes=None,
) -> ProductionPhaseResult:
    """制作フェーズ: 音声合成 → 動画生成

    reading_fixes（019）: 読みの置き換え表（ReadingFixes）。合成に渡す文だけを直す。
    
    Args:
        script: 台本データ（台本作成フェーズの出力）
        config: アプリケーション設定
        output_dir: 出力ディレクトリ
        project_root: プロジェクトルート
        speed_scale: 音声スピード倍率（オプション）
        segments: スクリプトセグメント（セグメント単位の背景切り替え用）
        visual_identity: ビジュアルアイデンティティ（色とスタイルの統一用）
        callbacks: 進捗コールバック
        scene_options: 前の run の画像の使い回し・指定場面の作り直し（SceneImageOptions。016）

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
    
    if reading_fixes is not None:
        cb.log(f"読みの置き換え表: {getattr(reading_fixes, 'source', None)}（{len(reading_fixes)} 語）")
        voicevox = VoicevoxClient(config, reading_fixes=reading_fixes)
    else:
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
    scene_kwargs = {"scene_options": scene_options} if scene_options is not None else {}
    render_result = await ffmpeg.render(
        **scene_kwargs,
        synthesis_result=synthesis_result,
        background_image=background_image,
        bgm_file=bgm_file,
        output_path=video_output_path,
        subtitle_path=synthesis_result.subtitle_path,
        chapters=synthesis_result.chapters,
        segments=segments,  # セグメント単位の背景切り替え用
        visual_identity=visual_identity,  # ビジュアルアイデンティティを渡す
        # 画像プロンプトの記録は run のディレクトリ直下に置く（サムネと同じ場所。013）
        prompt_log_dir=output_dir,
    )

    render_duration = time.time() - render_start
    render_warnings = list(getattr(render_result, "warnings", None) or [])
    for message in render_warnings:
        cb.log(f"[WARN] {message}")

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
        segment_bg_generation_time=render_result.segment_bg_generation_time,
        warnings=render_warnings,
        scene_images=list(getattr(render_result, "scene_images", None) or []),
    )


async def execute_images_only_phase(
    segments: Optional[list],
    config: AppConfig,
    output_dir: Path,
    visual_identity: Optional[VisualIdentity] = None,
    scene_options=None,
    callbacks: Optional[ProgressCallback] = None,
    speed_scale: Optional[float] = None,
) -> ImagesOnlyPhaseResult:
    """画像だけを先に作る（016）: 場面の区切り → 背景画像の生成 → run の下にコピー。

    音声は作らない。場面の区切りは本番と同じ TimelineCalculator.plan_scene_segments を使う
    （visuals がある章は台本の start_turn で切るので本番と同じ。visuals の無い章は
    発話の長さを文字数から見積もって切るため、本番と場面の数が変わることがある）。
    画像は <output_dir>/scene_images/ にコピーし、一覧を <output_dir>/scene_images.json に書く。
    """
    from core.interfaces import SegmentTiming
    from services.media_processing import ImageProvider
    from services.media_processing.scene_images import estimate_turn_times, export_scene_images
    from services.video_rendering.timeline_calculator import TimelineCalculator

    cb = callbacks or ProgressCallback()
    cb.log("\n== 画像だけを作る実行: 場面ごとの背景画像 ==")
    cb.progress(0.20, "🖼️ 場面ごとの背景画像を生成中...")

    provider = ImageProvider(config, visual_identity=visual_identity, output_dir=output_dir)
    if scene_options is not None:
        provider.scene_options = scene_options
    calculator = TimelineCalculator(config)
    calculator.warnings = []

    audio_config = getattr(config.yaml, "audio_synthesizer", None)
    if speed_scale is None:
        speed_scale = getattr(audio_config, "speed_scale", 1.0)
    speed_scale = float(speed_scale or 1.0)
    pause_sec = float(getattr(audio_config, "pause_between_phrases_ms", 500) or 0) / 1000.0

    start_sec = 0.0
    segment_list = list(segments or [])
    for i, segment in enumerate(segment_list):
        starts, end = estimate_turn_times(list(segment.turns or []), speed_scale, pause_sec)
        timing = SegmentTiming(
            segment_id=segment.segment_id,
            segment_type=segment.segment_type,
            topic_title=segment.topic_title,
            start_sec=start_sec,
            end_sec=start_sec + end,
            duration_sec=end,
            turn_start_secs=[start_sec + s for s in starts],
        )
        _, scene_segments = calculator.plan_scene_segments(segment, timing)
        for scene_segment in scene_segments:
            await provider.get_image_for_segment(scene_segment)
        start_sec = timing.end_sec + pause_sec
        cb.progress(0.20 + 0.75 * (i + 1) / max(1, len(segment_list)), f"🖼️ 背景画像 {i + 1}/{len(segment_list)} 章")

    provider.finish_scene_images()
    export_warnings: list[str] = []
    exported = export_scene_images(provider.scene_records, output_dir, export_warnings)
    warnings = list(calculator.warnings) + list(provider.warnings) + export_warnings
    for message in warnings:
        cb.log(f"[WARN] {message}")
    cb.log(f"✓ 背景画像 {len(exported)} 枚: {Path(output_dir) / 'scene_images'}")
    return ImagesOnlyPhaseResult(
        scene_images=exported,
        warnings=warnings,
        segment_bg_generation_time=provider.get_total_generation_time(),
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
    # 2026-11-02: YouTube はタイトルの半角 < > も拒否する (invalidTitle)
    formatted_title = strip_angle_brackets(formatted_title)

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


# アップロードしない実行のスイッチ (ブリーフ 012)。Mac 側の launch.ps1 が
# このプロセスにだけ設定する。"1" / "true" / "yes" / "on" で有効 (大小無視)。
NO_UPLOAD_ENV = "ARG_NO_UPLOAD"
_TRUTHY = {"1", "true", "yes", "on"}


def no_upload_requested(environ: Optional[dict] = None) -> bool:
    """環境変数 ARG_NO_UPLOAD でアップロードしない実行が指定されているか。"""
    env = os.environ if environ is None else environ
    return str(env.get(NO_UPLOAD_ENV, "")).strip().lower() in _TRUTHY


def build_upload_result_fields(result: "WorkflowResult") -> dict:
    """result.json に足す、公開の有無と mp4 のパスの欄を返す（012）。

    既存の欄は変えず、ここで返す欄を足すだけにする想定。
    upload_status は "uploaded"（公開した）/ "skipped"（アップロードしない実行）/
    "not_uploaded"（設定で無効・Mock・アップロード失敗）のどれか。
    """
    if result.upload_skipped:
        status = "skipped"
    elif result.uploaded_video_url:
        status = "uploaded"
    else:
        status = "not_uploaded"
    return {
        "upload_skipped": bool(result.upload_skipped),
        "upload_status": status,
        "video_path": str(result.video_path) if result.video_path else None,
    }


PROMPT_REPORT_NAME = "prompt_report.md"
GENERATION_METADATA_NAME = "generation_metadata.jsonl"


def find_prompt_logs(output_base: Optional[Path]) -> tuple[Optional[Path], Optional[Path]]:
    """run のディレクトリ直下の画像プロンプトの記録（無ければ None）を返す（013）。

    背景（場面ごと）とサムネの両方が、この 2 ファイルに 1 件ずつ追記される。
    """
    if not output_base:
        return None, None
    report = Path(output_base) / PROMPT_REPORT_NAME
    jsonl = Path(output_base) / GENERATION_METADATA_NAME
    return (report if report.exists() else None), (jsonl if jsonl.exists() else None)


def build_prompt_log_result_fields(result: "WorkflowResult") -> dict:
    """result.json に足す、画像プロンプトの記録のパスの欄を返す（013）。

    パスは Windows 上の絶対パス。記録が無ければ None（静的背景のときなど）。
    """
    return {
        "prompt_report_path": str(result.prompt_report_path) if result.prompt_report_path else None,
        "generation_metadata_path": (
            str(result.generation_metadata_path) if result.generation_metadata_path else None
        ),
    }


# Mac 書き出しの受け口（ブリーフ 019）。run_workflow_sync の引数が正で、引数が無いときだけ
# 環境変数を見る。どれも指定が無ければ今までどおりに動く。
REUSE_STRICT_ENV = "ARG_REUSE_STRICT"     # "1" / "true" / "yes" / "on" で reuse_strict
THUMBNAIL_BG_ENV = "ARG_THUMBNAIL_BG"     # サムネイル背景に使う画像のパス
YOUTUBE_DIR_ENV = "ARG_YOUTUBE_DIR"       # client_secret.json / token.json の置き場
READING_FIXES_ENV = "ARG_READING_FIXES"   # 読みの置き換え表（JSON）のパス


def _env_path(name: str, environ: Optional[dict] = None) -> Optional[str]:
    env = os.environ if environ is None else environ
    value = str(env.get(name, "") or "").strip()
    return value or None


def resolve_mac_render_options(
    reuse_strict: bool = False,
    thumbnail_bg: Optional[str] = None,
    youtube_dir: Optional[str] = None,
    reading_fixes: Optional[str] = None,
    environ: Optional[dict] = None,
) -> dict:
    """引数と環境変数から 019 の 4 つの指定を決める（引数があれば引数）。

    Returns:
        {"reuse_strict": bool, "thumbnail_bg": Path|None, "youtube_dir": Path|None,
         "reading_fixes": Path|None}
    """
    env = os.environ if environ is None else environ
    strict = bool(reuse_strict) or str(env.get(REUSE_STRICT_ENV, "")).strip().lower() in _TRUTHY
    thumb = thumbnail_bg if thumbnail_bg else _env_path(THUMBNAIL_BG_ENV, env)
    ydir = youtube_dir if youtube_dir else _env_path(YOUTUBE_DIR_ENV, env)
    fixes = reading_fixes if reading_fixes else _env_path(READING_FIXES_ENV, env)
    return {
        "reuse_strict": strict,
        "thumbnail_bg": Path(thumb) if thumb else None,
        "youtube_dir": Path(ydir) if ydir else None,
        "reading_fixes": Path(fixes) if fixes else None,
    }


def plan_scene_ids(segments: Optional[list], config: AppConfig, speed_scale: Optional[float] = None) -> list[str]:
    """合成の前に、場面の segment_id を本番の順に見積もる（019 の reuse_strict の事前確認）。

    画像だけを作る実行（016）と同じ区切り方。visuals がある章は台本の start_turn で切るので
    本番と同じ id になる。visuals の無い章は発話の長さを文字数から見積もるため、
    本番（実際の音声の長さ）と場面の数が変わることがある（そのときは本番の画像の段で止まる）。
    """
    from core.interfaces import SegmentTiming
    from services.media_processing.scene_images import estimate_turn_times
    from services.video_rendering.timeline_calculator import TimelineCalculator

    calculator = TimelineCalculator(config)
    calculator.warnings = []
    audio_config = getattr(config.yaml, "audio_synthesizer", None)
    if speed_scale is None:
        speed_scale = getattr(audio_config, "speed_scale", 1.0)
    speed_scale = float(speed_scale or 1.0)
    pause_sec = float(getattr(audio_config, "pause_between_phrases_ms", 500) or 0) / 1000.0

    ids: list[str] = []
    start_sec = 0.0
    for segment in list(segments or []):
        starts, end = estimate_turn_times(list(segment.turns or []), speed_scale, pause_sec)
        timing = SegmentTiming(
            segment_id=segment.segment_id,
            segment_type=segment.segment_type,
            topic_title=segment.topic_title,
            start_sec=start_sec,
            end_sec=start_sec + end,
            duration_sec=end,
            turn_start_secs=[start_sec + s for s in starts],
        )
        _, scene_segments = calculator.plan_scene_segments(segment, timing)
        ids.extend(s.segment_id for s in scene_segments)
        start_sec = timing.end_sec + pause_sec
    return ids


def build_mac_render_result_fields(result: "WorkflowResult") -> dict:
    """result.json に足す、Mac 書き出しの受け口（019）の欄を返す。

    - reuse_strict / reuse_strict_failed / reuse_strict_stage / missing_scene_images:
      使い回しの画像が足りずに止めたか。止めたときは success=False で、stage は
      "before_synthesis"（合成の前に分かった）か "render"（合成の後・画像を作る前に分かった）
    - thumbnail_bg_source: thumbnail_bg で渡した画像（使わなかったら None）
    - reading_fixes_path / reading_fixes_applied: 読みの置き換え表と置き換えた回数
    - upload_error: アップロードに失敗した理由（失敗していなければ None）
    """
    return {
        "reuse_strict": bool(result.reuse_strict),
        "reuse_strict_failed": bool(result.reuse_strict_failed),
        "reuse_strict_stage": result.reuse_strict_stage,
        "missing_scene_images": list(result.missing_scene_images or []),
        "thumbnail_bg_source": str(result.thumbnail_bg_source) if result.thumbnail_bg_source else None,
        "reading_fixes_path": str(result.reading_fixes_path) if result.reading_fixes_path else None,
        "reading_fixes_applied": int(result.reading_fixes_applied or 0),
        "upload_error": result.upload_error,
    }


def build_scene_images_result_fields(result: "WorkflowResult") -> dict:
    """result.json に足す、場面ごとの背景画像の一覧の欄を返す（016）。

    各要素は segment_id / scene_index（章の中の 0 始まり）/ image_path（Windows 上の絶対パス）/
    scene_visual / prompt / seed（不明なら None）/ cached / source
    （"generated" / "cache" / "reused" / "static"）。
    images_only の実行では image_path は <run>/scene_images/ の下のコピーを指す。
    """
    return {"scene_images": [dict(entry) for entry in (result.scene_images or [])]}


def build_image_candidates_result_fields(result: "WorkflowResult") -> dict:
    """result.json に足す、候補（021）の欄を返す。

    - image_candidates: 1 場面あたりの候補数（指定が無ければ 1）
    - thumbnail_bg_candidates / thumbnail_bg_contact_sheet: サムネイル背景の候補と一覧画像
    - thumbnail_bg_pick: picks の thumbnail で本番に使った候補番号（使っていなければ None）
    - image_generation_times: 1 枚ごとの生成時間（秒）の集計。この実行で実際に作った候補
      （キャッシュ・使い回しは除く）だけを数える。
      {"count", "total_sec", "mean_sec", "min_sec", "max_sec",
       "per_image": [{"segment_id", "index", "seed", "generation_time_sec"}, ...]}
      場面の候補ごとの値は scene_images の各要素の candidates[].generation_time_sec にもある。
    """
    per_image = []
    for entry in result.scene_images or []:
        if entry.get("source") == "reused":
            continue
        for cand in entry.get("candidates") or []:
            if cand.get("image_path") and not cand.get("cached"):
                per_image.append({
                    "segment_id": entry.get("segment_id"),
                    "index": cand.get("index"),
                    "seed": cand.get("seed"),
                    "generation_time_sec": float(cand.get("generation_time_sec") or 0.0),
                })
    for cand in result.thumbnail_bg_candidates or []:
        if cand.get("image_path") and not cand.get("cached") and not cand.get("reused"):
            per_image.append({
                "segment_id": "thumbnail",
                "index": cand.get("index"),
                "seed": cand.get("seed"),
                "generation_time_sec": float(cand.get("generation_time_sec") or 0.0),
            })
    times = [item["generation_time_sec"] for item in per_image]
    return {
        "image_candidates": int(result.image_candidates or 1),
        "thumbnail_bg_candidates": [dict(c) for c in (result.thumbnail_bg_candidates or [])],
        "thumbnail_bg_contact_sheet": str(result.thumbnail_bg_contact_sheet) if result.thumbnail_bg_contact_sheet else None,
        "thumbnail_bg_pick": result.thumbnail_bg_pick,
        "image_generation_times": {
            "count": len(times),
            "total_sec": round(sum(times), 3),
            "mean_sec": round(sum(times) / len(times), 3) if times else None,
            "min_sec": round(min(times), 3) if times else None,
            "max_sec": round(max(times), 3) if times else None,
            "per_image": per_image,
        },
    }


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
    youtube_dir: Optional[Path] = None,
    errors: Optional[list] = None,
) -> Optional[str]:
    """YouTube へ動画をアップロードし、アップロード URL を返す（失敗時は None）。

    youtube_dir（019）: 認証ファイルの置き場。指定すると YouTubeClient はそこを読み、
    token が無ければブラウザの同意に進まず失敗する（非致命なのは今までと同じ）。
    errors（019）: 渡されたら、アップロードの失敗の理由をここに足す。

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
        # 保険: 半角 < > は YouTube が invalidTitle / invalidDescription で拒否する (2026-11-02)
        guarded_title = strip_angle_brackets(formatted_title)
        guarded_description = strip_angle_brackets(formatted_description)
        if guarded_title != formatted_title or guarded_description != formatted_description:
            callbacks.log("⚠ タイトル/概要欄に < > が残っていたため取り除いてアップロードします")
        formatted_title, formatted_description = guarded_title, guarded_description
        callbacks.log("[INFO] YouTubeアップロードを開始します...")
        try:
            if youtube_dir is not None:
                callbacks.log(f"[INFO] YouTube の認証ファイルの置き場: {youtube_dir}（ブラウザの同意には進みません）")
                youtube_client = YouTubeClient(config, credentials_dir=youtube_dir)
            else:
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
            if errors is not None:
                errors.append(f"{type(upload_error).__name__}: {upload_error}")
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
    thumbnail_bg_override: Optional[Path] = None,
) -> tuple[Path, float]:
    """サムネイル背景(FLUX.1 dynamic / static フォールバック)と thumbnail.png を生成する。

    thumbnail_bg_override（019）: 渡されたら背景は作らず、その画像を
    <output_base>/thumbnail_bg.png にコピーして使う（thumbnail_background_mode に依らない）。

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

    if thumbnail_bg_override is not None:
        background_image = _use_given_thumbnail_bg(Path(thumbnail_bg_override), output_base, callbacks)
    elif thumbnail_bg_mode == "dynamic":
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


THUMBNAIL_BG_FILENAME = "thumbnail_bg.png"


def _use_given_thumbnail_bg(source: Path, output_base: Path, callbacks) -> Path:
    """渡されたサムネイル背景を <output_base>/thumbnail_bg.png にコピーして返す（019）。

    コピーできなければ元の画像をそのまま使う。
    """
    target = Path(output_base) / THUMBNAIL_BG_FILENAME
    try:
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        callbacks.log(f"[INFO] サムネイル背景は作らず、渡された画像を使います: {source}")
        return target
    except Exception as e:
        callbacks.log(f"⚠ サムネイル背景のコピーに失敗したため、元の画像をそのまま使います: {source} ({e})")
        return source


async def generate_images_only_thumbnail_bg(
    config: AppConfig,
    output_base: Path,
    theme: str,
    script,
    visual_identity,
    callbacks,
) -> tuple[Optional[Path], float, list[str]]:
    """画像だけを作る実行（018）で、サムネイル背景 <output_base>/thumbnail_bg.png を作る。

    書き出しは Mac 側（radio_director）がこの画像を --thumbnail-bg で使う。
    作るかどうかは本番と同じ video_renderer.thumbnail_background_mode で決める（"dynamic" のときだけ）。
    失敗しても例外は投げず、警告を返す（場面画像の結果は壊さない）。

    Returns:
        (thumbnail_bg_path or None, generation_time_sec, warnings)
    """
    video_config = getattr(config.yaml, "video_renderer", None)
    mode = getattr(video_config, "thumbnail_background_mode", "static") if video_config else "static"
    if mode != "dynamic":
        callbacks.log(f"[INFO] thumbnail_background_mode={mode} のため、サムネイル背景は作りません")
        return None, 0.0, []

    output_path = Path(output_base) / THUMBNAIL_BG_FILENAME
    start = time.time()
    try:
        callbacks.log("[INFO] サムネイル背景を生成中（画像だけを作る実行）...")
        description = getattr(script, "description", "") or ""
        title = getattr(script, "title", None)
        generator = ThumbnailBackgroundGenerator(config, output_dir=output_base)
        image_path = await generator.generate(
            theme=theme,
            script_summary=description[:300] if description else (theme or title or ""),
            output_path=output_path,
            visual_identity=visual_identity,
            topic_title=title,
        )
        image_path = Path(image_path)
        if not image_path.is_file():
            raise RuntimeError(f"generated file not found: {image_path}")
        if image_path.resolve() != output_path.resolve():
            shutil.copy2(image_path, output_path)
        elapsed = time.time() - start
        callbacks.log(f"✓ サムネイル背景: {output_path} ({elapsed:.1f}秒)")
        return output_path, elapsed, []
    except Exception as e:
        message = f"thumbnail background generation failed (scene images are kept): {e}"
        callbacks.log(f"[WARN] {message}")
        return None, time.time() - start, [message]


def wants_thumbnail_candidates(scene_options) -> bool:
    """画像だけを作る実行で、サムネイル背景を候補の形で作るか（021）。

    候補数が 2 以上か、サムネイル背景のプロンプトの上書き（prompt_overrides の "thumbnail"）があるとき。
    """
    from services.media_processing.scene_images import THUMBNAIL_KEY

    if scene_options is None:
        return False
    return int(getattr(scene_options, "candidates", 1) or 1) > 1 or THUMBNAIL_KEY in (
        getattr(scene_options, "prompt_overrides", None) or {}
    )


async def generate_images_only_thumbnail_candidates(
    config: AppConfig,
    output_base: Path,
    theme: str,
    script,
    visual_identity,
    callbacks,
    scene_options,
) -> tuple[Optional[Path], float, list[str], Optional[dict]]:
    """画像だけを作る実行で、サムネイル背景を N 枚の候補と一覧画像で作る（021）。

    - 作るかどうかは 018 と同じ（thumbnail_background_mode が "dynamic" のときだけ）
    - 候補は <output_base>/thumbnail_bg_candidates/thumbnail_bg_cK.png、一覧画像は同じ場所の
      thumbnail_bg_sheet.jpg。<output_base>/thumbnail_bg.png は候補 1（picks の thumbnail があればその候補）
    - seed は場面と同じ決め方（キーは "thumbnail"。regen_scenes に "thumbnail" があれば前の run の続きの seed）
    - reuse_images_from の run に候補があり、regen_scenes に "thumbnail" が無ければ作らずにその候補を使う
    - prompt_overrides の "thumbnail" があれば、その文をそのままプロンプトにする
    - scene_images.json に "thumbnail_bg" として書く。失敗しても例外は投げず警告を返す

    Returns:
        (thumbnail_bg_path or None, generation_time_sec, warnings, info or None)
    """
    from services.media_processing.scene_images import (
        THUMBNAIL_CANDIDATES_DIR,
        THUMBNAIL_KEY,
        add_thumbnail_to_manifest,
        export_candidates,
    )

    video_config = getattr(config.yaml, "video_renderer", None)
    mode = getattr(video_config, "thumbnail_background_mode", "static") if video_config else "static"
    if mode != "dynamic":
        callbacks.log(f"[INFO] thumbnail_background_mode={mode} のため、サムネイル背景は作りません")
        return None, 0.0, [], None

    options = scene_options
    output_base = Path(output_base)
    target_dir = output_base / THUMBNAIL_CANDIDATES_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_base / THUMBNAIL_BG_FILENAME
    warnings: list[str] = []
    start = time.time()
    regen = options.is_regen(THUMBNAIL_KEY)
    previous = options.reuse_thumbnail
    override = (options.prompt_overrides or {}).get(THUMBNAIL_KEY)
    info: dict = {}
    try:
        if previous and previous.get("candidates") and not regen:
            callbacks.log(f"[INFO] サムネイル背景は前の run の候補を使います: {options.reuse_dir}")
            if override is not None:
                warnings.append(
                    "prompt override of the thumbnail not used because its candidates were reused "
                    "(add \"thumbnail\" to regen_scenes to make them from the new prompt)"
                )
            candidates = [dict(c, reused=True) for c in previous["candidates"]]
            info.update(prompt=previous.get("prompt"), source="reused")
            if previous.get("prompt_override") is not None:
                info["prompt_override"] = previous["prompt_override"]
        else:
            description = getattr(script, "description", "") or ""
            title = getattr(script, "title", None)
            summary = description[:300] if description else (theme or title or "")
            generator = ThumbnailBackgroundGenerator(config, output_dir=output_base)
            if override is not None:
                prompt = override
                info["prompt_override"] = {"kind": "prompt", "text": override}
            else:
                prompt = await generator.build_prompt(theme, summary, visual_identity, title)
            seeds = options.candidate_seeds(THUMBNAIL_KEY, previous, regen)
            callbacks.log(f"[INFO] サムネイル背景の候補を {len(seeds)} 枚作ります（seed {seeds[0]}〜{seeds[-1]}）")
            candidates = []
            for index, seed in enumerate(seeds, start=1):
                cand = {"index": index, "image_path": None, "seed": seed, "cached": False, "generation_time_sec": 0.0}
                t0 = time.time()
                try:
                    image_path = await generator.generate(
                        theme=theme,
                        script_summary=summary,
                        output_path=target_dir / f"thumbnail_bg_c{index}.png",
                        visual_identity=visual_identity,
                        topic_title=title,
                        prompt=prompt,
                        seed=seed,
                    )
                    if not Path(image_path).is_file():
                        raise RuntimeError(f"generated file not found: {image_path}")
                    cand["image_path"] = str(Path(image_path))
                except Exception as e:
                    cand["error"] = f"{type(e).__name__}: {' '.join(str(e).split())[:160]}"
                cand["generation_time_sec"] = round(time.time() - t0, 3)
                candidates.append(cand)
            info.update(prompt=prompt, source="generated")
            failed = [c["index"] for c in candidates if not c["image_path"]]
            if failed and len(failed) < len(candidates):
                warnings.append(
                    f"{len(failed)} of {len(candidates)} thumbnail background candidates failed "
                    f"(candidate {', '.join(map(str, failed))}); the others are kept"
                )

        candidates, sheet = export_candidates(candidates, target_dir, "thumbnail_bg", "thumbnail", warnings)
        usable = [c for c in candidates if c.get("image_path")]
        if not usable:
            errors = "; ".join(c.get("error", "no image") for c in candidates[:3])
            raise RuntimeError(f"all {len(candidates)} candidates failed ({errors})")
        chosen = usable[0]
        pick = (options.picks or {}).get(THUMBNAIL_KEY)
        if pick is not None:
            match = [c for c in usable if c.get("index") == pick]
            if match:
                chosen = match[0]
                info["picked"] = pick
            else:
                warnings.append(f"picked thumbnail background candidate {pick} has no image; used candidate {chosen['index']}")
        if Path(chosen["image_path"]).resolve() != output_path.resolve():
            shutil.copy2(chosen["image_path"], output_path)
        info.update(image_path=str(output_path.resolve()), candidates=candidates, contact_sheet=sheet)
        add_thumbnail_to_manifest(output_base, info)
        elapsed = time.time() - start
        callbacks.log(f"✓ サムネイル背景の候補 {len(usable)} 枚: {target_dir}（一覧画像: {sheet}）")
        for message in warnings:
            callbacks.log(f"[WARN] {message}")
        return output_path, elapsed, warnings, info
    except Exception as e:
        message = f"thumbnail background generation failed (scene images are kept): {e}"
        callbacks.log(f"[WARN] {message}")
        return None, time.time() - start, warnings + [message], None


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
    skip_upload: bool = False,
    images_only: bool = False,
    regen_scenes: Optional[List[str]] = None,
    regen_seed_offset: Optional[int] = None,
    reuse_images_from: Optional[str] = None,
    reuse_strict: bool = False,
    thumbnail_bg: Optional[str] = None,
    youtube_dir: Optional[str] = None,
    reading_fixes: Optional[str] = None,
    candidates: Optional[int] = None,
    picks: Optional[Any] = None,
    prompt_overrides: Optional[Any] = None,
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
        skip_upload: True なら YouTube への公開（サムネ設定・再生リスト追加を含む）
            だけを飛ばす。音声・画像・字幕・エンコード・メタデータ・サムネ画像は
            本番と同じに作る。環境変数 ARG_NO_UPLOAD=1 でも同じになり、
            どちらかが指定されれば upload_override より優先する（012）
        images_only: True なら台本の読み込み・場面の区切り・背景画像の生成までで止める。
            音声・字幕・エンコード・メタデータ・サムネ・アップロードはしない。画像は
            <run>/scene_images/ にコピーし、一覧を <run>/scene_images.json と
            WorkflowResult.scene_images に入れる（016）
        regen_scenes: 作り直す場面の segment_id のリスト（例 ["deep_dive_2_scene3"]）。
            その場面だけキャッシュを使わず、seed をずらして作り直す（016）
        regen_seed_offset: 作り直す場面の seed のずらし幅（既定 1）。元の seed は
            reuse_images_from の run の記録から取り、無ければ segment_id から決めた値（016）
        reuse_images_from: 前の images_only の run の出力ディレクトリ。その run の
            scene_images.json を読み、segment_id が合う場面はその画像をそのまま使う。
            合わない場面は今までどおり作り、どちらも warnings に残す（016）
        reuse_strict: True なら、reuse_images_from に画像が無い場面（regen_scenes を除く）が
            あるとき、画像を作らずに success=False で止める。合成の前に場面を見積もって確かめ、
            見積もりで漏れた場面も本番の画像の段（画像を作る前）で止める。
            reuse_images_from が要る。環境変数 ARG_REUSE_STRICT=1 でも同じ（019）
        thumbnail_bg: サムネイル背景に使う画像のパス。指定するとサムネイル背景は作らず、
            その画像を <run>/thumbnail_bg.png にコピーして使う。環境変数 ARG_THUMBNAIL_BG（019）
        youtube_dir: YouTube の認証ファイル（client_secret.json / token.json）の置き場。
            指定するとそこを読み、token が無ければブラウザの同意に進まずアップロードを失敗にする
            （動画は成功のまま。理由は WorkflowResult.upload_error）。環境変数 ARG_YOUTUBE_DIR（019）
        reading_fixes: 読みの置き換え表（JSON: 語 → かな書き）のパス。合成に渡す文だけを
            置き換え、字幕は本文のまま。環境変数 ARG_READING_FIXES（019）
        candidates: 1 場面あたりの候補数（images_only のときだけ。1〜16、既定 1 = 今までどおり）。
            2 以上なら各場面を seed 違いで N 枚作り、scene_images/NN_<id>_cK.png と一覧画像
            NN_<id>_sheet.jpg を置く。サムネイル背景も N 枚 + 一覧画像（021）
        picks: 前の run（reuse_images_from）の候補から使う番号。{segment_id: 番号, "thumbnail": 番号}
            の dict か、その JSON ファイルのパス。指定が無い場面は候補 1。reuse_images_from が要る（021）
        prompt_overrides: 場面ごとのプロンプトの上書き。{segment_id: 文}（文をそのままプロンプトに）か
            {segment_id: {focus/draw/avoid}}（描く指示を差し替えて作る）。"thumbnail" は文だけ。
            dict か JSON ファイルのパス。作る場面（regen_scenes・使い回しの無い場面）にだけ効く（021）
        どれも、指定が無ければ今までどおりに動く。指定したファイル・フォルダが無い・読めない
        ときは、合成の前に success=False で止まる。

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

            # 場面の画像の使い回し・作り直し（016）。指定が無ければ None（今までどおり）
            # Mac 書き出しの受け口（019）。指定が無ければどれも None / False（今までどおり）
            mac_options = resolve_mac_render_options(
                reuse_strict=reuse_strict,
                thumbnail_bg=thumbnail_bg,
                youtube_dir=youtube_dir,
                reading_fixes=reading_fixes,
            )
            strict_requested = mac_options["reuse_strict"]
            thumbnail_bg_override = mac_options["thumbnail_bg"]
            youtube_dir_path = mac_options["youtube_dir"]
            reading_fixes_path = mac_options["reading_fixes"]
            reading_fixes_table = None
            try:
                if thumbnail_bg_override is not None and not thumbnail_bg_override.is_file():
                    raise ValueError(f"thumbnail_bg: {thumbnail_bg_override} not found")
                if youtube_dir_path is not None and not youtube_dir_path.is_dir():
                    raise ValueError(f"youtube_dir: {youtube_dir_path} is not a directory")
                if reading_fixes_path is not None:
                    from services.audio_synthesis.reading_fixes import load_reading_fixes
                    reading_fixes_table = load_reading_fixes(reading_fixes_path)
            except ValueError as _e:
                callbacks.log(f"❌ {_e}")
                if log_writer:
                    log_writer.finalize()
                return WorkflowResult(
                    success=False, error_message=str(_e), output_dir=output_base, reuse_strict=strict_requested,
                )
            if thumbnail_bg_override is not None:
                callbacks.log(f"サムネイル背景は渡された画像を使います: {thumbnail_bg_override}")
            if reading_fixes_table is not None:
                callbacks.log(f"読みの置き換え表: {reading_fixes_path}（{len(reading_fixes_table)} 語）")

            scene_options = None
            # 候補・選択・プロンプトの上書き（021）。指定が無ければ None のまま（今までどおり）
            candidate_kwargs = {}
            if candidates is not None:
                candidate_kwargs["candidates"] = candidates
            if picks:
                candidate_kwargs["picks"] = picks
            if prompt_overrides:
                candidate_kwargs["prompt_overrides"] = prompt_overrides
            if regen_scenes or reuse_images_from or strict_requested or candidate_kwargs:
                from services.media_processing.scene_images import build_scene_image_options
                try:
                    scene_options = build_scene_image_options(
                        regen_scenes=regen_scenes,
                        regen_seed_offset=regen_seed_offset,
                        reuse_images_from=reuse_images_from,
                        **({"reuse_strict": True} if strict_requested else {}),
                        **candidate_kwargs,
                    )
                    if scene_options.candidates > 1 and not images_only:
                        raise ValueError(
                            f"candidates: only for images_only runs (got {scene_options.candidates}); "
                            "the full run uses one image per scene (pick it with picks)"
                        )
                except ValueError as _e:
                    callbacks.log(f"❌ {_e}")
                    if log_writer:
                        log_writer.finalize()
                    return WorkflowResult(
                        success=False, error_message=str(_e), output_dir=output_base,
                        reuse_strict=strict_requested,
                    )
                if strict_requested:
                    callbacks.log("使い回しの画像が足りない場面があれば、画像を作らずに止めます（reuse_strict）")
                if scene_options.reuse_dir is not None:
                    callbacks.log(f"前の run の画像を使い回します: {scene_options.reuse_dir}")
                if scene_options.regen_scenes:
                    callbacks.log(
                        f"作り直す場面: {', '.join(sorted(scene_options.regen_scenes))}"
                        f"（seed +{scene_options.regen_seed_offset}）"
                    )
                if scene_options.candidates > 1:
                    callbacks.log(f"1 場面あたり {scene_options.candidates} 枚の候補を作ります")
                if scene_options.picks:
                    callbacks.log(f"選んだ候補: {', '.join(f'{k}={v}' for k, v in scene_options.picks.items())}")
                if scene_options.prompt_overrides:
                    callbacks.log(f"プロンプトを上書きする場面: {', '.join(scene_options.prompt_overrides)}")

            # 選んだサムネイル背景の候補（021）。本番の実行では thumbnail_bg と同じに使う
            thumbnail_bg_pick = None
            if scene_options is not None and not images_only:
                from services.media_processing.scene_images import THUMBNAIL_KEY, pick_thumbnail_path
                _pick = scene_options.picks.get(THUMBNAIL_KEY)
                if _pick is not None and thumbnail_bg_override is not None:
                    callbacks.log(
                        f"[WARN] thumbnail_bg が指定されているため、picks の thumbnail={_pick} は使いません"
                    )
                elif _pick is not None:
                    thumbnail_bg_override = pick_thumbnail_path(scene_options, _pick)
                    thumbnail_bg_pick = _pick
                    callbacks.log(f"サムネイル背景は前の run の候補 {_pick} を使います: {thumbnail_bg_override}")

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

            def _reuse_strict_failure(err) -> WorkflowResult:
                callbacks.log(f"❌ {err}")
                callbacks.log("使い回しの画像が足りないため、画像を作らずに止めました（reuse_strict）")
                if log_writer:
                    log_writer.finalize()
                return WorkflowResult(
                    success=False,
                    error_message=str(err),
                    script=scripting_result.script,
                    theme=theme,
                    output_dir=output_base,
                    warnings=list(getattr(external_phase_result, "warnings", None) or []),
                    reuse_strict=True,
                    reuse_strict_failed=True,
                    reuse_strict_stage=err.stage,
                    missing_scene_images=list(err.missing_ids),
                    images_only=bool(images_only),
                )

            # ========== 使い回しの画像の事前確認（019 reuse_strict）: 合成の前に止める ==========
            if scene_options is not None and getattr(scene_options, "strict", False):
                from services.media_processing.scene_images import ReuseStrictError
                planned_ids = plan_scene_ids(scripting_result.segments, config, overrides_obj.speed_scale)
                missing_ids = scene_options.missing_for(planned_ids)
                if missing_ids:
                    return _reuse_strict_failure(
                        ReuseStrictError(missing_ids, scene_options.reuse_dir, "before_synthesis")
                    )
                callbacks.log(f"✓ 使い回しの画像は {len(planned_ids)} 場面ぶん揃っています（見積もり）")

            # ========== 画像だけを作る実行（016）: 背景画像までで止める ==========
            if images_only:
                from services.media_processing.scene_images import ReuseStrictError
                try:
                    images_result = await execute_images_only_phase(
                        segments=scripting_result.segments,
                        config=config,
                        output_dir=output_base,
                        speed_scale=overrides_obj.speed_scale,
                        visual_identity=scripting_result.visual_identity,
                        scene_options=scene_options,
                        callbacks=callbacks,
                    )
                except ReuseStrictError as _strict_error:
                    return _reuse_strict_failure(_strict_error)
                run_warnings = list(getattr(external_phase_result, "warnings", None) or [])
                run_warnings += list(images_result.warnings)
                # サムネイル背景も作る（018）。失敗しても警告だけにして場面画像の結果は返す
                thumbnail_info = None
                if wants_thumbnail_candidates(scene_options):
                    # 候補の形で作る（021）
                    (
                        thumbnail_bg_path, thumbnail_bg_time, thumbnail_bg_warnings, thumbnail_info,
                    ) = await generate_images_only_thumbnail_candidates(
                        config=config,
                        output_base=output_base,
                        theme=theme,
                        script=scripting_result.script,
                        visual_identity=scripting_result.visual_identity,
                        callbacks=callbacks,
                        scene_options=scene_options,
                    )
                else:
                    thumbnail_bg_path, thumbnail_bg_time, thumbnail_bg_warnings = await generate_images_only_thumbnail_bg(
                        config=config,
                        output_base=output_base,
                        theme=theme,
                        script=scripting_result.script,
                        visual_identity=scripting_result.visual_identity,
                        callbacks=callbacks,
                    )
                run_warnings += thumbnail_bg_warnings
                total_usage.thumbnail_bg_generation_time = thumbnail_bg_time
                prompt_report_path, generation_metadata_path = find_prompt_logs(output_base)
                total_usage.segment_bg_generation_time = images_result.segment_bg_generation_time
                total_usage.total_duration_sec = time.time() - workflow_start
                callbacks.log("\n== 完了（画像だけを作る実行。音声・動画・アップロードは行っていません） ==")
                callbacks.log(f"場面の画像: {len(images_result.scene_images)} 枚 ({output_base / 'scene_images'})")
                if run_warnings:
                    callbacks.log(f"警告 {len(run_warnings)} 件:")
                    for message in run_warnings:
                        callbacks.log(f"  - {message}")
                callbacks.progress(1.0, "✨ 背景画像の生成が完了しました")
                if log_writer:
                    log_writer.finalize()
                return WorkflowResult(
                    success=True,
                    script=scripting_result.script,
                    usage=total_usage,
                    upload_skipped=True,
                    warnings=run_warnings,
                    prompt_report_path=prompt_report_path,
                    generation_metadata_path=generation_metadata_path,
                    images_only=True,
                    reuse_strict=strict_requested,
                    scene_images=images_result.scene_images,
                    thumbnail_bg_path=thumbnail_bg_path,
                    image_candidates=int(getattr(scene_options, "candidates", 1) or 1),
                    thumbnail_bg_candidates=list((thumbnail_info or {}).get("candidates") or []),
                    thumbnail_bg_contact_sheet=(
                        Path(thumbnail_info["contact_sheet"])
                        if thumbnail_info and thumbnail_info.get("contact_sheet") else None
                    ),
                    thumbnail_bg_pick=(thumbnail_info or {}).get("picked"),
                    theme=theme,
                    output_dir=output_base,
                    segment_bg_generation_time=images_result.segment_bg_generation_time,
                    thumbnail_bg_generation_time=thumbnail_bg_time,
                    visual_identity=scripting_result.visual_identity,
                )

            # ========== Phase 3: 制作（音声合成 → 動画生成） ==========
            production_kwargs = {"scene_options": scene_options} if scene_options is not None else {}
            if reading_fixes_table is not None:
                production_kwargs["reading_fixes"] = reading_fixes_table
            from services.media_processing.scene_images import ReuseStrictError
            try:
                production_result = await execute_production_phase(
                    script=scripting_result.script,
                    config=config,
                    output_dir=output_base,
                    project_root=PROJECT_ROOT,
                    speed_scale=overrides_obj.speed_scale,
                    segments=scripting_result.segments,  # セグメント情報を渡す
                    visual_identity=scripting_result.visual_identity,  # ビジュアルアイデンティティを渡す
                    callbacks=callbacks,
                    **production_kwargs,
                )
            except ReuseStrictError as _strict_error:
                return _reuse_strict_failure(_strict_error)
            
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
                **({"thumbnail_bg_override": thumbnail_bg_override} if thumbnail_bg_override is not None else {}),
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
            # アップロードしない実行では公開だけを飛ばす（012）
            upload_skipped = bool(skip_upload or no_upload_requested())
            upload_errors: list[str] = []
            if upload_skipped:
                uploaded_video_url = None
                callbacks.log(
                    "[INFO] アップロードしない実行のため YouTube への公開"
                    f"（サムネ設定・再生リスト追加を含む）を飛ばしました: {production_result.video_path}"
                )
            else:
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
                    errors=upload_errors,
                    **({"youtube_dir": youtube_dir_path} if youtube_dir_path is not None else {}),
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

            # result.json の warnings に載せる警告（読み込み時 + タイムライン計算時。013）
            run_warnings = list(getattr(external_phase_result, "warnings", None) or [])
            run_warnings += list(getattr(production_result, "warnings", None) or [])
            prompt_report_path, generation_metadata_path = find_prompt_logs(output_base)

            callbacks.log(f"\n== 完了 ==")
            callbacks.log(f"動画: {production_result.video_path}")
            if prompt_report_path:
                callbacks.log(f"画像プロンプトの記録: {prompt_report_path}")
            if run_warnings:
                callbacks.log(f"警告 {len(run_warnings)} 件:")
                for message in run_warnings:
                    callbacks.log(f"  - {message}")
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
                upload_skipped=upload_skipped,
                upload_error=upload_errors[0] if upload_errors else None,
                reuse_strict=strict_requested,
                thumbnail_bg_source=str(thumbnail_bg_override) if thumbnail_bg_override is not None else None,
                thumbnail_bg_pick=thumbnail_bg_pick,
                reading_fixes_path=reading_fixes_path if reading_fixes_table is not None else None,
                reading_fixes_applied=reading_fixes_table.applied_count if reading_fixes_table is not None else 0,
                warnings=run_warnings,
                prompt_report_path=prompt_report_path,
                generation_metadata_path=generation_metadata_path,
                scene_images=list(getattr(production_result, "scene_images", None) or []),
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
