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

"""Test that go2.connection.make_connection forwards aes_128_key.

The leaf (UnitreeWebRTCConnection.__init__) is covered in
dimos/robot/unitree/test_connection.py; this pins the go2-local routing.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from dimos.core.global_config import GlobalConfig
from dimos.robot.unitree.go2 import connection as go2_conn
from dimos.robot.unitree.go2.connection import ConnectionConfig


@pytest.fixture
def stub_webrtc(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace UnitreeWebRTCConnection in go2.connection so the webrtc branch
    runs without dialing out."""
    stub = MagicMock(name="UnitreeWebRTCConnection")
    monkeypatch.setattr(go2_conn, "UnitreeWebRTCConnection", stub)
    return stub


def test_make_connection_webrtc_forwards_aes_128_key(stub_webrtc: MagicMock) -> None:
    """Webrtc branch forwards aes_128_key as a kwarg to UnitreeWebRTCConnection."""
    cfg = SimpleNamespace(unitree_connection_type="webrtc")
    go2_conn.make_connection("192.168.123.161", cfg, aes_128_key="cafe" * 8)
    stub_webrtc.assert_called_once_with("192.168.123.161", aes_128_key="cafe" * 8)


def test_connection_config_aes_key_defaults_from_global_config() -> None:
    """ConnectionConfig.aes_128_key defaults from GlobalConfig.unitree_aes_128_key."""
    g = GlobalConfig(robot_ip="127.0.0.1", unitree_aes_128_key="dd" * 16)
    assert ConnectionConfig(g=g).aes_128_key == "dd" * 16


def test_go2connection_satisfies_go2connectionspec() -> None:
    """GO2Connection provides the play_audio_track method SpeakSkill calls over
    RPC, so it satisfies GO2ConnectionSpec both structurally and by signature."""
    from dimos.robot.unitree.go2.connection import GO2Connection
    from dimos.robot.unitree.go2.connection_spec import GO2ConnectionSpec
    from dimos.spec.utils import spec_annotation_compliance, spec_structural_compliance

    assert spec_structural_compliance(GO2Connection, GO2ConnectionSpec)
    assert spec_annotation_compliance(GO2Connection, GO2ConnectionSpec)


def test_audio_stream_is_in_the_connection_protocol() -> None:
    """The robot-mic stream is part of the connection contract every backend
    implements, mirroring video_stream (requirements FR-M1)."""
    from dimos.robot.unitree.go2.connection import Go2ConnectionProtocol

    assert hasattr(Go2ConnectionProtocol, "audio_stream")


def test_replay_audio_stream_is_empty_noop() -> None:
    """Replay/sim datasets carry no mic audio: audio_stream() emits nothing and
    does not error, so a robot-by-default WebInput degrades cleanly (VT-6)."""
    from dimos.robot.unitree.go2.connection import ReplayConnection

    conn = ReplayConnection(dataset="go2_short")
    emissions: list[object] = []
    conn.audio_stream().subscribe(on_next=emissions.append)
    assert emissions == []


def test_go2connection_declares_mic_audio_out_stream() -> None:
    """GO2Connection publishes the robot mic across the module boundary as an
    Out[AudioEvent], mirroring color_image (Out[Image])."""
    from dimos.core.stream import Out
    from dimos.robot.unitree.go2.connection import GO2Connection
    from dimos.stream.audio.base import AudioEvent

    ann = GO2Connection.__annotations__["mic_audio"]
    assert ann == Out[AudioEvent]


def test_microphone_config_defaults_on() -> None:
    from dimos.robot.unitree.go2.connection import ConnectionConfig

    assert ConnectionConfig.model_fields["microphone"].default is True
