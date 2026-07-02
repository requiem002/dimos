# Copyright 2025-2026 Dimensional Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Non-hardware tests for RobotSpeakerTrack (requirements doc section 7.1).

These cover the static, robot-free guarantees of the persistent speaker track:
uniform wire-format frames, idle silence, clip swapping, monotonic
unit-consistent timestamps, wall-clock pacing, and — the central design
property (DR-S1) — that the track never raises MediaStreamError when a clip
ends, so the aiortc sender is never torn down.

The decode/timestamp tests are REGRESSIONS for the bug where 24 kHz TTS clips
were fed to the sender with pts stamped in 48 kHz units: the RTP timeline ran
at half real time, garbling the first clip and permanently silencing every
clip after it (the robot's jitter buffer discarded all "late" packets).

What these tests CANNOT cover (section 7.2, hardware-only): that the Go2's
speaker actually emits the audio, that repeated utterances play over one live
WebRTC session, and audio quality/clipping/timing on the wire. Those need the
physical robot.
"""

import os
import tempfile
import time

import numpy as np
import pytest
from aiortc.mediastreams import MediaStreamError
import soundfile as sf  # type: ignore[import-untyped]

from dimos.robot.unitree.robot_speaker_track import (
    CHANNELS,
    FRAME_DURATION,
    SAMPLE_RATE,
    SAMPLES_PER_FRAME,
    RobotSpeakerTrack,
    decode_clip_to_pcm,
)

_VALUES_PER_FRAME = SAMPLES_PER_FRAME * CHANNELS


def _clip_pcm(n_frames: int, fill: int = 1000) -> np.ndarray:
    """Wire-format PCM spanning exactly n_frames track frames."""
    return np.full(n_frames * _VALUES_PER_FRAME, fill, dtype=np.int16)


def _write_tts_style_wav(seconds: float, rate: int = 24000) -> str:
    """A mono WAV at the OpenAI TTS sample rate — the format that triggered the
    original half-rate RTP timeline bug."""
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="dimos_test_tts_")
    os.close(fd)
    t = np.linspace(0, seconds, int(seconds * rate), endpoint=False)
    sf.write(path, (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), rate)
    return path


def test_decode_resamples_24k_tts_wav_to_wire_format() -> None:
    # REGRESSION: clips must reach the track already at 48 kHz stereo so every
    # emitted frame's sample count matches its pts advance.
    path = _write_tts_style_wav(0.5)
    try:
        pcm = decode_clip_to_pcm(path)
    finally:
        os.remove(path)
    assert pcm.dtype == np.int16
    assert pcm.ndim == 1
    expected = int(0.5 * SAMPLE_RATE) * CHANNELS
    assert abs(pcm.size - expected) <= _VALUES_PER_FRAME  # resampler edge slack
    assert np.abs(pcm.astype(np.int32)).max() > 1000  # audio survived the trip


async def test_idle_recv_emits_silence_in_wire_format() -> None:
    track = RobotSpeakerTrack()
    frame = await track.recv()
    assert frame.format.name == "s16"
    assert len(frame.layout.channels) == CHANNELS
    assert frame.samples == SAMPLES_PER_FRAME
    assert frame.sample_rate == SAMPLE_RATE
    assert not frame.to_ndarray().any()


async def test_clip_frames_then_silence_never_eof() -> None:
    track = RobotSpeakerTrack()
    track.play_pcm(_clip_pcm(3))

    for _ in range(3):
        frame = await track.recv()
        assert frame.to_ndarray().any(), "clip frames should carry the clip audio"

    # After the clip is exhausted the track falls back to silence rather than
    # raising MediaStreamError — the sender must never be torn down.
    frame = await track.recv()
    assert not frame.to_ndarray().any()


async def test_partial_final_chunk_is_padded() -> None:
    track = RobotSpeakerTrack()
    track.play_pcm(np.full(_VALUES_PER_FRAME + 10, 1000, dtype=np.int16))
    first = await track.recv()
    assert first.samples == SAMPLES_PER_FRAME
    second = await track.recv()
    assert second.samples == SAMPLES_PER_FRAME  # 10 values + silence padding
    third = await track.recv()
    assert not third.to_ndarray().any()


async def test_play_replaces_previous_clip() -> None:
    track = RobotSpeakerTrack()
    track.play_pcm(_clip_pcm(5, fill=1000))
    await track.recv()
    track.play_pcm(_clip_pcm(2, fill=2000))
    frame = await track.recv()
    assert frame.to_ndarray().flatten()[0] == 2000


async def test_timestamps_uniform_and_monotonic_across_swap() -> None:
    # REGRESSION: pts must advance by exactly SAMPLES_PER_FRAME per frame in
    # 1/48000 time base, for silence and clips alike, regardless of the clip's
    # source sample rate — this is what keeps the RTP timeline at real time.
    track = RobotSpeakerTrack()

    pts: list[int] = []
    pts.append((await track.recv()).pts)  # idle silence
    track.play_pcm(_clip_pcm(2))
    pts.append((await track.recv()).pts)  # clip frame 1
    pts.append((await track.recv()).pts)  # clip frame 2
    pts.append((await track.recv()).pts)  # silence after clip end

    assert all(b - a == SAMPLES_PER_FRAME for a, b in zip(pts, pts[1:]))
    frame = await track.recv()
    assert frame.time_base.denominator == SAMPLE_RATE


async def test_recv_paces_against_wall_clock() -> None:
    track = RobotSpeakerTrack()
    n = 10
    start = time.monotonic()
    for _ in range(n):
        await track.recv()
    elapsed = time.monotonic() - start
    # Anchored pacing: n frames span ~(n-1)*20ms of sleep. Loose bounds to stay
    # CI-safe; the essential property is "roughly real time, not unpaced".
    assert elapsed >= (n - 2) * FRAME_DURATION
    assert elapsed <= n * FRAME_DURATION + 0.25


async def test_recv_reanchors_after_long_stall_instead_of_bursting() -> None:
    track = RobotSpeakerTrack()
    await track.recv()  # establish the anchor
    assert track._anchor is not None
    track._anchor -= 5.0  # simulate a 5s event-loop stall

    start = time.monotonic()
    await track.recv()  # detects the lag and re-anchors
    frames_after = 3
    for _ in range(frames_after):
        await track.recv()
    elapsed = time.monotonic() - start
    # Without re-anchoring these recvs would all return instantly (a packet
    # burst); with it they pace normally again.
    assert elapsed >= (frames_after - 1) * FRAME_DURATION


async def test_recently_active_gates_during_clip_and_tail() -> None:
    # Echo-gate support: the mic path drops frames while the speaker is (or
    # just was) playing, so the robot doesn't transcribe its own TTS.
    track = RobotSpeakerTrack()
    assert track.recently_active(0.5) is False  # fresh track: never played

    track.play_pcm(_clip_pcm(2))
    assert track.recently_active(0.5) is True  # clip queued

    await track.recv()
    await track.recv()
    await track.recv()  # clip exhausted -> silence
    assert track.recently_active(10.0) is True  # still inside the tail
    assert track.recently_active(0.0) is False  # zero tail: gate lifts at once


async def test_recv_raises_once_track_is_stopped() -> None:
    track = RobotSpeakerTrack()
    track.stop()  # MediaStreamTrack.stop() -> readyState "ended"
    with pytest.raises(MediaStreamError):
        await track.recv()
