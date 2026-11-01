"""Mac 書き出し用の正式な受け口 (ブリーフ 019)

radio_director の run_remote.py が実行中に差し替えていた 4 つを、run_workflow_sync の引数
(と環境変数) で受ける。指定が無いときの動きは今までと同じ。
1. reuse_strict: 使い回しの画像が足りなければ、合成の前に (漏れは画像を作る前に) 止める
2. thumbnail_bg: サムネイル背景を作らず、渡した画像を使う
3. youtube_dir: 認証ファイルをその置き場から読み、token が無ければブラウザに進まず失敗する
4. reading_fixes: 合成に渡す文だけを置き換え表で直す (字幕は本文のまま)
"""
import asyncio
import json
import wave
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

import workflow
from services.audio_synthesis import voicevox_client as voicevox_module
from services.audio_synthesis.reading_fixes import (
    ReadingFixesError,
    load_reading_fixes,
    parse_reading_fixes,
)
from services.audio_synthesis.voicevox_client import VoicevoxClient
from services.media_processing.scene_images import (
    SCENE_IMAGES_MANIFEST,
    ReuseStrictError,
    SceneImageOptions,
    build_scene_image_options,
)
from services.publishing import youtube_client as youtube_module
from services.publishing.youtube_client import YouTubeAuthError, YouTubeClient
from tests.test_scene_images_016 import IDS, _images_run, _provider, _scene, images_workflow  # noqa: F401
from tests.test_workflow_no_upload import _FakeYouTubeClient, _run, stubbed_workflow  # noqa: F401

ENV_NAMES = (
    workflow.REUSE_STRICT_ENV,
    workflow.THUMBNAIL_BG_ENV,
    workflow.YOUTUBE_DIR_ENV,
    workflow.READING_FIXES_ENV,
)


@pytest.fixture(autouse=True)
def _no_mac_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


# ================================================================ 共通: 指定が無ければ今までどおり

def test_resolve_defaults_are_off():
    assert workflow.resolve_mac_render_options(environ={}) == {
        "reuse_strict": False, "thumbnail_bg": None, "youtube_dir": None, "reading_fixes": None,
    }


def test_resolve_reads_env_and_arguments_win():
    env = {
        workflow.REUSE_STRICT_ENV: "yes",
        workflow.THUMBNAIL_BG_ENV: "/env/thumb.png",
        workflow.YOUTUBE_DIR_ENV: "/env/yt",
        workflow.READING_FIXES_ENV: " /env/fixes.json ",
    }
    assert workflow.resolve_mac_render_options(environ=env) == {
        "reuse_strict": True,
        "thumbnail_bg": Path("/env/thumb.png"),
        "youtube_dir": Path("/env/yt"),
        "reading_fixes": Path("/env/fixes.json"),
    }
    got = workflow.resolve_mac_render_options(
        thumbnail_bg="/arg/t.png", youtube_dir="/arg/yt", reading_fixes="/arg/f.json", environ=env,
    )
    assert got["thumbnail_bg"] == Path("/arg/t.png")
    assert got["youtube_dir"] == Path("/arg/yt")
    assert got["reading_fixes"] == Path("/arg/f.json")
    assert workflow.resolve_mac_render_options(environ={workflow.REUSE_STRICT_ENV: "0"})["reuse_strict"] is False


def test_default_run_result_fields(stubbed_workflow):
    result, _ = _run(skip_upload=True)
    assert result.success, result.error_message
    assert workflow.build_mac_render_result_fields(result) == {
        "reuse_strict": False,
        "reuse_strict_failed": False,
        "reuse_strict_stage": None,
        "missing_scene_images": [],
        "thumbnail_bg_source": None,
        "reading_fixes_path": None,
        "reading_fixes_applied": 0,
        "upload_error": None,
    }


def test_default_run_passes_no_new_kwargs(stubbed_workflow, monkeypatch):
    seen = {}

    async def _production(**kwargs):
        seen["production"] = kwargs
        raise RuntimeError("stop")

    monkeypatch.setattr(workflow, "execute_production_phase", _production)
    _run()
    assert "reading_fixes" not in seen["production"]
    assert "scene_options" not in seen["production"]


# ================================================================ 1. reuse_strict

