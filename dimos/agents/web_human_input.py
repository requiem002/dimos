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

import difflib
import string
from threading import Thread

from pydantic import Field
import reactivex as rx
import reactivex.operators as ops

from dimos.constants import DEFAULT_THREAD_JOIN_TIMEOUT
from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.core.stream import In
from dimos.core.transport import pLCMTransport
from dimos.robot.unitree.go2.connection_spec import GO2ConnectionSpec, use_robot_audio
from dimos.stream.audio.base import AudioEvent
from dimos.stream.audio.node_normalizer import AudioNormalizer
from dimos.stream.audio.node_vad_recorder import VoiceActivityRecorder
from dimos.stream.audio.resample import resample_audio_event
from dimos.utils.logging_config import setup_logger
from dimos.web.robot_web_interface import RobotWebInterface

logger = setup_logger()

# Common Whisper transcriptions of a spoken "hey" (clipped or misheard). The
# wake-word gate accepts any of these in place of a literal leading "hey".
_HEY_VARIANTS = {"hey", "hay", "hi", "he", "a", "eh", "yo"}

# Minimum difflib similarity for a transcribed word to count as a wake-word
# token ("robots"/"robot" ≈ 0.91, "robert"/"robot" ≈ 0.73; unrelated words
# like "rabbit" fall below).
_WAKE_TOKEN_SIMILARITY = 0.7


def _clean_word(word: str) -> str:
    return word.strip(string.punctuation + "…").lower()


def strip_wake_word(text: str, wake_word: str) -> str | None:
    """Return the command after the wake phrase, or None if text lacks it.

    Matching is tolerant of STT noise: casing and punctuation are ignored,
    "hey"-like fillers are interchangeable, and each wake token accepts close
    transcription variants. The wake phrase alone (no command after it) is
    treated as no command. An empty wake_word disables the gate entirely and
    passes text through unchanged.
    """
    if not wake_word:
        return text
    wake_tokens = [_clean_word(token) for token in wake_word.split()]
    words = text.split()
    if len(words) < len(wake_tokens):
        return None
    for wake_token, word in zip(wake_tokens, words, strict=False):
        cleaned = _clean_word(word)
        if cleaned == wake_token:
            continue
        if wake_token in _HEY_VARIANTS and cleaned in _HEY_VARIANTS:
            continue
        if difflib.SequenceMatcher(None, cleaned, wake_token).ratio() >= _WAKE_TOKEN_SIMILARITY:
            continue
        return None
    remainder = " ".join(words[len(wake_tokens) :]).strip()
    return remainder or None


def _select_whisper_model(configured: str) -> str:
    """Pick the Whisper model: explicit config wins, else best for the device.

    small.en is markedly more accurate than base.en and near-realtime on any
    CUDA GPU, but too slow for live STT on CPU — so auto-select by device.
    """
    if configured:
        return configured
    import torch

    return "small.en" if torch.cuda.is_available() else "base.en"


class WebInputConfig(ModuleConfig):
    force_local_audio: bool = Field(default_factory=lambda m: m["g"].force_local_audio)
    voice_input: bool = Field(default_factory=lambda m: m["g"].voice_input)
    wake_word: str = Field(default_factory=lambda m: m["g"].wake_word)
    whisper_model: str = Field(default_factory=lambda m: m["g"].whisper_model)


