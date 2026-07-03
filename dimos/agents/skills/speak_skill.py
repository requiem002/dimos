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

from langchain_core.messages import BaseMessage
from pydantic import Field
from reactivex import Subject
import soundfile as sf  # type: ignore[import-untyped]

from dimos.agents.annotation import skill
from dimos.constants import DEFAULT_THREAD_JOIN_TIMEOUT
from dimos.core.core import rpc
from dimos.core.module import Module, ModuleConfig
from dimos.core.stream import In
from dimos.robot.unitree.go2.connection_spec import GO2ConnectionSpec, use_robot_audio
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


def _robot_playback_wait(clip_duration: float) -> float:
    """Seconds to block after handing a clip to the robot so the next utterance
    cannot clip its tail.

    Duration-based with a margin rather than the real playback-ended signal,
    which lives behind the RPC boundary in the dedicated_worker connection
    process (see requirements DR-S3).
    """
    return clip_duration * _ROBOT_PLAYBACK_MARGIN_FACTOR + _ROBOT_PLAYBACK_MARGIN_SECONDS


# Auto-speak: skip an agent text reply that lands this soon after a speak-tool
# call finished — it is the model's post-speak confirmation ("I've introduced
# myself through my speakers."), already voiced by the tool call itself.
_AUTO_SPEAK_COOLDOWN_SECONDS = 3.0
# Auto-speak: cap so a long text dump (module lists, etc.) can't hold the
# speaker for a minute.
_AUTO_SPEAK_MAX_CHARS = 350


