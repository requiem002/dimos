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

import os
import tempfile
import threading
import time

from pydantic import Field
from reactivex import Subject
import soundfile as sf  # type: ignore[import-untyped]

from dimos.agents.annotation import skill
from dimos.constants import DEFAULT_THREAD_JOIN_TIMEOUT
from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.robot.unitree.go2.connection_spec import GO2ConnectionSpec
from dimos.stream.audio.node_output import SounddeviceAudioOutput
from dimos.stream.audio.tts.node_openai import OpenAITTSNode, Voice
from dimos.utils.logging_config import setup_logger

logger = setup_logger()

# Extra time on top of the computed clip length before releasing the speak lock,
# so the next utterance cannot clip the tail of this one. The robot plays the
# clip asynchronously and its real playback-completion signal lives behind the
# RPC boundary (see _play_on_robot), so we pad rather than wait on it.
_ROBOT_PLAYBACK_MARGIN_FACTOR = 1.15
_ROBOT_PLAYBACK_MARGIN_SECONDS = 0.5


class SpeakSkillConfig(ModuleConfig):
    speak_through_robot: bool = Field(default_factory=lambda m: m["g"].speak_through_robot)


class SpeakSkill(Module):
    config: SpeakSkillConfig

    _tts_node: OpenAITTSNode | None = None
    _audio_output: SounddeviceAudioOutput | None = None
    _connection: GO2ConnectionSpec | None = None
    _audio_lock: threading.Lock = threading.Lock()
    _bg_threads: list[threading.Thread] = []
    _bg_threads_lock: threading.Lock = threading.Lock()
    _robot_audio_sub = None
    _robot_clip_dispatched: threading.Event = threading.Event()
    _robot_clip_duration: float = 0.0

    @rpc
    def start(self) -> None:
        super().start()
        self._tts_node = OpenAITTSNode(speed=1.2, voice=Voice.ONYX)
        if self.config.speak_through_robot:
            # Route TTS to the robot speaker only — skip the local audio device.
            self._robot_audio_sub = self._tts_node.emit_audio().subscribe(
                on_next=self._play_on_robot,
                on_error=lambda e: logger.error(f"Error in robot speaker audio stream: {e}"),
            )
        else:
            self._audio_output = SounddeviceAudioOutput(sample_rate=24000)
            self._audio_output.consume_audio(self._tts_node.emit_audio())

    @rpc
    def stop(self) -> None:
        with self._bg_threads_lock:
            threads = list(self._bg_threads)
        for t in threads:
            t.join(timeout=DEFAULT_THREAD_JOIN_TIMEOUT)
        if self._robot_audio_sub is not None:
            self._robot_audio_sub.dispose()
            self._robot_audio_sub = None
        if self._tts_node:
            self._tts_node.dispose()
            self._tts_node = None
        if self._audio_output:
            self._audio_output.stop()
            self._audio_output = None
        super().stop()

    def _play_on_robot(self, audio_event) -> None:  # type: ignore[no-untyped-def]
        """Write a synthesized clip to a temp WAV and play it on the robot speaker.

        WAV is equivalent to the proven MP3 path here: aiortc's MediaPlayer
        decodes any container through ffmpeg and resamples to 48 kHz/s16/stereo,
        and the Opus sender resamples again — so the source format and sample
        rate are irrelevant to what the negotiated sender receives.
        """
        try:
            data = audio_event.data
            sample_rate = audio_event.sample_rate
            fd, path = tempfile.mkstemp(suffix=".wav", prefix="dimos_tts_")
            os.close(fd)
            sf.write(path, data, sample_rate)

            self._robot_clip_duration = len(data) / sample_rate
            if self._connection is not None:
                self._connection.play_audio_track(path)
            else:
                logger.warning("speak_through_robot is set but no robot connection is wired")

            # Remove the temp file once playback (plus margin) is well past.
            cleanup_delay = self._robot_clip_duration + 5.0
            threading.Timer(cleanup_delay, self._remove_temp_file, args=(path,)).start()
        except Exception as e:
            logger.error(f"Error routing TTS audio to robot speaker: {e}")
        finally:
            self._robot_clip_dispatched.set()

    @staticmethod
    def _remove_temp_file(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            pass

    @skill
    def speak(self, text: str, blocking: bool = True) -> str:
        """Speak text out loud through the robot's speakers.

        USE THIS TOOL AS OFTEN AS NEEDED. People can't normally see what you say in text, but can hear what you speak.

        Try to be as concise as possible. Remember that speaking takes time, so get to the point quickly.

        Example usage:

            speak("Hello, I am your robot assistant.")
        """
        if self._tts_node is None:
            return "Error: TTS not initialized"

        if not blocking:
            thread = threading.Thread(
                target=self._speak_bg, args=(text,), daemon=True, name="SpeakSkill-bg"
            )
            with self._bg_threads_lock:
                self._bg_threads.append(thread)
            thread.start()
            return f"Speaking (non-blocking): {text}"

        return self._speak_blocking(text)

    def _speak_bg(self, text: str) -> None:
        try:
            self._speak_blocking(text)
        finally:
            # Remove this thread from the list of background threads when done
            with self._bg_threads_lock:
                self._bg_threads = [
                    t for t in self._bg_threads if t is not threading.current_thread()
                ]

    def _speak_blocking(self, text: str) -> str:
        # Use lock to prevent simultaneous speech
        with self._audio_lock:
            if self._tts_node is None:
                return "Error: TTS not initialized"

            if self.config.speak_through_robot:
                return self._speak_blocking_robot(text)

            text_subject: Subject[str] = Subject()
            audio_complete = threading.Event()
            self._tts_node.consume_text(text_subject)

            def set_as_complete(_t: str) -> None:
                audio_complete.set()

            def set_as_complete_e(_e: Exception) -> None:
                audio_complete.set()

            subscription = self._tts_node.emit_text().subscribe(
                on_next=set_as_complete,
                on_error=set_as_complete_e,
            )

            text_subject.on_next(text)
            text_subject.on_completed()

            timeout = max(5, len(text) * 0.1)

            if not audio_complete.wait(timeout=timeout):
                logger.warning(f"TTS timeout reached for: {text}")
                subscription.dispose()
                return f"Warning: TTS timeout while speaking: {text}"
            else:
                # Small delay to ensure buffers flush
                time.sleep(0.3)

            subscription.dispose()

            return f"Spoke: {text}"

    def _speak_blocking_robot(self, text: str) -> str:
        # Caller (_speak_blocking) already holds self._audio_lock.
        assert self._tts_node is not None

        # Drive completion off the audio dispatch, not emit_text: the TTS node
        # emits the text before the audio, so waiting on text could return
        # before _play_on_robot has handed the clip to the robot.
        self._robot_clip_dispatched.clear()
        text_subject: Subject[str] = Subject()
        self._tts_node.consume_text(text_subject)
        text_subject.on_next(text)
        text_subject.on_completed()

        timeout = max(5, len(text) * 0.1)
        if not self._robot_clip_dispatched.wait(timeout=timeout):
            logger.warning(f"TTS timeout reached for: {text}")
            return f"Warning: TTS timeout while speaking: {text}"

        playback_wait = (
            self._robot_clip_duration * _ROBOT_PLAYBACK_MARGIN_FACTOR
            + _ROBOT_PLAYBACK_MARGIN_SECONDS
        )
        time.sleep(playback_wait)
        return f"Spoke: {text}"
