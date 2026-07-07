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

from math import gcd

import numpy as np
from scipy.signal import resample_poly

from dimos.stream.audio.base import AudioEvent


def resample_audio_event(
    event: AudioEvent, target_rate: int = 16000, mono: bool = True
) -> AudioEvent:
    """Convert an AudioEvent to the format an STT engine expects.

    The robot mic publishes native 48 kHz stereo int16; Whisper wants 16 kHz
    mono float32. Kept out of the connection layer so the connection stays
    format-agnostic (it publishes a generic AudioEvent, consumers adapt).

    48000 -> 16000 is an exact integer decimation (down=3), so resample_poly
    introduces no fractional-rate artefacts. scipy is already a dependency; no
    new/x86-only packages are pulled in.
    """
    ev = event.to_float32()
    data = ev.data

    if mono and data.ndim == 2 and data.shape[1] > 1:
        data = data.mean(axis=1)
    elif data.ndim == 2 and data.shape[1] == 1:
        data = data.reshape(-1)

    if ev.sample_rate != target_rate:
        divisor = gcd(ev.sample_rate, target_rate)
        up = target_rate // divisor
        down = ev.sample_rate // divisor
        data = resample_poly(data, up, down, axis=0)

    out_channels = 1 if (mono or data.ndim == 1) else data.shape[1]
    return AudioEvent(
        data=data.astype(np.float32),
        sample_rate=target_rate,
        timestamp=event.timestamp,
        channels=out_channels,
    )
