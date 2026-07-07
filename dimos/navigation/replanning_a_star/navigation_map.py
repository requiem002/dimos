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

from threading import RLock

from dimos.core.global_config import GlobalConfig
from dimos.mapping.occupancy.gradient import GradientStrategy
from dimos.mapping.occupancy.operations import clear_disc
from dimos.mapping.occupancy.path_map import make_navigation_map
from dimos.msgs.geometry_msgs.Vector3 import VectorLike
from dimos.msgs.nav_msgs.OccupancyGrid import OccupancyGrid
from dimos.utils.logging_config import setup_logger

logger = setup_logger()


class NavigationMap:
    _global_config: GlobalConfig
    _gradient_strategy: GradientStrategy
    _binary: OccupancyGrid | None = None
    _lock: RLock

    def __init__(self, global_config: GlobalConfig, gradient_strategy: GradientStrategy) -> None:
        self._global_config = global_config
        self._gradient_strategy = gradient_strategy
        self._lock = RLock()

    def update(self, occupancy_grid: OccupancyGrid) -> None:
        with self._lock:
            self._binary = occupancy_grid

    @property
    def binary_costmap(self) -> OccupancyGrid:
        """
        Get the latest binary costmap received from the global costmap source.
        """

        with self._lock:
            if self._binary is None:
                raise ValueError("No current global costmap available")

            return self._binary

    @property
    def gradient_costmap(self) -> OccupancyGrid:
        return self.make_gradient_costmap()

    def make_gradient_costmap(
        self, robot_increase: float = 1.0, clear_footprint: VectorLike | None = None
    ) -> OccupancyGrid:
        """
        Get the latest navigation map created from inflating and applying a
        gradient to the binary costmap.

        Args:
            robot_increase: Multiplier on the robot width used for inflation.
            clear_footprint: If given (the robot's world position), occupied
                cells within the robot's rotation clearance of this point are
                cleared BEFORE inflation. The robot is standing there, so the
                space is traversable; without this, lidar self-hits from
                sit/dance/jump poses wall the start cell in and every plan
                fails with "No path found".
        """

        with self._lock:
            binary = self._binary
            if binary is None:
                raise ValueError("No current global costmap available")

        if clear_footprint is not None:
            binary, cleared = clear_disc(
                binary, clear_footprint, self._global_config.robot_rotation_diameter / 2
            )
            if cleared:
                logger.info(
                    f"Cleared {cleared} occupied cell(s) under the robot footprint before planning"
                )

        return make_navigation_map(
            binary,
            self._global_config.robot_width * robot_increase,
            strategy="simple",
            gradient_strategy=self._gradient_strategy,
        )
