# Copyright 2026 Dimensional Inc.
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

from typing import Any, Protocol

from dimos.spec.utils import Spec


class GO2ConnectionSpec(Spec, Protocol):
    def publish_request(self, topic: str, data: dict[str, Any]) -> dict[Any, Any]: ...
    def play_audio_track(self, audio_path: str) -> None: ...
    def set_volume(self, level: int) -> None: ...


def use_robot_audio(connection: object | None, force_local_audio: bool) -> bool:
    """Single decision point for routing agent audio to the robot vs. local devices.

    Robot audio (speaker + mic) is the default whenever a connection is injected;
    fall back to local audio only when there is no robot connection (non-robot
    blueprints, unit tests) or when the operator forces local audio via config.
    Governs both SpeakSkill (speaker out) and WebInput (mic in) so the two
    directions stay in lock-step. See requirements FR-D1..D4.
    """
    return connection is not None and not force_local_audio
