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

from aiortc import MediaStreamTrack
from aiortc.contrib.media import MediaPlayer
from aiortc.mediastreams import MediaStreamError
import av

from dimos.utils.logging_config import setup_logger

logger = setup_logger()

SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLES_PER_FRAME = 960  # 20 ms at 48 kHz, matching aiortc's audio frame size
FRAME_DURATION = SAMPLES_PER_FRAME / SAMPLE_RATE
TIME_BASE = fractions.Fraction(1, SAMPLE_RATE)


class RobotSpeakerTrack(MediaStreamTrack):
    """Persistent outbound audio track for the Go2's onboard speaker.

    A single instance is attached ONCE to the connection's pre-negotiated audio
    sender and lives for the whole WebRTC session. It emits digital silence when
    idle and the current clip's frames when speaking, and it NEVER raises
    MediaStreamError. This is deliberate: aiortc tears the sender's RTP loop down
    permanently the first time a track signals end-of-file (rtcrtpsender.py
    `_run_rtp` exits on MediaStreamError), and the Go2 does not support SDP
    renegotiation, so a torn-down sender cannot be revived. Keeping one
    never-ending track alive lets every subsequent utterance reuse the same
    sender by simply swapping the internal source.
    """

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._source: MediaPlayer | None = None
        self._source_track: MediaStreamTrack | None = None
        # Monotonic sample clock. Every emitted frame (silence or clip) is
        # re-stamped from this so timestamps never reset between clips — each
        # MediaPlayer restarts its own pts at 0, which would otherwise make the
        # Opus encoder's RTP timestamps regress and glitch the stream.
        self._timestamp = 0

    def play(self, player: MediaPlayer) -> None:
        """Switch the active source to ``player`` (scheduled on the loop thread).

        Any in-flight clip is stopped and released so its decode thread and file
        handle don't leak. Called on the connection's event-loop thread, the same
        thread as ``recv``, so the swap needs no extra locking.
        """
        old = self._source
        self._source = player
        self._source_track = player.audio
        if old is not None and old is not player:
            self._stop_player(old)

    async def recv(self) -> av.AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError

        frame = None
        track = self._source_track
        if track is not None:
            try:
                frame = await track.recv()
            except MediaStreamError:
                # Current clip reached end-of-file: release it and fall through
                # to silence. Crucially we do NOT propagate this error, so the
                # sender keeps running and the next utterance can reuse it.
                finished = self._source
                self._source = None
                self._source_track = None
                if finished is not None:
                    self._stop_player(finished)

        if frame is None:
            frame = self._silence_frame()
            # Clip frames are already paced to real time by MediaPlayer's
            # throttling; silence frames are not, so pace them ourselves.
            await asyncio.sleep(FRAME_DURATION)

        frame.pts = self._timestamp
        frame.time_base = TIME_BASE
        self._timestamp += frame.samples
        return frame

    @staticmethod
    def _silence_frame() -> av.AudioFrame:
        frame = av.AudioFrame(format="s16", layout="stereo", samples=SAMPLES_PER_FRAME)
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        frame.sample_rate = SAMPLE_RATE
        return frame

    @staticmethod
    def _stop_player(player: MediaPlayer) -> None:
        try:
            if player.audio is not None:
                player.audio.stop()
        except Exception as e:
            logger.debug("Error stopping previous speaker clip: %s", e)
