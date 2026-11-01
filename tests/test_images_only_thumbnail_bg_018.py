"""画像だけを作る実行でサムネイル背景も作る (018)

Mac の書き出し (radio_director) は、画像段の run の <output_dir>/thumbnail_bg.png を --thumbnail-bg で使う。
- thumbnail_background_mode="dynamic" なら images_only で thumbnail_bg.png を場面画像と同じ run に作る
- 失敗しても場面画像の結果はそのまま返し、警告にする
- 本番の実行 (images_only でない) の経路は変えない
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

import workflow
from services.media_processing.scene_images import SCENE_IMAGES_DIR, SCENE_IMAGES_MANIFEST
from tests.test_scene_images_016 import images_workflow  # noqa: F401
from tests.test_workflow_no_upload import _run, stubbed_workflow  # noqa: F401


class _FakeThumbnailBgGenerator:
    instances = []
    fail = False
    write_elsewhere = False

    def __init__(self, config, output_dir=None):
        self.output_dir = output_dir
        self.calls = []
        _FakeThumbnailBgGenerator.instances.append(self)

    async def generate(self, theme, script_summary, output_path, visual_identity=None, topic_title=None):
        self.calls.append({
            "theme": theme, "script_summary": script_summary, "output_path": output_path,
            "visual_identity": visual_identity, "topic_title": topic_title,
        })
        if _FakeThumbnailBgGenerator.fail:
            raise RuntimeError("ComfyUI is down")
        if _FakeThumbnailBgGenerator.write_elsewhere:
            other = Path(output_path).parent / "elsewhere_thumb.png"
            other.write_bytes(b"thumb-bg")
            return other
        Path(output_path).write_bytes(b"thumb-bg")
        return output_path


@pytest.fixture
def thumb_workflow(images_workflow, monkeypatch):  # noqa: F811
    _FakeThumbnailBgGenerator.instances = []
    _FakeThumbnailBgGenerator.fail = False
    _FakeThumbnailBgGenerator.write_elsewhere = False
    monkeypatch.setattr(workflow, "ThumbnailBackgroundGenerator", _FakeThumbnailBgGenerator)
    config = workflow.load_config(None)
    monkeypatch.setattr(
        config.yaml, "video_renderer", SimpleNamespace(thumbnail_background_mode="dynamic"), raising=False,
    )
    images_workflow.config = config
    return images_workflow


def test_images_only_writes_thumbnail_bg_into_run_dir(thumb_workflow):
    result, logs = _run(images_only=True)
    assert result.success, result.error_message
    assert thumb_workflow.calls == {"production": 0, "metadata": 0, "thumbnail": 0}

    expected = result.output_dir / workflow.THUMBNAIL_BG_FILENAME
    assert expected.name == "thumbnail_bg.png"
    assert result.thumbnail_bg_path == expected
    assert expected.read_bytes() == b"thumb-bg"
    # 場面画像と同じ run に置く
    assert expected.parent == result.output_dir
    assert (result.output_dir / SCENE_IMAGES_MANIFEST).is_file()
    assert (result.output_dir / SCENE_IMAGES_DIR).is_dir()
    assert len(result.scene_images) == 3

    [gen] = _FakeThumbnailBgGenerator.instances
    assert gen.output_dir == result.output_dir
    assert gen.calls[0]["output_path"] == expected
    assert gen.calls[0]["topic_title"] == "加湿器の回"
    assert not any("thumbnail" in w for w in result.warnings)
    assert any("サムネイル背景" in m for m in logs)


def test_images_only_thumbnail_bg_failure_is_a_warning(thumb_workflow):
    _FakeThumbnailBgGenerator.fail = True
    result, logs = _run(images_only=True)
    assert result.success, result.error_message
    assert result.thumbnail_bg_path is None
    assert not (result.output_dir / "thumbnail_bg.png").exists()
    # 場面画像の結果は壊さない
    assert [r["segment_id"] for r in result.scene_images] == ["intro", "deep_dive_1_scene1", "deep_dive_1_scene2"]
    assert all(Path(r["image_path"]).is_file() for r in result.scene_images)
    assert (result.output_dir / SCENE_IMAGES_MANIFEST).is_file()
    warnings = [w for w in result.warnings if "thumbnail background" in w]
    assert len(warnings) == 1 and "ComfyUI is down" in warnings[0]
    assert any("ComfyUI is down" in m for m in logs)


def test_images_only_copies_when_generator_returns_another_path(thumb_workflow):
    _FakeThumbnailBgGenerator.write_elsewhere = True
    result, _ = _run(images_only=True)
    assert result.success, result.error_message
    assert result.thumbnail_bg_path == result.output_dir / "thumbnail_bg.png"
    assert result.thumbnail_bg_path.read_bytes() == b"thumb-bg"


def test_images_only_static_mode_makes_no_thumbnail_bg(thumb_workflow, monkeypatch):
    monkeypatch.setattr(
        thumb_workflow.config.yaml, "video_renderer", SimpleNamespace(thumbnail_background_mode="static"),
    )
    result, _ = _run(images_only=True)
    assert result.success, result.error_message
    assert _FakeThumbnailBgGenerator.instances == []
    assert result.thumbnail_bg_path is None
    assert not (result.output_dir / "thumbnail_bg.png").exists()
    assert not any("thumbnail" in w for w in result.warnings)


def test_full_run_does_not_use_the_images_only_path(thumb_workflow):
    result, _ = _run(skip_upload=True)
    assert result.success, result.error_message
    # 本番の実行は今までどおり _generate_thumbnail_assets だけを通る
    assert thumb_workflow.calls == {"production": 1, "metadata": 1, "thumbnail": 1}
    assert _FakeThumbnailBgGenerator.instances == []
    assert result.thumbnail_bg_path is None
