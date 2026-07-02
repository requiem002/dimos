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

import time

import numpy as np
from reactivex import Observable, create, disposable

from dimos.stream.audio.base import AbstractAudioTransform, AudioEvent
from dimos.stream.audio.volume import calculate_rms_volume
from dimos.utils.logging_config import setup_logger

logger = setup_logger()


class VoiceActivityRecorder(AbstractAudioTransform):
    """Segment a continuous audio stream into utterances using RMS voice activity.

    A continuous microphone (e.g. the robot's onboard mic) delivers small frames
    (~20 ms) forever, never gated by a push-to-talk. Feeding those frames one by
    one into a transcriber is wrong on two counts: each frame is far too short to
    transcribe, and running the transcriber on every silent frame both burns CPU
    and floods downstream consumers with empty/hallucinated text.

    This node watches per-frame RMS. It buffers frames once speech starts (with a
    short pre-roll so word onsets aren't clipped) and emits a single combined
    AudioEvent for the whole utterance once enough trailing silence is observed.
    Only complete utterances reach the transcriber. RMS is cheap, so this runs
    per frame without the cost of per-frame transcription.

    Speech is detected against an ADAPTIVE noise floor, not a fixed threshold: a
    fixed threshold that sits below the robot mic's ambient noise floor never sees
    silence, so every utterance runs to the max-length cap and buries the speech
    in seconds of room noise. The floor tracks ambient level (falling fast toward
    quiet, rising slowly), and a frame counts as speech only when it exceeds
    floor * noise_floor_ratio (and a small absolute minimum so a dead-silent room
    doesn't drive the gate to zero).

    Timing (silence, min/max length) is measured in AUDIO time via sample counts,
    not wall clock, so behaviour is independent of how fast frames arrive.
    """

    def __init__(
        self,
        speech_rms_threshold: float = 0.01,
        noise_floor_ratio: float = 2.5,
        silence_duration: float = 0.7,
        min_speech_duration: float = 0.4,
        max_utterance_duration: float = 15.0,
        pre_roll_duration: float = 0.3,
    ) -> None:
        """
        Args:
            speech_rms_threshold: Absolute minimum RMS (0..1) for the speech gate,
                so a dead-silent room doesn't drive the adaptive gate to zero.
            noise_floor_ratio: A frame counts as speech when its RMS exceeds the
                tracked ambient noise floor times this ratio.
            silence_duration: Trailing silence (s of audio) that ends an utterance.
            min_speech_duration: Utterances with less than this much speech (s) are
                discarded as blips, so coughs/clicks don't reach the transcriber.
            max_utterance_duration: Hard cap (s of audio); flush even without
                trailing silence so a noisy room can't buffer forever.
            pre_roll_duration: Audio (s) kept before speech onset so the first
                word isn't clipped.
        """
        self.speech_rms_threshold = speech_rms_threshold
        self.noise_floor_ratio = noise_floor_ratio
        self.silence_duration = silence_duration
        self.min_speech_duration = min_speech_duration
        self.max_utterance_duration = max_utterance_duration
        self.pre_roll_duration = pre_roll_duration
        self.audio_observable: Observable | None = None  # type: ignore[type-arg]

    def consume_audio(self, audio_observable: Observable) -> "VoiceActivityRecorder":  # type: ignore[type-arg]
        self.audio_observable = audio_observable
        return self

    def emit_recording(self) -> Observable:  # type: ignore[type-arg]
        """Observable emitting one AudioEvent per detected utterance."""
        if self.audio_observable is None:
            raise ValueError("No audio source provided. Call consume_audio() first.")

        def on_subscribe(observer, scheduler):
            # Per-subscription state so a resubscribe starts clean.
            state = {
                "recording": False,
                "buffer": [],  # list[np.ndarray] of float32 mono frames
                "total_samples": 0,
                "trailing_silence_samples": 0,
                "pre_roll": [],  # list[np.ndarray] kept before speech onset
                "pre_roll_samples": 0,
                "sample_rate": None,
                "noise_floor": None,  # tracked ambient RMS
            }

            def reset_utterance() -> None:
                state["recording"] = False
                state["buffer"] = []
                state["total_samples"] = 0
                state["trailing_silence_samples"] = 0

            def finalize() -> None:
                if not state["recording"]:
                    return
                sample_rate = state["sample_rate"]
                buffer = state["buffer"]
                total = state["total_samples"]
                speech_samples = total - state["trailing_silence_samples"]
                reset_utterance()
                if sample_rate is None or total == 0:
                    return
                if speech_samples / sample_rate < self.min_speech_duration:
                    logger.debug(
                        f"Discarding utterance with {speech_samples / sample_rate:.2f}s "
                        "of speech (below min)"
                    )
                    return
                combined = np.concatenate(buffer, axis=0)
                logger.info(f"Emitting {total / sample_rate:.2f}s utterance for transcription")
                observer.on_next(
                    AudioEvent(
                        data=combined,
                        sample_rate=sample_rate,
                        timestamp=time.time(),
                        channels=1,
                    )
                )

            def on_frame(event: AudioEvent) -> None:
                try:
                    frame = event.to_float32().data
                    # Collapse any residual channel axis to mono for a stable RMS.
                    if frame.ndim > 1:
                        frame = frame.mean(axis=1)
                    frame = frame.astype(np.float32, copy=False)
                    n = frame.shape[0]
                    if n == 0:
                        return

                    state["sample_rate"] = event.sample_rate
                    max_pre_roll = int(self.pre_roll_duration * event.sample_rate)

                    rms = calculate_rms_volume(frame)
                    floor = state["noise_floor"]
                    if floor is None:
                        floor = rms
                    speech_gate = max(self.speech_rms_threshold, floor * self.noise_floor_ratio)
                    is_speech = rms >= speech_gate

                    # Track the ambient floor. Always fall fast toward any quieter
                    # frame (so a floor seeded on a loud frame converges down and
                    # can't get stuck high), but rise only on non-speech frames and
                    # slowly (so speech never inflates the gate away from itself).
                    if rms < floor:
                        floor = 0.7 * floor + 0.3 * rms
                    elif not is_speech:
                        floor = 0.98 * floor + 0.02 * rms
                    state["noise_floor"] = floor

                    if is_speech:
                        if not state["recording"]:
                            state["recording"] = True
                            state["buffer"] = list(state["pre_roll"])
                            state["total_samples"] = state["pre_roll_samples"]
                            state["pre_roll"] = []
                            state["pre_roll_samples"] = 0
                        state["buffer"].append(frame)
                        state["total_samples"] += n
                        state["trailing_silence_samples"] = 0
                        if state["total_samples"] / event.sample_rate >= self.max_utterance_duration:
                            finalize()
                        return

                    # Silence.
                    if state["recording"]:
                        state["buffer"].append(frame)  # keep a short trailing tail
                        state["total_samples"] += n
                        state["trailing_silence_samples"] += n
                        if (
                            state["trailing_silence_samples"] / event.sample_rate
                            >= self.silence_duration
                        ):
                            finalize()
                        return

                    # Idle: keep a rolling pre-roll window of recent audio.
                    state["pre_roll"].append(frame)
                    state["pre_roll_samples"] += n
                    while (
                        state["pre_roll"]
                        and state["pre_roll_samples"] - state["pre_roll"][0].shape[0]
                        >= max_pre_roll
                    ):
                        state["pre_roll_samples"] -= state["pre_roll"].pop(0).shape[0]
                except Exception as e:
                    logger.error(f"Error in voice activity recorder: {e}")
                    observer.on_error(e)

            subscription = self.audio_observable.subscribe(
                on_next=on_frame,
                on_error=lambda e: observer.on_error(e),
                on_completed=lambda: observer.on_completed(),
            )

            logger.info(
                "Started voice-activity recorder "
                f"(threshold={self.speech_rms_threshold}, silence={self.silence_duration}s)"
            )

            return disposable.Disposable(subscription.dispose)

        return create(on_subscribe)

    def emit_audio(self) -> Observable:  # type: ignore[type-arg]
        """Alias for emit_recording so this satisfies AbstractAudioTransform."""
        return self.emit_recording()
