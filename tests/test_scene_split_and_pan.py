"""章を場面に分けて背景を増やし、ゆっくりパンさせることのテスト (009)

- 場面の枚数: 章の長さ ÷ 約 90 秒 (1 章 1〜4 枚)
- 区切り: 等分した位置に一番近いターンの頭。±2 ターン以内に新しい論点の出典 [src=N] の
  初出があればそこへ寄せる
- タイムライン: 場面は章の映像の範囲を隙間なく埋める。章タイトル・チャプターは章単位のまま
- 各場面の画像プロンプトは、その場面のターンだけから作る
- パン: scale で少し大きくしてから crop の x を時刻 t の式で動かす (ズームなし)
"""
from itertools import accumulate
from pathlib import Path
from unittest.mock import MagicMock

import anyio
import pytest

from core.interfaces import ChapterMarker, SegmentTiming, SynthesisResult
from core.models import SceneTimelineEntry, SegmentTimelineEntry, VideoTimeline
from core.models.curation import ScriptSegment
from services.video_rendering.scene_splitter import (
    key_claim_first_turns,
    scene_count,
    split_scene_starts,
)
from services.video_rendering.timeline_calculator import TimelineCalculator
from services.video_rendering.video_track_renderer import VideoTrackRenderer, build_pan_filter
from tests.test_chapter_background_sync import _build


# ---------------------------------------------------------------- 枚数

@pytest.mark.parametrize("duration, expected", [
    (0.0, 1),
    (40.0, 1),
    (120.0, 1),    # 1.33 → 1
    (135.0, 2),    # 1.5 → 2 (四捨五入)
    (150.0, 2),
    (240.0, 3),    # 4 分の章
    (300.0, 3),    # 5 分の章
    (330.0, 4),
    (900.0, 4),    # 上限 4
])
def test_scene_count_by_chapter_length(duration, expected):
    assert scene_count(duration, 90.0, 4) == expected


def test_scene_count_respects_max_and_target():
    assert scene_count(900.0, 90.0, 1) == 1
    assert scene_count(300.0, 60.0, 10) == 5


# ---------------------------------------------------------------- ターン境界への寄せ

def _even_turns(num_turns: int, every_sec: float = 10.0, start: float = 2.0):
    starts = [start + every_sec * k for k in range(num_turns)]
    return starts, starts[-1] + every_sec


def test_short_chapter_is_one_scene():
    starts, end = _even_turns(8)  # 80 秒
    assert split_scene_starts(starts, end) == [0]


def test_single_turn_chapter_is_one_scene():
    assert split_scene_starts([2.0], 400.0) == [0]


def test_cuts_land_on_turn_nearest_to_even_positions():
    starts, end = _even_turns(30)  # 300 秒 → 3 場面、100 秒・200 秒の位置
    assert split_scene_starts(starts, end) == [0, 10, 20]


def test_cut_uses_nearest_turn_start_with_uneven_turns():
    # 270 秒 → 3 場面。等分位置は 92 秒・182 秒 (開始 2 秒)
    starts = [2.0, 30.0, 60.0, 85.0, 97.0, 140.0, 175.0, 190.0, 230.0, 260.0]
    cuts = split_scene_starts(starts, 272.0)
    assert cuts == [0, 4, 6]  # 97 (|5|) は 85 (|7|) より近い / 175 (|7|) は 190 (|8|) より近い
    assert all(starts[c] != starts[0] for c in cuts[1:])


def _turns(num_turns: int, cites: dict[int, str]) -> list[dict]:
    return [
        {"speaker": "AB"[k % 2], "text": f"発話 {k}" + (f" {cites[k]}" if k in cites else "")}
        for k in range(num_turns)
    ]


def test_cut_snaps_to_new_key_claim_within_two_turns():
    starts, end = _even_turns(30)
    turns = _turns(30, {3: "[src=1]", 12: "[src=2]", 18: "[src=3]"})
    # 10 → 12 (新しい論点 src=2)、20 → 18 (src=3)
    assert split_scene_starts(starts, end, turns, [1, 2, 3]) == [0, 12, 18]


def test_no_snap_beyond_two_turns():
    starts, end = _even_turns(30)
    turns = _turns(30, {13: "[src=2]", 23: "[src=3]"})
    assert split_scene_starts(starts, end, turns, [2, 3]) == [0, 10, 20]


