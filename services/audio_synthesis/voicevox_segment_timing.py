"""Segment timing calculation utilities for VoicevoxClient

Calculates segment-level timing information from phrase data and script segments.
"""
from typing import Optional

from core.interfaces import SegmentTiming
from core.models import Script


def calculate_segment_timings(
    script: Script,
    segments: Optional[list],
    phrase_data: list,
    segment_pauses: Optional[list] = None
) -> list[SegmentTiming]:
    """Calculate segment-level timing information
    
    Maps phrase-level timing data to segment boundaries based on
    the number of turns in each segment.
    
    Invariant: len(returned_list) == len(segments). Empty/invalid segments
    get a dummy entry to preserve index-based correspondence.
    
    Args:
        script: Script object
        segments: List of ScriptSegment objects
        phrase_data: List of (audio_segment, start_ms, end_ms, text, speaker) tuples
        segment_pauses: List of (pause_sec, jingle_path, jingle_duration) indexed by segment position
    
    Returns:
        List of SegmentTiming objects with jingle information, one per input segment
    """
    if not segments or not phrase_data:
        return []
    
    segment_timings: list[SegmentTiming] = []
    phrase_index = 0
    pre_roll_offset_ms = 2000  # Pre-roll silence added to audio
    post_roll_offset_ms = 5000  # Post-roll silence added to audio (末尾5秒)
    last_end_sec = pre_roll_offset_ms / 1000.0  # Fallback for empty segments
    
    for i, segment in enumerate(segments):
        num_turns = len(segment.turns)
        is_last_segment = (i == len(segments) - 1)
        
        # Extract jingle information from segment_pauses by index (not by segment_id)
        # This correctly handles duplicate segment_ids (e.g., multiple "deep_dive")
        jingle_path = None
        jingle_duration = None
        if segment_pauses and i < len(segment_pauses):
            _pause_sec, jingle_path, jingle_duration = segment_pauses[i]
        
        # Handle empty segment or phrase exhaustion:
        # Insert dummy timing to preserve invariant len(segment_timings) == len(segments)
        if num_turns == 0 or phrase_index >= len(phrase_data):
            # For the last segment, still account for post-roll silence that is
            # always appended to combined_audio. Otherwise the reported end_sec
            # falls short of the actual audio duration by post_roll_offset_ms.
            dummy_start_sec = last_end_sec
            dummy_end_sec = last_end_sec
            if is_last_segment:
                dummy_end_sec += post_roll_offset_ms / 1000.0
                last_end_sec = dummy_end_sec
            
            segment_timings.append(SegmentTiming(
                segment_id=segment.segment_id,
                segment_type=segment.segment_type,
                topic_title=segment.topic_title,
                start_sec=dummy_start_sec,
                end_sec=dummy_end_sec,
                duration_sec=dummy_end_sec - dummy_start_sec,
                jingle_path=jingle_path,
                jingle_duration=jingle_duration,
            ))
            continue
        
        # Calculate start time from first phrase in segment
        start_ms = phrase_data[phrase_index][1]
        start_sec = (start_ms + pre_roll_offset_ms) / 1000.0
        
        # Calculate end time from last phrase in segment
        end_phrase_index = min(phrase_index + num_turns - 1, len(phrase_data) - 1)
        end_ms = phrase_data[end_phrase_index][2]
        end_sec = (end_ms + pre_roll_offset_ms) / 1000.0
        
        # Add post-roll to the last segment to match total audio duration
        if is_last_segment:
            end_sec += post_roll_offset_ms / 1000.0
        
        duration_sec = end_sec - start_sec
        last_end_sec = end_sec

        # Start of every turn in the segment (1 turn = 1 phrase), for scene cuts
        turn_start_secs = [
            (phrase_data[k][1] + pre_roll_offset_ms) / 1000.0
            for k in range(phrase_index, end_phrase_index + 1)
        ]

        segment_timings.append(SegmentTiming(
            segment_id=segment.segment_id,
            segment_type=segment.segment_type,
            topic_title=segment.topic_title,
            start_sec=start_sec,
            end_sec=end_sec,
            duration_sec=duration_sec,
            jingle_path=jingle_path,
            jingle_duration=jingle_duration,
            turn_start_secs=turn_start_secs,
        ))

        phrase_index += num_turns
    
    return segment_timings
