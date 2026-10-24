"""設定モデル（Pydantic）"""
import os
from pathlib import Path
from typing import Literal, Dict, Optional, List

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# =============================================================================
# 環境変数設定 (.env)
# =============================================================================
class EnvSettings(BaseSettings):
    """環境変数から読み込む設定"""
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )
    
    perplexity_api_key: str = Field(default="", alias="PERPLEXITY_API_KEY")
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    voicevox_base_url: str = Field(
        default="http://localhost:50021",
        alias="VOICEVOX_BASE_URL"
    )


# =============================================================================
# YAML設定モデル (config.yaml)
# =============================================================================

# リサーチモード設定
class ResearchModeConfig(BaseModel):
    """リサーチモードの設定"""
    name: str
    description: str
    system_prompt: str


class ResearcherConfig(BaseModel):
    """リサーチャー（Perplexity）設定"""
    model: str = "sonar-pro"
    max_tokens: int = 2048
    max_queries_per_plan: int = 3
    max_requests_per_workflow: int = 6
    enable_session_cache: bool = True
    modes: Dict[str, ResearchModeConfig] = Field(default_factory=dict)


# 台本生成設定
class ModelCost(BaseModel):
    """モデルのコスト情報（USD per 1M tokens）"""
    input: float
    output: float


class GeminiConfig(BaseModel):
    """Gemini API設定"""
    model: str = "gemini-3.1-pro-preview"
    fallback_model: str = "gemini-2.5-pro"
    flash_model: str = "gemini-2.5-flash"  # 軽量モデル（サムネイル再作成用）
    max_tokens: int = 8192
    costs: Dict[str, ModelCost] = Field(default_factory=dict)


class OpenAIConfig(BaseModel):
    """OpenAI API設定"""
    model: str = "gpt-4o-mini"
    fallback_model: str = "gpt-4o"
    max_tokens: int = 8192
    temperature: float = 0.85
    costs: Dict[str, ModelCost] = Field(default_factory=dict)


class AnthropicConfig(BaseModel):
    """Anthropic API設定"""
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 8192
    temperature: float = 0.85
    costs: Dict[str, ModelCost] = Field(default_factory=dict)


class OllamaConfig(BaseModel):
    """Ollama（ローカルLLM）設定

    既定値は shipped config.yaml の現行運用値（GX10 + プロキシ越しの
    DeepSeekV4Flash / temperature=0.7）に揃える。YAML 欠損時に旧 model
    （gpt-oss:20b-long）でアクセスしないよう SSOT 整合を取る。
    2026-07: バックエンドが DeepSeekV4Flash（2 ノードクラスター）に移行。
    served-model-name (/v1/models 実測) の deepseek-v4-flash に統一。
    """
    model: str = "deepseek-v4-flash"
    # 2026-07-14: 無線停止に伴い有線 IP (en0) へ変更。旧値は無線 en1 の 192.168.0.3。
    base_url: str = "http://192.168.0.86:11435/v1"  # Production: Mac 有線 IP (via queue proxy on 11435)
    max_tokens: int = 16384  # Increased for long-form content
    temperature: float = 0.7
    costs: Dict[str, ModelCost] = Field(default_factory=dict)
    # 2026-05-06: vLLM (Qwen3.5 thinking model) では chat_template_kwargs.enable_thinking
    # を False にしないと thinking token が max_tokens を食い潰し空応答になる。
    # 現時点は全エージェントで False を採用。将来エージェント別に True へ切替えられる
    # よう config 駆動化しておく（既定値は False）。Ollama 本体は extra_body の未知
    # フィールドを無視するため、Ollama 経路でも無害。
    enable_thinking: bool = Field(
        default=False,
        description=(
            "True にすると vLLM (OpenAI 互換) 側で thinking token を許可する。"
            "Qwen3.5 系の thinking モデルでは False 推奨（既定）。"
        ),
    )
    # 2026-07: DeepSeekV4Flash 移行対応。/no_think は Qwen3 系 chat template 専用の
    # 制御トークンで、DeepSeek には制御トークンでなくただのリテラルとして届き
    # プロンプト汚染になる。デフォルト True は既存挙動（Qwen 系）の維持のため。
    # shipped config.yaml では false を明示（thinking 抑制は Proxy 側
    # reasoning_effort="none" で担保）。
    inject_no_think: bool = Field(
        default=True,
        description=(
            "True にすると enable_thinking=False のリクエストで system prompt 先頭に "
            "/no_think トークンを注入する（Qwen3 系 chat template 専用）。"
            "DeepSeek 等 Qwen 以外のバックエンドでは False にすること。"
        ),
    )