def test_missing_for_does_not_consume_and_skips_regen():
    options = SceneImageOptions(
        regen_scenes={"c"},
        reuse_dir=Path("/prev"),
        reuse_entries={
            "a": [{"segment_id": "a", "image_path": "/prev/a.png"}],
            "b": [{"segment_id": "b", "image_path": None}],
            "d": [{"segment_id": "d", "image_path": "/prev/d.png"}],
        },
    )
    # b は画像が無い、c は作り直すので数えない、2 回目の a は 1 枚しかないので足りない、e は無い
    assert options.missing_for(["a", "b", "c", "a", "d", "e"]) == ["b", "a", "e"]
    assert len(options.reuse_entries["a"]) == 1  # 消費しない


def test_strict_needs_reuse_images_from():
    with pytest.raises(ValueError, match="reuse_images_from is required"):
        build_scene_image_options(reuse_strict=True)


def test_strict_provider_stops_before_generating(tmp_path, monkeypatch):
    _images_run(tmp_path, monkeypatch, IDS[:2])  # run1 には deep_dive_2 が無い
    options = build_scene_image_options(reuse_images_from=tmp_path / "run1", reuse_strict=True)
    assert options.strict is True
    calls = []
    provider = _provider(tmp_path, monkeypatch, calls, options, run="run2")
    assert asyncio.run(provider.get_image_for_segment(_scene("deep_dive_1_scene1"))).is_file()
    with pytest.raises(ReuseStrictError) as info:
        asyncio.run(provider.get_image_for_segment(_scene("deep_dive_2")))
    assert info.value.missing_ids == ["deep_dive_2"]
    assert info.value.stage == "render"
    assert calls == []  # 画像は 1 枚も作っていない


def test_strict_provider_still_regenerates_regen_scenes(tmp_path, monkeypatch):
    _images_run(tmp_path, monkeypatch, IDS)
    options = build_scene_image_options(
        reuse_images_from=tmp_path / "run1", regen_scenes=["deep_dive_2"], reuse_strict=True,
    )
    calls = []
    provider = _provider(tmp_path, monkeypatch, calls, options, run="run2")
    for sid in IDS:
        asyncio.run(provider.get_image_for_segment(_scene(sid)))
    assert [c["segment_id"] for c in calls] == ["deep_dive_2"]


