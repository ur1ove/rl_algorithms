# -*- coding: utf-8 -*-
"""DDPGfD agent using demo agent for episodic tasks in OpenAI Gym.

- Author: Kh Kim
- Contact: kh.kim@medipixel.io
- Paper: https://arxiv.org/pdf/1509.02971.pdf
         https://arxiv.org/pdf/1511.05952.pdf
         https://arxiv.org/pdf/1707.08817.pdf
"""

import argparse
import os
import pickle
from typing import Tuple

import gym
import numpy as np
import torch
import wandb

import algorithms.common.helper_functions as common_utils
from algorithms.common.abstract.agent import AbstractAgent
from algorithms.common.buffer.priortized_replay_buffer import PrioritizedReplayBufferfD
from algorithms.common.noise import OUNoise

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


class Agent(AbstractAgent):
    """ActorCritic interacting with environment.

    Attributes:
        memory (PrioritizedReplayBuffer): replay memory
        noise (OUNoise): random noise for exploration
        actor (nn.Module): actor model to select actions
        actor_target (nn.Module): target actor model to select actions
        critic (nn.Module): critic model to predict state values
        critic_target (nn.Module): target critic model to predict state values
        actor_optimizer (Optimizer): optimizer for training actor
        critic_optimizer (Optimizer): optimizer for training critic
        hyper_params (dict): hyper-parameters
        beta (float): beta parameter for prioritized replay buffer
        curr_state (np.ndarray): temporary storage of the current state
        total_step (int): total step numbers
        episode_step (int): step number of the current episode

    """

    def __init__(
        self,
        env: gym.Env,
        args: argparse.Namespace,
        hyper_params: dict,
        models: tuple,
        optims: tuple,
        noise: OUNoise,
    ):
        """Initialization.

        Args:
            env (gym.Env): openAI Gym environment
            args (argparse.Namespace): arguments including hyperparameters and training settings
            hyper_params (dict): hyper-parameters
            models (tuple): models including actor and critic
            optims (tuple): optimizers for actor and critic
            noise (OUNoise): random noise for exploration

        """
        AbstractAgent.__init__(self, env, args)

        self.actor, self.actor_target, self.critic, self.critic_target = models
        self.actor_optimizer, self.critic_optimizer = optims
        self.hyper_params = hyper_params
        self.curr_state = np.zeros((1,))
        self.noise = noise
        self.total_step = 0
        self.episode_step = 0

        # load the optimizer and model parameters
        if args.load_from is not None and os.path.exists(args.load_from):
            self.load_params(args.load_from)

        if not self.args.test:
            # load demo replay memory
            with open(self.args.demo_path, "rb") as f:
                demo = pickle.load(f)

            # replay memory
            self.beta = self.hyper_params["PER_BETA"]
            self.memory = PrioritizedReplayBufferfD(
                self.hyper_params["BUFFER_SIZE"],
                self.hyper_params["BATCH_SIZE"],
                demo=list(demo),
                alpha=self.hyper_params["PER_ALPHA"],
            )

    def select_action(self, state: np.ndarray) -> np.ndarray:
        """Select an action from the input space."""
        self.curr_state = state

        # if initial random action should be conducted
        if (
            self.total_step < self.hyper_params["INITIAL_RANDOM_ACTION"]
            and not self.args.test
        ):
            return self.env.action_space.sample()

        state = torch.FloatTensor(state).to(device)
        selected_action = self.actor(state)

        if not self.args.test:
            selected_action += torch.FloatTensor(self.noise.sample()).to(device)
            selected_action = torch.clamp(selected_action, -1.0, 1.0)

        return selected_action.detach().cpu().numpy()

    def step(self, action: np.ndarray) -> Tuple[np.ndarray, np.float64, bool]:
        """Take an action and return the response of the env."""
        self.total_step += 1
        self.episode_step += 1

        next_state, reward, done, _ = self.env.step(action)

        if not self.args.test:
            # if the last state is not a terminal state, store done as false
            done_bool = (
                False if self.episode_step == self.args.max_episode_steps else done
            )
            self.memory.add(self.curr_state, action, reward, next_state, done_bool)

        return next_state, reward, done

    def update_model(
        self, experiences: Tuple[torch.Tensor, ...]
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Train the model after each episode."""
        states, actions, rewards, next_states, dones, weights, indexes, eps_d = (
            experiences
        )
        # G_t   = r + gamma * v(s_{t+1})  if state != Terminal
        #       = r                       otherwise
        masks = 1 - dones
        next_actions = self.actor_target(next_states)
        next_states_actions = torch.cat((next_states, next_actions), dim=-1)
        next_values = self.critic_target(next_states_actions)
        curr_returns = rewards + self.hyper_params["GAMMA"] * next_values * masks
        curr_returns = curr_returns.to(device).detach()

        # train critic
        values = self.critic(torch.cat((states, actions), dim=-1))
        critic_loss = torch.mean((values - curr_returns).pow(2) * weights)
        self.critic_optimizer.zero_grad()
        critic_loss.backward()
        self.critic_optimizer.step()

        # train actor
        actions = self.actor(states)
        actor_loss_element_wise = -self.critic(torch.cat((states, actions), dim=-1))
        actor_loss = torch.mean(actor_loss_element_wise * weights)
        self.actor_optimizer.zero_grad()
        actor_loss.backward()
        self.actor_optimizer.step()

        # update target networks
        tau = self.hyper_params["TAU"]
        common_utils.soft_update(self.actor, self.actor_target, tau)
        common_utils.soft_update(self.critic, self.critic_target, tau)

        # update priorities
        new_priorities = (values - curr_returns).pow(2)
        new_priorities += self.hyper_params["LAMDA3"] * actor_loss_element_wise.pow(2)
        new_priorities += self.hyper_params["PER_EPS"]
        new_priorities = new_priorities.data.cpu().numpy().squeeze()
        new_priorities += eps_d
        self.memory.update_priorities(indexes, new_priorities)

        return actor_loss.data, critic_loss.data

    def load_params(self, path: str):
        """Load model and optimizer parameters."""
        if not os.path.exists(path):
            print("[ERROR] the input path does not exist. ->", path)
            return

        params = torch.load(path)
        self.actor.load_state_dict(params["actor_state_dict"])
        self.actor_target.load_state_dict(params["actor_target_state_dict"])
        self.critic.load_state_dict(params["critic_state_dict"])
        self.critic_target.load_state_dict(params["critic_target_state_dict"])
        self.actor_optimizer.load_state_dict(params["actor_optim_state_dict"])
        self.critic_optimizer.load_state_dict(params["critic_optim_state_dict"])
        print("[INFO] loaded the model and optimizer from", path)

    def save_params(self, n_episode: int):
        """Save model and optimizer parameters."""
        params = {
            "actor_state_dict": self.actor.state_dict(),
            "actor_target_state_dict": self.actor_target.state_dict(),
            "critic_state_dict": self.critic.state_dict(),
            "critic_target_state_dict": self.critic_target.state_dict(),
            "actor_optim_state_dict": self.actor_optimizer.state_dict(),
            "critic_optim_state_dict": self.critic_optimizer.state_dict(),
        }

        AbstractAgent.save_params(self, params, n_episode)

    def write_log(self, i: int, loss: np.ndarray, score: int = 0):
        """Write log about loss and score"""
        total_loss = loss.sum()

        print(
            "[INFO] episode %d, episode step: %d, total step: %d, total score: %d\n"
            "total loss: %f actor_loss: %.3f critic_loss: %.3f\n"
            % (
                i,
                self.episode_step,
                self.total_step,
                score,
                total_loss,
                loss[0],
                loss[1],
            )  # actor loss  # critic loss
        )

        if self.args.log:
            wandb.log(
                {
                    "score": score,
                    "total loss": total_loss,
                    "actor loss": loss[0],
                    "critic loss": loss[1],
                }
            )

    def pretrain(self):
        """Pretraining steps."""
        pretrain_loss = list()
        print("[INFO] Pre-Train %d step." % self.hyper_params["PRETRAIN_STEP"])
        for i_step in range(1, self.hyper_params["PRETRAIN_STEP"] + 1):
            experiences = self.memory.sample()
            loss = self.update_model(experiences)
            pretrain_loss.append(loss)  # for logging

            # logging
            if i_step == 1 or i_step % 100 == 0:
                avg_loss = np.vstack(pretrain_loss).mean(axis=0)
                pretrain_loss.clear()
                self.write_log(0, avg_loss)

    def train(self):
        """Train the agent."""
        # logger
        if self.args.log:
            wandb.init()
            wandb.config.update(self.hyper_params)
            wandb.watch([self.actor, self.critic], log="parameters")

        # pre-training by demo
        self.pretrain()

        for i_episode in range(1, self.args.episode_num + 1):
            state = self.env.reset()
            done = False
            score = 0
            self.episode_step = 0
            loss_episode = list()

            while not done:
                if self.args.render and i_episode >= self.args.render_after:
                    self.env.render()

                action = self.select_action(state)
                next_state, reward, done = self.step(action)

                if len(self.memory) >= self.hyper_params["BATCH_SIZE"]:
                    loss_multiple_learn = []
                    for _ in range(self.hyper_params["MULTIPLE_LEARN"]):
                        experiences = self.memory.sample(self.beta)
                        loss = self.update_model(experiences)
                        loss_multiple_learn.append(loss)
                    # for logging
                    loss_episode.append(np.vstack(loss_multiple_learn).mean(axis=0))

                state = next_state
                score += reward

            # increase beta
            fraction = min(float(i_episode) / self.args.max_episode_steps, 1.0)
            self.beta = self.beta + fraction * (1.0 - self.beta)

            # logging
            if loss_episode:
                avg_loss = np.vstack(loss_episode).mean(axis=0)
                self.write_log(i_episode, avg_loss, score)

            if i_episode % self.args.save_period == 0:
                self.save_params(i_episode)

        # termination
        self.env.close()