def _message_text(msg: BaseMessage) -> str:
    """Plain text of a langchain message (content may be a string or a list of
    typed blocks)."""
    content = msg.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _truncate_for_speech(text: str, max_chars: int = _AUTO_SPEAK_MAX_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    # Prefer ending on a sentence boundary.
    for sep in (". ", "! ", "? "):
        idx = cut.rfind(sep)
        if idx > max_chars // 2:
            return cut[: idx + 1]
    return cut + "…"


class SpeakSkillConfig(ModuleConfig):
    force_local_audio: bool = Field(default_factory=lambda m: m["g"].force_local_audio)
    # Voice the agent's text replies automatically. The system prompt asks the
    # model to use the speak tool, but models don't do so reliably; people next
    # to the robot get no response at all when it "answers" in text only. This
    # makes speaking the default deterministically rather than by LLM goodwill.
    speak_agent_replies: bool = True


class SpeakSkill(Module):
    config: SpeakSkillConfig

    # Every message flowing through the agent (human/ai/tool), published by
    # McpClient; used to auto-speak the agent's text replies.
    agent: In[BaseMessage]

    _tts_node: OpenAITTSNode | None = None
    _audio_output: SounddeviceAudioOutput | None = None
    _connection: GO2ConnectionSpec | None = None
    _audio_lock: threading.Lock = threading.Lock()
    _bg_threads: list[threading.Thread] = []
    _bg_threads_lock: threading.Lock = threading.Lock()
    _robot_audio_sub = None
    _robot_clip_dispatched: threading.Event = threading.Event()
    _robot_clip_duration: float = 0.0
    _speak_through_robot: bool = False
    _text_subject: Subject[str] | None = None
    _agent_unsub = None
    _last_tool_speak_end: float = 0.0

    @staticmethod
    def _should_use_robot_speaker(connection: object | None, force_local_audio: bool) -> bool:
        """Thin wrapper over the shared use_robot_audio decision so the speaker
        and mic paths route identically. See requirements FR-D1..D4."""
        return use_robot_audio(connection, force_local_audio)

    @rpc
    def start(self) -> None:
        super().start()
        self._tts_node = OpenAITTSNode(speed=1.2, voice=Voice.ONYX)
        # Feed the TTS node from ONE long-lived text subject. consume_text spawns
        # a processing thread and a subscription, so calling it per utterance (as
        # before) leaked a thread and a subscriber on every speak() for the whole
        # session. Push text into this subject instead; never complete it.
        self._text_subject = Subject()
        self._tts_node.consume_text(self._text_subject)
        self._speak_through_robot = self._should_use_robot_speaker(
            self._connection, self.config.force_local_audio
        )
        if self._speak_through_robot:
            # Route TTS to the robot speaker only — skip the local audio device.
            self._robot_audio_sub = self._tts_node.emit_audio().subscribe(
                on_next=self._play_on_robot,
                on_error=lambda e: logger.error(f"Error in robot speaker audio stream: {e}"),
            )
        else:
            self._audio_output = SounddeviceAudioOutput(sample_rate=24000)
            self._audio_output.consume_audio(self._tts_node.emit_audio())

        if self.config.speak_agent_replies:
            try:
                self._agent_unsub = self.agent.subscribe(self._on_agent_message)
                logger.info("Auto-speak of agent text replies enabled")
            except Exception as e:
                logger.warning(f"Auto-speak disabled (no agent message stream wired): {e}")

    def _on_agent_message(self, msg: BaseMessage) -> None:
        """Voice the agent's text replies so speaking never depends on the LLM
        remembering to call the speak tool.

        Speaks AI messages with text content, including action announcements
        that accompany tool calls. Skipped: turns where the model called the
        speak tool itself (that call voices the turn), and text landing within
        a short cooldown of a finished speak-tool call (post-speak confirmation
        chatter, which would be spoken twice otherwise).
        """
        try:
            if getattr(msg, "type", None) != "ai":
                return
            for tool_call in getattr(msg, "tool_calls", None) or []:
                name = (
                    tool_call.get("name")
                    if isinstance(tool_call, dict)
                    else getattr(tool_call, "name", None)
                )
                if name == "speak":
                    return
            text = _message_text(msg).strip()
            if not text:
                return
            if time.monotonic() - self._last_tool_speak_end < _AUTO_SPEAK_COOLDOWN_SECONDS:
                return
            self._auto_speak_bg(_truncate_for_speech(text))
        except Exception as e:
            logger.error(f"Auto-speak failed: {e}")

    def _auto_speak_bg(self, text: str) -> None:
        # Same background-thread bookkeeping as speak(blocking=False), but
        # without touching _last_tool_speak_end: the cooldown must only track
        # the LLM's own speak-tool calls, not auto-spoken replies.
        thread = threading.Thread(
            target=self._speak_bg_no_mark, args=(text,), daemon=True, name="SpeakSkill-auto"
        )
        with self._bg_threads_lock:
            self._bg_threads.append(thread)
        thread.start()

    def _speak_bg_no_mark(self, text: str) -> None:
        try:
            self._speak_blocking(text)
        finally:
            with self._bg_threads_lock:
                self._bg_threads = [
                    t for t in self._bg_threads if t is not threading.current_thread()
                ]

    @rpc
    def stop(self) -> None:
        with self._bg_threads_lock:
            threads = list(self._bg_threads)
        for t in threads:
            t.join(timeout=DEFAULT_THREAD_JOIN_TIMEOUT)
        if self._agent_unsub is not None:
            self._agent_unsub()
            self._agent_unsub = None
        if self._robot_audio_sub is not None:
            self._robot_audio_sub.dispose()
            self._robot_audio_sub = None
        if self._text_subject is not None:
            self._text_subject.on_completed()
            self._text_subject = None
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
                logger.warning("Robot speaker selected but no robot connection is wired")

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

        SPEAK BY DEFAULT: whenever you reply to a person — answering a question,
        reporting what you see, confirming an action — say it with this tool.
        People near the robot cannot see your text; if you don't speak, they get
        no response at all. Do not wait to be asked to use the speaker.

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

        result = self._speak_blocking(text)
        # Marks the end of an LLM-initiated speak so auto-speak can suppress the
        # model's post-speak confirmation text (see _on_agent_message).
        self._last_tool_speak_end = time.monotonic()
        return result

    @skill
    def set_volume(self, level: int) -> str:
        """Set the loudness of the robot's speaker.

        Use when asked to speak louder/quieter or to change the volume.

        Args:
            level: Volume from 0 (mute) to 10 (maximum).
        """
        if self._connection is None:
            return "Error: no robot connection; volume control is unavailable"
        level = max(0, min(10, int(level)))
        self._connection.set_volume(level)
        return f"Speaker volume set to {level}/10"

    def _speak_bg(self, text: str) -> None:
        try:
            self._speak_blocking(text)
            self._last_tool_speak_end = time.monotonic()
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

            if self._speak_through_robot:
                return self._speak_blocking_robot(text)

            assert self._text_subject is not None
            audio_complete = threading.Event()

            def set_as_complete(_t: str) -> None:
                audio_complete.set()

            def set_as_complete_e(_e: Exception) -> None:
                audio_complete.set()

            subscription = self._tts_node.emit_text().subscribe(
                on_next=set_as_complete,
                on_error=set_as_complete_e,
            )

            self._text_subject.on_next(text)

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
        assert self._text_subject is not None

        # Drive completion off the audio dispatch, not emit_text: the TTS node
        # emits the text before the audio, so waiting on text could return
        # before _play_on_robot has handed the clip to the robot.
        self._robot_clip_dispatched.clear()
        self._text_subject.on_next(text)

        timeout = max(5, len(text) * 0.1)
        if not self._robot_clip_dispatched.wait(timeout=timeout):
            logger.warning(f"TTS timeout reached for: {text}")
            return f"Warning: TTS timeout while speaking: {text}"

        time.sleep(_robot_playback_wait(self._robot_clip_duration))
        return f"Spoke: {text}"