class ScriptStructureConfig(BaseModel):
    """台本構成比率"""
    main_topic_ratio: int = 70
    listener_mail_ratio: int = 20
    ending_ratio: int = 10


class CurrencyConfig(BaseModel):
    """通貨換算設定"""
    usd_to_jpy: float = 150.0


class OrchestratorSegmentConfig(BaseModel):
    """オーケストレーターのセグメント設定"""
    min_turns: int = 10
    max_turns: int = 20


class ShowRunnerConfig(BaseModel):
    """ShowRunner（番組構成プランナー）設定 - Phase 3 施策④

    SSOT: 既定値・docstring・shipped config.yaml はいずれも enabled=True で統一する。
    Phase 3 施策④のロールアウト完了に伴い、ラジオ番組生成のデフォルトパイプラインの
    一部として ShowRunner が自動実行される。意図的に無効化したい場合のみ
    config.yaml 側で enabled: false を明示する（従来フローと完全互換）。
    """
    enabled: bool = Field(
        default=True,
        description="Trueにするとショーランナー（番組構成プランナー）をCurator後に実行する（既定: True）"
    )
    model: str = Field(
        default="",
        description="ShowRunner用モデル（空=curator_modelと同じ軽量モデルを使用）"
    )
    max_tokens: int = Field(
        default=4096,
        ge=256,
        description=(
            "LLM への max_tokens（出力トークン上限）。ShowPlan は compact な単一 JSON。"
            "この上限で切り詰められた（finish_reason=length）場合、RuntimeError を送出し"
            "orchestrator 側のフェイルオープンで show_plan=None に落とす（PR-D Issue C）。"
        ),
    )


class FactExtractorConfig(BaseModel):
    """FactExtractor（Research 事実抽出）設定 - Phase 4 施策③

    SSOT: 既定値・docstring・shipped config.yaml はいずれも enabled=True で統一する。
    Phase 4 施策③のロールアウト完了に伴い、ラジオ番組生成のデフォルトパイプラインの
    一部として FactExtractor が自動実行される。意図的に無効化したい場合のみ
    config.yaml 側で enabled: false を明示する（従来フローと完全互換）。
    """
    enabled: bool = Field(
        default=True,
        description="Trueにするとリサーチ生文字列からFactSheetを抽出してCuratorに渡す（既定: True）"
    )
    model: str = Field(
        default="",
        description="FactExtractor用モデル（空=curator_modelと同じ軽量モデルを使用）"
    )
    max_facts: int = Field(
        default=30,
        ge=1,
        description="抽出するファクトの最大数（LLMへの指示値、実出力はこれ以下になりうる）"
    )
    max_tokens: int = Field(
        default=8192,
        ge=256,
        description=(
            "LLM への max_tokens（出力トークン上限）。"
            "max_facts に見合う十分な値を与えること。"
            "この上限で切り詰められた（finish_reason=length）場合、"
            "FactExtractor は部分 JSON を返さず例外を投げる（フェイルファスト）。"
        )
    )


# ---------------------------------------------------------------------------
# PR-D (Issue C): 他エージェントの max_tokens config 駆動化
# ---------------------------------------------------------------------------
# PR-A で FactExtractor に採用した「max_tokens を config 駆動化 + finish_reason=length
# で RuntimeError 送出」パターンを、他の 5 エージェント（TopicCurator / ShowRunner /
# SegmentGenerator 3 phase / MetadataGenerator）に横展開する。
#
# 既定値は各エージェントの旧ハードコード値をそのまま踏襲（後方互換優先）。
# 数値の引き上げ調整は運用判断として config.yaml で変更する。

class TopicCuratorConfig(BaseModel):
    """TopicCurator（トピック選定）設定 - PR-D Issue C 横展開"""
    max_tokens: int = Field(
        default=8192,
        ge=256,
        description=(
            "LLM への max_tokens。旧ハードコード 8192 を踏襲。"
            "切り詰め時は RuntimeError 送出 → orchestrator がフェイルオープン。"
        ),
    )


