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

"""Non-hardware tests for the robot-mic → STT format conversion (requirements 7.1).

Covers the pure resample helper: the Go2 mic's native 48 kHz stereo int16 frames
become the 16 kHz mono float32 that Whisper expects. Hardware-only (section 7.2):
that the resampled audio is actually intelligible to STT on the wire.
"""

import numpy as np
import pytest

from dimos.stream.audio.base import AudioEvent
from dimos.stream.audio.resample import resample_audio_event


def _stereo_int16(n: int, sample_rate: int = 48000) -> AudioEvent:
    data = (np.random.randn(n, 2) * 1000).astype(np.int16)
    return AudioEvent(data=data, sample_rate=sample_rate, timestamp=1.0, channels=2)


def test_downsamples_stereo_int16_to_mono_float32_16k() -> None:
    n = 24000  # 0.5 s at 48 kHz
    out = resample_audio_event(_stereo_int16(n))
    assert out.sample_rate == 16000
    assert out.channels == 1
    assert out.data.ndim == 1
    assert out.data.dtype == np.float32
    # 48000 -> 16000 is exact decimation by 3.
    assert out.data.shape[0] == n // 3


def test_silence_stays_silent() -> None:
    sil = AudioEvent(
        data=np.zeros((24000, 2), dtype=np.int16), sample_rate=48000, timestamp=0.0, channels=2
    )
    out = resample_audio_event(sil)
    assert np.abs(out.data).max() == 0.0


def test_preserves_timestamp() -> None:
    out = resample_audio_event(_stereo_int16(9600))
    assert out.timestamp == 1.0


def test_int16_full_scale_maps_to_unit_float() -> None:
    # int16 full-scale must map to ~1.0 float32. Checked rate-matched so the
    # anti-alias filter (which rings/overshoots at step edges) isn't in play.
    data = np.full((16000, 2), 32767, dtype=np.int16)
    ev = AudioEvent(data=data, sample_rate=16000, timestamp=0.0, channels=2)
    out = resample_audio_event(ev, target_rate=16000)
    assert out.data.max() == pytest.approx(1.0, abs=1e-3)
    assert out.data.ndim == 1


def test_tone_length_and_rate_after_resample() -> None:
    sample_rate = 48000
    t = np.arange(sample_rate, dtype=np.float32) / sample_rate  # 1.0 s
    tone = np.sin(2 * np.pi * 440 * t).astype(np.float32)
    stereo = np.stack([tone, tone], axis=1)
    ev = AudioEvent(data=stereo, sample_rate=sample_rate, timestamp=0.0, channels=2)
    out = resample_audio_event(ev)
    assert out.sample_rate == 16000
    assert out.data.shape[0] == pytest.approx(16000, abs=2)
    assert out.data.ndim == 1


def test_already_mono_1d_input_passes_through_shape() -> None:
    mono = (np.random.randn(48000) * 1000).astype(np.int16)
    ev = AudioEvent(data=mono, sample_rate=48000, timestamp=0.0, channels=1)
    out = resample_audio_event(ev)
    assert out.channels == 1
    assert out.data.ndim == 1
    assert out.data.shape[0] == 16000


def test_no_resample_when_rate_matches() -> None:
    mono = (np.random.randn(16000) * 1000).astype(np.int16)
    ev = AudioEvent(data=mono, sample_rate=16000, timestamp=0.0, channels=1)
    out = resample_audio_event(ev, target_rate=16000)
    assert out.sample_rate == 16000
    assert out.data.shape[0] == 16000
    assert out.data.dtype == np.float32
