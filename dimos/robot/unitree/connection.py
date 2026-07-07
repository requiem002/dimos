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

import asyncio
from dataclasses import dataclass
import functools
import json
import threading
import time
from typing import Any, TypeAlias, TypeVar

import numpy as np
from numpy.typing import NDArray
from reactivex import operators as ops
from reactivex.observable import Observable
from reactivex.subject import Subject
from unitree_webrtc_connect.constants import (
    RTC_TOPIC,
    SPORT_CMD,
    VUI_COLOR,
)
from unitree_webrtc_connect.webrtc_driver import (
    UnitreeWebRTCConnection as LegionConnection,
    WebRTCConnectionMethod,
)

from dimos.constants import DEFAULT_THREAD_JOIN_TIMEOUT
from dimos.core.resource import Resource
from dimos.msgs.geometry_msgs.Pose import Pose
from dimos.msgs.geometry_msgs.Transform import Transform
from dimos.msgs.geometry_msgs.Twist import Twist
from dimos.msgs.sensor_msgs.Image import Image, ImageFormat
from dimos.msgs.sensor_msgs.PointCloud2 import PointCloud2
from dimos.robot.unitree.type.lidar import (
    RawLidarMsg,
    pointcloud2_from_webrtc_lidar,
)
from dimos.robot.unitree.type.lowstate import LowStateMsg
from dimos.robot.unitree.type.odometry import Odometry
from dimos.stream.audio.base import AudioEvent
from dimos.types.timestamped import Timestamped
from dimos.utils.decorators.decorators import simple_mcache
from dimos.utils.logging_config import setup_logger
from dimos.utils.reactive import backpressure, callback_to_observable

VideoMessage: TypeAlias = NDArray[np.uint8]  # Shape: (height, width, 3)

logger = setup_logger()

# How long after the speaker track's last clip frame the mic stays gated,
# covering network + jitter-buffer + playout latency of the robot's speaker.
_ECHO_GATE_TAIL_SECONDS = 1.0


_T = TypeVar("_T", bound=Timestamped)


def time_is_now(x: _T) -> _T:
    x.ts = time.time()
    return x


@dataclass
class SerializableVideoFrame:
    """Pickleable wrapper for av.VideoFrame with all metadata"""

    data: np.ndarray
    pts: int | None = None
    time: float | None = None
    dts: int | None = None
    width: int | None = None
    height: int | None = None
    format: str | None = None

    @classmethod
    def from_av_frame(cls, frame):  # type: ignore[no-untyped-def]
        return cls(
            data=frame.to_ndarray(format="rgb24"),
            pts=frame.pts,
            time=frame.time,
            dts=frame.dts,
            width=frame.width,
            height=frame.height,
            format=frame.format.name if hasattr(frame, "format") and frame.format else None,
        )

    def to_ndarray(self, format=None):  # type: ignore[no-untyped-def]
        return self.data


@dataclass
class SerializableAudioFrame:
    """Pickleable wrapper for one av.AudioFrame from the robot mic.

    The Go2 mic delivers 48 kHz stereo int16 frames (~960 samples each). We pull
    the interleaved samples out immediately in the callback so nothing downstream
    holds a live aiortc frame.
    """

    data: np.ndarray  # int16, interleaved
    sample_rate: int
    channels: int

    @classmethod
    def from_av_frame(cls, frame):  # type: ignore[no-untyped-def]
        layout = getattr(frame, "layout", None)
        channels = len(layout.channels) if layout and layout.channels else 2
        sample_rate = getattr(frame, "sample_rate", None) or 48000
        # frombuffer yields a read-only view; copy so downstream owns writable memory.
        data = np.frombuffer(frame.to_ndarray().tobytes(), dtype=np.int16).copy()
        return cls(data=data, sample_rate=sample_rate, channels=channels)


