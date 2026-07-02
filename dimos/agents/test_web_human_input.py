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

"""Non-hardware tests for WebInput's STT-source routing (requirements 7.1).

Covered here: the robot-mic-by-default routing decision shared with the speaker
(FR-D1..D4 via use_robot_audio), that the robot branch draws from the mic_audio
stream while the browser-fallback branch does not, and that WebInput's connection
dependency is an OPTIONAL spec ref so it resolves cleanly in a non-robot blueprint.

Not covered here (section 7.2, hardware-only): that the Go2 mic audio actually
transcribes, live STT latency, and coexistence with the speaker on one WebRTC
session. Those need the physical robot.
"""

from types import SimpleNamespace

import pytest
import reactivex as rx

from dimos.agents.web_human_input import WebInput
from dimos.robot.unitree.go2.connection_spec import GO2ConnectionSpec, use_robot_audio


@pytest.mark.parametrize(
    "connection, force_local, expected",
    [
        (object(), False, True),   # connection present, default -> robot mic
        (None, False, False),      # no connection -> browser fallback
        (object(), True, False),   # explicit force-local override
        (None, True, False),
    ],
)
def test_use_robot_audio_matrix(connection: object, force_local: bool, expected: bool) -> None:
    assert use_robot_audio(connection, force_local) is expected


class _FakeMic:
    """Stand-in for the mic_audio In[AudioEvent] stream."""

    def __init__(self) -> None:
        self.observed = False

    def observable(self) -> rx.Observable:  # type: ignore[type-arg]
        self.observed = True
        return rx.empty()


def _make_webinput(connection: object | None, force_local: bool) -> tuple[WebInput, _FakeMic]:
    wi = WebInput.__new__(WebInput)
    wi._connection = connection  # type: ignore[attr-defined]
    wi.config = SimpleNamespace(force_local_audio=force_local)  # type: ignore[assignment]
    mic = _FakeMic()
    wi.mic_audio = mic  # type: ignore[assignment]
    return wi, mic


def test_robot_branch_draws_from_mic_stream() -> None:
    wi, mic = _make_webinput(connection=object(), force_local=False)
    source = wi._stt_audio_source(rx.empty())
    assert mic.observed is True
    assert isinstance(source, rx.Observable)


def test_browser_fallback_does_not_touch_mic_stream() -> None:
    browser = rx.empty()
    for connection, force_local in [(None, False), (object(), True), (None, True)]:
        wi, mic = _make_webinput(connection=connection, force_local=force_local)
        source = wi._stt_audio_source(browser)
        assert mic.observed is False
        assert isinstance(source, rx.Observable)


def test_connection_dependency_is_optional_spec_ref() -> None:
    """WebInput resolves cleanly in a non-robot blueprint: its connection dep is
    an OPTIONAL spec ref, so autoconnect injects None when no provider exists."""
    from dimos.core.coordination.blueprints import BlueprintAtom

    atom = BlueprintAtom.create(WebInput, kwargs={})
    conn_refs = [r for r in atom.module_refs if getattr(r, "spec", None) is GO2ConnectionSpec]
    assert len(conn_refs) == 1
    assert conn_refs[0].optional is True


def test_mic_audio_is_declared_in_stream() -> None:
    from dimos.core.coordination.blueprints import BlueprintAtom
    from dimos.stream.audio.base import AudioEvent

    atom = BlueprintAtom.create(WebInput, kwargs={})
    mic_streams = [s for s in atom.streams if s.name == "mic_audio"]
    assert len(mic_streams) == 1
    assert mic_streams[0].type is AudioEvent
