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
frame format, idle silence, source swapping, monotonic timestamps, and — the
central design property (DR-S1) — that the track never raises MediaStreamError
when a clip ends, so the aiortc sender is never torn down.

What these tests CANNOT cover (section 7.2, hardware-only): that the Go2's
speaker actually emits the audio, that repeated utterances play over one live
WebRTC session, and audio quality/clipping/timing on the wire. Those need the
physical robot.
"""

import pytest
from aiortc.mediastreams import MediaStreamError
import av

from dimos.robot.unitree.robot_speaker_track import (
    CHANNELS,
    SAMPLE_RATE,
    SAMPLES_PER_FRAME,
    RobotSpeakerTrack,
)


class _FakeSourceTrack:
    """Stand-in for MediaPlayer.audio: yields ``n_frames`` non-silent frames,
    then raises MediaStreamError (EOF), and records that it was stopped."""

    def __init__(self, n_frames: int, fill: int = 0x7F) -> None:
        self._remaining = n_frames
        self._fill = fill
        self.stopped = False

    async def recv(self) -> av.AudioFrame:
        if self._remaining <= 0:
            raise MediaStreamError
        self._remaining -= 1
        frame = av.AudioFrame(format="s16", layout="stereo", samples=SAMPLES_PER_FRAME)
        for plane in frame.planes:
            plane.update(bytes([self._fill]) * plane.buffer_size)
        frame.sample_rate = SAMPLE_RATE
        frame.pts = 0  # MediaPlayer restarts pts at 0 on every clip
        return frame

    def stop(self) -> None:
        self.stopped = True


class _FakePlayer:
    def __init__(self, n_frames: int, fill: int = 0x7F) -> None:
        self.audio = _FakeSourceTrack(n_frames, fill=fill)


def _is_silent(frame: av.AudioFrame) -> bool:
    return not frame.to_ndarray().any()


def test_silence_frame_format() -> None:
    frame = RobotSpeakerTrack._silence_frame()
    assert frame.format.name == "s16"
    assert len(frame.layout.channels) == CHANNELS
    assert frame.samples == SAMPLES_PER_FRAME
    assert frame.sample_rate == SAMPLE_RATE
    assert _is_silent(frame)


async def test_idle_recv_emits_silence() -> None:
    track = RobotSpeakerTrack()
    frame = await track.recv()
    assert frame.format.name == "s16"
    assert frame.samples == SAMPLES_PER_FRAME
    assert frame.sample_rate == SAMPLE_RATE
    assert _is_silent(frame)


async def test_source_swap_yields_clip_frames_then_silence() -> None:
    track = RobotSpeakerTrack()
    track.play(_FakePlayer(n_frames=3))

    for _ in range(3):
        frame = await track.recv()
        assert not _is_silent(frame), "clip frames should carry the source audio"

    # After the clip's EOF, the track falls back to silence rather than ending.
    frame = await track.recv()
    assert _is_silent(frame)


async def test_recv_never_raises_on_source_eof_and_releases_clip() -> None:
    track = RobotSpeakerTrack()
    player = _FakePlayer(n_frames=1)
    track.play(player)

    await track.recv()  # consumes the one clip frame
    # Next recv hits the source's EOF internally; it must NOT propagate, so the
    # sender stays alive. The exhausted clip must be stopped/released.
    frame = await track.recv()
    assert _is_silent(frame)
    assert player.audio.stopped is True


async def test_play_stops_the_previous_player() -> None:
    track = RobotSpeakerTrack()
    first = _FakePlayer(n_frames=5)
    track.play(first)
    second = _FakePlayer(n_frames=5)
    track.play(second)
    assert first.audio.stopped is True
    assert second.audio.stopped is False


async def test_timestamps_are_monotonic_across_a_source_swap() -> None:
    track = RobotSpeakerTrack()

    pts: list[int] = []
    pts.append((await track.recv()).pts)  # idle silence
    track.play(_FakePlayer(n_frames=2))
    pts.append((await track.recv()).pts)  # clip frame 1
    pts.append((await track.recv()).pts)  # clip frame 2
    pts.append((await track.recv()).pts)  # silence after EOF

    # Despite each MediaPlayer restarting pts at 0, the track re-stamps from a
    # monotonic sample clock so RTP timestamps never regress.
    assert pts == sorted(pts)
    assert all(b - a == SAMPLES_PER_FRAME for a, b in zip(pts, pts[1:]))


async def test_recv_raises_once_track_is_stopped() -> None:
    track = RobotSpeakerTrack()
    track.stop()  # MediaStreamTrack.stop() -> readyState "ended"
    with pytest.raises(MediaStreamError):
        await track.recv()
