"""キュレーションと台本セグメントのデータモデル"""
from typing import List, Literal, Optional
from pydantic import BaseModel, Field, field_validator


class CuratedTopic(BaseModel):
    """キュレーション済みトピック - リサーチデータから選定された面白いネタ"""
    title: str = Field(..., description="トピックタイトル（例: 'CGMの精度問題と血糖値測定の誤差'）")
    content: str = Field(..., description="詳細情報（500〜800文字、具体例・数値データ含む）")
    priority: int = Field(..., description="優先度（1が最高）")
    estimated_turns: int = Field(default=30, description="推定ターン数（20〜40）")
    tone: str = Field(default="議論", description="推奨トーン（例: '驚き', '議論', '解説'）")
    key_facts: List[str] = Field(
        default_factory=list,
        description="最重要ファクトのリスト（数値・固有名詞・エピソード）"
    )
    selection_reason: str = Field(
        default="",
        description="このトピックを選んだ理由（80〜120字、面白さの核心。下流のSegmentGeneratorに渡される）"
    )


class ScriptSegment(BaseModel):
    """台本の1セグメント - 独立したAPI呼び出しで生成された会話ブロック"""
    segment_id: str = Field(..., description="セグメントID（例: 'intro', 'deep_dive_1', 'conclusion'）")
    segment_type: Literal["intro", "deep_dive", "conclusion"] = Field(
        ..., description="セグメント種別"
    )
    topic_title: Optional[str] = Field(
        None, description="深掘りセグメントの場合のトピックタイトル"
    )
    turns: List[dict] = Field(
        ..., description="このセグメントの対話ターン（DialogueTurn互換）"
    )
    context_summary: str = Field(
        default="",
        description="このセグメントまでの文脈要約（次セグメント生成用、200〜300文字）"
    )
    token_count: int = Field(default=0, description="このセグメントの出力トークン数")
    key_claim_source_idxs: List[int] = Field(
        default_factory=list,
        description=(
            "このセグメントの論点 (show_spec.topics[].key_claims) の出典番号 (登場順)。"
            "画像プロンプト用の本文抜粋で、各論点が最初に出るターン ([src=N]) を探すのに使う。"
            "外部台本以外や intro / conclusion では空"
        ),
    )


class CurationResult(BaseModel):
    """キュレーション結果 - TopicCuratorの出力

    ## 契約（Phase 4 review #4）

    `topics` は **必ず 1 件以上** を含むこと。これは以下の不変条件を表現する:

      - Curator は「最低 1 件のトピックを選定する」責務を負っている（0 件は失敗と同義）
      - `preset_curation` として Orchestrator に渡される際も、受け取り側の分岐
        （`preset_curation is not None and preset_curation.topics`）が暗黙に前提している
      - 0 件の `CurationResult` は下流の SegmentGenerator に渡ると台本生成不能を招く

    旧実装では orchestrator 側の `and preset_curation.topics` 条件で silent に
    Curator 実行にフォールスルーしていたため、壊れた preset が検知されず debug を
    困難にしていた。モデル層で ValidationError を送出することで、生成時点 or
    JSON ロード時点で早期に検知する（fail-fast）。
    """
    topics: List[CuratedTopic] = Field(
        ...,
        description="選定されたトピック（優先度順、最低 1 件）",
    )
    curator_reasoning: str = Field(
        default="",
        description="選定理由（デバッグ用）"
    )

    @field_validator("topics")
    @classmethod
    def _topics_must_not_be_empty(cls, v: List[CuratedTopic]) -> List[CuratedTopic]:
        """Phase 4 review #4: topics が空の CurationResult は構造上壊れているため拒否。

        既存セッションで破損した curation_result.json が存在する場合、ロード時に
        ValidationError が送出される。これは **意図した破壊的変更** であり、手動で
        curation_result.json を修復するか、該当セッションを破棄して再実行する必要がある。
        """
        if len(v) == 0:
            raise ValueError(
                "CurationResult.topics must contain at least one CuratedTopic; "
                "got an empty list. This indicates a broken preset_curation or a "
                "failed Curator run. Downstream segment generation cannot proceed "
                "without at least one topic."
            )
        return v
