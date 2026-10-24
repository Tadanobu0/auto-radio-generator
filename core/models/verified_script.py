"""VerifiedScript - Mac 側 radio_director パイプラインの SSOT 出力に対する Windows 側 view モデル

Step 3 (2026-05-09) 外部台本モード化で導入。Mac Studio 側 `radio_director` が生成する
VerifiedScript JSON を Windows 側 auto-radio-generator が読み込むためのデータモデル定義。

## SSOT 同期注記
このモデルは Mac 側 `~/radio_director/src/radio_director/models/verified_script.py` の
スキーマと同期する。**変更時は両方を同時に更新すること**。Mac 側スキーマが進化した
場合、`tests/fixtures/verified_script_sample.json` を Mac 側 fixture から再取得して
配置し直し、本モデルの定義も合わせて更新する。

## 例外: segments[].visuals (spec v1.11.0 §12)
不正な visuals は ValidationError にせず、その章だけ従来の場面分割に戻す (仕様)。
そのため `Segment.visuals` は形を問わず受け、loader で章ごとに検査する。

## 設計方針
- 読み取り用 view モデル: Windows 側では VerifiedScript を**生成しない**ため、
  生成側のロジックや retry / sanitize は持たない
- Pydantic v2 idiom (`field_validator` / `Field(min_length=...)`)
- 必須フィールドは Mac 側 SSOT (Step 1 完了仕様、§4.3) に整合
- 不正な構造は ValidationError で拒否 (silent fallback 禁止、指示書 §3.4)
- `references=[]` は正常系として扱う (§4.4 既知制約)

## トップレベル構造
```json
{
  "script": {                           // 台本本体 (ScriptBody)
    "show_spec": {...},
    "segments": [{...}, ...],           // 5 segments (intro + 3 deep_dive + conclusion)
    "metrics": {...}
  },
  "metrics": {...},                     // 抽出統計 (Windows 側では未使用)
  "warnings": [{...}, ...],             // 警告ログ (Windows 側では未使用)
  "metadata": {                         // ★ Script への変換ターゲット (VideoMetadata)
    "title": str,
    "thumbnail_title": str,             // max 15 字
    "description": str,                 // 50〜2000 字
    "hashtags": [str, ...],             // 3〜15 件
    "chapters": [{...}, ...],           // 2 件以上
    "references": []                    // 当面常に空 (§4.4)
  }
}
```
"""
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, HttpUrl


# ---------------------------------------------------------------------------
# Inner models
# ---------------------------------------------------------------------------

class Turn(BaseModel):
    """1 つの発話ターン。VerifiedScript.script.segments[*].turns[*]"""
    speaker: Literal["A", "B"] = Field(..., description="話者 ID ('A' or 'B')")
    text: str = Field(..., min_length=1, description="セリフ本文")


class Segment(BaseModel):
    """1 つのセグメント。intro / deep_dive ×N / conclusion のいずれか"""
    segment_type: Literal["intro", "deep_dive", "conclusion"] = Field(
        ...,
        description="セグメント種別 (Mac 側 SSOT: intro/deep_dive/conclusion の 3 値)",
    )
    # Mac 側 fixture では intro / conclusion で None になるため Optional
    topic_index: Optional[int] = Field(
        default=None,
        description="トピックインデックス (deep_dive では 0 起点、intro/conclusion では None)",
    )
    title: str = Field(..., min_length=1, description="セグメントタイトル (chapter 表示等で使用)")
    turns: List[Turn] = Field(..., min_length=1, description="ターンリスト (最低 1 件)")
    # spec v1.11.0 §12: 場面ごとに何を描くか (SceneVisual のリスト、任意)。
    # 不正でもファイル全体は拒否せず、その章だけ scene_splitter に戻す仕様なので、
    # ここでは形を問わず受け、loader (build_script_segments) で章ごとに検査する。
    visuals: Optional[Any] = Field(
        default=None,
        description="場面の指定 (SceneVisual のリスト)。検査は loader で行う",
    )


class KeyClaim(BaseModel):
    """show_spec の key_claims 要素 (Mac 側 SSOT、Windows 側では参照のみ)。

    Mac 側 fixture では confidence は文字列の場合もある (例: "medium")。
    Windows 側では show_spec は使用しないため、ここは Any で寛容に受ける。
    """
    text: str
    source_idx: Optional[int] = None
    source_tier: Optional[str] = None
    confidence: Optional[Any] = None  # Mac 側で str / float のいずれもありうる


class TopicSpec(BaseModel):
    """show_spec.topics 要素"""
    title: str
    hook: Optional[str] = None
    key_claims: List[KeyClaim] = Field(default_factory=list)
    tone: Optional[str] = None
    estimated_turns: Optional[int] = None