def test_no_snap_to_sources_outside_key_claims_or_repeated_citations():
    starts, end = _even_turns(30)
    # src=7 は論点の出典ではない / src=2 の 2 回目 (11) は初出ではない
    turns = _turns(30, {2: "[src=2]", 11: "[src=2]", 19: "[src=7]"})
    assert split_scene_starts(starts, end, turns, [2]) == [0, 10, 20]


def test_snap_handles_multi_source_tags():
    starts, end = _even_turns(30)
    turns = _turns(30, {1: "[src=9]", 9: "[src=9, 3]"})
    assert key_claim_first_turns(turns, [3, 9]) == {1, 9}
    assert split_scene_starts(starts, end, turns, [3, 9]) == [0, 9, 20]


def test_snap_picks_the_new_claim_nearest_in_time():
    starts, end = _even_turns(30)
    turns = _turns(30, {8: "[src=1]", 11: "[src=2]"})
    assert split_scene_starts(starts, end, turns, [1, 2]) == [0, 11, 20]


# ---------------------------------------------------------------- タイムライン

LONG_SPECS = [
    ("intro", "intro", "はじめに", [5000] * 6),                    # 約 31 秒 → 1 場面
    ("deep_dive_1", "deep_dive", "論点1", [9000] * 30),             # 約 277 秒 → 3 場面
    ("deep_dive_2", "deep_dive", "論点2", [8000] * 20),             # 約 165 秒 → 2 場面
    ("conclusion", "conclusion", "まとめ", [6000] * 8),            # 約 50 秒 → 1 場面
]


def _scene_timeline(mock_app_config, specs, jingles=True):
    segments, chapters, timings, total_sec = _build(specs, jingles)
    asked: list[ScriptSegment] = []

    async def get_image(seg):
        asked.append(seg)
        return Path(f"bg_{seg.segment_id}.png")

    image_provider = MagicMock()
    image_provider.get_image_for_segment = get_image
    calc = TimelineCalculator(mock_app_config)

    async def run():
        return await calc.calculate_timeline(
            segments=segments,
            synthesis_result=SynthesisResult(
                audio_path=Path("audio.wav"),
                subtitle_path=Path("subs.ass"),
                total_duration_sec=total_sec,
                segment_timings=timings,
            ),
            image_provider=image_provider,
            jingle_provider=MagicMock(),
            bgm_path=Path("bgm.wav"),
        )

    return segments, chapters, timings, total_sec, anyio.run(run), asked


def test_segment_timing_has_turn_starts(mock_app_config):
    segments, chapters, timings, *_ = _build(LONG_SPECS)
    for seg, timing in zip(segments, timings):
        assert len(timing.turn_start_secs) == len(seg.turns)
        assert timing.turn_start_secs[0] == pytest.approx(timing.start_sec)
        assert timing.turn_start_secs == sorted(timing.turn_start_secs)


def test_scene_counts_per_chapter(mock_app_config):
    *_, timeline, _asked = _scene_timeline(mock_app_config, LONG_SPECS)
    assert [len(seg.scenes) for seg in timeline.segments] == [1, 3, 2, 1]
    assert len(timeline.video_scenes()) == 7


@pytest.mark.parametrize("jingles", [True, False])
def test_scenes_fill_each_chapter_and_chapter_titles_stay(mock_app_config, jingles):
    segments, chapters, timings, total_sec, timeline, _ = _scene_timeline(mock_app_config, LONG_SPECS, jingles)
    # 章の境界 (= 章タイトル・チャプターの時刻) は今までどおり
    assert len(chapters) == len(segments)
    chapter_cuts = [seg.video_cut_time_sec for seg in timeline.segments]
    for cut, next_chapter in zip(chapter_cuts, chapters[1:]):
        assert cut == pytest.approx(next_chapter.start_time_sec, abs=1e-6)
    assert chapter_cuts[-1] == pytest.approx(total_sec, abs=1e-6)

    for entry in timeline.segments:
        video_start = entry.video_cut_time_sec - entry.video_duration_sec
        assert entry.scenes[0].video_start_sec == pytest.approx(video_start)
        assert sum(sc.video_duration_sec for sc in entry.scenes) == pytest.approx(entry.video_duration_sec)
        for a, b in zip(entry.scenes, entry.scenes[1:]):
            assert a.video_start_sec + a.video_duration_sec == pytest.approx(b.video_start_sec)
            assert a.end_turn == b.start_turn

    # 映像トラックが場面を 0 秒から並べた境界のうち、章の境界は章タイトルの時刻と一致する
    scene_cuts = list(accumulate(d for _img, d in timeline.video_scenes()))
    for next_chapter in chapters[1:]:
        assert any(c == pytest.approx(next_chapter.start_time_sec, abs=1e-6) for c in scene_cuts)
    assert scene_cuts[-1] == pytest.approx(total_sec, abs=1e-6)


