"""概要欄に URL を載せないことのテスト (2026-10-24)

2026-10-23 の回が、概要欄の出典リンク先ドメインの分類を理由に YouTube から削除された。
出典は「タイトル(ドメイン名)」で示し、http:// / https:// で始まる文字列を概要欄に
一切載せない。実台本 fixture (tests/fixtures/real_scripts/) の references で確かめる。
"""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import workflow
from core.interfaces.audio_synthesizer import ChapterMarker
from core.models.research import ResearchSource
from core.models.usage import TotalUsage
from core.models.verified_script import VerifiedScript
from services.publishing.metadata_builder import (
    build_video_description,
    format_reference_label,
    format_reference_text_lines,
    reference_domain,
    strip_urls,
)
from services.script_loading.radio_director_loader import RadioDirectorScriptLoader
from workflow import (
    _build_publish_fields,
    _upload_to_youtube,
    _verified_refs_to_research_sources,
)

REAL_SCRIPTS_DIR = Path(__file__).parent / "fixtures" / "real_scripts"
REAL_SCRIPTS = sorted(REAL_SCRIPTS_DIR.glob("*.json"))
URL_RE = re.compile(r"https?://", re.IGNORECASE)


def _load(path: Path):
    vs = VerifiedScript.model_validate_json(path.read_text(encoding="utf-8"))
    script = RadioDirectorScriptLoader().load(path)
    return vs, script


def _chapters(vs: VerifiedScript) -> list[ChapterMarker]:
    return [
        ChapterMarker(start_time_sec=float(i * 90), title=f"チャプター{i + 1}", section_id=f"s{i}")
        for i, _seg in enumerate(vs.script.segments)
    ]


def _publish_description(path: Path) -> tuple[str, VerifiedScript]:
    """本番 (外部台本モード) と同じ経路で概要欄を組み立てる。"""
    vs, script = _load(path)
    config = SimpleNamespace(
        yaml=SimpleNamespace(
            publishing=SimpleNamespace(default_tags=["#ラジオ"], footer_text="footer")
        )
    )
    _title, description, _pub = _build_publish_fields(
        config=config,
        theme=vs.metadata.title,
        script=script,
        research_sources=_verified_refs_to_research_sources(vs.metadata.references) or None,
        generated_metadata={"title": vs.metadata.title, "description": vs.metadata.description},
        chapters=_chapters(vs),
        total_usage=TotalUsage(),
        footer_text_override=None,
        creation_date="2026.10.24",
    )
    return description, vs


def test_real_scripts_have_reference_urls():
    """前提: fixture の多くは references に URL を持つ (持たなければテストが空回りする)"""
    with_refs = [p for p in REAL_SCRIPTS if _load(p)[0].metadata.references]
    assert len(with_refs) >= 5


@pytest.mark.parametrize("path", REAL_SCRIPTS, ids=lambda p: p.stem)
def test_publish_description_has_no_urls(path):
    description, vs = _publish_description(path)

    assert not URL_RE.search(description), description

    # 出典は「タイトル(ドメイン名)」で残っている
    for ref in vs.metadata.references:
        domain = reference_domain(str(ref.url))
        assert domain and domain in description
        if ref.title:
            assert ref.title.strip() in description

    # 目次 (チャプター) は従来どおり
    assert "【目次】\n0:00 チャプター1\n1:30 チャプター2" in description


def test_faultnote_reference_is_title_and_domain():
    """削除の原因になった出典は、URL を出さず「タイトル(ドメイン名)」になる"""
    url = "https://faultnote.com/learn/comparisons/hdd-workload-rate-mttf-afr-spec-reading-before-buying/"
    description = build_video_description(
        script_description="HDD の回",
        chapters=["00:00 オープニング"],
        references=[ResearchSource(title="HDDのワークロード定格", url=url)],
        dynamic_tags=[],
        fixed_tags=[],
        footer_text="footer",
    )
    assert not URL_RE.search(description)
    assert "【参考文献】\n📄 HDDのワークロード定格(faultnote.com)" in description


def test_reference_label_formats():
    assert format_reference_label(
        ResearchSource(title="T", url="https://www.example.com/a", published_date="2026-06-28"), 1
    ) == "T(example.com、2026-06-28)"
    assert format_reference_label("https://example.org/x", 2) == "参考文献2(example.org)"
    # タイトルに URL が入っていても落とす
    assert format_reference_label(
        ResearchSource(title="https://example.net/page", url="https://example.net/page"), 3
    ) == "参考文献3"


def test_metadata_txt_reference_lines_have_no_urls():
    lines = format_reference_text_lines([
        ResearchSource(title="T", url="https://example.com/a"),
        "https://example.org/b",
    ])
    assert lines == ["参考文献1: T(example.com)", "参考文献2(example.org)"]


def test_urls_in_summary_and_footer_are_dropped():
    description = build_video_description(
        script_description="詳しくは https://example.com/x を参照\nhttps://example.org/only",
        chapters=["00:00 オープニング"],
        references=[],
        dynamic_tags=[],
        fixed_tags=[],
        footer_text="footer http://example.net",
    )
    assert not URL_RE.search(description)
    assert "詳しくは  を参照" in description
    assert description.endswith("footer")


def test_strip_urls_drops_link_only_lines():
    text, removed = strip_urls("a\n🔗 https://example.com/x\nb")
    assert text == "a\nb"
    assert removed == ["https://example.com/x"]


class _FakeCallbacks:
    def __init__(self):
        self.logs = []

    def log(self, msg):
        self.logs.append(msg)


class _FakeYouTubeClient:
    last = None

    def __init__(self, config):
        _FakeYouTubeClient.last = self
        self.upload_calls = []

    def upload_video(self, **kwargs):
        self.upload_calls.append(kwargs)
        return "https://www.youtube.com/watch?v=ABC123"


def test_upload_guard_drops_urls_and_continues(monkeypatch):
    """アップロード直前に URL が残っていても、落として続行する"""
    monkeypatch.setattr(workflow, "YouTubeClient", _FakeYouTubeClient)
    cb = _FakeCallbacks()
    url = _upload_to_youtube(
        config=SimpleNamespace(yaml=SimpleNamespace()),
        use_mock=False,
        upload_override=True,
        publishing_config=SimpleNamespace(enable_upload=True, playlist_id=None),
        video_path="v.mp4",
        formatted_title="t",
        formatted_description="概要\n🔗 https://faultnote.com/learn/x/\n00:00 オープニング",
        thumbnail_path="th.png",
        callbacks=cb,
    )
    assert url == "https://www.youtube.com/watch?v=ABC123"
    sent = _FakeYouTubeClient.last.upload_calls[0]["description"]
    assert sent == "概要\n00:00 オープニング"
    assert any("URL が 1 件残っていた" in m for m in cb.logs)
