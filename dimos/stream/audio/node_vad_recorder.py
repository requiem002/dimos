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

    The gate has HYSTERESIS: once recording, frames only need to exceed
    floor * continuation_ratio (< noise_floor_ratio) to count as speech. Without
    this, only the loudest syllables of distant speech stay above the onset gate
    and utterances get chopped after ~1s, feeding the transcriber fragments
    ("Mm-hmm", "He's dead") instead of whole sentences.

    The RMS gate is deliberately permissive; PRECISION comes from a second
    stage: each candidate utterance is confirmed by the Silero neural VAD
    (bundled with faster-whisper, no extra dependency) before it is emitted.
    Non-speech candidates — servo whine, dance thuds, jump impacts — are
    dropped, and confirmed speech is trimmed to the detected span (plus
    padding) so the transcriber sees speech, not room noise. This is the same
    class of technology commercial voice assistants use for speech detection.
    If the neural VAD is unavailable it fails OPEN (utterances pass through).

    Timing (silence, min/max length) is measured in AUDIO time via sample counts,
    not wall clock, so behaviour is independent of how fast frames arrive.
    """

    # Silero is trained on 8/16 kHz; our robot-mic STT path resamples to 16 kHz
    # before this node. Confirmation is skipped at any other rate.
    _NEURAL_VAD_SAMPLE_RATE = 16000

    def __init__(
        self,
        speech_rms_threshold: float = 0.008,
        noise_floor_ratio: float = 2.0,
        continuation_ratio: float = 1.4,
        silence_duration: float = 0.7,
        min_speech_duration: float = 0.4,
        max_utterance_duration: float = 15.0,
        pre_roll_duration: float = 0.3,
        level_log_interval: float = 10.0,
        use_neural_vad: bool = True,
    ) -> None:
        """
        Args:
            speech_rms_threshold: Absolute minimum RMS (0..1) for the speech gate,
                so a dead-silent room doesn't drive the adaptive gate to zero.
            noise_floor_ratio: A frame counts as speech ONSET when its RMS exceeds
                the tracked ambient noise floor times this ratio.
            continuation_ratio: While already recording, a frame counts as speech
                when it exceeds floor times this (lower) ratio — hysteresis so
                quieter syllables don't end the utterance early.
            silence_duration: Trailing silence (s of audio) that ends an utterance.
            min_speech_duration: Utterances with less than this much speech (s) are
                discarded as blips, so coughs/clicks don't reach the transcriber.
            max_utterance_duration: Hard cap (s of audio); flush even without
                trailing silence so a noisy room can't buffer forever.
            pre_roll_duration: Audio (s) kept before speech onset so the first
                word isn't clipped.
            level_log_interval: Seconds between mic-level diagnostic log lines
                (peak RMS / noise floor / speech gate), for tuning the gate to a
                specific room and mic gain from the logs. 0 disables.
            use_neural_vad: Confirm each candidate utterance with the Silero
                neural VAD and trim it to the detected speech span. Candidates
                with no detected speech are dropped.
        """
        self.speech_rms_threshold = speech_rms_threshold
        self.noise_floor_ratio = noise_floor_ratio
        self.continuation_ratio = continuation_ratio
        self.silence_duration = silence_duration
        self.min_speech_duration = min_speech_duration
        self.max_utterance_duration = max_utterance_duration
        self.pre_roll_duration = pre_roll_duration
        self.level_log_interval = level_log_interval
        self.use_neural_vad = use_neural_vad
        self._neural_vad_broken = False
        self.audio_observable: Observable | None = None  # type: ignore[type-arg]

    def _speech_spans(self, audio: np.ndarray, sample_rate: int) -> list[tuple[int, int]] | None:
        """Run the Silero neural VAD over a candidate utterance.

        Returns a list of (start, end) sample spans of detected speech (already
        padded), an empty list when the clip contains no speech, or None when
        confirmation is unavailable (disabled, wrong sample rate, or the VAD
        failed to load) — in which case the caller emits the clip unfiltered.
        """
        if (
            not self.use_neural_vad
            or self._neural_vad_broken
            or sample_rate != self._NEURAL_VAD_SAMPLE_RATE
        ):
            return None
        try:
            # Bundled with faster-whisper (already a dimos dependency); the
            # small ONNX model loads once (~2 MB) and runs in milliseconds.
            from faster_whisper.vad import VadOptions, get_speech_timestamps

            options = VadOptions(
                threshold=0.5,
                min_speech_duration_ms=200,
                min_silence_duration_ms=500,
                speech_pad_ms=300,
            )
            timestamps = get_speech_timestamps(
                audio.astype(np.float32, copy=False), options, sampling_rate=sample_rate
            )
            return [(int(t["start"]), int(t["end"])) for t in timestamps]
        except Exception as e:
            # Fail OPEN: better to transcribe some noise than to go deaf.
            logger.error(f"Neural VAD unavailable, emitting utterances unfiltered: {e}")
            self._neural_vad_broken = True
            return None

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
                "peak_rms": 0.0,  # loudest frame since the last level log
                "last_level_log": time.time(),
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

                spans = self._speech_spans(combined, sample_rate)
                if spans is not None:
                    if not spans:
                        logger.info(
                            f"Dropping {total / sample_rate:.2f}s candidate: "
                            "neural VAD found no speech (motion/ambient noise)"
                        )
                        return
                    # Trim to the detected speech (spans are already padded) so
                    # the transcriber isn't fed leading/trailing room noise.
                    combined = combined[spans[0][0] : spans[-1][1]]

                logger.info(
                    f"Emitting {combined.shape[0] / sample_rate:.2f}s utterance for transcription"
                )
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
                    # Hysteresis: a lower bar keeps an utterance going than the
                    # one that starts it (see class docstring).
                    ratio = self.continuation_ratio if state["recording"] else self.noise_floor_ratio
                    speech_gate = max(self.speech_rms_threshold, floor * ratio)
                    is_speech = rms >= speech_gate

                    # Periodic level diagnostics so the gate can be tuned to a
                    # specific room/mic from the logs alone.
                    state["peak_rms"] = max(state["peak_rms"], rms)
                    if (
                        self.level_log_interval > 0
                        and time.time() - state["last_level_log"] >= self.level_log_interval
                    ):
                        logger.info(
                            f"Mic level: peak_rms={state['peak_rms']:.4f} "
                            f"noise_floor={floor:.4f} speech_gate={speech_gate:.4f} "
                            f"{'RECORDING' if state['recording'] else 'idle'}"
                        )
                        state["peak_rms"] = 0.0
                        state["last_level_log"] = time.time()

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
                f"(threshold={self.speech_rms_threshold}, silence={self.silence_duration}s, "
                f"neural_vad={'on' if self.use_neural_vad else 'off'})"
            )

            return disposable.Disposable(subscription.dispose)

        return create(on_subscribe)

    def emit_audio(self) -> Observable:  # type: ignore[type-arg]
        """Alias for emit_recording so this satisfies AbstractAudioTransform."""
        return self.emit_recording()
