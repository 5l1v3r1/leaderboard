#!/usr/bin/env python

# Copyright (c) 2018-2019 Intel Corporation
#
# This work is licensed under the terms of the MIT license.
# For a copy, see <https://opensource.org/licenses/MIT>.

"""
This module provides the Scenario and ScenarioManager implementations.
These must not be modified and are for reference only!
"""

from __future__ import print_function
import signal
import sys
import time
import threading

import py_trees
import carla

from srunner.scenariomanager.carla_data_provider import CarlaDataProvider
from srunner.scenariomanager.timer import GameTime, TimeOut
from srunner.scenariomanager.traffic_events import TrafficEventType


from leaderboard.autoagents.agent_wrapper import AgentWrapper
from leaderboard.utils.statistics_manager import *


class Scenario(object):

    """
    Basic scenario class. This class holds the behavior_tree describing the
    scenario and the test criteria.

    The user must not modify this class.

    Important parameters:
    - behavior: User defined scenario with py_tree
    - criteria_list: List of user defined test criteria with py_tree
    - timeout (default = 60s): Timeout of the scenario in seconds
    - terminate_on_failure: Terminate scenario on first failure
    """

    def __init__(self, behavior, criteria, name, timeout=60, terminate_on_failure=False):
        self.behavior = behavior
        self.test_criteria = criteria
        self.timeout = timeout
        self.name = name

        if self.test_criteria is not None and not isinstance(self.test_criteria, py_trees.composites.Parallel):
            # list of nodes
            for criterion in self.test_criteria:
                criterion.terminate_on_failure = terminate_on_failure

            # Create py_tree for test criteria
            self.criteria_tree = py_trees.composites.Parallel(name="Test Criteria")
            self.criteria_tree.add_children(self.test_criteria)
            self.criteria_tree.setup(timeout=1)
        else:
            self.criteria_tree = criteria

        # Create node for timeout
        self.timeout_node = TimeOut(self.timeout, name="TimeOut")

        # Create overall py_tree
        self.scenario_tree = py_trees.composites.Parallel(name, policy=py_trees.common.ParallelPolicy.SUCCESS_ON_ONE)
        if behavior is not None:
            self.scenario_tree.add_child(self.behavior)
        self.scenario_tree.add_child(self.timeout_node)
        if criteria is not None:
            self.scenario_tree.add_child(self.criteria_tree)
        self.scenario_tree.setup(timeout=1)

    def _extract_nodes_from_tree(self, tree):  # pylint: disable=no-self-use
        """
        Returns the list of all nodes from the given tree
        """
        node_list = [tree]
        more_nodes_exist = True
        while more_nodes_exist:
            more_nodes_exist = False
            for node in node_list:
                if node.children:
                    node_list.remove(node)
                    more_nodes_exist = True
                    for child in node.children:
                        node_list.append(child)

        if len(node_list) == 1 and isinstance(node_list[0], py_trees.composites.Parallel):
            return []

        return node_list

    def get_criteria(self):
        """
        Return the list of test criteria (all leave nodes)
        """
        criteria_list = self._extract_nodes_from_tree(self.criteria_tree)
        return criteria_list

    def terminate(self):
        """
        This function sets the status of all leaves in the scenario tree to INVALID
        """
        # Get list of all nodes in the tree
        node_list = self._extract_nodes_from_tree(self.scenario_tree)

        # Set status to INVALID
        for node in node_list:
            node.terminate(py_trees.common.Status.INVALID)


