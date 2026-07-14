"""Speaker attribution for batch transcription.

Regression cover for the meeting-66 defect: attribution used to vote per
Deepgram speaker_index and take an independent argmax per index. The meeting's
dominant speaker (lit ~78% of the time) therefore won nearly every index, so
segments actually spoken by other participants were relabelled with his name and
two participants vanished from the transcript entirely.

Attribution is now per segment against the active-speaker timeline, with the
index only as a fallback for segments the timeline doesn't cover.
"""

from meeting_api.batch_transcribe import _attribute_speakers


def _samples(*pairs):
    """(t_ms, [names lit]) -> timeline samples."""
    return [{"t_ms": t, "speaking": names} for t, names in pairs]


def test_quiet_speaker_is_not_absorbed_by_the_dominant_one():
    """The meeting-66 shape: one speaker dominates the timeline, and Deepgram
    splits him across several indices. A quieter speaker sharing one of those
    indices must still keep his own segments."""
    timeline = _samples(
        (0, ["Loud"]), (500, ["Loud"]), (1000, ["Loud"]), (1500, ["Loud"]),
        (2000, ["Loud"]), (2500, ["Loud"]), (3000, ["Loud"]), (3500, ["Loud"]),
        # Quiet speaks briefly, while sharing Deepgram index 0 with Loud.
        (4000, ["Quiet"]), (4500, ["Quiet"]),
        (5000, ["Loud"]), (5500, ["Loud"]),
    )
    segments = [
        {"start": 0.0, "end": 3.5, "speaker_index": 0},   # Loud
        {"start": 4.0, "end": 4.5, "speaker_index": 0},   # Quiet — same index!
        {"start": 5.0, "end": 5.5, "speaker_index": 0},   # Loud
    ]

    names, _ = _attribute_speakers(segments, timeline)

    # Pre-fix this returned ["Loud", "Loud", "Loud"] — the whole index went to
    # the dominant speaker and Quiet disappeared.
    assert names == ["Loud", "Quiet", "Loud"]


def test_every_distinct_speaker_survives():
    """No participant may be dropped just because they spoke little."""
    timeline = _samples(
        (0, ["A"]), (500, ["A"]), (1000, ["A"]), (1500, ["A"]),
        (2000, ["B"]),
        (3000, ["C"]),
        (4000, ["D"]),
    )
    segments = [
        {"start": 0.0, "end": 1.5, "speaker_index": 0},
        {"start": 2.0, "end": 2.4, "speaker_index": 0},
        {"start": 3.0, "end": 3.4, "speaker_index": 1},
        {"start": 4.0, "end": 4.4, "speaker_index": 1},
    ]

    names, _ = _attribute_speakers(segments, timeline)

    assert set(n for n in names if n) == {"A", "B", "C", "D"}


def test_overlapping_tiles_split_the_vote_so_the_clear_speaker_wins():
    """Two tiles lit at once each get half a vote; the one lit alone elsewhere
    in the window still wins."""
    timeline = _samples(
        (0, ["A", "B"]),   # ambiguous: 0.5 each
        (500, ["A"]),      # A alone: +1
    )
    segments = [{"start": 0.0, "end": 0.5, "speaker_index": 0}]

    names, _ = _attribute_speakers(segments, timeline)

    assert names == ["A"]


def test_segment_between_samples_is_recovered_by_widening():
    """Samples land every ~500ms, so a short utterance can fall between two of
    them. It must still be attributed rather than left blank."""
    timeline = _samples((0, ["A"]), (2000, ["B"]))
    # Segment sits in the gap, nearest sample is B.
    segments = [{"start": 1.9, "end": 1.95, "speaker_index": 0}]

    names, _ = _attribute_speakers(segments, timeline)

    assert names == ["B"]


def test_uncovered_segment_falls_back_to_its_index():
    """A segment with no timeline coverage at all inherits the name its index
    won from the segments that WERE covered."""
    timeline = _samples((0, ["A"]), (500, ["A"]))
    segments = [
        {"start": 0.0, "end": 0.5, "speaker_index": 7},      # covered -> A
        {"start": 900.0, "end": 901.0, "speaker_index": 7},  # far past timeline
    ]

    names, index_map = _attribute_speakers(segments, timeline)

    assert index_map[7] == "A"
    assert names == ["A", "A"]


def test_no_timeline_yields_no_names():
    """Without a timeline there is nothing to attribute; the caller falls back
    to generic 'Speaker N' labels."""
    segments = [{"start": 0.0, "end": 1.0, "speaker_index": 0}]

    names, index_map = _attribute_speakers(segments, [])

    assert names == [None]
    assert index_map == {}