def _drop_from_manifest(run_dir: Path, segment_id: str) -> None:
    manifest = run_dir / SCENE_IMAGES_MANIFEST
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["scene_images"] = [e for e in data["scene_images"] if e["segment_id"] != segment_id]
    manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_strict_full_run_with_all_images_goes_on(images_workflow, monkeypatch):
    first, _ = _run(images_only=True)
    seen = {}

    async def _production(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop after production is called")

    monkeypatch.setattr(workflow, "execute_production_phase", _production)
    result, logs = _run(reuse_images_from=str(first.output_dir), reuse_strict=True)
    assert seen["scene_options"].strict is True
    assert result.reuse_strict_failed is False
    assert any("揃っています" in m for m in logs)


def test_strict_full_run_stops_before_synthesis(images_workflow):
    first, _ = _run(images_only=True)
    _drop_from_manifest(first.output_dir, "deep_dive_1_scene2")
    image_calls_before = len(images_workflow.image_calls)

    result, logs = _run(reuse_images_from=str(first.output_dir), reuse_strict=True, skip_upload=True)
    assert result.success is False
    assert images_workflow.calls["production"] == 0  # 合成していない
    assert len(images_workflow.image_calls) == image_calls_before  # 画像も作っていない
    assert result.reuse_strict_failed is True
    assert result.reuse_strict_stage == "before_synthesis"
    assert result.missing_scene_images == ["deep_dive_1_scene2"]
    assert "reuse_strict" in result.error_message and "deep_dive_1_scene2" in result.error_message
    fields = workflow.build_mac_render_result_fields(result)
    assert fields["reuse_strict"] is True and fields["reuse_strict_failed"] is True
    assert fields["reuse_strict_stage"] == "before_synthesis"
    assert fields["missing_scene_images"] == ["deep_dive_1_scene2"]
    assert any("画像を作らずに止めました" in m for m in logs)


def test_strict_env_var_is_the_same(images_workflow, monkeypatch):
    first, _ = _run(images_only=True)
    _drop_from_manifest(first.output_dir, "intro")
    monkeypatch.setenv(workflow.REUSE_STRICT_ENV, "1")
    result, _ = _run(reuse_images_from=str(first.output_dir))
    assert result.success is False and result.reuse_strict_stage == "before_synthesis"
    assert result.missing_scene_images == ["intro"]
    assert images_workflow.calls["production"] == 0


def test_without_strict_missing_images_are_generated_as_before(images_workflow, monkeypatch):
    first, _ = _run(images_only=True)
    _drop_from_manifest(first.output_dir, "intro")
    seen = {}

    async def _production(**kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop")

    monkeypatch.setattr(workflow, "execute_production_phase", _production)
    result, _ = _run(reuse_images_from=str(first.output_dir))
    assert seen["scene_options"].strict is False  # 今までどおり作る経路
    assert result.reuse_strict_failed is False


def test_strict_failure_found_in_render_stage(images_workflow, monkeypatch):
    first, _ = _run(images_only=True)

    async def _production(**kwargs):
        raise ReuseStrictError(["deep_dive_1_scene3"], first.output_dir, "render")

    monkeypatch.setattr(workflow, "execute_production_phase", _production)
    result, _ = _run(reuse_images_from=str(first.output_dir), reuse_strict=True)
    assert result.success is False
    assert result.reuse_strict_failed is True and result.reuse_strict_stage == "render"
    assert result.missing_scene_images == ["deep_dive_1_scene3"]
    assert images_workflow.calls["metadata"] == 0 and images_workflow.calls["thumbnail"] == 0


def test_strict_without_reuse_dir_fails_early(stubbed_workflow):
    result, _ = _run(reuse_strict=True)
    assert result.success is False
    assert "reuse_images_from is required" in result.error_message
    assert stubbed_workflow.calls["production"] == 0


def test_strict_images_only_run_stops_too(images_workflow):
    first, _ = _run(images_only=True)
    _drop_from_manifest(first.output_dir, "deep_dive_1_scene1")
    before = len(images_workflow.image_calls)
    result, _ = _run(images_only=True, reuse_images_from=str(first.output_dir), reuse_strict=True)
    assert result.success is False and result.reuse_strict_stage == "before_synthesis"
    assert len(images_workflow.image_calls) == before


# ================================================================ 2. thumbnail_bg

class _FakeThumbnailGenerator:
    calls = []

    def generate(self, **kwargs):
        _FakeThumbnailGenerator.calls.append(kwargs)


class _NeverBgGen:
    def __init__(self, *a, **k):
        raise AssertionError("thumbnail_bg を渡したのに背景を作ろうとした")


class _Callbacks:
    def __init__(self):
        self.logs = []

    def log(self, msg):
        self.logs.append(msg)


@pytest.mark.parametrize("mode", ["dynamic", "static"])
def test_given_thumbnail_bg_is_used_without_generating(monkeypatch, tmp_path, mode):
    _FakeThumbnailGenerator.calls = []
    monkeypatch.setattr(workflow, "ThumbnailGenerator", _FakeThumbnailGenerator)
    monkeypatch.setattr(workflow, "ThumbnailBackgroundGenerator", _NeverBgGen)
    given = tmp_path / "images_run" / "thumbnail_bg.png"
    given.parent.mkdir()
    given.write_bytes(b"given-bg")
    out = tmp_path / "run"
    out.mkdir()
    config = SimpleNamespace(yaml=SimpleNamespace(
        video_renderer=SimpleNamespace(thumbnail_background_mode=mode),
        paths=SimpleNamespace(background_image="assets/bg.png"),
    ))
    cb = _Callbacks()
    thumb, bg_time = asyncio.run(workflow._generate_thumbnail_assets(
        config=config, output_base=out, project_root=tmp_path, theme="テーマ",
        script=SimpleNamespace(title="題", description="概要"), visual_identity=None,
        generated_metadata={"title": "MT", "thumbnail_title": "TT"}, skip_thumbnail_in_mock=False,
        callbacks=cb, thumbnail_bg_override=given,
    ))
    assert thumb == out / "thumbnail.png"
    assert bg_time == 0.0
    copied = out / "thumbnail_bg.png"
    assert copied.read_bytes() == b"given-bg"
    assert _FakeThumbnailGenerator.calls[0]["background_path"] == copied
    assert any("渡された画像を使います" in m for m in cb.logs)


def test_thumbnail_bg_is_passed_and_recorded(stubbed_workflow, monkeypatch, tmp_path):
    seen = {}

    async def _thumbnail(**kwargs):
        seen.update(kwargs)
        return kwargs["output_base"] / "thumbnail.png", 0.0

    monkeypatch.setattr(workflow, "_generate_thumbnail_assets", _thumbnail)
    given = tmp_path / "thumb.png"
    given.write_bytes(b"x")
    result, _ = _run(skip_upload=True, thumbnail_bg=str(given))
    assert result.success, result.error_message
    assert seen["thumbnail_bg_override"] == given
    assert workflow.build_mac_render_result_fields(result)["thumbnail_bg_source"] == str(given)


def test_default_run_does_not_pass_thumbnail_override(stubbed_workflow, monkeypatch):
    seen = {}

    async def _thumbnail(**kwargs):
        seen.update(kwargs)
        return kwargs["output_base"] / "thumbnail.png", 0.0

    monkeypatch.setattr(workflow, "_generate_thumbnail_assets", _thumbnail)
    result, _ = _run(skip_upload=True)
    assert result.success, result.error_message
    assert "thumbnail_bg_override" not in seen


def test_missing_thumbnail_bg_fails_before_synthesis(stubbed_workflow, tmp_path):
    result, _ = _run(thumbnail_bg=str(tmp_path / "nope.png"))
    assert result.success is False
    assert "thumbnail_bg" in result.error_message
    assert stubbed_workflow.calls["production"] == 0


# ================================================================ 3. youtube_dir

def _yt_config(root: Path):
    return SimpleNamespace(project_root=root, yaml=SimpleNamespace(publishing=None))


class _Creds:
    def __init__(self, valid=True, expired=False, refresh_token=None):
        self.valid = valid
        self.expired = expired
        self.refresh_token = refresh_token


@pytest.fixture
def no_browser(monkeypatch):
    def _flow(*a, **k):
        raise AssertionError("ブラウザの同意に進んだ")

    monkeypatch.setattr(youtube_module, "InstalledAppFlow", SimpleNamespace(from_client_secrets_file=_flow))


def test_youtube_default_paths_are_unchanged(tmp_path):
    client = YouTubeClient(_yt_config(tmp_path))
    assert client.client_secret_path == tmp_path / "client_secret.json"
    assert client.token_path == tmp_path / "token.json"
    assert client.allow_browser_auth is True
    assert client.credentials_dir is None


def test_youtube_dir_without_token_fails_without_browser(tmp_path, no_browser):
    ydir = tmp_path / "yt"
    ydir.mkdir()
    client = YouTubeClient(_yt_config(tmp_path), credentials_dir=ydir)
    assert client.client_secret_path == ydir / "client_secret.json"
    assert client.token_path == ydir / "token.json"
    with pytest.raises(YouTubeAuthError, match="token.json"):
        client._get_credentials()


def test_youtube_dir_reads_token_from_that_dir(tmp_path, monkeypatch, no_browser):
    ydir = tmp_path / "yt"
    ydir.mkdir()
    (ydir / "token.json").write_text("{}", encoding="utf-8")  # 中身は偽物 (読み込みは差し替え)
    read = []

    def _from_file(path, scopes):
        read.append(path)
        return _Creds(valid=True)

    monkeypatch.setattr(youtube_module, "Credentials", SimpleNamespace(from_authorized_user_file=_from_file))
    creds = YouTubeClient(_yt_config(tmp_path), credentials_dir=ydir)._get_credentials()
    assert creds.valid and read == [str(ydir / "token.json")]


def test_youtube_dir_unusable_token_fails_without_browser(tmp_path, monkeypatch, no_browser):
    ydir = tmp_path / "yt"
    ydir.mkdir()
    (ydir / "token.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        youtube_module, "Credentials",
        SimpleNamespace(from_authorized_user_file=lambda p, s: _Creds(valid=False, expired=True)),
    )
    with pytest.raises(YouTubeAuthError, match="使えません"):
        YouTubeClient(_yt_config(tmp_path), credentials_dir=ydir)._get_credentials()


class _DirAwareYouTubeClient:
    instances = []
    fail = False

    def __init__(self, config, credentials_dir=None):
        self.credentials_dir = credentials_dir
        _DirAwareYouTubeClient.instances.append(self)

    def upload_video(self, **kwargs):
        if _DirAwareYouTubeClient.fail:
            raise YouTubeAuthError("token.json が見つかりません")
        return "https://www.youtube.com/watch?v=XYZ"

    def add_video_to_playlist(self, **kwargs):
        pass


@pytest.fixture
def dir_aware_client(monkeypatch):
    _DirAwareYouTubeClient.instances = []
    _DirAwareYouTubeClient.fail = False
    monkeypatch.setattr(workflow, "YouTubeClient", _DirAwareYouTubeClient)
    return _DirAwareYouTubeClient


def test_youtube_dir_is_passed_to_the_client(stubbed_workflow, dir_aware_client, tmp_path):
    ydir = tmp_path / "yt"
    ydir.mkdir()
    result, _ = _run(youtube_dir=str(ydir))
    assert result.success, result.error_message
    [client] = dir_aware_client.instances
    assert client.credentials_dir == ydir
    assert result.uploaded_video_url == "https://www.youtube.com/watch?v=XYZ"


def test_youtube_dir_upload_failure_is_reported(stubbed_workflow, dir_aware_client, tmp_path):
    ydir = tmp_path / "yt"
    ydir.mkdir()
    dir_aware_client.fail = True
    result, _ = _run(youtube_dir=str(ydir))
    assert result.success, result.error_message  # 動画は成功のまま (今までと同じく非致命)
    assert result.uploaded_video_url is None
    assert workflow.build_upload_result_fields(result)["upload_status"] == "not_uploaded"
    assert "YouTubeAuthError" in workflow.build_mac_render_result_fields(result)["upload_error"]


def test_default_upload_does_not_pass_credentials_dir(stubbed_workflow, dir_aware_client):
    result, _ = _run()
    assert result.success, result.error_message
    assert dir_aware_client.instances[0].credentials_dir is None


def test_missing_youtube_dir_fails_before_synthesis(stubbed_workflow, tmp_path):
    result, _ = _run(youtube_dir=str(tmp_path / "nope"))
    assert result.success is False and "youtube_dir" in result.error_message
    assert stubbed_workflow.calls["production"] == 0


# ================================================================ 4. reading_fixes

def test_reading_fixes_shapes():
    flat = parse_reading_fixes({"_comment": "メモ", "Wi-Fi": "ワイファイ", "5GHz": "ごギガヘルツ"})
    assert flat.table == {"Wi-Fi": "ワイファイ", "5GHz": "ごギガヘルツ"}
    wrapped = parse_reading_fixes({"version": 1, "fixes": {"BLE": "ビーエルイー"}})
    assert wrapped.table == {"BLE": "ビーエルイー"}
    listed = parse_reading_fixes({"entries": [{"word": "IoT", "reading": "アイオーティー"}, {"from": "x", "to": "エックス"}]})
    assert listed.table == {"IoT": "アイオーティー", "x": "エックス"}
    with pytest.raises(ReadingFixesError):
        parse_reading_fixes({"Wi-Fi": 1})
    with pytest.raises(ReadingFixesError):
        parse_reading_fixes("Wi-Fi")


def test_reading_fixes_longest_first_and_single_pass():
    fixes = parse_reading_fixes({"Wi-Fi": "ワイファイ", "Wi-Fi 6": "ワイファイシックス", "ワイ": "だめ"})
    assert fixes.apply("Wi-Fi 6 と Wi-Fi") == "ワイファイシックス と ワイファイ"
    assert fixes.applied_count == 2
    assert fixes.apply("") == ""


def test_load_reading_fixes_errors(tmp_path):
    with pytest.raises(ReadingFixesError, match="not found"):
        load_reading_fixes(tmp_path / "nope.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(ReadingFixesError, match="cannot read"):
        load_reading_fixes(broken)
    good = tmp_path / "reading_fixes.json"
    good.write_text(json.dumps({"Wi-Fi": "ワイファイ"}, ensure_ascii=False), encoding="utf-8")
    assert load_reading_fixes(good).table == {"Wi-Fi": "ワイファイ"}


def _wav_bytes() -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * 2400)
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, data=None, content=b""):
        self._data = data
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return dict(self._data or {})


class _FakeHttp:
    queries = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, params=None, json=None):
        if url.endswith("/audio_query"):
            _FakeHttp.queries.append(params["text"])
            return _FakeResponse(data={})
        return _FakeResponse(content=_wav_bytes())


