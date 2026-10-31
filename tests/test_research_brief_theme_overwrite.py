"""PR-I: ResearchBrief.theme overwrite regression tests.

`workflow._save_research_results` と `workflow.generate_video_workflow` 内のインライン
ResearchBrief 構築箇所では従来 `theme=research_data.topic` を使用していた。
`research_data.topic` は `PerplexityClient` が `", ".join(normalized_queries)` で
連結した「検索クエリ全文」になるため、ResearchBrief.theme として下流に流すと
MetadataGenerator 等が長大な文字列を「テーマ」と誤認し、台本やメタデータの整合性が
壊れる本運用バグが発生していた。

PR-I では `_save_research_results` のシグネチャに `theme` / `plan_queries` /
`plan_angle` を追加し、呼び出し元から元テーマ・実検索クエリ・切り口を明示渡しする
方針に切り替えた。本テストはその契約変更が将来のリファクタで失われないことを担保する。
"""
import inspect
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.interfaces import ResearchResult
from core.models.artifacts import ResearchBrief
import workflow as wf


# ---------------------------------------------------------------------------
# (1) シグネチャ契約: _save_research_results が新しいキーワード引数を受け取る
# ---------------------------------------------------------------------------

def test_save_research_results_signature_has_keyword_only_params():
    """新しいキーワード専用引数 theme / plan_queries / plan_angle が存在する。"""
    sig = inspect.signature(wf._save_research_results)
    params = sig.parameters

    assert "theme" in params, "PR-I: theme キーワード引数が必要"
    assert "plan_queries" in params, "PR-I: plan_queries キーワード引数が必要"
    assert "plan_angle" in params, "PR-I: plan_angle キーワード引数が必要"

    # theme / plan_queries は keyword-only であるべき（位置引数化を防ぐ）
    for kw in ("theme", "plan_queries", "plan_angle"):
        assert params[kw].kind == inspect.Parameter.KEYWORD_ONLY, (
            f"{kw} は keyword-only であるべき（位置引数だと将来の引数追加時に壊れやすい）"
        )

    # plan_angle はデフォルト値「自動生成」を持つ（ResearchPlan が無い経路用）
    assert params["plan_angle"].default == "自動生成"


# ---------------------------------------------------------------------------
# (2) ResearchBrief.theme は呼び出し元の theme を保持する（連結クエリで上書きされない）
# ---------------------------------------------------------------------------

def _make_research_data(topic: str = "デフォルトのトピック文字列") -> ResearchResult:
    return ResearchResult(
        topic=topic,
        mode="lecture",
        content="ダミーリサーチ本文。最低限の長さを確保。" * 5,
        sources=[],
        usage=None,
    )


def test_research_brief_theme_uses_caller_provided_theme(tmp_path: Path):
    """`research_data.topic` が連結クエリでも、書き出される theme は呼び出し元の元テーマ。"""
    # 連結クエリ全文を模した topic（PerplexityClient の挙動を再現）
    concatenated = (
        "CGMの仕組み, CGMの精度評価, CGM活用事例, "
        "CGMと従来血糖測定の比較, CGMの保険適用範囲"
    )
    research_data = _make_research_data(topic=concatenated)

    callbacks = MagicMock()
    output_dir = tmp_path / "20260423_120000"
    output_dir.mkdir()

    wf._save_research_results(
        research_data,
        output_dir,
        callbacks,
        theme="持続血糖測定器CGMについて",
        plan_queries=["CGMの仕組み", "CGMの精度評価", "CGM活用事例"],
        plan_angle="初心者向けに比喩を使って解説",
    )

    brief_path = output_dir / "research_brief.json"
    assert brief_path.exists()

    brief = ResearchBrief.model_validate_json(brief_path.read_text(encoding="utf-8"))
    assert brief.theme == "持続血糖測定器CGMについて", (
        "PR-I: brief.theme は呼び出し元の元テーマを保持すべきで、"
        f"連結クエリ {concatenated!r} で上書きされてはならない"
    )


