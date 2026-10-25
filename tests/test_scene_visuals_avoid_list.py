"""visuals の avoid を仕様 (spec §12) の List[str] で受け、警告と画像プロンプトの記録を Mac に返すことのテスト (013)

- radio_director-019 の写しと同じ形 (avoid がリスト、深掘り 3 章が 3・4・4 場面) で、
  3 章とも visuals どおりの場面数・draw になる (警告なし)
- avoid は List[str]。str 1 つは要素 1 つのリスト、null・欄なしは空リスト。要素が str でなければ不正
- 不正な visuals を捨てたときの警告が ExternalScriptPhaseResult / RenderResult / WorkflowResult の
  warnings に載る (result.json の warnings に入れる元)
- 画像プロンプトの記録 (generation_metadata.jsonl / prompt_report.md) に場面の指定とキャッシュの使用が残る
"""
import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

import services.pipeline
import workflow
from core.models.curation import SceneVisual, ScriptSegment
from core.models.generation_metadata import GenerationMetadata
from core.models.verified_script import VerifiedScript
from services.media_processing.image_provider import ImageProvider
from services.media_processing.prompt_ops_logger import PromptOpsLogger
from services.pipeline.external_script_phase import execute_external_script_phase
from services.script_loading.radio_director_loader import (
    build_script_segments,
    check_scene_visuals,
    collect_visuals_warnings,
)
from tests.test_scene_visuals import (
    BASE_SCRIPT,
    FACTORY_PATH,
    _deep_dive_indices,
    _make_generator,
    _raw,
    _run_timeline,
    _timings,
)
from tests.test_workflow_no_upload import _run, stubbed_workflow  # noqa: F401 (fixture)

LOADER_LOGGER = "services.script_loading.radio_director_loader"

# 019 の写しの形: 深掘り 3 章に 3・4・4 場面。avoid はリスト (1 件・2 件・欄なし・空リストを混ぜる)
SCENE_COUNTS = [3, 4, 4]


