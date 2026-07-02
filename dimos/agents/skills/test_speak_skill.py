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

"""Non-hardware tests for SpeakSkill's robot-audio routing (requirements 7.1).

Covered here: the robot-by-default routing decision (FR-D1..D4), the
duration-based blocking margin (DR-S3), the temp-WAV + connection dispatch path
(FR-S6), and that SpeakSkill's connection dependency is an OPTIONAL spec ref so
it resolves cleanly in a non-robot blueprint (no provider -> None).

Not covered here (section 7.2, hardware-only): that the dog actually speaks,
repeated utterances over one session, and audio timing on the wire.
"""

import os

import numpy as np
import pytest

from dimos.agents.skills import speak_skill
from dimos.agents.skills.speak_skill import (
    _ROBOT_PLAYBACK_MARGIN_FACTOR,
    _ROBOT_PLAYBACK_MARGIN_SECONDS,
    SpeakSkill,
    _robot_playback_wait,
)


@pytest.mark.parametrize(
    "connection, force_local, expected",
    [
        (object(), False, True),   # connection present, default -> robot (FR-D1/D2)
        (None, False, False),      # no connection -> local fallback (FR-D3)
        (object(), True, False),   # explicit force-local override (FR-D4)
        (None, True, False),
    ],
)
def test_routing_decision(connection: object, force_local: bool, expected: bool) -> None:
    assert SpeakSkill._should_use_robot_speaker(connection, force_local) is expected


def test_playback_wait_applies_margin() -> None:
    assert _robot_playback_wait(0.0) == _ROBOT_PLAYBACK_MARGIN_SECONDS
    for duration in (0.5, 1.0, 3.7):
        expected = duration * _ROBOT_PLAYBACK_MARGIN_FACTOR + _ROBOT_PLAYBACK_MARGIN_SECONDS
        assert _robot_playback_wait(duration) == pytest.approx(expected)


def test_playback_wait_is_strictly_increasing() -> None:
    waits = [_robot_playback_wait(d) for d in (0.0, 0.1, 1.0, 5.0)]
    assert waits == sorted(waits)
    assert len(set(waits)) == len(waits)


class _FakeConnection:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def play_audio_track(self, audio_path: str) -> None:
        self.paths.append(audio_path)


class _Captured:
    """Record what _play_on_robot wrote so the test can assert and clean up."""

    def __init__(self) -> None:
        self.path: str | None = None


def _make_skill(connection: object | None) -> SpeakSkill:
    skill = SpeakSkill.__new__(SpeakSkill)
    skill._connection = connection  # type: ignore[attr-defined]
    skill._robot_clip_dispatched = __import__("threading").Event()
    skill._robot_clip_duration = 0.0
    return skill


def test_play_on_robot_writes_wav_and_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    # Don't leave a real cleanup Timer running after the test.
    monkeypatch.setattr(speak_skill.threading, "Timer", lambda *a, **k: _NoopTimer())

    conn = _FakeConnection()
    skill = _make_skill(conn)

    sample_rate = 24000
    data = np.zeros(sample_rate, dtype=np.float32)  # 1.0 s of audio
    event = type("AudioEvent", (), {"data": data, "sample_rate": sample_rate})()

    try:
        skill._play_on_robot(event)
        assert skill._robot_clip_dispatched.is_set()
        assert skill._robot_clip_duration == pytest.approx(1.0)
        assert len(conn.paths) == 1
        written = conn.paths[0]
        assert written.endswith(".wav") and os.path.exists(written)
    finally:
        for p in conn.paths:
            try:
                os.remove(p)
            except OSError:
                pass


def test_play_on_robot_without_connection_still_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(speak_skill.threading, "Timer", lambda *a, **k: _NoopTimer())
    skill = _make_skill(None)
    data = np.zeros(16000, dtype=np.float32)
    event = type("AudioEvent", (), {"data": data, "sample_rate": 16000})()
    skill._play_on_robot(event)  # must not raise
    assert skill._robot_clip_dispatched.is_set()


class _NoopTimer:
    def start(self) -> None:
        pass


def test_connection_dependency_is_optional_spec_ref() -> None:
    """SpeakSkill resolves cleanly in a non-robot blueprint: its connection dep
    is an OPTIONAL spec ref, so autoconnect injects None when no provider exists
    rather than erroring (requirements 7.1)."""
    from dimos.core.coordination.blueprints import BlueprintAtom
    from dimos.robot.unitree.go2.connection_spec import GO2ConnectionSpec

    atom = BlueprintAtom.create(SpeakSkill, kwargs={})
    conn_refs = [r for r in atom.module_refs if getattr(r, "spec", None) is GO2ConnectionSpec]
    assert len(conn_refs) == 1
    assert conn_refs[0].optional is True