class ScenarioManager(object):

    """
    Basic scenario manager class. This class holds all functionality
    required to start, and analyze a scenario.

    The user must not modify this class.

    To use the ScenarioManager:
    1. Create an object via manager = ScenarioManager()
    2. Load a scenario via manager.load_scenario()
    3. Trigger the execution of the scenario manager.execute()
       This function is designed to explicitly control start and end of
       the scenario execution
    4. Trigger a result evaluation with manager.analyze()
    5. Cleanup with manager.stop_scenario()
    """

    def __init__(self, debug_mode=False, challenge_mode=False, track=None):
        """
        Init requires scenario as input
        """
        self.scenario = None
        self.scenario_tree = None
        self.scenario_class = None
        self.ego_vehicles = None
        self.other_actors = None

        self._debug_mode = debug_mode
        self._challenge_mode = challenge_mode
        self._track = track
        self._agent = None
        self._running = False
        self._timestamp_last_run = 0.0
        self._my_lock = threading.Lock()

        self.scenario_duration_system = 0.0
        self.scenario_duration_game = 0.0
        self.start_system_time = None
        self.end_system_time = None

        self.route_record = RouteRecord()

        # Register the scenario tick as callback for the CARLA world
        # Use the callback_id inside the signal handler to allow external interrupts
        signal.signal(signal.SIGINT, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """
        Terminate scenario ticking when receiving a signal interrupt
        """
        self._running = False

    def _reset(self):
        """
        Reset all parameters
        """
        self._running = False
        self._timestamp_last_run = 0.0
        self.scenario_duration_system = 0.0
        self.scenario_duration_game = 0.0
        self.start_system_time = None
        self.end_system_time = None
        self.route_record = RouteRecord()
        GameTime.restart()

    def load_scenario(self, scenario, agent=None):
        """
        Load a new scenario
        """
        self._reset()
        self._agent = AgentWrapper(agent, self._challenge_mode) if agent else None
        self.scenario_class = scenario
        self.scenario = scenario.scenario
        self.scenario_tree = self.scenario.scenario_tree
        self.ego_vehicles = scenario.ego_vehicles
        self.other_actors = scenario.other_actors

        CarlaDataProvider.register_actors(self.ego_vehicles)
        CarlaDataProvider.register_actors(self.other_actors)
        # To print the scenario tree uncomment the next line
        # py_trees.display.render_dot_tree(self.scenario_tree)

        if self._agent is not None:
            self._agent.setup_sensors(self.ego_vehicles[0], self._debug_mode, self._track)

    def run_scenario(self):
        """
        Trigger the start of the scenario and wait for it to finish/fail
        """
        print("ScenarioManager: Running scenario {}".format(self.scenario_tree.name))
        self.start_system_time = time.time()
        start_game_time = GameTime.get_time()

        self._running = True

        while self._running:
            self._tick_scenario(CarlaDataProvider.get_world().get_snapshot().timestamp)

        self.end_system_time = time.time()
        end_game_time = GameTime.get_time()

        self.scenario_duration_system = self.end_system_time - \
            self.start_system_time
        self.scenario_duration_game = end_game_time - start_game_time

        if self.scenario_tree.status == py_trees.common.Status.FAILURE:
            print("ScenarioManager: Terminated due to failure")

    def show_current_score(self):
        master_scenario = self.scenario

        target_reached = False
        score_penalty = 1.0
        score_route = 0.0

        for node in master_scenario.get_criteria():
            if node.list_traffic_events:
                # analyze all traffic events
                for event in node.list_traffic_events:
                    if event.get_type() == TrafficEventType.COLLISION_STATIC:
                        score_penalty *= PENALTY_COLLISION_STATIC
                        self.route_record.infractions['collisions_layout'].append(event.get_message())

                    elif event.get_type() == TrafficEventType.COLLISION_VEHICLE:
                        score_penalty *= PENALTY_COLLISION_VEHICLE
                        self.route_record.infractions['collisions_vehicle'].append(event.get_message())

                    elif event.get_type() == TrafficEventType.COLLISION_PEDESTRIAN:
                        score_penalty *= PENALTY_COLLISION_PEDESTRIAN
                        self.route_record.infractions['collisions_pedestrian'].append(event.get_message())

                    elif event.get_type() == TrafficEventType.TRAFFIC_LIGHT_INFRACTION:
                        score_penalty *= PENALTY_TRAFFIC_LIGHT
                        self.route_record.infractions['red_light'].append(event.get_message())

                    elif event.get_type() == TrafficEventType.WRONG_WAY_INFRACTION:
                        score_penalty *= PENALTY_WRONG_WAY
                        score_penalty *= math.pow(PENALTY_WRONG_WAY_PER_METER, event.get_dict()['distance'])
                        self.route_record.infractions['wrong_way'].append(event.get_message())

                    elif event.get_type() == TrafficEventType.ROUTE_DEVIATION:
                        self.route_record.infractions['route_dev'].append(event.get_message())

                    elif event.get_type() == TrafficEventType.ON_SIDEWALK_INFRACTION:
                        score_penalty *= PENALTY_SIDEWALK_INVASION
                        score_penalty *= math.pow(PENALTY_SIDEWALK_INVASION_PER_METER, event.get_dict()['distance'])
                        self.route_record.infractions['sidewalk_invasion'].append(event.get_message())

                    elif event.get_type() == TrafficEventType.OUTSIDE_LANE_INFRACTION:
                        score_penalty *= PENALTY_OUTSIDE_LANE_INVASION
                        score_penalty *= math.pow(PENALTY_OUTSIDE_LANE_PER_METER, event.get_dict()['distance'])
                        self.route_record.infractions['outside_driving_lanes'].append(event.get_message())

                    elif event.get_type() == TrafficEventType.STOP_INFRACTION:
                        score_penalty *= PENALTY_STOP
                        self.route_record.infractions['stop_infraction'].append(event.get_message())

                    elif event.get_type() == TrafficEventType.ROUTE_COMPLETED:
                        score_route = 100.0
                        target_reached = True
                    elif event.get_type() == TrafficEventType.ROUTE_COMPLETION:
                        if not target_reached:
                            if event.get_dict():
                                score_route = event.get_dict()['route_completed']
                            else:
                                score_route = 0

        # update route scores
        self.route_record.scores['score_route'] = score_route
        self.route_record.scores['score_penalty'] = score_penalty
        self.route_record.scores['score_composed'] = max(score_route*score_penalty, 0.0)

        print("[Agent score] [route={:.2f}] [penalty={:.2f}] [total={:.2f}]".format(self.route_record.scores['score_route'],
                                                                        self.route_record.scores['score_penalty'],
                                                                        self.route_record.scores['score_composed']
                                                                        ))

    def _tick_scenario(self, timestamp):
        """
        Run next tick of scenario
        This function is a callback for world.on_tick()

        Important:
        - It has to be ensured that the scenario has not yet completed/failed
          and that the time moved forward.
        - A thread lock should be used to avoid that the scenario tick is performed
          multiple times in parallel.
        """

        with self._my_lock:
            if self._timestamp_last_run < timestamp.elapsed_seconds:
                self._timestamp_last_run = timestamp.elapsed_seconds

                if self._debug_mode:
                    print("\n--------- Tick ---------\n")

                # Update game time and actor information
                GameTime.on_carla_tick(timestamp)
                CarlaDataProvider.on_carla_tick()

                if self._agent is not None:
                    ego_action = self._agent()

                # Tick scenario
                self.scenario_tree.tick_once()

                if self._debug_mode:
                    print("\n")
                    py_trees.display.print_ascii_tree(
                        self.scenario_tree, show_status=True)
                    sys.stdout.flush()

                if self.scenario_tree.status != py_trees.common.Status.RUNNING:
                    self._running = False

                if self._challenge_mode:

                    spectator = CarlaDataProvider.get_world().get_spectator()
                    ego_trans = self.ego_vehicles[0].get_transform()
                    spectator.set_transform(carla.Transform(ego_trans.location + carla.Location(z=50),
                                                                carla.Rotation(pitch=-90)))

                if self._agent is not None:
                    self.ego_vehicles[0].apply_control(ego_action)

                if True:
                    self.show_current_score()

        if self._agent:
            CarlaDataProvider.get_world().tick()

    def stop_scenario(self):
        """
        This function triggers a proper termination of a scenario
        """

        if self.scenario is not None:
            self.scenario.terminate()

        if self._agent is not None:
            self._agent.cleanup()
            self._agent = None

        CarlaDataProvider.cleanup()