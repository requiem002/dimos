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

import asyncio
import fractions
import time

from aiortc import MediaStreamTrack
from aiortc.mediastreams import MediaStreamError
import av
import numpy as np

from dimos.utils.logging_config import setup_logger

logger = setup_logger()

SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLES_PER_FRAME = 960  # 20 ms at 48 kHz, matching aiortc's audio frame size
FRAME_DURATION = SAMPLES_PER_FRAME / SAMPLE_RATE
TIME_BASE = fractions.Fraction(1, SAMPLE_RATE)
# Interleaved int16 values per frame (samples x channels).
_VALUES_PER_FRAME = SAMPLES_PER_FRAME * CHANNELS
# If the event loop stalls long enough that we fall this far behind the pacing
# clock, re-anchor instead of bursting frames to catch up (a burst would flood
# the robot's jitter buffer; a re-anchor is one recoverable timeline step).
_MAX_LAG_SECONDS = 1.0

_SILENCE = np.zeros(_VALUES_PER_FRAME, dtype=np.int16)


def decode_clip_to_pcm(audio_path: str) -> np.ndarray:
    """Decode an audio file to the track's wire format: 48 kHz stereo s16,
    returned as 1-D interleaved int16.

    Resampling happens HERE, once, with the frame's native pts intact — not in
    aiortc's encoder. The encoder derives RTP timestamps from frame pts, so
    frames handed to the sender must already be in 48 kHz units or the RTP
    timeline runs at the wrong rate (see RobotSpeakerTrack docstring).
    """
    container = av.open(audio_path)
    resampler = av.AudioResampler(format="s16", layout="stereo", rate=SAMPLE_RATE)
    chunks: list[np.ndarray] = []
    try:
        for frame in container.decode(audio=0):
            # Let the resampler treat the stream as gapless rather than
            # compensating for container pts jitter.
            frame.pts = None
            chunks += [out.to_ndarray() for out in resampler.resample(frame)]
        chunks += [out.to_ndarray() for out in resampler.resample(None)]  # flush
    finally:
        container.close()
    if not chunks:
        return np.zeros(0, dtype=np.int16)
    # Packed s16 frames come out as (1, samples*channels) interleaved.
    return np.concatenate(chunks, axis=1).reshape(-1)


class RobotSpeakerTrack(MediaStreamTrack):
    """Persistent outbound audio track for the Go2's onboard speaker.

    A single instance is attached ONCE to the connection's pre-negotiated audio
    sender and lives for the whole WebRTC session. It emits digital silence when
    idle and the current clip's PCM while speaking, and it NEVER raises
    MediaStreamError. This is deliberate: aiortc tears the sender's RTP loop down
    permanently the first time a track signals end-of-file (rtcrtpsender.py
    `_run_rtp` exits on MediaStreamError), and the Go2 does not support SDP
    renegotiation, so a torn-down sender cannot be revived.

    Every frame is a uniform 20 ms of 48 kHz stereo s16 with pts advancing by
    exactly SAMPLES_PER_FRAME in 1/48000 time base. This uniformity is a hard
    correctness requirement, not a convenience: aiortc's Opus encoder derives
    RTP timestamps from frame pts. An earlier version fed the sender clips at
    their native rate (24 kHz TTS WAVs) while stamping pts in 48 kHz units, so
    the RTP timeline advanced at half real time during clips — the encoder's
    resampler dropped overlapping samples (garbled, quiet audio) and after one
    clip the timeline lagged seconds behind arrival, so the robot's jitter
    buffer discarded every later packet (speaker "worked once per boot").
    Clips are therefore decoded/resampled to the wire format up front
    (decode_clip_to_pcm) and the track only ever emits wire-format frames.

    Frames are paced against an absolute wall-clock anchor (anchor +
    samples_sent / 48000), not by sleeping a fixed 20 ms per frame: incremental
    sleeps overshoot and the drift compounds, which slews the RTP timeline away
    from real time the same way the unit bug did, just slower.
    """

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._pcm: np.ndarray | None = None  # current clip, 1-D interleaved s16
        self._offset = 0  # read position into _pcm, in int16 values
        self._timestamp = 0  # samples sent, monotonic across clips
        self._anchor: float | None = None  # wall-clock time of sample 0
        self._last_clip_time = 0.0  # monotonic time a clip frame last went out

    def play_pcm(self, pcm: np.ndarray) -> None:
        """Make ``pcm`` (from decode_clip_to_pcm) the active clip.

        Replaces any clip still playing, matching the speak path's semantics of
        one utterance at a time. Called on the connection's event-loop thread —
        the same thread as ``recv`` — so the swap needs no locking.
        """
        self._pcm = pcm if pcm.size else None
        self._offset = 0
        if self._pcm is not None:
            self._last_clip_time = time.monotonic()

    def recently_active(self, tail_seconds: float) -> bool:
        """True while a clip is playing or finished under ``tail_seconds`` ago.

        Used by the mic path to gate out the robot hearing its own speaker
        (self-echo): the tail covers network + jitter-buffer + playout latency
        between our last clip frame leaving and the speaker going quiet.
        """
        if self._pcm is not None:
            return True
        return time.monotonic() - self._last_clip_time < tail_seconds

    async def recv(self) -> av.AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError

        # Pace against the absolute anchor so per-sleep overshoot can't compound.
        if self._anchor is None:
            self._anchor = time.monotonic()
        target = self._anchor + self._timestamp / SAMPLE_RATE
        now = time.monotonic()
        if target > now:
            await asyncio.sleep(target - now)
        elif now - target > _MAX_LAG_SECONDS:
            logger.warning(
                "Speaker track fell %.2fs behind (event loop stall); re-anchoring",
                now - target,
            )
            self._anchor = now - self._timestamp / SAMPLE_RATE

        if self._pcm is not None:
            self._last_clip_time = now
            chunk = self._pcm[self._offset : self._offset + _VALUES_PER_FRAME]
            self._offset += _VALUES_PER_FRAME
            if self._offset >= self._pcm.size:
                self._pcm = None
                self._offset = 0
            if chunk.size < _VALUES_PER_FRAME:
                chunk = np.concatenate(
                    [chunk, np.zeros(_VALUES_PER_FRAME - chunk.size, dtype=np.int16)]
                )
            data = chunk
        else:
            data = _SILENCE

        frame = av.AudioFrame(format="s16", layout="stereo", samples=SAMPLES_PER_FRAME)
        frame.planes[0].update(data.tobytes())
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._timestamp
        frame.time_base = TIME_BASE
        self._timestamp += SAMPLES_PER_FRAME
        return frame

    @staticmethod
    def _silence_frame() -> av.AudioFrame:
        frame = av.AudioFrame(format="s16", layout="stereo", samples=SAMPLES_PER_FRAME)
        frame.planes[0].update(_SILENCE.tobytes())
        frame.sample_rate = SAMPLE_RATE
        return frame
