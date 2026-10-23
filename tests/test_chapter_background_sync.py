"""背景の切り替わりと章タイトルの切り替わりが同じ時刻になることのテスト

2026-10-23 (radio_director-013 のコマ評価): 2m00s で背景はもう次の章なのに章タイトルが前の章の
ままだった。章タイトルは次のセグメントの最初の発話 (ジングルの後) で切り替わるが、背景は
セグメントごとの長さ (最初の発話〜ジングルの終わり) を足し並べていたため、冒頭 2 秒の無音と
各セグメント最後の発話の後の間 (250ms) の分だけ、境界ごとに前へずれていた。

VOICEVOX と同じ組み立て (_combine_audio_with_pauses → _build_chapters /
calculate_segment_timings → TimelineCalculator) で時刻を作り、両者を比べる。
"""
from itertools import accumulate
from pathlib import Path
from unittest.mock import MagicMock

import anyio
import pytest
from pydub import AudioSegment

from core.interfaces import SynthesisResult
from core.models.curation import ScriptSegment
from core.models.script import DialogueTurn, Script
from services.audio_synthesis.voicevox_client import VoicevoxClient
from services.audio_synthesis.voicevox_segment_timing import calculate_segment_timings
from services.video_rendering.timeline_calculator import TimelineCalculator

PAUSE_MS = 250  # config の pause_between_phrases_ms と同じ
PRE_JINGLE_PAUSE_SEC = 0.5
JINGLE_SEC = 3.0
PRE_ROLL_SEC = 2.0
POST_ROLL_SEC = 5.0

# (segment_id, segment_type, 題, 各ターンの長さ ms)
SEGMENTS = [
    ("intro", "intro", "はじめに", [4000, 3500, 5200]),
    ("deep_dive_1", "deep_dive", "HDDはなぜ壊れるのか", [6100, 4300, 5000, 2700]),
    ("deep_dive_2", "deep_dive", "年間故障率1.5%の意味", [3900, 4800, 5600]),
    ("deep_dive_3", "deep_dive", "家のHDDに当てはめる：1台の確率と前ぶれと備え", [5100, 3300, 4400]),
    ("conclusion", "conclusion", "まとめ", [3000, 2500]),
]


def _build(segment_specs, jingles: bool = True):
    segments = []
    turns = []
    for seg_id, seg_type, title, durations in segment_specs:
        seg_turns = [
            {"speaker": "AB"[n % 2], "text": f"{seg_id} {n}", "section": seg_type if n == 0 else None}
            for n in range(len(durations))
        ]
        segments.append(ScriptSegment(
            segment_id=seg_id,
            segment_type=seg_type,  # type: ignore[arg-type]
            topic_title=title,
            turns=seg_turns,
            context_summary="",
        ))
        for n, t in enumerate(seg_turns):
            turns.append(DialogueTurn(
                speaker=t["speaker"],
                text=t["text"],
                section=seg_type if n == 0 else None,
                chapter_title=title if n == 0 else None,
            ))
    script = Script(title="t", thumbnail_title="t", sections=turns)

    # VoicevoxClient.synthesize と同じ: 発話のあとに PAUSE_MS を空けて次の発話
    phrase_data = []
    current = 0
    for (_, _, _, durations) in segment_specs:
        for d in durations:
            phrase_data.append((AudioSegment.silent(duration=d), current, current + d, "x", "A"))
            current += d + PAUSE_MS

    segment_pauses = [
        (PRE_JINGLE_PAUSE_SEC + JINGLE_SEC, Path("jingle.wav"), JINGLE_SEC)
        if jingles and i < len(segments) - 1 and segments[i].turns else (0.0, None, None)
        for i in range(len(segments))
    ]
    client = VoicevoxClient.__new__(VoicevoxClient)
    combined, adjusted = client._combine_audio_with_pauses(phrase_data, PAUSE_MS, segment_pauses, segments)
    chapters = client._build_chapters(adjusted, script, segments=segments)
    timings = calculate_segment_timings(script, segments, adjusted, segment_pauses)
    total_sec = PRE_ROLL_SEC + len(combined) / 1000.0 + POST_ROLL_SEC
    return segments, chapters, timings, total_sec


def _timeline(mock_app_config, segments, timings, total_sec):
    synthesis_result = SynthesisResult(
        audio_path=Path("audio.wav"),
        subtitle_path=Path("subs.ass"),
        total_duration_sec=total_sec,
        segment_timings=timings,
    )
    image_provider = MagicMock()

    async def get_image(seg):
        return Path(f"bg_{seg.segment_id}.png")

    image_provider.get_image_for_segment = get_image
    calc = TimelineCalculator(mock_app_config)

    async def run():
        return await calc.calculate_timeline(
            segments=segments,
            synthesis_result=synthesis_result,
            image_provider=image_provider,
            jingle_provider=MagicMock(),
            bgm_path=Path("bgm.wav"),
        )

    return anyio.run(run)


def _background_cuts(timeline):
    # 映像トラックは各セグメントの画像を video_duration_sec ずつ 0 秒から並べる
    return list(accumulate(seg.video_duration_sec for seg in timeline.segments))


@pytest.mark.parametrize("jingles", [True, False])
def test_background_switches_when_chapter_title_switches(mock_app_config, jingles):
    segments, chapters, timings, total_sec = _build(SEGMENTS, jingles)
    timeline = _timeline(mock_app_config, segments, timings, total_sec)
    cuts = _background_cuts(timeline)
    assert len(chapters) == len(segments)
    # 背景の境界 = 次の章の章タイトルが出る時刻
    for cut, next_chapter in zip(cuts, chapters[1:]):
        assert cut == pytest.approx(next_chapter.start_time_sec, abs=1e-6)
    # 最後の背景は音声の終わりまで
    assert cuts[-1] == pytest.approx(total_sec, abs=1e-6)
    assert [seg.video_cut_time_sec for seg in timeline.segments] == pytest.approx(cuts)


def test_jingle_plays_over_the_previous_background(mock_app_config):
    # ジングルは前の章の背景のうちに鳴り、鳴り終わってから背景と章タイトルが切り替わる
    segments, chapters, timings, total_sec = _build(SEGMENTS)
    timeline = _timeline(mock_app_config, segments, timings, total_sec)
    for seg in timeline.segments[:-1]:
        assert seg.jingle_start_sec + seg.jingle_duration_sec <= seg.video_cut_time_sec + 1e-6


def test_first_background_covers_pre_roll(mock_app_config):
    segments, chapters, timings, total_sec = _build(SEGMENTS)
    timeline = _timeline(mock_app_config, segments, timings, total_sec)
    first = timeline.segments[0]
    assert first.video_duration_sec == pytest.approx(chapters[1].start_time_sec)
    assert chapters[0].start_time_sec == pytest.approx(PRE_ROLL_SEC)


def test_empty_segment_gets_no_screen_time(mock_app_config):
    specs = SEGMENTS[:2] + [("deep_dive_x", "deep_dive", "空", [])] + SEGMENTS[2:]
    segments, chapters, timings, total_sec = _build(specs)
    timeline = _timeline(mock_app_config, segments, timings, total_sec)
    assert timeline.segments[2].video_duration_sec == 0.0
    cuts = [c for c, seg in zip(_background_cuts(timeline), timeline.segments) if seg.video_duration_sec > 0]
    for cut, next_chapter in zip(cuts, chapters[1:]):
        assert cut == pytest.approx(next_chapter.start_time_sec, abs=1e-6)
    assert cuts[-1] == pytest.approx(total_sec, abs=1e-6)
