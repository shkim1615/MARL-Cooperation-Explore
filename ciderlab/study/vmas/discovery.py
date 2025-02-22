#  Copyright (c) 2022-2024.
#  ProrokLab (https://www.proroklab.org/)
#  All rights reserved.

# 랜덤으로 생성되는 스팟을 라이다로 탐색하고 발견하면 해당 자리로 가는 것
# 서로 간의 충돌은 고려하지 않음

import typing
from typing import Callable, Dict, List

import torch
from torch import Tensor

from vmas import render_interactively
from vmas.simulator.core import Agent, Entity, Landmark, Sphere, World
from vmas.simulator.heuristic_policy import BaseHeuristicPolicy
from vmas.simulator.scenario import BaseScenario
from vmas.simulator.sensors import Lidar
from vmas.simulator.utils import Color, ScenarioUtils, X, Y

if typing.TYPE_CHECKING:
    from vmas.simulator.rendering import Geom


class Scenario(BaseScenario):
    def make_world(self, batch_dim: int, device: torch.device, **kwargs):
        self.n_agents = kwargs.pop("n_agents", 5)
        self.n_targets = kwargs.pop("n_targets", 7)
        self.x_semidim = kwargs.pop("x_semidim", 1)
        self.y_semidim = kwargs.pop("y_semidim", 1)
        self._min_dist_between_entities = kwargs.pop("min_dist_between_entities", 0.2)
        self._lidar_range = kwargs.pop("lidar_range", 0.35)
        self._covering_range = kwargs.pop("covering_range", 0.25)

        self.use_agent_lidar = kwargs.pop("use_agent_lidar", False)
        self.n_lidar_rays_entities = kwargs.pop("n_lidar_rays_entities", 15)
        self.n_lidar_rays_agents = kwargs.pop("n_lidar_rays_agents", 12)

        self._agents_per_target = kwargs.pop("agents_per_target", 2)
        self.targets_respawn = kwargs.pop("targets_respawn", True)
        self.shared_reward = kwargs.pop("shared_reward", False)

        self.agent_collision_penalty = kwargs.pop("agent_collision_penalty", 0)
        self.covering_rew_coeff = kwargs.pop("covering_rew_coeff", 1.0)
        self.time_penalty = kwargs.pop("time_penalty", 0)
        ScenarioUtils.check_kwargs_consumed(kwargs)

        # comms_range: 에이전트 간 통신 가능 거리
        self._comms_range = self._lidar_range
        self.min_collision_distance = 0.005
        self.agent_radius = 0.05
        self.target_radius = self.agent_radius

        self.viewer_zoom = 1
        self.target_color = Color.GREEN

        # Make world
        world = World(
            batch_dim,
            device,
            x_semidim=self.x_semidim,
            y_semidim=self.y_semidim,
            collision_force=500,
            substeps=2,
            drag=0.25,
        )

        # 에이전트랑 타겟이랑 섞인 엔터티에서 원하는 객체 종류만 골라냄
        # Add agents
        entity_filter_agents: Callable[[Entity], bool] = lambda e: e.name.startswith(
            "agent"
        )
        entity_filter_targets: Callable[[Entity], bool] = lambda e: e.name.startswith(
            "target"
        )
        for i in range(self.n_agents):
            # Constraint: all agents have same action range and multiplier
            agent = Agent(
                name=f"agent_{i}",
                collide=True,
                shape=Sphere(radius=self.agent_radius),
                sensors=(
                    [
                        Lidar(
                            world,
                            n_rays=self.n_lidar_rays_entities,
                            max_range=self._lidar_range,
                            entity_filter=entity_filter_targets,
                            render_color=Color.GREEN,
                        )
                    ]
                    + (
                        [
                            Lidar(
                                world,
                                angle_start=0.05,
                                angle_end=2 * torch.pi + 0.05,
                                n_rays=self.n_lidar_rays_agents,
                                max_range=self._lidar_range,
                                entity_filter=entity_filter_agents,
                                render_color=Color.BLUE,
                            )
                        ]
                        if self.use_agent_lidar
                        else []
                    )
                ),
            )
            agent.collision_rew = torch.zeros(batch_dim, device=device)
            agent.covering_reward = agent.collision_rew.clone()
            world.add_agent(agent)

        self._targets = []
        for i in range(self.n_targets):
            target = Landmark(
                name=f"target_{i}",
                collide=True,
                movable=False,
                shape=Sphere(radius=self.target_radius),
                color=self.target_color,
            )
            world.add_landmark(target)
            self._targets.append(target)

        self.covered_targets = torch.zeros(batch_dim, self.n_targets, device=device)
        self.shared_covering_rew = torch.zeros(batch_dim, device=device)

        return world

    def reset_world_at(self, env_index: int = None):
        placable_entities = self._targets[: self.n_targets] + self.world.agents
        if env_index is None:
            self.all_time_covered_targets = torch.full(
                (self.world.batch_dim, self.n_targets),
                False,
                device=self.world.device,
            )
        else:
            self.all_time_covered_targets[env_index] = False
        ScenarioUtils.spawn_entities_randomly(
            entities=placable_entities,
            world=self.world,
            env_index=env_index,
            min_dist_between_entities=self._min_dist_between_entities,
            x_bounds=(-self.world.x_semidim, self.world.x_semidim),
            y_bounds=(-self.world.y_semidim, self.world.y_semidim),
        )
        for target in self._targets[self.n_targets :]:
            target.set_pos(self.get_outside_pos(env_index), batch_index=env_index)

    def reward(self, agent: Agent):
        is_first = agent == self.world.agents[0]
        is_last = agent == self.world.agents[-1]

        if is_first:
            # 시간 패널티 보상 초기화
            self.time_rew = torch.full(
                (self.world.batch_dim,),
                self.time_penalty,
                device=self.world.device,
            )
            
            # 에이전트와 타겟의 위치 수집
            self.agents_pos = torch.stack(
                [a.state.pos for a in self.world.agents], dim=1
            )
            self.targets_pos = torch.stack([t.state.pos for t in self._targets], dim=1)
            self.agents_targets_dists = torch.cdist(self.agents_pos, self.targets_pos)
            self.agents_per_target = torch.sum(
                (self.agents_targets_dists < self._covering_range).type(torch.int),
                dim=1,
            )
            self.covered_targets = self.agents_per_target >= self._agents_per_target

            self.shared_covering_rew[:] = 0
            for a in self.world.agents:
                self.shared_covering_rew += self.agent_reward(a)
            self.shared_covering_rew[self.shared_covering_rew != 0] /= 2

        # Avoid collisions with each other
        agent.collision_rew[:] = 0
        for a in self.world.agents:
            if a != agent:
                agent.collision_rew[
                    self.world.get_distance(a, agent) < self.min_collision_distance
                ] += self.agent_collision_penalty

        if is_last:
            if self.targets_respawn:
                occupied_positions_agents = [self.agents_pos]
                for i, target in enumerate(self._targets):
                    occupied_positions_targets = [
                        o.state.pos.unsqueeze(1)
                        for o in self._targets
                        if o is not target
                    ]
                    occupied_positions = torch.cat(
                        occupied_positions_agents + occupied_positions_targets,
                        dim=1,
                    )
                    pos = ScenarioUtils.find_random_pos_for_entity(
                        occupied_positions,
                        env_index=None,
                        world=self.world,
                        min_dist_between_entities=self._min_dist_between_entities,
                        x_bounds=(-self.world.x_semidim, self.world.x_semidim),
                        y_bounds=(-self.world.y_semidim, self.world.y_semidim),
                    )

                    target.state.pos[self.covered_targets[:, i]] = pos[
                        self.covered_targets[:, i]
                    ].squeeze(1)
            else:
                self.all_time_covered_targets += self.covered_targets
                for i, target in enumerate(self._targets):
                    target.state.pos[self.covered_targets[:, i]] = self.get_outside_pos(
                        None
                    )[self.covered_targets[:, i]]
        covering_rew = (
            agent.covering_reward
            if not self.shared_reward
            else self.shared_covering_rew
        )

        return agent.collision_rew + covering_rew + self.time_rew

    def get_outside_pos(self, env_index):
        return torch.empty(
            (
                (1, self.world.dim_p)
                if env_index is not None
                else (self.world.batch_dim, self.world.dim_p)
            ),
            device=self.world.device,
        ).uniform_(-1000 * self.world.x_semidim, -10 * self.world.x_semidim)

    def agent_reward(self, agent):
        agent_index = self.world.agents.index(agent)

        agent.covering_reward[:] = 0
        targets_covered_by_agent = (
            self.agents_targets_dists[:, agent_index] < self._covering_range
        )
        num_covered_targets_covered_by_agent = (
            targets_covered_by_agent * self.covered_targets
        ).sum(dim=-1)
        agent.covering_reward += (
            num_covered_targets_covered_by_agent * self.covering_rew_coeff
        )
        return agent.covering_reward

    def observation(self, agent: Agent):
        lidar_1_measures = agent.sensors[0].measure()
        return torch.cat(
            [agent.state.pos, agent.state.vel, lidar_1_measures]
            + ([agent.sensors[1].measure()] if self.use_agent_lidar else []),
            dim=-1,
        )

    def info(self, agent: Agent) -> Dict[str, Tensor]:
        info = {
            "covering_reward": (
                agent.covering_reward
                if not self.shared_reward
                else self.shared_covering_rew
            ),
            "collision_rew": agent.collision_rew,
            "targets_covered": self.covered_targets.sum(-1),
        }
        return info

    def done(self):
        return self.all_time_covered_targets.all(dim=-1)

    def extra_render(self, env_index: int = 0) -> "List[Geom]":
        from vmas.simulator import rendering

        geoms: List[Geom] = []
        # Target ranges
        for target in self._targets:
            range_circle = rendering.make_circle(self._covering_range, filled=False)
            xform = rendering.Transform()
            xform.set_translation(*target.state.pos[env_index])
            range_circle.add_attr(xform)
            range_circle.set_color(*self.target_color.value)
            geoms.append(range_circle)
        # Communication lines
        for i, agent1 in enumerate(self.world.agents):
            for j, agent2 in enumerate(self.world.agents):
                if j <= i:
                    continue
                agent_dist = torch.linalg.vector_norm(
                    agent1.state.pos - agent2.state.pos, dim=-1
                )
                if agent_dist[env_index] <= self._comms_range:
                    color = Color.BLACK.value
                    line = rendering.Line(
                        (agent1.state.pos[env_index]),
                        (agent2.state.pos[env_index]),
                        width=1,
                    )
                    xform = rendering.Transform()
                    line.add_attr(xform)
                    line.set_color(*color)
                    geoms.append(line)

        return geoms


