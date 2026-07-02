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

"""Non-hardware tests for the voice-activity utterance recorder.

These assert the gate's segmentation logic on synthetic frames: continuous
silence emits nothing, a speech burst followed by enough trailing silence emits
exactly one combined utterance, and a burst too short is discarded. Timing is
measured in audio samples, so no real-time waiting is needed.

Not covered here (hardware-only): that a real spoken sentence over the Go2 mic
transcribes correctly, and tuning of the RMS threshold to the robot's mic gain.
"""

import numpy as np
import reactivex as rx

from dimos.stream.audio.base import AudioEvent
from dimos.stream.audio.node_vad_recorder import VoiceActivityRecorder

SR = 16000
FRAME = 320  # 20 ms at 16 kHz


def _frame(rms_level: float) -> AudioEvent:
    """A float32 mono frame whose samples sit at +/- rms_level (constant => RMS == level)."""
    data = np.full(FRAME, rms_level, dtype=np.float32)
    return AudioEvent(data=data, sample_rate=SR, timestamp=0.0, channels=1)


def _run(frames: list[AudioEvent], **kwargs) -> list[AudioEvent]:
    recorder = VoiceActivityRecorder(**kwargs)
    recorder.consume_audio(rx.of(*frames))
    out: list[AudioEvent] = []
    recorder.emit_recording().subscribe(on_next=out.append)
    return out


def _silence(n: int) -> list[AudioEvent]:
    return [_frame(0.0) for _ in range(n)]


def _speech(n: int, level: float = 0.2) -> list[AudioEvent]:
    return [_frame(level) for _ in range(n)]


def test_pure_silence_emits_nothing() -> None:
    assert _run(_silence(200)) == []


def test_single_utterance_is_emitted_once() -> None:
    # 1 s of speech, then 0.7 s of trailing silence to close the utterance.
    frames = _silence(5) + _speech(50) + _silence(40)
    out = _run(frames, silence_duration=0.5, min_speech_duration=0.4)
    assert len(out) == 1
    assert out[0].sample_rate == SR
    assert out[0].data.ndim == 1
    # Utterance holds the speech plus a short pre-roll and trailing tail.
    assert out[0].data.shape[0] >= 50 * FRAME


def test_short_blip_is_discarded() -> None:
    # 3 frames (~60 ms) of speech is below min_speech_duration -> dropped.
    frames = _silence(5) + _speech(3) + _silence(40)
    out = _run(frames, silence_duration=0.5, min_speech_duration=0.4)
    assert out == []


def test_two_utterances_separated_by_silence() -> None:
    frames = (
        _silence(5)
        + _speech(40)
        + _silence(40)  # closes utterance 1
        + _speech(40)
        + _silence(40)  # closes utterance 2
    )
    out = _run(frames, silence_duration=0.5, min_speech_duration=0.4)
    assert len(out) == 2


def test_pre_roll_prepends_audio_before_onset() -> None:
    # No leading silence to trim from; pre-roll should still not error and the
    # utterance should contain at least the speech samples.
    frames = _speech(50) + _silence(40)
    out = _run(frames, silence_duration=0.5, min_speech_duration=0.4)
    assert len(out) == 1
    assert out[0].data.shape[0] >= 50 * FRAME