class SegmentGeneratorConfig(BaseModel):
    """SegmentGenerator（セグメント生成）設定 - PR-D Issue C 横展開

    1-phase JSON 生成と 2-phase 生成（Markdown → JSON）で異なる max_tokens を必要とするため、
    フェーズ別に 3 フィールドを持つ。
    """
    max_tokens_single: int = Field(
        default=8192,
        ge=256,
        description="1-phase JSON 生成時の max_tokens（旧ハードコード 8192）",
    )
    max_tokens_phase1: int = Field(
        default=4096,
        ge=256,
        description="2-phase 生成の Phase 1（Markdown creative）の max_tokens（旧ハードコード 4096）",
    )
    max_tokens_phase2: int = Field(
        default=2048,
        ge=256,
        description="2-phase 生成の Phase 2（JSON 構造化）の max_tokens（旧ハードコード 2048）",
    )


class MetadataGeneratorConfig(BaseModel):
    """MetadataGenerator（後処理メタデータ生成）設定 - PR-D Issue C 横展開"""
    max_tokens: int = Field(
        default=8192,
        ge=256,
        description=(
            "LLM への max_tokens。2026-04-24 に 2048→4096 へ引き上げた後も、"
            "本運用で 2 セッション連続 truncation が発生したため、2026-04-27 に "
            "4096→8192 へ再引き上げ。出力想定: title/thumbnail_title/description/"
            "hashtags 合計 ~580 文字 × 日本語トークン化率 ~2.5 = 実使用 ~1500 "
            "トークン + JSON オーバーヘッドだが、実測で 4096 が不足したため "
            "8192 を新たな運用値とする。切り詰め時は RuntimeError 送出 → "
            "呼び出し側がデフォルトメタデータへフォールバック。"
        ),
    )


class FactCheckerConfig(BaseModel):
    """FactChecker（生成台本のハルシネーション検出）設定

    SSOT: 既定値・docstring・shipped config.yaml はいずれも enabled=True で統一する。
    生成台本（Script）とリサーチデータ（ResearchBrief）を LLM に投げ込み、
    ハルシネーション・誇張・出典不明な主張を検出する後処理エージェント。

    フェイルオープン契約: FactChecker のエラーはパイプラインを止めない。
    呼び出し側（scripting_phase）が except Exception で WARNING に落とし、
    factcheck_report.json は単に生成されないだけで台本生成は完走する。
    """
    enabled: bool = Field(
        default=True,
        description="True にするとファクトチェックを実行（既定: True、エラーはフェイルオープン）",
    )
    model: str = Field(
        default="",
        description="FactChecker 用モデル（空=curator_model にフォールバック）",
    )
    max_tokens: int = Field(
        default=8192,
        ge=256,
        description=(
            "LLM への max_tokens。台本全体 + リサーチを評価して JSON を返すため、"
            "issues が多いと膨らみやすい。length 切り詰め時は WARNING + フォールバック。"
        ),
    )
    min_confidence_warning: int = Field(
        default=60,
        ge=0,
        le=100,
        description=(
            "overall_confidence がこの値以下の場合、processing_log.txt に "
            "WARNING を出力して人間の確認を促す（既定: 60）。"
        ),
    )
    script_char_limit: int = Field(
        default=8000,
        ge=500,
        description="LLM に渡す台本本文の最大文字数（先頭から切り出し）。長すぎるトークン消費を抑制。",
    )
    research_char_limit: int = Field(
        default=8000,
        ge=500,
        description="LLM に渡すリサーチデータの最大文字数（先頭から切り出し）。",
    )
    # Phase 3A: 自動修正エンジン (FactFixAgent)
    auto_fix: bool = Field(
        default=True,
        description=(
            "True にすると high/medium severity の issue を LLM で自動修正し、"
            "script_fixed.json を出力する。修正は failopen（エラーは WARNING のみ）。"
        ),
    )
    auto_fix_max_tokens: int = Field(
        default=1024,
        ge=128,
        description="FactFixAgent の 1 件あたり max_tokens（短い書き換えのみのため小さめ）",
    )


