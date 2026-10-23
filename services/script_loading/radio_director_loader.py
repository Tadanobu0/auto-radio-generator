"""RadioDirectorScriptLoader - Mac 側 radio_director パイプラインの VerifiedScript JSON を Script に変換

Step 3 (2026-05-09) 外部台本モード化で導入。`IScriptLoader` 実装。

VerifiedScript の構造 (Mac 側 SSOT) を Windows 側 `core.models.script.Script` へ
変換する責務のみを持つ。Pydantic v2 の `model_validate_json` で input を厳密検証し、
不正な構造は ValidationError で即拒否する (silent fallback 禁止、指示書 §3.4)。

## 変換マッピング (実装プラン B.2.3 準拠)
- `VerifiedScript.script.segments[*].turns[*]` を平坦化して `Script.sections` に
- 各 segment の **先頭 turn** に `section=segment_type` / `chapter_title=segment.title` を付与
- `VerifiedScript.metadata.title` → `Script.title`
- `VerifiedScript.metadata.thumbnail_title` → `Script.thumbnail_title`
- `VerifiedScript.metadata.description` → `Script.description`
- `VerifiedScript.metadata.hashtags` → `Script.hashtags`
- `VerifiedScript.metadata.references[*].url` → `Script.references` (HttpUrl → str)

## ScriptSegment への変換 (production_phase chapter rendering 用)
`build_script_segments()` を別途公開し、`execute_external_script_phase` 等から
利用できるようにする。`segment_id` は "intro"、"deep_dive_1/2/3"、"conclusion" の
形式 (既存 voicevox_client._build_segment_index_map 互換)。
"""
import logging
from pathlib import Path
from typing import List

from core.interfaces.script_loader import IScriptLoader
from core.models.curation import ScriptSegment
from core.models.script import DialogueTurn, Script, TurnType
from core.models.verified_script import Segment as VSSegment, VerifiedScript

logger = logging.getLogger(__name__)


class RadioDirectorScriptLoader(IScriptLoader):
    """Mac 側 radio_director の VerifiedScript JSON を Script に変換するローダー。

    本クラスは状態を持たない。`load()` は同期メソッド (ファイル I/O + Pydantic 検証
    のみ、ネットワーク不要)。
    """

    def load(self, verified_script_path: Path) -> Script:
        """VerifiedScript JSON を読み取り Script に変換する。

        Args:
            verified_script_path: VerifiedScript JSON のパス

        Returns:
            Script: VOICEVOX / FFmpeg パイプライン側で使える Script オブジェクト

        Raises:
            FileNotFoundError: パスが存在しない
            pydantic.ValidationError: ファイル内容がスキーマに整合しない
        """
        path = Path(verified_script_path)
        if not path.exists():
            raise FileNotFoundError(
                f"VerifiedScript not found: {path}. "
                "Mac 側 radio_director の出力 JSON を output/imports/<run_id>/ に配置してください。"
            )

        text = path.read_text(encoding="utf-8")
        # silent fallback 禁止: 不正 JSON / スキーマ違反は ValidationError で即エラー
        vs = VerifiedScript.model_validate_json(text)

        sections = self._flatten_segments_to_sections(vs)
        ref_urls = [str(ref.url) for ref in vs.metadata.references]

        script = Script(
            title=vs.metadata.title,
            sections=sections,
            thumbnail_title=vs.metadata.thumbnail_title,
            description=vs.metadata.description,
            hashtags=list(vs.metadata.hashtags),
            references=ref_urls,
        )

        logger.info(
            "VerifiedScript loaded: %d segments → %d turns flattened, %d references",
            len(vs.script.segments), len(sections), len(ref_urls),
        )
        return script

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _flatten_segments_to_sections(vs: VerifiedScript) -> List[DialogueTurn]:
        """VerifiedScript.script.segments を平坦化して DialogueTurn のリストに。

        各 segment の先頭 turn に section=segment_type, chapter_title=segment.title を付与。
        VOICEVOX の chapter rendering ロジック (voicevox_client._build_chapters) は
        DialogueTurn.section の境界 + chapter_title の有無で章マーカーを建てる。
        """
        sections: List[DialogueTurn] = []
        for seg in vs.script.segments:
            for turn_idx, turn in enumerate(seg.turns):
                dt = DialogueTurn(
                    speaker=turn.speaker,
                    text=turn.text,
                    turn_type=TurnType.DIALOGUE,
                    # 先頭 turn のみ chapter 用メタを付与（既存 voicevox_client の dedup 仕様に整合）
                    section=seg.segment_type if turn_idx == 0 else None,
                    chapter_title=seg.title if turn_idx == 0 else None,
                )
                sections.append(dt)
        return sections


# ---------------------------------------------------------------------------
# Module-level helper (production_phase / external_script_phase で使用)
# ---------------------------------------------------------------------------

def build_script_segments(vs: VerifiedScript) -> List[ScriptSegment]:
    """VerifiedScript から ScriptSegment のリストを構築する。

    voicevox_client._build_segment_index_map 互換の `segment_id` を生成:
      - intro: "intro"
      - deep_dive: "deep_dive_1", "deep_dive_2", "deep_dive_3", ...
      - conclusion: "conclusion"

    deep_dive のカウントは出現順 (segments 配列の登場順)。

    Args:
        vs: VerifiedScript インスタンス (検証済み)

    Returns:
        List[ScriptSegment]: production_phase に渡せる ScriptSegment 列
    """
    segments: List[ScriptSegment] = []
    deep_dive_counter = 0

    for seg in vs.script.segments:
        if seg.segment_type == "deep_dive":
            deep_dive_counter += 1
            segment_id = f"deep_dive_{deep_dive_counter}"
        else:
            # intro / conclusion はそのまま (1 セグメントしか無い前提)
            segment_id = seg.segment_type

        # turns は dict 形式 (ScriptSegment.turns: List[dict] 仕様)
        turn_dicts: List[dict] = []
        for t_idx, t in enumerate(seg.turns):
            turn_dicts.append({
                "speaker": t.speaker,
                "text": t.text,
                "section": seg.segment_type if t_idx == 0 else None,
                "chapter_title": seg.title if t_idx == 0 else None,
            })

        segments.append(ScriptSegment(
            segment_id=segment_id,
            segment_type=seg.segment_type,  # type: ignore[arg-type]
            topic_title=seg.title,
            turns=turn_dicts,
            context_summary="",
            token_count=0,
            key_claim_source_idxs=_key_claim_source_idxs(vs, seg, deep_dive_counter),
        ))

    return segments


def _key_claim_source_idxs(vs: VerifiedScript, seg: VSSegment, deep_dive_counter: int) -> List[int]:
    """deep_dive セグメントに対応する show_spec.topics[].key_claims の出典番号を登場順で返す。

    topic_index が無い deep_dive は出現順 (deep_dive_counter - 1) で対応付ける。
    intro / conclusion・対応する topic が無い場合は空リスト (画像プロンプト側で等分抜粋になる)。
    """
    if seg.segment_type != "deep_dive":
        return []
    topic_index = seg.topic_index if seg.topic_index is not None else deep_dive_counter - 1
    topics = vs.script.show_spec.topics
    if not 0 <= topic_index < len(topics):
        return []
    idxs: List[int] = []
    for claim in topics[topic_index].key_claims:
        if claim.source_idx is not None and claim.source_idx not in idxs:
            idxs.append(claim.source_idx)
    return idxs
