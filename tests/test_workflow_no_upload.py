"""アップロードしない実行のスイッチ (ブリーフ 012) のテスト。

run_workflow_sync を、各フェーズをスタブにして通しで呼び、
- skip_upload=True / 環境変数 ARG_NO_UPLOAD=1 のときは YouTubeClient を作らない
  (公開・サムネ設定・再生リスト追加が起きない) が、制作・メタデータ・サムネ画像は通る
- どちらも無ければ今までどおりアップロードする
- result.json に足す欄 (build_upload_result_fields) が公開の有無と mp4 のパスを返す
を確かめる。
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

import services.pipeline
import workflow
from workflow import (
    NO_UPLOAD_ENV,
    WorkflowResult,
    build_upload_result_fields,
    no_upload_requested,
    run_workflow_sync,
)


class _FakeYouTubeClient:
    instances = []

    def __init__(self, config):
        self.upload_calls = []
        _FakeYouTubeClient.instances.append(self)

    def upload_video(self, **kwargs):
        self.upload_calls.append(kwargs)
        return "https://www.youtube.com/watch?v=ABC123"

    def add_video_to_playlist(self, **kwargs):
        pass


class _FakeLogWriter:
    def __init__(self, output_base):
        self.log_path = Path(output_base) / "process.log"

    def write(self, msg):
        pass

    def finalize(self):
        pass


class _FakeCostCalculator:
    def __init__(self, config):
        pass

    def calculate(self, usage):
        return SimpleNamespace(total_usd=0.0)

    def format_cost_report(self, usage, cost):
        return ""


@pytest.fixture
def stubbed_workflow(monkeypatch, tmp_path):
    """run_workflow_sync の重い処理をすべてスタブにし、呼ばれたものを記録する。"""
    calls = {"production": 0, "metadata": 0, "thumbnail": 0}
    _FakeYouTubeClient.instances = []
    monkeypatch.delenv(NO_UPLOAD_ENV, raising=False)

    config = SimpleNamespace(
        yaml=SimpleNamespace(
            dev=SimpleNamespace(mock_mode=False, mock_data_path="tests/mock_data"),
            paths=SimpleNamespace(output_dir="output"),
            script_generator=SimpleNamespace(default_provider="gemini"),
        )
    )
    monkeypatch.setattr(workflow, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(workflow, "load_config", lambda root: config)
    monkeypatch.setattr(workflow, "apply_overrides", lambda cfg, ov: cfg)

    async def _prereq(cfg, log_cb):
        return True, None

    monkeypatch.setattr(workflow, "check_prerequisites", _prereq)
    monkeypatch.setattr(workflow, "LogFileWriter", _FakeLogWriter)

    import core.session_manager

    monkeypatch.setattr(
        core.session_manager, "SessionManager", lambda **kwargs: SimpleNamespace()
    )

    async def _external(**kwargs):
        return SimpleNamespace(
            verified_script=SimpleNamespace(
                metadata=SimpleNamespace(title="Wi-Fi の回", references=[])
            ),
            script=SimpleNamespace(sections=[], title="Wi-Fi の回"),
            segments=[],
            visual_identity=None,
        )

    monkeypatch.setattr(services.pipeline, "execute_external_script_phase", _external)

    video_path = tmp_path / "output" / "video.mp4"

    async def _production(**kwargs):
        calls["production"] += 1
        return SimpleNamespace(
            video_path=video_path,
            audio_path=tmp_path / "audio.wav",
            subtitle_path=tmp_path / "sub.ass",
            duration_sec=60.0,
            file_size_mb=5.0,
            chapters=[],
            voicevox_usage=None,
            audio_duration_sec=60.0,
            render_duration_sec=10.0,
            segment_bg_generation_time=0.0,
        )

    monkeypatch.setattr(workflow, "execute_production_phase", _production)

    def _metadata(**kwargs):
        calls["metadata"] += 1
        path = kwargs["output_base"] / "metadata.txt"
        path.write_text("meta", encoding="utf-8")
        return path, {}

    monkeypatch.setattr(workflow, "_run_metadata_phase", _metadata)

    async def _thumbnail(**kwargs):
        calls["thumbnail"] += 1
        return kwargs["output_base"] / "thumbnail.png", 0.0

    monkeypatch.setattr(workflow, "_generate_thumbnail_assets", _thumbnail)
    monkeypatch.setattr(
        workflow,
        "_build_publish_fields",
        lambda **kwargs: (
            "題",
            "概要",
            SimpleNamespace(enable_upload=True, playlist_id=None),
        ),
    )
    monkeypatch.setattr(workflow, "CostCalculator", _FakeCostCalculator)
    monkeypatch.setattr(workflow, "_record_execution_logs", lambda **kwargs: None)
    monkeypatch.setattr(workflow, "YouTubeClient", _FakeYouTubeClient)

    return SimpleNamespace(calls=calls, video_path=video_path)


def _run(**kwargs):
    logs = []
    result = run_workflow_sync(
        theme="",
        log_callback=logs.append,
        external_script_path="verified_script.json",
        **kwargs,
    )
    return result, logs


def test_default_still_uploads(stubbed_workflow):
    result, _ = _run()
    assert result.success, result.error_message
    assert len(_FakeYouTubeClient.instances) == 1
    assert result.uploaded_video_url == "https://www.youtube.com/watch?v=ABC123"
    assert result.upload_skipped is False
    assert build_upload_result_fields(result) == {
        "upload_skipped": False,
        "upload_status": "uploaded",
        "video_path": str(stubbed_workflow.video_path),
    }


def test_skip_upload_argument_skips_only_publishing(stubbed_workflow):
    # UI 優先フラグで有効にしていても、アップロードしない実行が勝つ
    result, logs = _run(skip_upload=True, upload_override=True)
    assert result.success, result.error_message
    assert _FakeYouTubeClient.instances == []  # 公開・サムネ設定・再生リストのどれも起きない
    assert result.uploaded_video_url is None
    assert result.upload_skipped is True
    assert result.video_path == stubbed_workflow.video_path
    # 制作・メタデータ・サムネ画像は本番と同じに通る
    assert stubbed_workflow.calls == {"production": 1, "metadata": 1, "thumbnail": 1}
    assert any("アップロードしない実行" in m for m in logs)
    assert build_upload_result_fields(result) == {
        "upload_skipped": True,
        "upload_status": "skipped",
        "video_path": str(stubbed_workflow.video_path),
    }


def test_env_var_skips_upload(stubbed_workflow, monkeypatch):
    monkeypatch.setenv(NO_UPLOAD_ENV, "1")
    result, _ = _run(upload_override=True)
    assert result.success, result.error_message
    assert _FakeYouTubeClient.instances == []
    assert result.upload_skipped is True
    assert stubbed_workflow.calls["production"] == 1


@pytest.mark.parametrize(
    "value, expected",
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        (" yes ", True),
        ("on", True),
        ("", False),
        ("0", False),
        ("false", False),
        ("no", False),
    ],
)
def test_no_upload_requested_values(value, expected):
    assert no_upload_requested({NO_UPLOAD_ENV: value}) is expected


def test_no_upload_requested_when_unset():
    assert no_upload_requested({}) is False


def test_result_fields_not_uploaded_when_disabled_or_failed():
    result = WorkflowResult(success=True, video_path=Path("out/video.mp4"))
    fields = build_upload_result_fields(result)
    assert fields["upload_skipped"] is False
    assert fields["upload_status"] == "not_uploaded"
    assert fields["video_path"] == str(Path("out/video.mp4"))


def test_result_fields_without_video_path():
    result = WorkflowResult(success=False, error_message="boom")
    assert build_upload_result_fields(result)["video_path"] is None