class OrchestratorConfig(BaseModel):
    """Hierarchical Agentic Workflow オーケストレーター設定

    SSOT: 既定値・docstring・shipped config.yaml はいずれも enabled=True で統一する。
    新アーキテクチャ（TopicCuration → ShowRunner → SegmentGeneration → MetadataGenerator）
    がデフォルトパイプラインとなったため、意図的に旧経路（単一 API 呼び出し）に
    落としたい場合のみ config.yaml 側で enabled: false を明示する。
    ShowRunnerConfig / FactExtractorConfig と同型の SSOT 整合対応。
    """

    # Direct Regex Bypass: Phase 2 JSON変換をスキップするプロバイダー
    # ローカルLLMはJSON構造化が不安定なため、正規表現パーサーを優先
    LOCAL_LLM_PROVIDERS: set[str] = {"ollama", "lmstudio", "localai"}

    enabled: bool = Field(
        default=True,
        description="Trueにすると新アーキテクチャ（推奨）を使用（既定: True）"
    )
    two_phase_generation: bool = Field(
        default=False,
        description="Trueにすると2段階生成（Markdown→JSON）を使用"
    )
    curator_model: str = Field(
        default="gemini-2.5-flash",
        description="キュレーション・要約用軽量モデル"
    )
    segment_model: str = Field(
        default="",
        description="Phase 1（クリエイティブ生成）用モデル（空の場合はデフォルトモデルを使用）"
    )
    json_model: str = Field(
        default="",
        description="Phase 2（JSON構造化）専用モデル（空の場合はsegment_modelと同じ）"
    )
    max_topics: int = Field(default=3, description="キュレーションで選定する最大トピック数")
    context_summary_max_length: int = Field(
        default=300, description="文脈要約の最大文字数"
    )
    intro: OrchestratorSegmentConfig = Field(
        default_factory=lambda: OrchestratorSegmentConfig(min_turns=10, max_turns=20)
    )
    deep_dive: OrchestratorSegmentConfig = Field(
        default_factory=lambda: OrchestratorSegmentConfig(min_turns=25, max_turns=45)
    )
    conclusion: OrchestratorSegmentConfig = Field(
        default_factory=lambda: OrchestratorSegmentConfig(min_turns=10, max_turns=20)
    )
    # Phase 3 施策④: 番組構成プランナー（後方互換: 既定は disabled）
    show_runner: ShowRunnerConfig = Field(default_factory=ShowRunnerConfig)
    # Phase 4 施策③: Research 事実抽出エージェント（後方互換: 既定は disabled）
    fact_extractor: FactExtractorConfig = Field(default_factory=FactExtractorConfig)
    # PR-D Issue C: max_tokens 横展開
    topic_curator: TopicCuratorConfig = Field(default_factory=TopicCuratorConfig)
    segment_generator: SegmentGeneratorConfig = Field(default_factory=SegmentGeneratorConfig)
    metadata_generator: MetadataGeneratorConfig = Field(default_factory=MetadataGeneratorConfig)
    # FactChecker: 生成台本のハルシネーション検出（後処理、フェイルオープン）
    fact_checker: FactCheckerConfig = Field(default_factory=FactCheckerConfig)


class ScriptGeneratorConfig(BaseModel):
    """台本生成エンジン設定"""
    default_provider: str = "gemini"
    gemini: GeminiConfig = Field(default_factory=GeminiConfig)
    openai: OpenAIConfig = Field(default_factory=OpenAIConfig)
    anthropic: AnthropicConfig = Field(default_factory=AnthropicConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    structure: ScriptStructureConfig = Field(default_factory=ScriptStructureConfig)
    currency: CurrencyConfig = Field(default_factory=CurrencyConfig)
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)


class SpeakersConfig(BaseModel):
    """話者ID設定"""
    main: int = 3  # ずんだもん
    sub: int = 2   # 四国めたん


class AudioSynthesizerConfig(BaseModel):
    """音声合成設定"""
    speakers: SpeakersConfig = Field(default_factory=SpeakersConfig)
    speed_scale: float = 1.0
    pitch_scale: float = 0.0
    intonation_scale: float = 1.0
    volume_scale: float = 1.0
    pause_between_phrases_ms: int = 500