def _synthesize(mock_app_config, tmp_path, monkeypatch, reading_fixes=None):
    _FakeHttp.queries = []
    monkeypatch.setattr(voicevox_module.httpx, "AsyncClient", _FakeHttp)
    client = VoicevoxClient(mock_app_config, reading_fixes=reading_fixes) if reading_fixes else VoicevoxClient(mock_app_config)
    subtitles = []
    monkeypatch.setattr(client, "_generate_ass", lambda phrase_data, path: subtitles.extend(p[3] for p in phrase_data))
    monkeypatch.setattr(client, "_build_chapters", lambda *a, **k: [])
    lines = [
        SimpleNamespace(speaker="A", text="Wi-Fi の 5GHz は速いのだ[src=1]", section=None, chapter_title=None),
        SimpleNamespace(speaker="B", text="そうね。", section=None, chapter_title=None),
    ]
    script = SimpleNamespace(get_dialogue_only=lambda: lines)
    asyncio.run(client.synthesize(script, tmp_path / "audio"))
    return _FakeHttp.queries, subtitles


def test_reading_fixes_change_only_the_synthesis_text(mock_app_config, tmp_path, monkeypatch):
    fixes = parse_reading_fixes({"Wi-Fi": "ワイファイ", "5GHz": "ごギガヘルツ"})
    queries, subtitles = _synthesize(mock_app_config, tmp_path, monkeypatch, fixes)
    assert queries == ["ワイファイ の ごギガヘルツ は速いのだ", "そうね。"]
    assert subtitles == ["Wi-Fi の 5GHz は速いのだ[src=1]", "そうね。"]  # 字幕は本文のまま
    assert fixes.applied_count == 2


