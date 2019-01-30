# -*- coding: utf-8 -*-
"""1-Step Advantage Actor-Critic agent for episodic tasks in OpenAI Gym.

- Author: Curt Park
- Contact: curt.park@medipixel.io
"""

import argparse
import os
from typing import Tuple

import gym
import numpy as np
import torch
import torch.nn.functional as F
import torch.optim as optim
import wandb

from algorithms.a2c.model import ActorCritic
from algorithms.abstract_agent import AbstractAgent

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


# hyper parameters
hyper_params = {"GAMMA": 0.99, "STD": 1.0}


class Agent(AbstractAgent):
    """1-Step Advantage Actor-Critic interacting with environment.

    Attributes:
        model (nn.Module): policy gradient model to select actions
        optimizer (Optimizer): optimizer for training

    """

    def __init__(self, env: gym.Env, args: argparse.Namespace):
        """Initialization.

        Args:
            env (gym.Env): openAI Gym environment with discrete action space
            args (argparse.Namespace): arguments including hyperparameters and training settings

        """
        AbstractAgent.__init__(self, env, args)

        self.log_prob = torch.zeros((1,))
        self.predicted_value = torch.zeros((1,))

        # create a model
        self.model = ActorCritic(
            hyper_params["STD"], self.state_dim, self.action_dim
        ).to(device)

        # create optimizer
        self.optimizer = optim.Adam(self.model.parameters())

        if args.load_from is not None and os.path.exists(args.load_from):
            self.load_params(args.load_from)

    def select_action(self, state: np.ndarray) -> torch.Tensor:
        """Select an action from the input space."""
        state = torch.FloatTensor(state).to(device)
        selected_action, predicted_value, dist = self.model(state)

        self.log_prob = dist.log_prob(selected_action).sum()
        self.predicted_value = predicted_value

        return selected_action

    def step(self, action: torch.Tensor) -> Tuple[np.ndarray, np.float64, bool]:
        """Take an action and return the response of the env."""
        action = action.detach().cpu().numpy()
        next_state, reward, done, _ = self.env.step(action)

        return (next_state, reward, done)

    def update_model(
        self, experience: Tuple[torch.Tensor, torch.Tensor, torch.Tensor]
    ) -> torch.Tensor:
        reward, next_state, done = experience
        next_state = torch.FloatTensor(next_state).to(device)

        # Q_t   = r + gamma * V(s_{t+1})  if state != Terminal
        #       = r                       otherwise
        mask = 1 - done
        next_value = self.model.critic(next_state).detach()
        q_value = reward + hyper_params["GAMMA"] * next_value * mask
        q_value = q_value.to(device)

        # advantage = Q_t - V(s_t)
        advantage = q_value - self.predicted_value

        # calculate loss at the current step
        policy_loss = -advantage.detach() * self.log_prob  # adv. is not backpropagated
        value_loss = F.mse_loss(self.predicted_value, q_value.detach())
        loss = policy_loss + value_loss

        # train
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()

        return loss.data

    def load_params(self, path: str):
        """Load model and optimizer parameters."""
        if not os.path.exists(path):
            print("[INFO] The input path does not exist. ->", path)
            return

        params = torch.load(path)
        self.model.load_state_dict(params["model_state_dict"])
        self.optimizer.load_state_dict(params["optim_state_dict"])
        print("[INFO] Loaded the model and optimizer from", path)

    def save_params(self, n_episode: int):
        """Save model and optimizer parameters."""
        params = {
            "model_state_dict": self.model.state_dict(),
            "optim_state_dict": self.optimizer.state_dict(),
        }

        AbstractAgent.save_params(self, self.args.algo, params, n_episode)

    def train(self):
        """Train the agent."""
        # logger
        if self.args.log:
            wandb.init()
            wandb.config.update(hyper_params)
            wandb.watch(self.model, log="parameters")

        for i_episode in range(1, self.args.episode_num + 1):
            state = self.env.reset()
            done = False
            score = 0
            loss_episode = list()

            while not done:
                if self.args.render and i_episode >= self.args.render_after:
                    self.env.render()

                action = self.select_action(state)
                next_state, reward, done = self.step(action)

                loss = self.update_model((reward, next_state, done))
                loss_episode.append(loss)

                state = next_state
                score += reward

            # logging
            avg_loss = np.array(loss_episode).mean()
            print(
                "[INFO] episode %d\ttotal score: %d\tloss: %f"
                % (i_episode, score, avg_loss)
            )

            if self.args.log:
                wandb.log({"score": score, "avg_loss": avg_loss})

            if i_episode % self.args.save_period == 0:
                self.save_params(i_episode)

        # termination
        self.env.close()