def test_scene_cuts_inside_a_chapter_are_turn_starts(mock_app_config):
    segments, chapters, timings, total_sec, timeline, _ = _scene_timeline(mock_app_config, LONG_SPECS)
    chapter_times = [c.start_time_sec for c in chapters]
    for entry, timing in zip(timeline.segments, timings):
        for scene in entry.scenes[1:]:
            assert scene.video_start_sec == pytest.approx(timing.turn_start_secs[scene.start_turn])
            # 章の途中の切り替えでは章タイトルは出ない
            assert all(abs(scene.video_start_sec - t) > 1.0 for t in chapter_times)


def test_jingle_still_ends_on_the_chapters_last_scene(mock_app_config):
    *_, timeline, _ = _scene_timeline(mock_app_config, LONG_SPECS)
    for entry in timeline.segments[:-1]:
        last = entry.scenes[-1]
        assert last.video_start_sec <= entry.jingle_start_sec
        assert entry.jingle_start_sec + entry.jingle_duration_sec <= last.video_start_sec + last.video_duration_sec + 1e-6


def test_each_scene_prompt_comes_from_its_own_turns(mock_app_config):
    segments, *_, timeline, asked = _scene_timeline(mock_app_config, LONG_SPECS)
    by_id = {seg.segment_id: seg for seg in asked}
    dd1 = timeline.segments[1]
    for scene in dd1.scenes:
        scene_seg = by_id[f"deep_dive_1_scene{scene.scene_index + 1}"]
        assert scene_seg.turns == segments[1].turns[scene.start_turn:scene.end_turn]
        assert scene_seg.topic_title == segments[1].topic_title
        assert scene_seg.segment_type == "deep_dive"
        assert scene.background_image_path == Path(f"bg_deep_dive_1_scene{scene.scene_index + 1}.png")
    # 1 場面の章は今までどおり章そのものを渡す
    assert "intro" in by_id and "conclusion" in by_id
    assert len(asked) == 7


def test_max_scenes_one_restores_one_image_per_chapter(mock_app_config):
    mock_app_config.yaml.video_renderer.max_scenes_per_chapter = 1
    *_, timeline, asked = _scene_timeline(mock_app_config, LONG_SPECS)
    assert [len(seg.scenes) for seg in timeline.segments] == [1, 1, 1, 1]
    assert [seg.segment_id for seg in asked] == ["intro", "deep_dive_1", "deep_dive_2", "conclusion"]


def test_empty_chapter_has_no_scenes(mock_app_config):
    specs = LONG_SPECS[:2] + [("deep_dive_x", "deep_dive", "空", [])] + LONG_SPECS[2:]
    *_, timeline, _ = _scene_timeline(mock_app_config, specs)
    assert timeline.segments[2].scenes == []
    assert timeline.segments[2].video_duration_sec == 0.0


def test_scene_key_claims_snap_through_timeline(mock_app_config):
    segments, chapters, timings, total_sec = _build(LONG_SPECS)
    dd1 = segments[1]
    turns = [dict(t) for t in dd1.turns]
    turns[12]["text"] += " [src=4]"
    segments[1] = dd1.model_copy(update={"turns": turns, "key_claim_source_idxs": [4]})
    calc = TimelineCalculator(mock_app_config)
    assert calc._plan_scene_starts(segments[1], timings[1]) == [0, 12, 20]


# ---------------------------------------------------------------- パンのフィルタ

def test_pan_filter_string_left_to_right():
    assert build_pan_filter(1920, 1080, 90.0, 0.05, 0) == (
        "scale=2016:1134:force_original_aspect_ratio=increase,"
        "crop=2016:1134,"
        "crop=1920:1080:x=96*t/90.000:y=27,"
        "setsar=1"
    )


def test_pan_filter_alternates_direction_and_moves_five_percent():
    odd = build_pan_filter(1920, 1080, 75.5, 0.05, 1)
    assert "crop=1920:1080:x=96-96*t/75.500:y=27" in odd
    # 動く量 = 画面幅の 5% (96 / 1920)
    assert 96 / 1920 == pytest.approx(0.05)


