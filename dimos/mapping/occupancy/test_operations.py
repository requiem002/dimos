#!/usr/bin/env python3
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


import cv2
import numpy as np

from dimos.mapping.occupancy.operations import clear_disc, overlay_occupied, smooth_occupied
from dimos.mapping.occupancy.visualizations import visualize_occupancy_grid
from dimos.msgs.geometry_msgs.Vector3 import Vector3
from dimos.msgs.nav_msgs.OccupancyGrid import CostValues, OccupancyGrid
from dimos.utils.data import get_data


def test_smooth_occupied(occupancy) -> None:
    expected = cv2.imread(get_data("smooth_occupied.png"), cv2.IMREAD_COLOR)

    result = visualize_occupancy_grid(smooth_occupied(occupancy), "rainbow")

    np.testing.assert_array_equal(result.data, expected)


def test_overlay_occupied(occupancy) -> None:
    expected = cv2.imread(get_data("overlay_occupied.png"), cv2.IMREAD_COLOR)
    overlay = occupancy.copy()
    overlay.grid[50:100, 50:100] = 100

    result = visualize_occupancy_grid(overlay_occupied(occupancy, overlay), "rainbow")

    np.testing.assert_array_equal(result.data, expected)


def test_clear_disc_frees_footprint_and_reports_count() -> None:
    grid = np.zeros((20, 20), dtype=np.int8)
    grid[10, 10] = 100  # under the robot (lidar self-hit)
    grid[10, 11] = 100  # within the disc
    grid[0, 0] = 100  # far away, must survive
    occupancy = OccupancyGrid(grid=grid, resolution=1.0)

    cleared_map, count = clear_disc(occupancy, Vector3(10.0, 10.0, 0.0), 2.0)

    assert count == 2
    assert cleared_map.grid[10, 10] == CostValues.FREE
    assert cleared_map.grid[10, 11] == CostValues.FREE
    assert cleared_map.grid[0, 0] == 100
    # Input grid must not be mutated (it is shared planner state).
    assert occupancy.grid[10, 10] == 100


def test_clear_disc_frees_unknown_cells_without_counting_them() -> None:
    grid = np.full((10, 10), int(CostValues.UNKNOWN), dtype=np.int8)
    occupancy = OccupancyGrid(grid=grid, resolution=1.0)

    cleared_map, count = clear_disc(occupancy, Vector3(5.0, 5.0, 0.0), 1.5)

    assert count == 0  # only previously OCCUPIED cells are counted
    assert cleared_map.grid[5, 5] == CostValues.FREE