class HeuristicPolicy(BaseHeuristicPolicy):
    def compute_action(self, observation: torch.Tensor, u_range: float) -> torch.Tensor:
        assert self.continuous_actions

        # First calculate the closest point to a circle of radius circle_radius given the current position
        circle_origin = torch.zeros(1, 2, device=observation.device)
        circle_radius = 0.75
        current_pos = observation[:, :2]
        v = current_pos - circle_origin
        closest_point_on_circ = (
            circle_origin + v / torch.linalg.norm(v, dim=1).unsqueeze(1) * circle_radius
        )

        # calculate the normal vector of the vector from the origin of the circle to that closest point
        # on the circle. Adding this scaled normal vector to the other vector gives us a target point we
        # try to reach, thus resulting in a circular motion.
        closest_point_on_circ_normal = torch.stack(
            [closest_point_on_circ[:, Y], -closest_point_on_circ[:, X]], dim=1
        )
        closest_point_on_circ_normal /= torch.linalg.norm(
            closest_point_on_circ_normal, dim=1
        ).unsqueeze(1)
        closest_point_on_circ_normal *= 0.1
        des_pos = closest_point_on_circ + closest_point_on_circ_normal

        # Move towards targets within visibility range
        lidar_targets = observation[:, 4:19]
        target_visible = torch.any(lidar_targets < 0.3, dim=1)
        _, target_dir_index = torch.min(lidar_targets, dim=1)
        target_dir = target_dir_index / lidar_targets.shape[1] * 2 * torch.pi
        target_vec = torch.stack([torch.cos(target_dir), torch.sin(target_dir)], dim=1)
        des_pos_target = current_pos + target_vec * 0.1
        des_pos[target_visible] = des_pos_target[target_visible]

        if observation.shape[-1] > 19:
            # Move away from other agents within visibility range
            lidar_agents = observation[:, 19:31]
            agent_visible = torch.any(lidar_agents < 0.15, dim=1)
            _, agent_dir_index = torch.min(lidar_agents, dim=1)
            agent_dir = agent_dir_index / lidar_agents.shape[1] * 2 * torch.pi
            agent_vec = torch.stack([torch.cos(agent_dir), torch.sin(agent_dir)], dim=1)
            des_pos_agent = current_pos - agent_vec * 0.1
            des_pos[agent_visible] = des_pos_agent[agent_visible]

        action = torch.clamp(
            (des_pos - current_pos) * 10,
            min=-u_range,
            max=u_range,
        )

        return action


if __name__ == "__main__":
    # __file__을 "discovery"로 바꾸니까 정상 작동하긴 하는데....
    render_interactively(__file__, control_two_agents=True)