class VideoRendererConfig(BaseModel):
    """動画生成設定"""
    output_resolution: str = "1920x1080"
    output_fps: int = 30
    output_codec: str = "libx264"
    output_audio_codec: str = "aac"
    output_audio_bitrate: str = "192k"
    use_gpu: bool = True  # GPU加速（NVENC）を使用
    bgm_volume: float = 0.15
    bgm_fade_in_sec: float = 3.0
    bgm_fade_out_sec: float = 3.0
    # スペクトラム可視化設定
    enable_spectrum: bool = True
    spectrum_color: str = "0x00FF88"
    spectrum_mode: str = "cline"
    # 背景画像生成設定
    background_mode: str = "static"  # "static" or "dynamic"
    thumbnail_background_mode: str = "static"  # "static" or "dynamic"
    # 章の中の場面分割(1 章を約 scene_target_sec ごとに 1 枚、1〜max_scenes_per_chapter 枚)
    scene_target_sec: float = 90.0
    max_scenes_per_chapter: int = 4
    # 各場面の背景をゆっくり動かす
    enable_pan: bool = True
    # "perspective": 小数ピクセルで滑らかに動かす(平行移動 + ゆっくりズーム、010)
    # "crop": 009 の方式(1.05 倍に拡大して crop の x を動かす。整数ピクセル単位でカクつく)
    pan_method: str = "perspective"
    # perspective: ズームを pan_zoom_min ⇔ pan_zoom_max で動かし、画面中央の点を画面幅の
    # (pan_zoom_min - 1) だけ平行移動させる。pan_full_range_sec より短い場面は同じ速さで途中まで、
    # 長い場面は全体をゆっくり動く
    pan_zoom_min: float = 1.12
    pan_zoom_max: float = 1.22
    pan_full_range_sec: float = 90.0
    # perspective を掛ける作業用の幅(0 = 出力と同じ)。perspective は毎コマ 1 スレッドで画素ごとの
    # 対応表を作り直すので、小さい画像で動かしてから出力の大きさに拡大する
    pan_work_width: int = 0
    # crop: 場面の長さで画面幅の pan_ratio だけ動く
    pan_ratio: float = 0.05
    # 映像トラック(Phase B)を NVENC でエンコードするか。use_gpu は 1 枚画像モードだけに効き、
    # 映像トラックは既定で libx264 ultrafast のまま(009 の試しエンコードを見て PM が決める)
    video_track_use_gpu: bool = False
    # 映像トラックの libx264 の preset
    video_track_x264_preset: str = "ultrafast"


class FluxConfig(BaseModel):
    """FLUX.1 (Forge API) 設定"""
    base_url: str = "http://127.0.0.1:7890"
    timeout: int = 120
    steps: int = 20
    width: int = 1344
    height: int = 768
    sampler_name: str = "Euler"
    scheduler: str = "Simple"
    cfg_scale: float = 1.0
    enable_pre_generation_cleanup: bool = True
    enable_resolution_fallback: bool = True
    fallback_resolutions: list[list[int]] = [[896, 504], [768, 432], [640, 360]]


class ComfyUIConfig(BaseModel):
    """ComfyUI API 設定"""
    base_url: str = "http://127.0.0.1:8188"
    workflow_path: str = "config/workflow_api.json"
    timeout: int = 600
    steps: int = 4
    width: int = 768
    height: int = 432
    cfg: float = 1.0
    sampler_name: str = "euler"
    scheduler: str = "normal"


class VideoConfig(BaseModel):
    """動画表示設定（オーバーレイ等）"""
    show_topic_overlay: bool = True


class PathsConfig(BaseModel):
    """パス設定"""
    assets_dir: str = "assets"
    output_dir: str = "output"
    background_image: str = "assets/backgrounds/default.png"
    bgm_file: str = "assets/bgm/default.mp3"


class PersonalityConfig(BaseModel):
    """パーソナリティ設定"""
    name: str
    description: str


class PersonalitiesConfig(BaseModel):
    """パーソナリティ一覧"""
    main: PersonalityConfig = Field(
        default_factory=lambda: PersonalityConfig(
            name="ずんだもん",
            description="元気で明るいメインパーソナリティ。語尾に「なのだ」をつける。"
        )
    )
    sub: PersonalityConfig = Field(
        default_factory=lambda: PersonalityConfig(
            name="めたん",
            description="落ち着いた雰囲気のアシスタント。丁寧な言葉遣い。"
        )
    )


