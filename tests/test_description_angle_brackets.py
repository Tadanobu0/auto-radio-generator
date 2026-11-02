"""タイトル・概要欄から半角 < > を落とすことのテスト (2026-11-02, ブリーフ 020)

2026-11-02 の部屋干し回の公開で、参考文献の題に PDF の内部名 <4D6963…> が入り、
YouTube が概要欄を invalidDescription で拒否した。YouTube はタイトル・概要欄の
半角 < > を受け付けない。題が <16 進> だけのときは題を出さず「参考文献N(ドメイン)」にする
(radio_director 側 phase_d/metadata_generator.resolve_references と同じ振る舞い)。
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

import workflow
from core.models.research import ResearchSource
from core.models.script import DialogueTurn, Script, TurnType
from core.models.usage import TotalUsage
from core.models.verified_script import VerifiedScript
from services.publishing.metadata_builder import (
    build_video_description,
    format_reference_label,
    format_reference_text_lines,
)
from services.publishing.text_sanitizer import (
    is_hex_only_title,
    sanitize_for_youtube,
    strip_angle_brackets,
)
from services.script_loading.radio_director_loader import RadioDirectorScriptLoader
from workflow import (
    _build_publish_fields,
    _upload_to_youtube,
    _verified_refs_to_research_sources,
)

REAL_SCRIPTS = sorted((Path(__file__).parent / "fixtures" / "real_scripts").glob("*.json"))
PDF_URL = "https://www.example.go.jp/content/000123456.pdf"


def _build(refs):
    return build_video_description(
        script_description="部屋干しの回",
        chapters=["00:00 オープニング"],
        references=refs,
        dynamic_tags=[],
        fixed_tags=[],
        footer_text="footer",
    )


# --- sanitize_for_youtube -------------------------------------------------

def test_sanitize_drops_angle_brackets():
    assert sanitize_for_youtube("A <b> C > D < E") == "A b C  D  E"


def test_sanitize_keeps_fullwidth_brackets_and_normal_text():
    text = "＜全角＞はそのまま。通常の文。"
    assert sanitize_for_youtube(text) == text


def test_strip_angle_brackets_handles_empty():
    assert strip_angle_brackets("") == ""
    assert strip_angle_brackets(None) == ""


@pytest.mark.parametrize("title, expected", [
    ("<4D6963726F736F667420576F7264>", True),
    ("  <4d69 >  ", True),
    ("<ABC>題", False),
    ("<不明>", False),
    ("通常の題", False),
    ("", False),
])
def test_is_hex_only_title(title, expected):
    assert is_hex_only_title(title) is expected


# --- 参考文献の題 ---------------------------------------------------------

def test_hex_only_title_becomes_numbered_reference_with_domain():
    ref = ResearchSource(title="<4D6963726F736F667420576F7264>", url=PDF_URL)
    assert format_reference_label(ref, 2) == "参考文献2(example.go.jp)"

    description = _build([ref])
    assert "<" not in description and ">" not in description
    assert "4D6963" not in description
    assert "【参考文献】\n📄 参考文献1(example.go.jp)" in description


def test_hex_only_title_in_metadata_txt_lines():
    lines = format_reference_text_lines([
        ResearchSource(title="<4D6963>", url=PDF_URL),
        ResearchSource(title="通常の題", url="https://example.com/a"),
    ])
    assert lines == ["参考文献1(example.go.jp)", "参考文献2: 通常の題(example.com)"]


def test_title_with_angle_brackets_keeps_text_without_brackets():
    ref = ResearchSource(title="室内干しの<臭い>対策", url="https://example.com/a")
    assert format_reference_label(ref, 1) == "室内干しの臭い対策(example.com)"

    description = _build([ref])
    assert "<" not in description and ">" not in description
    assert "📄 室内干しの臭い対策(example.com)" in description


def test_title_of_only_brackets_falls_back_to_numbered_reference():
    ref = ResearchSource(title="<>", url="https://example.com/a")
    assert format_reference_label(ref, 3) == "参考文献3(example.com)"


def test_normal_title_unchanged():
    ref = ResearchSource(title="部屋干しの生乾き臭の原因", url="https://www.example.com/a",
                         published_date="2026-10-30")
    assert format_reference_label(ref, 1) == "部屋干しの生乾き臭の原因(example.com、2026-10-30)"
    assert "📄 部屋干しの生乾き臭の原因(example.com、2026-10-30)" in _build([ref])


def test_angle_brackets_in_summary_and_footer_are_dropped():
    description = build_video_description(
        script_description="比較: A < B > C",
        chapters=["00:00 オープニング"],
        references=[],
        dynamic_tags=[],
        fixed_tags=[],
        footer_text="<footer>",
    )
    assert "<" not in description and ">" not in description
    assert description.endswith("footer")


# --- 動画タイトル / 公開経路 -----------------------------------------------

def _config():
    return SimpleNamespace(
        yaml=SimpleNamespace(
            publishing=SimpleNamespace(default_tags=["#ラジオ"], footer_text="footer")
        )
    )


def test_publish_fields_title_and_description_have_no_angle_brackets():
    # script.references が空なので research_sources がそのまま参考文献になる
    script = Script(
        title="台本タイトル",
        thumbnail_title="th",
        sections=[
            DialogueTurn(speaker="A", text=f"line{i}", turn_type=TurnType.DIALOGUE)
            for i in range(10)
        ],
        description="説明文",
    )
    title, description, _pub = _build_publish_fields(
        config=_config(),
        theme="テーマ",
        script=script,
        research_sources=[
            ResearchSource(title="<4D6963726F736F6674>", url=PDF_URL),
            ResearchSource(title="通常の題", url="https://example.com/a"),
        ],
        generated_metadata={"title": "<速報> 部屋干し", "description": "概要 <注>"},
        chapters=[],
        total_usage=TotalUsage(),
        footer_text_override=None,
        creation_date="2026.11.02",
    )
    assert title == "速報 部屋干し (2026.11.02制作)"
    assert "<" not in description and ">" not in description
    assert "📄 参考文献1(example.go.jp)\n📄 通常の題(example.com)" in description


@pytest.mark.parametrize("path", REAL_SCRIPTS, ids=lambda p: p.stem)
def test_real_scripts_publish_fields_have_no_angle_brackets(path):
    """既存の実台本 fixture で metadata を生成し、< > が出ないことを確かめる"""
    vs = VerifiedScript.model_validate_json(path.read_text(encoding="utf-8"))
    script = RadioDirectorScriptLoader().load(path)
    title, description, _pub = _build_publish_fields(
        config=_config(),
        theme=vs.metadata.title,
        script=script,
        research_sources=_verified_refs_to_research_sources(vs.metadata.references) or None,
        generated_metadata={"title": vs.metadata.title, "description": vs.metadata.description},
        chapters=[],
        total_usage=TotalUsage(),
        footer_text_override=None,
        creation_date="2026.11.02",
    )
    for text in (title, description):
        assert "<" not in text and ">" not in text


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


def test_upload_guard_drops_angle_brackets(monkeypatch):
    """アップロード直前に < > が残っていても、落として続行する (実際の API は呼ばない)"""
    monkeypatch.setattr(workflow, "YouTubeClient", _FakeYouTubeClient)
    cb = _FakeCallbacks()
    url = _upload_to_youtube(
        config=SimpleNamespace(yaml=SimpleNamespace()),
        use_mock=False,
        upload_override=True,
        publishing_config=SimpleNamespace(enable_upload=True, playlist_id=None),
        video_path="v.mp4",
        formatted_title="<題>",
        formatted_description="概要\n📄 <4D6963>(example.go.jp)",
        thumbnail_path="th.png",
        callbacks=cb,
    )
    assert url == "https://www.youtube.com/watch?v=ABC123"
    sent = _FakeYouTubeClient.last.upload_calls[0]
    assert sent["title"] == "題"
    assert sent["description"] == "概要\n📄 4D6963(example.go.jp)"
    assert any("< > が残っていた" in m for m in cb.logs)