def test_without_reading_fixes_synthesis_text_is_unchanged(mock_app_config, tmp_path, monkeypatch):
    queries, _ = _synthesize(mock_app_config, tmp_path, monkeypatch)
    assert queries == ["Wi-Fi の 5GHz は速いのだ", "そうね。"]


def test_reading_fixes_are_passed_to_production(stubbed_workflow, monkeypatch, tmp_path):
    path = tmp_path / "reading_fixes.json"
    path.write_text(json.dumps({"Wi-Fi": "ワイファイ"}, ensure_ascii=False), encoding="utf-8")
    seen = {}

    async def _production(**kwargs):
        seen.update(kwargs)
        kwargs["reading_fixes"].apply("Wi-Fi と Wi-Fi")  # 合成で 2 回置き換えた想定
        return SimpleNamespace(
            video_path=tmp_path / "v.mp4", audio_path=None, subtitle_path=None, duration_sec=1.0,
            file_size_mb=1.0, chapters=[], voicevox_usage=None, audio_duration_sec=1.0,
            render_duration_sec=1.0, segment_bg_generation_time=0.0,
        )

    monkeypatch.setattr(workflow, "execute_production_phase", _production)
    result, _ = _run(skip_upload=True, reading_fixes=str(path))
    assert result.success, result.error_message
    assert seen["reading_fixes"].table == {"Wi-Fi": "ワイファイ"}
    fields = workflow.build_mac_render_result_fields(result)
    assert fields["reading_fixes_path"] == str(path)
    assert fields["reading_fixes_applied"] == 2


def test_broken_reading_fixes_fail_before_synthesis(stubbed_workflow, tmp_path):
    path = tmp_path / "reading_fixes.json"
    path.write_text("[1, 2]", encoding="utf-8")
    result, _ = _run(reading_fixes=str(path))
    assert result.success is False and "reading_fixes" in result.error_message
    assert stubbed_workflow.calls["production"] == 0
