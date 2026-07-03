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
    # The synthetic constant-value frames used here exercise the RMS state
    # machine; the Silero stage would (correctly) reject them as non-speech,
    # so it is disabled except in the dedicated neural-stage tests below.
    kwargs.setdefault("use_neural_vad", False)
    recorder = VoiceActivityRecorder(**kwargs)
    recorder.consume_audio(rx.of(*frames))
    out: list[AudioEvent] = []
    recorder.emit_recording().subscribe(on_next=out.append)
    return out


def _silence(n: int) -> list[AudioEvent]:
    return [_frame(0.0) for _ in range(n)]


def _silence_at(level: float, n: int) -> list[AudioEvent]:
    """Ambient 'silence' at a nonzero noise level (constant RMS == level)."""
    return [_frame(level) for _ in range(n)]


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


def test_high_ambient_floor_still_segments() -> None:
    # Room noise sits at 0.03 RMS -- above the old fixed 0.015 threshold, which
    # made every utterance run to the 30 s cap. The adaptive floor must treat
    # 0.03 as silence and only 0.2 as speech, so the utterance still closes.
    frames = _silence_at(0.03, 30) + _speech(40, level=0.2) + _silence_at(0.03, 40)
    out = _run(frames, silence_duration=0.5, min_speech_duration=0.4)
    assert len(out) == 1


def test_pre_roll_prepends_audio_before_onset() -> None:
    # A little leading ambient (as a real continuous mic always delivers before
    # speech), then a burst. Utterance holds the speech plus the pre-roll.
    frames = _silence(5) + _speech(50) + _silence(40)
    out = _run(frames, silence_duration=0.5, min_speech_duration=0.4)
    assert len(out) == 1
    assert out[0].data.shape[0] >= 50 * FRAME


def test_hysteresis_keeps_quiet_continuation() -> None:
    # Ambient 0.03 -> onset gate 0.06 (ratio 2.0), continuation gate 0.042
    # (ratio 1.4). Speech onsets loud (0.2) then continues quietly at 0.05:
    # above the continuation gate but below the onset gate. Without hysteresis
    # the quiet part would be counted as trailing silence and the utterance
    # chopped after the loud syllable (the run-1 "fragments" failure).
    frames = (
        _silence_at(0.03, 30)
        + _speech(10, level=0.2)
        + _speech(30, level=0.05)
        + _silence_at(0.03, 40)
    )
    out = _run(frames, silence_duration=0.5, min_speech_duration=0.4)
    assert len(out) == 1
    assert out[0].data.shape[0] >= 40 * FRAME  # loud onset + quiet continuation


def test_neural_vad_drops_constant_nonspeech() -> None:
    # With the real Silero stage enabled, a constant-value burst (RMS-loud but
    # spectrally nothing like speech, e.g. servo hum) must be dropped.
    frames = _silence(5) + _speech(50) + _silence(40)
    out = _run(frames, silence_duration=0.5, min_speech_duration=0.4, use_neural_vad=True)
    assert out == []


def test_neural_vad_trims_to_detected_span() -> None:
    recorder = VoiceActivityRecorder(silence_duration=0.5, min_speech_duration=0.4)
    recorder._speech_spans = lambda audio, sr: [(1000, 5000)]  # type: ignore[method-assign]
    frames = _silence(5) + _speech(50) + _silence(40)
    recorder.consume_audio(rx.of(*frames))
    out: list[AudioEvent] = []
    recorder.emit_recording().subscribe(on_next=out.append)
    assert len(out) == 1
    assert out[0].data.shape[0] == 4000


def test_neural_vad_fails_open_when_unavailable() -> None:
    # None means "confirmation unavailable" -> the utterance passes through.
    recorder = VoiceActivityRecorder(silence_duration=0.5, min_speech_duration=0.4)
    recorder._speech_spans = lambda audio, sr: None  # type: ignore[method-assign]
    frames = _silence(5) + _speech(50) + _silence(40)
    recorder.consume_audio(rx.of(*frames))
    out: list[AudioEvent] = []
    recorder.emit_recording().subscribe(on_next=out.append)
    assert len(out) == 1
    assert out[0].data.shape[0] >= 50 * FRAME


def test_speech_spans_skips_non_16k_audio() -> None:
    recorder = VoiceActivityRecorder()
    assert recorder._speech_spans(np.zeros(48000, dtype=np.float32), 48000) is None


def test_speech_spans_empty_on_silence() -> None:
    recorder = VoiceActivityRecorder()
    assert recorder._speech_spans(np.zeros(32000, dtype=np.float32), 16000) == []