class DevConfig(BaseModel):
    """開発用設定（Mockモード等）"""
    mock_mode: bool = Field(default=False)
    mock_data_path: str = Field(default="tests/mock_data")
    mock_skip_metadata: bool = Field(default=False)
    mock_skip_thumbnail: bool = Field(default=False)


class PublishingConfig(BaseModel):
    """公開・配信設定（YouTube等）"""
    enable_upload: bool = Field(default=False)
    privacy_status: str = Field(default="unlisted")
    category_id: str = Field(default="27")  # Education
    playlist_id: str = Field(default="")
    default_tags: List[str] = Field(
        default_factory=lambda: ["#ずんだもん", "#VOICEVOX", "#AI", "#ラジオ"]
    )
    footer_text: str = Field(
        default=(
            "-----------------------------------\n"
            "■使用音声\n"
            "VOICEVOX:ずんだもん\n"
            "VOICEVOX:四国めたん\n"
            "-----------------------------------"
        )
    )


class YamlConfig(BaseModel):
    """config.yamlから読み込む設定"""
    researcher: ResearcherConfig = Field(default_factory=ResearcherConfig)
    script_generator: ScriptGeneratorConfig = Field(default_factory=ScriptGeneratorConfig)
    audio_synthesizer: AudioSynthesizerConfig = Field(default_factory=AudioSynthesizerConfig)
    video_renderer: VideoRendererConfig = Field(default_factory=VideoRendererConfig)
    video: VideoConfig = Field(default_factory=VideoConfig)
    flux: FluxConfig = Field(default_factory=FluxConfig)
    comfyui: ComfyUIConfig = Field(default_factory=ComfyUIConfig)
    image_provider: str = "forge"  # "forge" or "comfyui"
    paths: PathsConfig = Field(default_factory=PathsConfig)
    personalities: PersonalitiesConfig = Field(default_factory=PersonalitiesConfig)
    dev: DevConfig = Field(default_factory=DevConfig)  # 開発用設定
    publishing: PublishingConfig = Field(default_factory=PublishingConfig)

    @model_validator(mode="after")
    def validate_image_provider(self):
        """Validate image_provider value"""
        valid_providers = {"forge", "comfyui"}
        if self.image_provider not in valid_providers:
            raise ValueError(
                f"Invalid image_provider: '{self.image_provider}'. "
                f"Must be one of: {', '.join(sorted(valid_providers))}"
            )
        return self


# =============================================================================
# 統合設定クラス
# =============================================================================
class AppConfig(BaseModel):
    """アプリケーション全体の設定"""
    env: EnvSettings
    yaml: YamlConfig
    project_root: Path

    class Config:
        arbitrary_types_allowed = True


def load_config(
    project_root: Path | str | None = None,
    config_file: str = "config.yaml"
) -> AppConfig:
    """設定を読み込む
    
    Args:
        project_root: プロジェクトルートディレクトリ
        config_file: YAML設定ファイル名
    
    Returns:
        AppConfig: 統合設定オブジェクト
    """
    if project_root is None:
        project_root = Path(__file__).parent.parent.parent
    else:
        project_root = Path(project_root)
    
    # .envファイルのパスを設定
    env_file = project_root / ".env"
    if env_file.exists():
        os.environ.setdefault("ENV_FILE", str(env_file))
    
    # 環境変数を読み込み
    env_settings = EnvSettings(_env_file=env_file if env_file.exists() else None)
    
    # YAML設定を読み込み
    yaml_path = project_root / config_file
    if yaml_path.exists():
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                yaml_data = yaml.safe_load(f)
            yaml_config = YamlConfig.model_validate(yaml_data)
        except Exception as e:
            print(f"WARNING: YAML設定読み込み失敗: {e}")
            print(f"デフォルト設定を使用します")
            yaml_config = YamlConfig()
    else:
        print(f"WARNING: YAML設定ファイルが見つかりません: {yaml_path}")
        print(f"デフォルト設定を使用します")
        yaml_config = YamlConfig()
    
    return AppConfig(
        env=env_settings,
        yaml=yaml_config,
        project_root=project_root
    )