class ShowSpec(BaseModel):
    """番組構成スペック。Windows 側では Script への直接変換には使わないが、
    Pydantic 検証で構造妥当性は担保する。
    """
    title: Optional[str] = None
    thumbnail_title: Optional[str] = None
    hook: Optional[str] = None
    angle: Optional[str] = None
    arc: Optional[str] = None
    tone: Optional[str] = None
    topics: List[TopicSpec] = Field(default_factory=list)
    conclusion_message: Optional[str] = None


class SegmentMetrics(BaseModel):
    """1 セグメントの抽出メトリクス (Windows 側では未使用)"""
    prompt_chars: Optional[int] = None
    output_chars: Optional[int] = None
    elapsed_sec: Optional[float] = None
    attempts: Optional[int] = None
    used_fallback: Optional[bool] = None


class ScriptBody(BaseModel):
    """VerifiedScript.script 部分。show_spec + segments + metrics を含む"""
    show_spec: ShowSpec = Field(..., description="番組構成スペック")
    segments: List[Segment] = Field(
        ...,
        min_length=2,
        description="セグメントリスト (intro + N deep_dive + conclusion で最低 2 件)",
    )
    # metrics は dict のまま受ける (segment_type 別の SegmentMetrics 群)
    metrics: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Metadata (Mac 側 Step 1 完了仕様 §4.3 に整合)
# ---------------------------------------------------------------------------

class Chapter(BaseModel):
    """metadata.chapters 要素"""
    timestamp: str = Field(..., description="HH:MM:SS or MM:SS 形式のタイムスタンプ文字列")
    title: str = Field(..., min_length=1, description="チャプタータイトル")


class SourceRef(BaseModel):
    """metadata.references 要素 (Mac 側 SSOT)。

    Mac 側 Phase B/C で出典タグが整備されるまでは references=[] が常態 (§4.4)。
    Windows 側 loader は本モデルを尊重するが、空配列を正常系として扱う。
    """
    url: HttpUrl
    title: Optional[str] = None
    tier: Optional[Literal["AAA", "AA", "A", "B"]] = None
    # spec v1.8.1 (2026-07-16) で追加されたフィールドへの追従。
    # 概要欄の参考文献表記 (タイトル + 日付 + URL) に使用する。
    published_date: Optional[str] = None


class VideoMetadata(BaseModel):
    """metadata 部分。Mac 側 SSOT (指示書 §4.3) に整合。

    制約:
      - thumbnail_title: 1〜15 文字 (Step 1 で確定)
      - description: 50〜2000 文字
      - hashtags: 3〜15 件
      - chapters: 2 件以上
      - references: 空配列 OK (§4.4)
    """
    title: str = Field(..., min_length=1, description="動画タイトル (字数制限なし、YouTube API 側で truncation)")
    thumbnail_title: str = Field(..., min_length=1, max_length=15, description="サムネイル用短縮タイトル (15 字以内)")
    description: str = Field(..., min_length=50, max_length=2000, description="動画概要文 (50〜2000 字)")
    hashtags: List[str] = Field(..., min_length=3, max_length=15, description="ハッシュタグ (3〜15 件)")
    chapters: List[Chapter] = Field(..., min_length=2, description="チャプター (2 件以上)")
    references: List[SourceRef] = Field(
        default_factory=list,
        description="参考文献 (空配列 OK、§4.4 既知制約。Mac 側 v2 で対応予定)",
    )


# ---------------------------------------------------------------------------
# Top-level model
# ---------------------------------------------------------------------------

class VerifiedScript(BaseModel):
    """Mac 側 radio_director パイプラインの SSOT 出力。

    Windows 側 auto-radio-generator は本 JSON を読み取り、
    `RadioDirectorScriptLoader.load()` で `core.models.script.Script` に変換する。
    変換ロジックは services/script_loading/radio_director_loader.py 参照。

    Pydantic v2 の `model_validate_json` で input を厳密検証。
    不正な構造は ValidationError で拒否 (silent fallback 禁止、指示書 §3.4)。
    """
    script: ScriptBody = Field(..., description="台本本体 (show_spec + segments + metrics)")
    # metrics / warnings は Windows 側で未使用だが、構造妥当性は維持する
    metrics: Dict[str, Any] = Field(default_factory=dict, description="抽出統計 (Windows 側未使用)")
    warnings: List[Dict[str, Any]] = Field(default_factory=list, description="警告ログ (Windows 側未使用)")
    metadata: VideoMetadata = Field(..., description="動画メタデータ (Script 変換ターゲット)")
