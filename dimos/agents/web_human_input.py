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


class WebInputConfig(ModuleConfig):
    force_local_audio: bool = Field(default_factory=lambda m: m["g"].force_local_audio)


class WebInput(Module):
    config: WebInputConfig

    _web_interface: RobotWebInterface | None = None
    _thread: Thread | None = None
    _human_transport: pLCMTransport[str] | None = None
    _connection: GO2ConnectionSpec | None = None
    mic_audio: In[AudioEvent]

    def _stt_audio_source(
        self, browser_audio: rx.Observable
    ) -> rx.Observable:  # type: ignore[type-arg]
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

        normalizer = AudioNormalizer()

        # Here to prevent unwanted imports in the file.
        from dimos.stream.audio.stt.node_whisper import WhisperNode

        # base.en is markedly more accurate than multilingual base for English
        # speech on the robot's noisy mic (the multilingual model hallucinates
        # other scripts under noise). Weights download on first use (~140 MB).
        stt_node = WhisperNode(model="base.en")

        # Connect audio pipeline: <robot mic | browser audio> → normalizer → whisper
        normalizer.consume_audio(self._stt_audio_source(audio_subject))
        stt_node.consume_audio(normalizer.emit_audio())

        # Subscribe to both text input sources
        # 1. Direct text from web interface
        unsub = self._web_interface.query_stream.subscribe(self._human_transport.publish)
        self.register_disposable(unsub)

        # 2. Transcribed text from STT.
        # Drop empty/whitespace-only transcriptions: Whisper emits a blank string
        # on silence, and publishing those to /human_input spams the agent with
        # empty turns (see _stt_audio_source).
        unsub = (
            stt_node.emit_text()
            .pipe(ops.filter(lambda text: bool(text and text.strip())))
            .subscribe(self._human_transport.publish)
        )
        self.register_disposable(unsub)

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