def _wifi_visuals(num_turns: int, num_scenes: int, chapter: int) -> list[dict]:
    starts = [num_turns * k // num_scenes for k in range(num_scenes)]
    visuals = []
    for k, start in enumerate(starts):
        item = {
            "start_turn": start,
            "focus": f"第{chapter}章の要点{k + 1}",
            "draw": f"第{chapter}章の場面{k + 1}: 棚の上の白いルーター {k + 1} 台",
        }
        if k % 4 == 0:
            item["avoid"] = ["ルーターが床に直置きされている"]
        elif k % 4 == 1:
            item["avoid"] = ["電子レンジのすぐ横に置かれている", "機器が 1 台だけ写っている"]
        elif k % 4 == 2:
            item["avoid"] = []
        # k % 4 == 3: avoid の欄なし
        visuals.append(item)
    return visuals


def _wifi_raw() -> dict:
    raw = _raw()
    for chapter, (idx, count) in enumerate(zip(_deep_dive_indices(raw), SCENE_COUNTS), start=1):
        seg = raw["script"]["segments"][idx]
        seg["visuals"] = _wifi_visuals(len(seg["turns"]), count, chapter)
    return raw


def _load(raw: dict) -> list[ScriptSegment]:
    return build_script_segments(VerifiedScript.model_validate_json(json.dumps(raw, ensure_ascii=False)))


# ---------------------------------------------------------------- 019 の形の写し

def test_wifi_copy_all_three_chapters_follow_visuals(mock_app_config, caplog):
    raw = _wifi_raw()
    with caplog.at_level(logging.WARNING):
        segments = _load(raw)
        timeline, asked, _ = _run_timeline(mock_app_config, segments)
    assert not [r for r in caplog.records if "visuals" in r.getMessage()]
    assert collect_visuals_warnings(segments) == []

    for chapter, (idx, count) in enumerate(zip(_deep_dive_indices(raw), SCENE_COUNTS), start=1):
        given = raw["script"]["segments"][idx]["visuals"]
        entry = timeline.segments[idx]
        assert len(entry.scenes) == count
        assert [scene.start_turn for scene in entry.scenes] == [v["start_turn"] for v in given]

        seg_id = segments[idx].segment_id
        scene_segs = [s for s in asked if s.segment_id.startswith(f"{seg_id}_scene")]
        assert [s.segment_id for s in scene_segs] == [f"{seg_id}_scene{k + 1}" for k in range(count)]
        assert [s.scene_visual.draw for s in scene_segs] == [v["draw"] for v in given]
        assert [s.scene_visual.focus for s in scene_segs] == [v["focus"] for v in given]
        assert [s.scene_visual.avoid for s in scene_segs] == [v.get("avoid", []) for v in given]


def test_wifi_copy_through_external_script_phase(tmp_path, mock_app_config):
    """本番と同じ読み込み口 (execute_external_script_phase) でも警告なしで visuals が残る。"""
    path = tmp_path / "verified_script.json"
    path.write_text(json.dumps(_wifi_raw(), ensure_ascii=False), encoding="utf-8")
    palette = MagicMock()
    palette.return_value.generate_identity = AsyncMock(return_value=None)
    with patch("services.pipeline.external_script_phase.VisualPaletteGenerator", palette):
        result = asyncio.run(execute_external_script_phase(
            verified_script_path=path,
            session_manager=MagicMock(session_id="s", save_script_artifact=lambda a: tmp_path / "a.json"),
            config=mock_app_config,
            callbacks=SimpleNamespace(log=lambda m: None),
        ))
    assert result.warnings == []
    deep_dives = [s for s in result.segments if s.segment_type == "deep_dive"]
    assert [len(s.visuals) for s in deep_dives] == SCENE_COUNTS


@patch(FACTORY_PATH)
def test_avoid_list_goes_into_prompt_on_one_line(mock_factory_create):
    gen, port = _make_generator(mock_factory_create)
    seg = ScriptSegment(
        segment_id="deep_dive_1_scene2",
        segment_type="deep_dive",
        topic_title="電子レンジと Wi-Fi",
        turns=[{"speaker": "A", "text": "電子レンジの近くは避けるのだ。"}],
        scene_visual=SceneVisual(
            start_turn=3,
            focus="電子レンジの干渉",
            draw="棚の上の黒いルーター 1 台",
            avoid=["電子レンジのすぐ横に置かれている", "機器が床に置かれている"],
        ),
    )
    asyncio.run(gen.generate_prompt(seg))
    user_prompt = port.generate.call_args.args[0].user_prompt
    assert (
        "- The picture must NOT show: 電子レンジのすぐ横に置かれている; 機器が床に置かれている"
        in user_prompt
    )
    assert "opposite state" in user_prompt


# ---------------------------------------------------------------- avoid の型

@pytest.mark.parametrize("avoid, expected", [
    (["床置き"], ["床置き"]),
    (["床置き", "窓際"], ["床置き", "窓際"]),
    ("床置き", ["床置き"]),       # str は要素 1 つのリスト
    (None, []),
    ([], []),
    (["床置き", "  "], ["床置き"]),  # 空白だけの要素は落とす
])
def test_avoid_accepts_list_and_single_string(avoid, expected):
    visuals, warning = check_scene_visuals(
        [{"start_turn": 0, "focus": "要点", "draw": "描くもの", "avoid": avoid}], 5, "deep_dive_1",
    )
    assert warning is None
    assert visuals[0].avoid == expected


def test_avoid_missing_is_empty_list():
    visuals, warning = check_scene_visuals([{"start_turn": 0, "focus": "要点", "draw": "描くもの"}], 5, "intro")
    assert warning is None and visuals[0].avoid == []


@pytest.mark.parametrize("avoid", [1, ["床置き", 1], {"text": "床置き"}, [["床置き"]]])
def test_avoid_with_non_string_items_is_invalid(avoid, caplog):
    with caplog.at_level(logging.WARNING, logger=LOADER_LOGGER):
        visuals, warning = check_scene_visuals(
            [{"start_turn": 0, "focus": "要点", "draw": "描くもの", "avoid": avoid}], 5, "deep_dive_2",
        )
    assert visuals is None
    assert "deep_dive_2" in warning and "avoid" in warning and "scene_splitter" in warning
    assert [r.getMessage() for r in caplog.records] == [warning]


# ---------------------------------------------------------------- 警告を result.json まで運ぶ

def test_invalid_visuals_warning_is_kept_on_segment():
    raw = _wifi_raw()
    dd2 = _deep_dive_indices(raw)[1]
    raw["script"]["segments"][dd2]["visuals"][1]["start_turn"] = 0  # 昇順でない
    segments = _load(raw)
    warnings = collect_visuals_warnings(segments)
    assert len(warnings) == 1
    assert warnings[0].startswith("visuals of deep_dive_2 is invalid (scene 1: start_turn 0 not after 0)")
    assert segments[dd2].visuals is None
    assert all(s.visuals is not None for s in segments if s.segment_type == "deep_dive" and s is not segments[dd2])


def test_external_script_phase_returns_and_logs_warnings(tmp_path, mock_app_config):
    raw = _wifi_raw()
    dd3 = _deep_dive_indices(raw)[2]
    raw["script"]["segments"][dd3]["visuals"] = [{"start_turn": 0, "focus": "要点"}]  # draw が無い
    path = tmp_path / "verified_script.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    logs = []
    palette = MagicMock()
    palette.return_value.generate_identity = AsyncMock(return_value=None)
    with patch("services.pipeline.external_script_phase.VisualPaletteGenerator", palette):
        result = asyncio.run(execute_external_script_phase(
            verified_script_path=path,
            session_manager=MagicMock(session_id="s", save_script_artifact=lambda a: tmp_path / "a.json"),
            config=mock_app_config,
            callbacks=SimpleNamespace(log=logs.append),
        ))
    assert len(result.warnings) == 1 and "deep_dive_3" in result.warnings[0]
    assert any(m.startswith("[WARN] visuals of deep_dive_3") for m in logs)


def test_timeline_keeps_warning_when_turn_times_are_missing(mock_app_config):
    segments = _load(_wifi_raw())
    timings, _ = _timings(segments)
    dd1 = _deep_dive_indices(_raw())[0]
    timings[dd1].turn_start_secs = []
    from services.video_rendering.timeline_calculator import TimelineCalculator

    calc = TimelineCalculator(mock_app_config)
    calc.warnings = ["前回の警告"]  # 計算のたびに作り直す
    image_provider = MagicMock()

    async def get_image(seg):
        return Path(f"bg_{seg.segment_id}.png")

    image_provider.get_image_for_segment = get_image
    from core.interfaces import SynthesisResult

    asyncio.run(calc.calculate_timeline(
        segments=segments,
        synthesis_result=SynthesisResult(
            audio_path=Path("a.wav"), subtitle_path=Path("s.ass"),
            total_duration_sec=timings[-1].end_sec + 2.0, segment_timings=timings,
        ),
        image_provider=image_provider,
        jingle_provider=MagicMock(),
        bgm_path=Path("bgm.wav"),
    ))
    assert len(calc.warnings) == 1
    assert calc.warnings[0].startswith("visuals of deep_dive_1 ignored")


def test_workflow_result_carries_warnings_and_prompt_logs(stubbed_workflow, monkeypatch):  # noqa: F811
    original_production = workflow.execute_production_phase

    async def _external(**kwargs):
        return SimpleNamespace(
            verified_script=SimpleNamespace(metadata=SimpleNamespace(title="Wi-Fi の回", references=[])),
            script=SimpleNamespace(sections=[], title="Wi-Fi の回"),
            segments=[],
            visual_identity=None,
            warnings=["visuals of deep_dive_2 is invalid (x); falling back to scene_splitter for this segment"],
        )

    async def _production(**kwargs):
        result = await original_production(**kwargs)
        out = kwargs["output_dir"]
        (out / "prompt_report.md").write_text("# report", encoding="utf-8")
        (out / "generation_metadata.jsonl").write_text("{}\n", encoding="utf-8")
        result.warnings = ["visuals of deep_dive_3 ignored: 0 turn start times for 9 turns; falling back to scene_splitter"]
        return result

    monkeypatch.setattr(services.pipeline, "execute_external_script_phase", _external)
    monkeypatch.setattr(workflow, "execute_production_phase", _production)
    result, logs = _run(skip_upload=True)
    assert result.success, result.error_message
    assert result.warnings == [
        "visuals of deep_dive_2 is invalid (x); falling back to scene_splitter for this segment",
        "visuals of deep_dive_3 ignored: 0 turn start times for 9 turns; falling back to scene_splitter",
    ]
    assert result.prompt_report_path == result.output_dir / "prompt_report.md"
    assert result.generation_metadata_path == result.output_dir / "generation_metadata.jsonl"
    assert workflow.build_prompt_log_result_fields(result) == {
        "prompt_report_path": str(result.output_dir / "prompt_report.md"),
        "generation_metadata_path": str(result.output_dir / "generation_metadata.jsonl"),
    }
    assert any("警告 2 件" in m for m in logs)


def test_workflow_result_without_warnings_or_prompt_logs(stubbed_workflow):  # noqa: F811
    result, _ = _run(skip_upload=True)
    assert result.success, result.error_message
    assert result.warnings == []
    assert workflow.build_prompt_log_result_fields(result) == {
        "prompt_report_path": None,
        "generation_metadata_path": None,
    }


# ---------------------------------------------------------------- 画像プロンプトの記録

def _dynamic_provider(tmp_path, monkeypatch, prompt="A white router on a wooden shelf"):
    monkeypatch.chdir(tmp_path)  # output/.image_cache を tmp に作る
    config = SimpleNamespace(yaml=SimpleNamespace(video_renderer=SimpleNamespace(background_mode="static")))
    provider = ImageProvider(config, output_dir=tmp_path / "run")
    provider.mode = "dynamic"
    provider.prompt_generator = SimpleNamespace(generate_prompt=AsyncMock(return_value=prompt))

    async def generate_image(prompt, output_path, **kwargs):
        output_path.write_bytes(b"png")
        return output_path, GenerationMetadata.create_from_segment(
            image_path=str(output_path), segment_id=kwargs["segment_id"], segment_type=kwargs["segment_type"],
            prompt=prompt, visual_identity={}, seed=7, generation_time_sec=1.0,
            resolution="1024x576", steps=4, sampler="euler", cfg_scale=1.0,
        )

    provider.image_client = SimpleNamespace(generate_image=generate_image)
    return provider


def _scene(segment_id="deep_dive_1_scene1", visual=True) -> ScriptSegment:
    return ScriptSegment(
        segment_id=segment_id,
        segment_type="deep_dive",
        topic_title="Wi-Fi",
        turns=[{"speaker": "A", "text": "棚の上に置くのだ。"}],
        scene_visual=SceneVisual(
            start_turn=0, focus="置き場所", draw="棚の上の白いルーター 1 台", avoid=["床に直置き"],
        ) if visual else None,
    )


def test_prompt_records_include_scene_visual_and_cache_hits(tmp_path, monkeypatch):
    provider = _dynamic_provider(tmp_path, monkeypatch)
    asyncio.run(provider.get_image_for_segment(_scene()))
    # 同じプロンプト → キャッシュを使う。それでも記録は残る
    asyncio.run(provider.get_image_for_segment(_scene("deep_dive_1_scene2")))
    # visuals の無い場面
    provider.prompt_generator.generate_prompt = AsyncMock(return_value="A kettle on a stove")
    asyncio.run(provider.get_image_for_segment(_scene("deep_dive_2", visual=False)))

    run_dir = tmp_path / "run"
    records = [json.loads(line) for line in (run_dir / "generation_metadata.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["segment_id"] for r in records] == ["deep_dive_1_scene1", "deep_dive_1_scene2", "deep_dive_2"]
    assert [r["cached"] for r in records] == [False, True, False]
    assert records[0]["scene_visual"] == {
        "start_turn": 0, "focus": "置き場所", "draw": "棚の上の白いルーター 1 台", "avoid": ["床に直置き"],
    }
    assert records[1]["prompt"] == "A white router on a wooden shelf"
    assert records[2]["scene_visual"] is None

    report = (run_dir / "prompt_report.md").read_text(encoding="utf-8")
    assert "**Draw**: 棚の上の白いルーター 1 台" in report
    assert "**Avoid**: 床に直置き" in report
    assert "**Cached**: yes" in report
    assert "**Scene source**: scene_splitter" in report


def test_ffmpeg_renderer_writes_prompt_logs_to_given_dir(tmp_path, mock_app_config):
    """制作フェーズは prompt_log_dir に run のディレクトリを渡す (動画の videos/ ではなく)。"""
    from services.video_rendering import ffmpeg_renderer as fr

    seen = {}

    class _StopProvider:
        def __init__(self, config, visual_identity=None, output_dir=None):
            seen["output_dir"] = output_dir
            raise RuntimeError("stop")

    renderer = fr.FfmpegRenderer.__new__(fr.FfmpegRenderer)
    renderer.config = mock_app_config
    with patch.object(fr, "ImageProvider", _StopProvider):
        with pytest.raises(RuntimeError):
            asyncio.run(renderer.render(
                synthesis_result=MagicMock(subtitle_path=None, chapters=[]),
                background_image=tmp_path / "bg.png",
                bgm_file=tmp_path / "bgm.wav",
                output_path=tmp_path / "run" / "videos" / "radio.mp4",
                segments=[_scene()],
                prompt_log_dir=tmp_path / "run",
            ))
    assert seen["output_dir"] == tmp_path / "run"