def test_pan_filter_has_no_zoom_or_zoompan():
    chain = build_pan_filter(1920, 1080, 60.0, 0.05, 0)
    assert "zoompan" not in chain
    # 大きさは場面の間ずっと同じ (scale は定数、t を含むのは crop の x だけ)
    assert chain.count("t/") == 1
    # filter_complex の区切りになる ',' ';' は式の中に無い
    x_expr = chain.split("x=")[1].split(":")[0]
    assert "," not in x_expr and ";" not in x_expr


def test_pan_filter_zero_length_scene_is_static_centre():
    assert "crop=1920:1080:x=48:y=27" in build_pan_filter(1920, 1080, 0.0, 0.05, 0)


def test_pan_filter_even_sizes_for_other_resolutions():
    chain = build_pan_filter(1280, 720, 30.0, 0.05, 0)
    assert chain.startswith("scale=1344:756:")
    assert "x=64*t/30.000:y=18" in chain


# ---------------------------------------------------------------- 映像トラックのコマンド

def _timeline_for_render() -> VideoTimeline:
    def scene(k, start, dur):
        return SceneTimelineEntry(k, k * 10, k * 10 + 10, start, dur, Path(f"s{k}.png"))

    seg = SegmentTimelineEntry(
        segment_id="deep_dive_1", segment_type="deep_dive", topic_title="t",
        audio_start_sec=2.0, audio_end_sec=180.0, duration_sec=178.0,
        video_duration_sec=180.0, video_cut_time_sec=180.0,
        background_image_path=Path("s0.png"),
        scenes=[scene(0, 0.0, 90.0), scene(1, 90.0, 90.0)],
    )
    legacy = SegmentTimelineEntry(
        segment_id="conclusion", segment_type="conclusion", topic_title="まとめ",
        audio_start_sec=180.0, audio_end_sec=200.0, duration_sec=20.0,
        video_duration_sec=20.0, video_cut_time_sec=200.0,
        background_image_path=Path("c.png"),
    )
    return VideoTimeline(
        segments=[seg, legacy], total_duration_sec=200.0,
        main_audio_path=Path("a.wav"), bgm_path=Path("b.wav"),
        bgm_volume=0.1, fade_in_sec=1.0, fade_out_sec=1.0,
    )


def _command(mock_app_config, **settings):
    for key, value in settings.items():
        setattr(mock_app_config.yaml.video_renderer, key, value)
    renderer = VideoTrackRenderer(mock_app_config)
    chapters = [ChapterMarker(2.0, "t", "deep_dive_1"), ChapterMarker(182.0, "まとめ", "conclusion")]
    return renderer._build_ffmpeg_command(_timeline_for_render(), Path("out.mp4"), chapters)


def test_render_inputs_are_scenes(mock_app_config):
    cmd = _command(mock_app_config)
    inputs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]
    durations = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-t"][:-1]  # 最後は出力の長さ
    assert inputs == ["s0.png", "s1.png", "c.png"]
    assert [float(d) for d in durations] == [90.0, 90.0, 20.0]
    assert cmd.count("-framerate") == 3


def test_render_filter_pans_each_scene(mock_app_config):
    # 010 で既定が perspective になったので、009 の crop 方式を明示する
    cmd = _command(mock_app_config, pan_method="crop")
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "[0:v]scale=2016:1134" in fc and "x=96*t/90.000" in fc
    assert "[1:v]scale=2016:1134" in fc and "x=96-96*t/90.000" in fc
    assert "[2:v]scale=2016:1134" in fc and "x=96*t/20.000" in fc
    assert "[v0][v1][v2]concat=n=3:v=1:a=0[vbase]" in fc


def test_render_without_pan_keeps_old_scale_pad(mock_app_config):
    cmd = _command(mock_app_config, enable_pan=False)
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "crop=" not in fc.split("[vbase]")[0]
    assert "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2[v0]" in fc


def test_video_track_encoder_defaults_to_libx264(mock_app_config):
    cmd = _command(mock_app_config)
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-preset") + 1] == "ultrafast"


def test_video_track_encoder_switches_to_nvenc(mock_app_config):
    cmd = _command(mock_app_config, video_track_use_gpu=True)
    assert cmd[cmd.index("-c:v") + 1] == "h264_nvenc"
    assert "libx264" not in cmd