def test_research_brief_queries_uses_plan_queries_not_topic(tmp_path: Path):
    """brief.queries は plan_queries を保持し、`[research_data.topic]` の 1 件に潰されない。"""
    research_data = _make_research_data(topic="クエリA, クエリB, クエリC")
    callbacks = MagicMock()
    output_dir = tmp_path / "20260423_120000"
    output_dir.mkdir()

    plan_queries = ["クエリA", "クエリB", "クエリC"]
    wf._save_research_results(
        research_data,
        output_dir,
        callbacks,
        theme="テストテーマ",
        plan_queries=plan_queries,
        plan_angle="テスト切り口",
    )

    brief = ResearchBrief.model_validate_json(
        (output_dir / "research_brief.json").read_text(encoding="utf-8")
    )
    assert brief.queries == plan_queries, (
        "PR-I: brief.queries は plan の実クエリリストを保持すべき"
    )
    assert len(brief.queries) == 3
    assert brief.angle == "テスト切り口"


def test_research_brief_falls_back_to_default_angle(tmp_path: Path):
    """plan_angle 未指定時は既定値「自動生成」が入る（plan が無い経路用）。"""
    research_data = _make_research_data()
    output_dir = tmp_path / "20260423_120000"
    output_dir.mkdir()

    wf._save_research_results(
        research_data,
        output_dir,
        MagicMock(),
        theme="テスト",
        plan_queries=["q1"],
    )

    brief = ResearchBrief.model_validate_json(
        (output_dir / "research_brief.json").read_text(encoding="utf-8")
    )
    assert brief.angle == "自動生成"


def test_research_report_md_displays_caller_theme(tmp_path: Path):
    """research_report.md の「テーマ」表示も連結クエリではなく元テーマであるべき。"""
    research_data = _make_research_data(topic="A, B, C, D")
    output_dir = tmp_path / "20260423_120000"
    output_dir.mkdir()

    wf._save_research_results(
        research_data,
        output_dir,
        MagicMock(),
        theme="ユーザが入力した本来のテーマ",
        plan_queries=["A", "B"],
    )

    report = (output_dir / "research_report.md").read_text(encoding="utf-8")
    assert "**テーマ**: ユーザが入力した本来のテーマ" in report
    assert "**テーマ**: A, B, C, D" not in report


# ---------------------------------------------------------------------------
# (3) 構造的回帰: workflow.py 内で theme=research_data.topic 形式が消えていること
# ---------------------------------------------------------------------------

def test_workflow_no_longer_uses_research_data_topic_for_theme():
    """`theme=research_data.topic` というアンチパターンが workflow.py から消えていること。

    将来のリファクタで誰かが復活させるリグレッション防止。コメント内の言及は許容するため、
    実コード行（インデント直後にこの代入が来るパターン）のみを検出する。
    """
    # 2026-11-01 (017): E:/ 直読みは Mac で落ちる。このテストファイルからの相対で読む
    src = (Path(__file__).resolve().parent.parent / "workflow.py").read_text(encoding="utf-8")

    for line in src.splitlines():
        stripped = line.lstrip()
        # コメント行はスキップ
        if stripped.startswith("#"):
            continue
        # ResearchBrief / dict キーワード渡しでの上書きパターンを検出
        assert "theme=research_data.topic" not in stripped, (
            f"PR-I: workflow.py 実コードに theme=research_data.topic が残っている: {line!r}"
        )
        # queries=[research_data.topic] パターンも禁止
        assert "queries=[research_data.topic]" not in stripped, (
            f"PR-I: workflow.py 実コードに queries=[research_data.topic] が残っている: {line!r}"
        )


# Step 4 v2 (2026-05-10): generate_video_workflow が物理削除されたため、
# `test_workflow_inline_research_brief_uses_plan_queries` は撤去。
# `_save_research_results` 単体への呼び出し契約は本ファイル冒頭のテストで担保される。
