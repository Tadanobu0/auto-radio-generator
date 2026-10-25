"""外部台本モード Phase: VerifiedScript JSON ロード → Script 構築 (Phase 1+2 完全 bypass)

Step 3 (2026-05-09) 外部台本モード化で導入。Mac 側 radio_director (Step 1 完了) が
生成した VerifiedScript JSON 1 ファイルを受け取り、Phase 1 (planning, Gemini) と
Phase 2 (scripting, Gemini) を**完全に bypass** して Phase 3 (production) に
直接渡せる ExternalScriptPhaseResult を構築する。

設計方針 (実装プラン B.1 / B.2.4 準拠):
- 既存 services.pipeline (research_phase / scripting_phase / production_phase) と
  対称な構造の独立した phase 関数として実装する
- LLM API は一切呼ばない (ファイル I/O + Pydantic 検証のみ、所要時間 1 秒未満)
- 戻り値 dataclass `ExternalScriptPhaseResult` は既存 RadioScriptArtifact と互換性
  (script + segments + visual_identity + pre_built_metadata) を持つ
- Phase 4 (metadata) で Gemini packaging prompt を bypass するため、
  pre_built_metadata を VerifiedScript.metadata から組み立てる
- workflow.py の研究 import 経路 (research_import_filepath) と同じく、本 phase で
  session への artifact 永続化も行う
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.models import AppConfig
from core.models.curation import ScriptSegment
from core.models.script import RadioScriptArtifact, Script
from core.models.verified_script import VerifiedScript
from core.models.visual import VisualIdentity
from core.session_manager import SessionManager
from services.script_loading.radio_director_loader import (
    RadioDirectorScriptLoader,
    build_script_segments,
    collect_visuals_warnings,
)
from services.script_generation.visual_palette_generator import VisualPaletteGenerator


@dataclass
class ExternalScriptPhaseResult:
    """外部台本モード phase の出力。

    既存 RadioScriptArtifact と互換性のあるフィールドを持ち、Phase 3 (production)
    にそのまま渡せる。`pre_built_metadata` は Phase 4 (metadata generation) で
    Gemini packaging prompt を bypass するための事前構築済みメタデータ。
    """
    script: Script
    segments: List[ScriptSegment]
    pre_built_metadata: Dict[str, Any]
    # Step 6 (2026-05-12): VerifiedScript.metadata.title から
    # VisualPaletteGenerator で動的生成する。Mac Studio Proxy 失敗時は
    # fallback identity (electric cyan + hot magenta) が返るので常に non-None。
    # 型は ScriptingPhaseResult.visual_identity と合わせて Optional[VisualIdentity]。
    visual_identity: Optional[VisualIdentity] = None
    # 元の VerifiedScript も保持（HITL / debug / 再実行で参照可能にする）
    verified_script: Optional[VerifiedScript] = field(default=None, repr=False)
    # 読み込み時の警告（不正な visuals を捨てた章など）。result.json の warnings に載せる（013）
    warnings: List[str] = field(default_factory=list)


async def execute_external_script_phase(
    verified_script_path: Path,
    session_manager: SessionManager,
    config: AppConfig,
    callbacks=None,
) -> ExternalScriptPhaseResult:
    """外部台本モードの phase を実行する。

    Args:
        verified_script_path: VerifiedScript JSON のパス
        session_manager: SessionManager (artifact 永続化用)
        config: AppConfig (本 phase では未使用、シグネチャ整合のため受ける)
        callbacks: ProgressCallback (任意、ログ出力用)

    Returns:
        ExternalScriptPhaseResult: Phase 3 production にそのまま渡せる結果

    Raises:
        FileNotFoundError: パスが存在しない (loader が raise)
        pydantic.ValidationError: スキーマ違反 (loader が raise)
    """
    log = (callbacks.log if callbacks is not None else print)

    log(f"\n== External Script Phase: VerifiedScript ロード ==")
    log(f"  Source: {verified_script_path}")

    # 1. VerifiedScript を Pydantic 検証付きで読む
    text = Path(verified_script_path).read_text(encoding="utf-8")
    vs = VerifiedScript.model_validate_json(text)

    # 2. Script に変換 (loader 経由)
    loader = RadioDirectorScriptLoader()
    script = loader.load(verified_script_path)

    log(f"✓ Script 構築完了: {len(script.sections)} sections / {len(vs.script.segments)} segments")

    # 3. ScriptSegment を構築 (production_phase の chapter rendering で使用)
    segments = build_script_segments(vs)
    # 不正な visuals は章ごとに捨てて scene_splitter に戻す。その警告を処理ログにも残す
    warnings = collect_visuals_warnings(segments)
    for message in warnings:
        log(f"[WARN] {message}")

    # 4. 事前構築メタデータ (Phase 4 で Gemini packaging prompt を bypass するため)
    pre_built_metadata: Dict[str, Any] = {
        "title": vs.metadata.title,
        "thumbnail_title": vs.metadata.thumbnail_title,
        "description": vs.metadata.description,
        "hashtags": list(vs.metadata.hashtags),
    }

    # 5. Step 6 (2026-05-12): テーマ色を Mac Studio Proxy で動的生成。
    # 旧 (Step 4 v2): visual_identity=None を返し DEFAULT_PRIMARY_COLOR (electric cyan)
    # + DEFAULT_SECONDARY_COLOR (hot magenta) が全動画に注入されていた。
    # VisualPaletteGenerator 内部で LLM 失敗時は fallback identity を返すため、
    # 通常 except には到達しない (構築時例外の保険のみ)。
    visual_identity: Optional[VisualIdentity] = None
    try:
        log("\n== External Script Phase: Visual Identity Generation ==")
        palette_generator = VisualPaletteGenerator(config)
        visual_identity = await palette_generator.generate_identity(
            theme=vs.metadata.title,
            script_summary=vs.metadata.description[:300],
        )
        log(f"✓ Visual identity: {visual_identity}")
        # 監査用 (2026-07-15): reasoning を processing_log.txt に残す。
        # フォールバック時は "Fallback visual identity for theme: ..." になるため
        # LLM 生成かフォールバックかをこの行だけでも判別できる。
        if visual_identity.reasoning:
            log(f"  reasoning: {visual_identity.reasoning}")
    except Exception as e:
        log(f"[WARN] Visual identity 生成で予期せぬ例外 (続行): {e}")
        visual_identity = None

    # 6. session に RadioScriptArtifact として永続化 (HITL / debug 用)
    try:
        artifact = RadioScriptArtifact(
            session_id=session_manager.session_id,
            script=script,
            segments=[seg.model_dump() for seg in segments],
            visual_identity=visual_identity.model_dump() if visual_identity else None,
            research_brief_path=None,
            llm_usage=None,
        )
        saved_path = session_manager.save_script_artifact(artifact)
        log(f"✓ RadioScriptArtifact saved: {saved_path}")
    except Exception as e:
        # 永続化失敗は WARNING のみ (動画生成は続行できる)
        log(f"[WARN] RadioScriptArtifact 永続化に失敗（続行）: {e}")

    return ExternalScriptPhaseResult(
        script=script,
        segments=segments,
        pre_built_metadata=pre_built_metadata,
        visual_identity=visual_identity,
        verified_script=vs,
        warnings=warnings,
    )