class UnitreeWebRTCConnection(Resource):
    _SPORT_API_ID_RAGEMODE: int = 2059

    def __init__(self, ip: str, mode: str = "ai", aes_128_key: str | None = None) -> None:
        self.ip = ip
        self.mode = mode
        self.stop_timer: threading.Timer | None = None
        self.cmd_vel_timeout = 0.2
        # Persistent outbound audio track for the robot speaker; attached lazily
        # on the first play_audio_track() call and reused for every utterance.
        self._speaker_track: Any = None
        # Per-device AES-128 key for new Unitree firmware (data2=3 handshake); omitted when unset.
        self.conn = LegionConnection(
            WebRTCConnectionMethod.LocalSTA, ip=self.ip, aes_128_key=aes_128_key
        )
        self.connect()

    def connect(self) -> None:
        self.loop = asyncio.new_event_loop()

        async def async_connect() -> None:
            await self.conn.connect()
            await self.conn.datachannel.disableTrafficSaving(True)

            self.conn.datachannel.set_decoder(decoder_type="native")

            await self.conn.datachannel.pub_sub.publish_request_new(
                RTC_TOPIC["MOTION_SWITCHER"], {"api_id": 1002, "parameter": {"name": self.mode}}
            )

        def start_background_loop() -> None:
            asyncio.set_event_loop(self.loop)
            self.loop.run_forever()

        self.thread = threading.Thread(target=start_background_loop, daemon=True)
        self.thread.start()

        # Blocks until connected; re-raises connect failures (e.g. missing AES key).
        try:
            asyncio.run_coroutine_threadsafe(async_connect(), self.loop).result()
        except Exception:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=DEFAULT_THREAD_JOIN_TIMEOUT)
            raise

    def start(self) -> None:
        pass

    def stop(self) -> None:
        # Cancel timer
        if self.stop_timer:
            self.stop_timer.cancel()
            self.stop_timer = None

        async def async_disconnect() -> None:
            try:
                # Send stop command directly since we're already in the event loop.
                self.conn.datachannel.pub_sub.publish_without_callback(
                    RTC_TOPIC["WIRELESS_CONTROLLER"],
                    data={"lx": 0, "ly": 0, "rx": 0, "ry": 0},
                )
                await self.conn.disconnect()
            except Exception:
                pass

        if self.loop.is_running():
            asyncio.run_coroutine_threadsafe(async_disconnect(), self.loop)

            self.loop.call_soon_threadsafe(self.loop.stop)

        if self.thread.is_alive():
            self.thread.join(timeout=DEFAULT_THREAD_JOIN_TIMEOUT)

    def move(self, twist: Twist, duration: float = 0.0) -> bool:
        """Send movement command to the robot using Twist commands.

        Args:
            twist: Twist message with linear and angular velocities
            duration: How long to move (seconds). If 0, command is continuous

        Returns:
            bool: True if command was sent successfully
        """
        x, y, yaw = twist.linear.x, twist.linear.y, twist.angular.z

        # WebRTC coordinate mapping:
        # x - Positive right, negative left
        # y - positive forward, negative backwards
        # yaw - Positive rotate right, negative rotate left
        async def async_move() -> None:
            self.conn.datachannel.pub_sub.publish_without_callback(
                RTC_TOPIC["WIRELESS_CONTROLLER"],
                data={"lx": -y, "ly": x, "rx": -yaw, "ry": 0},
            )

        async def async_move_duration() -> None:
            """Send movement commands continuously for the specified duration."""
            start_time = time.time()
            sleep_time = 0.01

            while time.time() - start_time < duration:
                await async_move()
                await asyncio.sleep(sleep_time)

        # Cancel existing timer and start a new one
        if self.stop_timer:
            self.stop_timer.cancel()

        # Auto-stop after 0.5 seconds if no new commands
        self.stop_timer = threading.Timer(self.cmd_vel_timeout, self.stop_movement)
        self.stop_timer.daemon = True
        self.stop_timer.start()

        try:
            if duration > 0:
                # Send continuous move commands for the duration
                future = asyncio.run_coroutine_threadsafe(async_move_duration(), self.loop)
                future.result()
                # Stop after duration
                self.stop_movement()
            else:
                # Single command for continuous movement
                future = asyncio.run_coroutine_threadsafe(async_move(), self.loop)
                future.result()
            return True
        except Exception as e:
            logger.warning("Failed to send movement command: %s", e)
            return False

    def play_audio_track(self, audio_path: str) -> None:
        """Play an audio file through the robot's onboard speaker.

        Routes the file over the EXISTING WebRTC connection (no second
        connection, no renegotiation) by feeding it to a persistent audio track
        on the pre-negotiated sendrecv audio sender. Follows the same
        run_coroutine_threadsafe pattern as move(): the work is scheduled onto
        the connection's background event loop. Returns as soon as the clip has
        been handed to the track — it does NOT block for the clip's duration, so
        concurrent robot commands (move, etc.) are not stalled and long clips
        cannot trip RPC timeouts.
        """
        from dimos.robot.unitree.robot_speaker_track import (
            RobotSpeakerTrack,
            decode_clip_to_pcm,
        )

        # Decode + resample to the wire format HERE, on the calling thread, so
        # the event loop only ever handles ready-made 48 kHz frames (see
        # RobotSpeakerTrack docstring for why the format must be exact).
        try:
            pcm = decode_clip_to_pcm(audio_path)
        except Exception as e:
            logger.warning("Failed to decode audio clip %s: %s", audio_path, e)
            return

        async def async_play() -> None:
            if self._speaker_track is None:
                audio_sender = next(
                    (s for s in self.conn.pc.getSenders() if s.kind == "audio"), None
                )
                if audio_sender is None:
                    logger.warning("No audio sender on WebRTC connection; cannot play audio")
                    return
                track = RobotSpeakerTrack()
                # replaceTrack (vs addTrack) is renegotiation-free and reuses the
                # transceiver the library pre-negotiated as sendrecv at connect.
                audio_sender.replaceTrack(track)
                self._speaker_track = track
            self._speaker_track.play_pcm(pcm)

        try:
            asyncio.run_coroutine_threadsafe(async_play(), self.loop).result()
        except Exception as e:
            logger.warning("Failed to play audio on robot speaker: %s", e)

    def set_volume(self, level: int) -> None:
        """Set the robot's speaker volume, 0 (mute) to 10 (max), via the VUI
        service on the datachannel (api_id 1003 — the same call the reference
        library's vui.py example uses)."""
        level = max(0, min(10, int(level)))

        async def async_set_volume() -> None:
            await self.conn.datachannel.pub_sub.publish_request_new(
                RTC_TOPIC["VUI"], {"api_id": 1003, "parameter": {"volume": level}}
            )

        try:
            asyncio.run_coroutine_threadsafe(async_set_volume(), self.loop).result()
        except Exception as e:
            logger.warning("Failed to set robot speaker volume: %s", e)

    # Generic conversion of unitree subscription to Subject (used for all subs)
    def unitree_sub_stream(self, topic_name: str):  # type: ignore[no-untyped-def]
        def subscribe_in_thread(cb) -> None:  # type: ignore[no-untyped-def]
            # Run the subscription in the background thread that has the event loop
            def run_subscription() -> None:
                self.conn.datachannel.pub_sub.subscribe(topic_name, cb)

            # Use call_soon_threadsafe to run in the background thread
            self.loop.call_soon_threadsafe(run_subscription)

        def unsubscribe_in_thread(cb) -> None:  # type: ignore[no-untyped-def]
            # Run the unsubscription in the background thread that has the event loop
            def run_unsubscription() -> None:
                self.conn.datachannel.pub_sub.unsubscribe(topic_name)

            # Use call_soon_threadsafe to run in the background thread
            self.loop.call_soon_threadsafe(run_unsubscription)

        return callback_to_observable(
            start=subscribe_in_thread,
            stop=unsubscribe_in_thread,
        )

    # Generic sync API call (we jump into the client thread)
    def publish_request(self, topic: str, data: dict[Any, Any]) -> Any:
        future = asyncio.run_coroutine_threadsafe(
            self.conn.datachannel.pub_sub.publish_request_new(topic, data), self.loop
        )
        return future.result()

    @simple_mcache
    def raw_lidar_stream(self) -> Observable[RawLidarMsg]:
        return backpressure(self.unitree_sub_stream(RTC_TOPIC["ULIDAR_ARRAY"]))

    @simple_mcache
    def raw_odom_stream(self) -> Observable[Pose]:
        return backpressure(self.unitree_sub_stream(RTC_TOPIC["ROBOTODOM"]))

    @simple_mcache
    def lidar_stream(self) -> Observable[PointCloud2]:
        return backpressure(
            self.raw_lidar_stream().pipe(
                ops.map(pointcloud2_from_webrtc_lidar),
                ops.map(time_is_now),
                # repair_stale_ts(),
            )
        )

    @simple_mcache
    def tf_stream(self) -> Observable[Transform]:
        base_link = functools.partial(Transform.from_pose, "base_link")
        return backpressure(self.odom_stream().pipe(ops.map(base_link)))

    @simple_mcache
    def odom_stream(self) -> Observable[Pose]:
        return backpressure(
            self.raw_odom_stream().pipe(
                ops.map(
                    Odometry.from_msg,
                ),
                ops.map(time_is_now),
            )
        )

    @simple_mcache
    def video_stream(self) -> Observable[Image]:
        return backpressure(
            self.raw_video_stream().pipe(
                ops.filter(lambda frame: frame is not None),
                ops.map(
                    lambda frame: Image.from_numpy(
                        # np.ascontiguousarray(frame.to_ndarray("rgb24")),
                        frame.to_ndarray(format="rgb24"),  # type: ignore[attr-defined]
                        format=ImageFormat.RGB,  # Frame is RGB24, not BGR
                        frame_id="camera_optical",
                    ),
                ),
                ops.map(time_is_now),
            )
        )

    @simple_mcache
    def raw_audio_stream(self) -> Observable[SerializableAudioFrame]:
        """Mic frames off the EXISTING WebRTC connection.

        Mirrors raw_video_stream, but the library's audio channel invokes the
        registered callback with each decoded frame (it runs the recv loop for
        us), so there is no self-driven recv loop here. Enabling/disabling the
        mic is scheduled onto the persistent loop, exactly like the video
        channel switch.

        Half-duplex echo gate: frames are dropped while the speaker track is
        playing a clip (plus a short tail for playout latency). The Go2's mic
        picks up its own speaker loudly enough to trigger the STT voice gate,
        so without this the robot transcribes its own TTS and answers itself.
        """
        subject: Subject[SerializableAudioFrame] = Subject()

        async def accept_frame(frame) -> None:  # type: ignore[no-untyped-def]
            speaker = self._speaker_track
            if speaker is not None and speaker.recently_active(_ECHO_GATE_TAIL_SECONDS):
                return
            subject.on_next(SerializableAudioFrame.from_av_frame(frame))

        self.conn.audio.add_track_callback(accept_frame)

        def switch_audio_channel() -> None:
            self.conn.audio.switchAudioChannel(True)

        self.loop.call_soon_threadsafe(switch_audio_channel)

        def stop() -> None:
            try:
                self.conn.audio.track_callbacks.remove(accept_frame)
            except ValueError:
                pass

            def switch_audio_channel_off() -> None:
                self.conn.audio.switchAudioChannel(False)

            self.loop.call_soon_threadsafe(switch_audio_channel_off)

        return subject.pipe(ops.finally_action(stop))

    @simple_mcache
    def audio_stream(self) -> Observable[AudioEvent]:
        """Robot mic as native 48 kHz stereo AudioEvents.

        Consumers (STT) resample to their own rate — the connection stays
        format-agnostic, mirroring how it publishes a generic Image and lets
        perception adapt.
        """

        def to_event(payload: SerializableAudioFrame) -> AudioEvent:
            data = payload.data
            if payload.channels > 1:
                data = data.reshape(-1, payload.channels)
            return AudioEvent(
                data=data,
                sample_rate=payload.sample_rate,
                timestamp=time.time(),
                channels=payload.channels,
            )

        return backpressure(self.raw_audio_stream().pipe(ops.map(to_event)))

    @simple_mcache
    def lowstate_stream(self) -> Observable[LowStateMsg]:
        return backpressure(self.unitree_sub_stream(RTC_TOPIC["LOW_STATE"]))

    def standup(self) -> bool:
        return bool(self.publish_request(RTC_TOPIC["SPORT_MOD"], {"api_id": SPORT_CMD["StandUp"]}))

    def balance_stand(self) -> bool:
        """Activate BalanceStand mode — enables WIRELESS_CONTROLLER joystick commands."""
        return bool(
            self.publish_request(RTC_TOPIC["SPORT_MOD"], {"api_id": SPORT_CMD["BalanceStand"]})
        )

    def set_obstacle_avoidance(self, enabled: bool = True) -> None:
        self.publish_request(
            RTC_TOPIC["OBSTACLES_AVOID"],
            {"api_id": 1001, "parameter": {"enable": int(enabled)}},
        )

    def set_motion_mode(self, name: str) -> None:
        """Select the top-level motion controller via the motion switcher.

        mcf is the AI/sport controller that traverses stairs. normal is basic.
        """
        # api_id 1001 = CheckMode, 1002 = SelectMode, param {"name": <mode>}.
        current = None
        try:
            resp = self.publish_request(RTC_TOPIC["MOTION_SWITCHER"], {"api_id": 1001})
            current = json.loads(resp["data"]["data"]).get("name")
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("Motion mode check failed: %s", e)
        if current == name:
            return
        self.publish_request(
            RTC_TOPIC["MOTION_SWITCHER"],
            {"api_id": 1002, "parameter": {"name": name}},
        )
        time.sleep(5)

    def free_walk(self) -> bool:
        """Activate FreeWalk locomotion mode — enables walking and velocity commands."""
        return bool(self.publish_request(RTC_TOPIC["SPORT_MOD"], {"api_id": SPORT_CMD["FreeWalk"]}))

    def set_rage_mode(self, enable: bool) -> bool:
        """Toggle Rage Mode (api 2059) over WebRTC, both directions.

        BalanceStand → 2059 {data:enable} → SwitchJoystick(enable). When on,
        normal move() twists drive at the ~2.5 m/s rage envelope.
        """
        # Re-establish BalanceStand before toggling (notes: always BalanceStand
        # before flipping Rage).
        if not self.balance_stand():
            logger.warning("balance_stand() failed before rage toggle — proceeding")
        time.sleep(0.3)

        rage_ok = bool(
            self.publish_request(
                RTC_TOPIC["SPORT_MOD"],
                {"api_id": self._SPORT_API_ID_RAGEMODE, "parameter": {"data": enable}},
            )
        )
        if not rage_ok:
            return False

        if enable:
            time.sleep(2.0)  # let FsmRageMode transition settle
        joystick_ok = bool(
            self.publish_request(
                RTC_TOPIC["SPORT_MOD"],
                {"api_id": SPORT_CMD["SwitchJoystick"], "parameter": {"data": enable}},
            )
        )
        if not joystick_ok:
            logger.warning("SwitchJoystick failed after rage toggle", enabled=enable)
        return joystick_ok

    def liedown(self) -> bool:
        return bool(
            self.publish_request(RTC_TOPIC["SPORT_MOD"], {"api_id": SPORT_CMD["StandDown"]})
        )

    async def handstand(self):  # type: ignore[no-untyped-def]
        return self.publish_request(
            RTC_TOPIC["SPORT_MOD"],
            {"api_id": SPORT_CMD["Standup"], "parameter": {"data": True}},
        )

    def color(self, color: VUI_COLOR = VUI_COLOR.RED, colortime: int = 60) -> bool:
        return self.publish_request(  # type: ignore[no-any-return]
            RTC_TOPIC["VUI"],
            {
                "api_id": 1001,
                "parameter": {
                    "color": color,
                    "time": colortime,
                },
            },
        )

    @simple_mcache
    def raw_video_stream(self) -> Observable[VideoMessage]:
        subject: Subject[VideoMessage] = Subject()
        stop_event = threading.Event()

        from aiortc import MediaStreamTrack

        async def accept_track(track: MediaStreamTrack) -> None:
            while True:
                if stop_event.is_set():
                    return
                frame = await track.recv()
                serializable_frame = SerializableVideoFrame.from_av_frame(frame)  # type: ignore[no-untyped-call]
                subject.on_next(serializable_frame)

        self.conn.video.add_track_callback(accept_track)

        # Run the video channel switching in the background thread
        def switch_video_channel() -> None:
            self.conn.video.switchVideoChannel(True)

        self.loop.call_soon_threadsafe(switch_video_channel)

        def stop() -> None:
            stop_event.set()  # Signal the loop to stop
            self.conn.video.track_callbacks.remove(accept_track)

            # Run the video channel switching off in the background thread
            def switch_video_channel_off() -> None:
                self.conn.video.switchVideoChannel(False)

            self.loop.call_soon_threadsafe(switch_video_channel_off)

        return subject.pipe(ops.finally_action(stop))

    def get_video_stream(self, fps: int = 30) -> Observable[Image]:
        """Get the video stream from the robot's camera.

        Implements the AbstractRobot interface method.

        Args:
            fps: Frames per second. This parameter is included for API compatibility,
                 but doesn't affect the actual frame rate which is determined by the camera.

        Returns:
            Observable: An observable stream of video frames or None if video is not available.
        """
        return self.video_stream()

    def stop_movement(self) -> None:
        """Cancel the auto-stop timer (used by move() for continuous commands)."""
        if self.stop_timer:
            self.stop_timer.cancel()
            self.stop_timer = None

    def disconnect(self) -> None:
        """Disconnect from the robot and clean up resources."""
        # Cancel timer
        if self.stop_timer:
            self.stop_timer.cancel()
            self.stop_timer = None

        if hasattr(self, "conn"):

            async def async_disconnect() -> None:
                try:
                    await self.conn.disconnect()
                except:
                    pass

            if hasattr(self, "loop") and self.loop.is_running():
                asyncio.run_coroutine_threadsafe(async_disconnect(), self.loop)

        if hasattr(self, "loop") and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)

        if hasattr(self, "thread") and self.thread.is_alive():
            self.thread.join(timeout=DEFAULT_THREAD_JOIN_TIMEOUT)