class WebInput(Module):
    config: WebInputConfig

    _web_interface: RobotWebInterface | None = None
    _thread: Thread | None = None
    _human_transport: pLCMTransport[str] | None = None
    _connection: GO2ConnectionSpec | None = None
    mic_audio: In[AudioEvent]

    def _stt_audio_source(self, browser_audio: rx.Observable) -> rx.Observable:  # type: ignore[type-arg]
        """Pick the STT source: robot mic by default, browser audio as fallback.

        Robot when a connection is injected and local audio isn't forced (the
        shared use_robot_audio rule that also governs the speaker). The robot mic
        arrives as native 48 kHz stereo AudioEvents, so resample to Whisper's
        16 kHz mono float32 here. See requirements FR-M1..M6.

        The robot mic is continuous and never push-to-talk gated, so it is passed
        through a voice-activity recorder that emits one combined AudioEvent per
        utterance. Without this gate Whisper runs on every ~20 ms frame, which
        can't be transcribed, saturates the CPU, and floods /human_input with
        empty/hallucinated text (agent loop). The browser source is already
        gated by push-to-talk, so it is left untouched.
        """
        if use_robot_audio(self._connection, self.config.force_local_audio):
            logger.info("STT source: robot onboard microphone")
            resampled = self.mic_audio.observable().pipe(ops.map(resample_audio_event))
            recorder = VoiceActivityRecorder()
            recorder.consume_audio(resampled)
            return recorder.emit_recording()
        logger.info("STT source: browser web audio")
        return browser_audio.pipe(ops.share())

    def _gate_transcript(self, text: str) -> str | None:
        command = strip_wake_word(text, self.config.wake_word)
        if command is None:
            logger.info(f'Ignored (no wake word): "{text.strip()}"')
        return command

    def _wire_stt(self, browser_audio: rx.Observable) -> None:  # type: ignore[type-arg]
        """Wire <robot mic | browser audio> → normalizer → Whisper → /human_input."""
        assert self._human_transport is not None
        normalizer = AudioNormalizer()

        # Here to prevent unwanted imports in the file.
        from dimos.stream.audio.stt.node_whisper import WhisperNode

        # .en models are markedly more accurate than the multilingual ones for
        # English speech on the robot's noisy mic (the multilingual models
        # hallucinate other scripts under noise). Weights download on first use.
        model = _select_whisper_model(self.config.whisper_model)
        logger.info(f"Whisper STT model: {model}")
        stt_node = WhisperNode(model=model)

        normalizer.consume_audio(self._stt_audio_source(browser_audio))
        stt_node.consume_audio(normalizer.emit_audio())

        # Drop empty/whitespace-only transcriptions: Whisper emits a blank string
        # on silence, and publishing those to /human_input spams the agent with
        # empty turns (see _stt_audio_source).
        transcripts = stt_node.emit_text().pipe(
            ops.filter(lambda text: bool(text and text.strip()))
        )

        # Wake-word gate, robot mic only: that mic is continuous and hears the
        # whole room, so require utterances to be addressed to the robot.
        # Browser push-to-talk is deliberate and bypasses the gate.
        if self.config.wake_word and use_robot_audio(
            self._connection, self.config.force_local_audio
        ):
            logger.info(f'Wake word active: "{self.config.wake_word}"')
            transcripts = transcripts.pipe(
                ops.map(self._gate_transcript),
                ops.filter(lambda command: command is not None),
            )

        unsub = transcripts.subscribe(self._human_transport.publish)
        self.register_disposable(unsub)

    @rpc
    def start(self) -> None:
        super().start()

        self._human_transport = pLCMTransport("/human_input")

        audio_subject: rx.subject.Subject[AudioEvent] = rx.subject.Subject()

        self._web_interface = RobotWebInterface(
            port=5555,
            text_streams={"agent_responses": rx.subject.Subject()},
            audio_subject=audio_subject,
        )

        # Text typed in the web UI always reaches the agent, voice or not.
        unsub = self._web_interface.query_stream.subscribe(self._human_transport.publish)
        self.register_disposable(unsub)

        if self.config.voice_input:
            self._wire_stt(audio_subject)
        else:
            logger.info("Voice input disabled by config; web text input only")

        self._thread = Thread(target=self._web_interface.run, daemon=True)
        self._thread.start()

        logger.info("Web interface started at http://localhost:5555")

    @rpc
    def stop(self) -> None:
        if self._web_interface:
            self._web_interface.shutdown()
        if self._thread:
            self._thread.join(timeout=DEFAULT_THREAD_JOIN_TIMEOUT)
        if self._human_transport:
            self._human_transport.lcm.stop()
        super().stop()
