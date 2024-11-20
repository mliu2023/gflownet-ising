import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from matplotlib import pyplot as plt
from tqdm import tqdm
import os
import random

from env_void import IsingEnvironmentVoid
from visualize import visualize_trajectory, visualize_terminal_state
from prioritized_replay_buffer import PrioritizedReplayBuffer

class GFlowNetVoid(nn.Module):
    def __init__(self, initial_lattice, hidden_size):
        super(GFlowNetVoid, self).__init__()
        input_size = initial_lattice.shape[0] * initial_lattice.shape[1]
        forward_output_size = 2 * initial_lattice.shape[0] * initial_lattice.shape[1]
        backward_output_size = initial_lattice.shape[0] * initial_lattice.shape[1]
        self.network = nn.Sequential(
            nn.Linear(input_size, hidden_size),
            nn.ReLU(),
            nn.Linear(hidden_size, hidden_size),
            nn.ReLU(),
            # nn.Linear(hidden_size, hidden_size),
            # nn.ReLU()
        )
        self.fwp = nn.Linear(hidden_size, forward_output_size)
        self.bwp = nn.Linear(hidden_size, backward_output_size)
        self.log_Z = nn.Parameter(torch.zeros(1))

    def forward(self, state):
        state = state[:, :-1]
        state = self.network(state)
        return self.fwp(state), 1

class GFNAgent():
    def __init__(self, 
                 initial_lattice, 
                 trajectory_len, 
                 hidden_size, 
                 temp, 
                 batch_size, 
                 replay_batch_size, 
                 buffer_size,
                 alpha,
                 beta,
                 device):
        super().__init__()
        self.initial_lattice = initial_lattice
        self.height = initial_lattice.shape[0]
        self.width = initial_lattice.shape[1]
        self.trajectory_len = trajectory_len
        self.sample_env = IsingEnvironmentVoid(self.initial_lattice, 
                                               trajectory_len, 
                                               temp, 
                                               batch_size)
        self.train_env = IsingEnvironmentVoid(self.initial_lattice, 
                                              trajectory_len, 
                                              temp, 
                                              replay_batch_size)

        self.batch_size = batch_size
        self.replay_batch_size = replay_batch_size
        self.buffer_size = buffer_size
        # Use the simplified prioritized replay buffer
        self.replay_buffer = PrioritizedReplayBuffer(buffer_size, alpha, beta)

        self.model = GFlowNetVoid(self.initial_lattice, hidden_size)
        self.model.to(device)
        self.device = device
        self.output_folder = f"eval_trajectories_void/{self.height}x{self.width}/length_{self.trajectory_len}/temp_{self.sample_env.temp}"

    def create_forward_actions_mask(self, state: torch.Tensor):
        mask = (state[:, :-1] == 0).float()
        return torch.cat((mask, mask), 1)

    def mask_and_norm_forward_actions(self, state: torch.Tensor, forward_flow: torch.Tensor):
        mask = self.create_forward_actions_mask(state)
        masked_flow = torch.where(mask.bool(), forward_flow, torch.tensor(-float('inf')))
        log_probs = F.log_softmax(masked_flow, dim=1)
        probs = log_probs.exp()
        return probs

    def create_backwards_actions_mask(self, state: torch.Tensor):
        mask = (state[:, :-1] != 0).float()
        return mask

    def mask_and_norm_backward_actions(self, state: torch.Tensor, backward_flow: torch.Tensor):
        mask = self.create_backwards_actions_mask(state)
        masked_flow = torch.where(mask.bool(), backward_flow, torch.tensor(-float('inf')))
        log_probs = F.log_softmax(masked_flow, dim=1)
        probs = log_probs.exp()
        return probs

    def sample_trajectory(self, eps):
        state = self.sample_env.reset()
        trajectory = [state]
        forward_probs = []
        backward_probs = []
        actions = []
        for t in range(self.trajectory_len + 1):
            state.to(self.device)
            if t < self.trajectory_len:
                log_forward_flow_values, log_backward_flow_values = self.model(state)
                probs = self.mask_and_norm_forward_actions(state, log_forward_flow_values)

                # Debug information
                if torch.isnan(probs).any() or torch.isinf(probs).any() or (probs < 0).any():
                    print(f"Invalid probabilities at step {t}")
                    print(f"Forward flow values: {log_forward_flow_values}")
                    print(f"Backward flow values: {log_backward_flow_values}")
                    print(f"Probs: {probs}")
                    print(f"Weights: {self.model.fwp.weight}")
                    print(f"Backward weights: {self.model.bwp.weight}")

                action = torch.zeros(self.batch_size, dtype=torch.long)
                for i in range(self.batch_size):
                    if random.random() < eps:
                        nonzero_indices = torch.nonzero(probs[i] >= 1e-8, as_tuple=False)
                        if nonzero_indices.numel() > 0:
                            action[i] = nonzero_indices[torch.randint(0, nonzero_indices.numel(), (1,))].item()
                        else:
                            raise Exception(f"No valid actions for batch item {i}")
                    else:
                        action[i] = torch.multinomial(probs[i], 1).item()

                next_state, log_reward, done = self.sample_env.step(action)
                state = next_state

                forward_probs.append(probs[torch.arange(self.batch_size), action])
                actions.append(action)
                trajectory.append(next_state)

            if t >= 1:
                prev_state = trajectory[t-1]
                curr_state = trajectory[t]
                diff = (curr_state - prev_state)[:, :-1]
                back_action = diff.abs().argmax(dim=1)
                prev_probs = self.mask_and_norm_backward_actions(state, log_backward_flow_values)
                backward_probs.append(prev_probs[torch.arange(self.batch_size), back_action])

            if done.all():
                break

        return trajectory, forward_probs, backward_probs, actions, log_reward

    def back_sample_trajectory(self):
        state = self.sample_env.state
        log_reward = self.sample_env.reward_fn(self.sample_env.state[:, :-1].reshape(self.batch_size, 
                                                                                     self.sample_env.initial_lattice.shape[0], 
                                                                                     -1))
        trajectory = [state]
        forward_probs = []
        backward_probs = []
        actions = []
        for t in range(self.trajectory_len):
            state.to(self.device)
            if t < self.trajectory_len - 1:
                log_forward_flow_values, log_backward_flow_values = self.model(state)
                probs = self.mask_and_norm_backward_actions(state, log_backward_flow_values)
                action = torch.multinomial(probs, 1).squeeze(-1)
                next_state, _, done = self.sample_env.reverse_step(action)
                state = next_state

                backward_probs.append(probs[torch.arange(self.batch_size), action])
                actions.append(action)
                trajectory.append(next_state)

            if t >= 1:
                prev_state = trajectory[t-1]
                curr_state = trajectory[t]
                diff = (curr_state - prev_state)[:, :-1]
                prev_action = diff.abs().argmax(dim=1)
                prev_probs = self.mask_and_norm_forward_actions(state, log_forward_flow_values)
                forward_probs.append(prev_probs[torch.arange(self.batch_size), prev_action])

            if done.all():
                break

        return trajectory, forward_probs, backward_probs, actions, log_reward

    def trajectory_balance_loss(self, 
                                forward_probs: torch.Tensor, 
                                backward_probs: torch.Tensor, 
                                log_reward: torch.Tensor):
        forward_log_prob = torch.sum(torch.log(torch.clamp(torch.stack(forward_probs), min=1e-10)), dim=0)
        backward_log_prob = torch.sum(torch.log(torch.clamp(torch.stack(backward_probs), min=1e-10)), dim=0) if backward_probs else torch.zeros(self.batch_size)
        log_ratio = self.train_env.beta * self.model.log_Z + forward_log_prob - log_reward - backward_log_prob
        loss = log_ratio ** 2
        return loss.mean()

    def compute_probabilities(self, batch):
        trajectory = []
        actions = []
        log_reward = []
        for (traj, act, log_rew) in batch:
            trajectory.append(traj)
            actions.append(act)
            log_reward.append(log_rew)
        trajectory = torch.stack(trajectory)
        actions = torch.stack(actions)
        log_reward = torch.stack(log_reward)
            
        state = self.train_env.reset()
        forward_probs = []
        backward_probs = []
        for t in range(self.trajectory_len + 1):
            state.to(self.device)
            if t < self.trajectory_len:
                log_forward_flow_values, log_backward_flow_values = self.model(state)
                probs = self.mask_and_norm_forward_actions(state, log_forward_flow_values)

                # Debug information
                if torch.isnan(probs).any() or torch.isinf(probs).any() or (probs < 0).any():
                    print(f"Invalid probabilities at step {t}")
                    print(f"Forward flow values: {log_forward_flow_values}")
                    print(f"Backward flow values: {log_backward_flow_values}")
                    print(f"Probs: {probs}")
                    print(f"Weights: {self.model.fwp.weight}")
                    print(f"Backward weights: {self.model.bwp.weight}")
                    # nans occur when a low-probability action is sampled using epsilon greedy

                next_state, log_reward, done = self.train_env.step(actions[:, t])
                state = next_state
                assert torch.equal(state, trajectory[:, t+1])

                forward_probs.append(probs[torch.arange(self.replay_batch_size), actions[:, t]])

            if t >= 1:
                prev_state = trajectory[:, t-1]
                curr_state = trajectory[:, t]
                diff = (curr_state - prev_state)[:, :-1]
                back_action = diff.abs().argmax(dim=1)
                prev_probs = self.mask_and_norm_backward_actions(state, log_backward_flow_values)
                backward_probs.append(prev_probs[torch.arange(self.replay_batch_size), back_action])

            if done.all():
                break
        
        return trajectory, forward_probs, backward_probs, actions, log_reward

    def train_gflownet(self, iterations, p):
        optimizer = optim.Adam([
            {'params': self.model.log_Z, 'lr': 1e-1},
            {'params': self.model.network.parameters(), 'lr': 1e-3},
            {'params': self.model.fwp.parameters(), 'lr': 1e-3},
            {'params': self.model.bwp.parameters(), 'lr': 1e-3},], weight_decay=1e-5)

        warmup_episodes = 1000
        scheduler = torch.optim.lr_scheduler.LinearLR(optimizer, 
                                                      start_factor=0.1, 
                                                      total_iters=warmup_episodes)

        print(f"Filling replay buffer...")
        for _ in tqdm(range(self.buffer_size // self.batch_size)):
            trajectory, _, _, actions, log_reward = self.sample_trajectory(eps=1)
            self.replay_buffer.push(torch.stack(trajectory), torch.stack(actions), log_reward)

        for i in tqdm(range(iterations)):
            # Sample new trajectories
            for _ in range(int(p * self.buffer_size // self.batch_size)):
                trajectory, _, _, actions, log_reward = self.sample_trajectory(eps=0.01)
                self.replay_buffer.push(torch.stack(trajectory), torch.stack(actions), log_reward)

            # Train on batches from the replay buffer
            total_log_reward = 0
            total_loss = 0
            total_episodes = 0
            
            for _ in range(8):
                batch = self.replay_buffer.sample(self.replay_batch_size)
                
                # Compute probabilities for the batch
                _, fwd_probs, bwd_probs, _, log_rew = self.compute_probabilities(batch)
                
                optimizer.zero_grad()
                loss = self.trajectory_balance_loss(fwd_probs, bwd_probs, log_rew)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=1.0)
                optimizer.step()
                scheduler.step()

                total_log_reward += log_rew.mean().item()
                total_loss += loss.item()
                total_episodes += 1

            if i % 40 == 0:
                trajectory, _, _, actions, _ = self.sample_trajectory(eps=0)
                lattices = [state[:, :-1].reshape((self.batch_size, self.height, self.width)) for state in trajectory]
                visualize_terminal_state(
                    lattice=lattices[-1][0],
                    filename=os.path.join(self.output_folder, f"training_trajectory_{i}.png")
                )
            # print(f"Avg loss: {total_loss / total_episodes}, Avg log reward: {total_log_reward / total_episodes}")

if __name__ == "__main__":
    height = 7
    width = 7
    initial_lattice = torch.zeros((height, width))

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    agent = GFNAgent(initial_lattice=initial_lattice, 
                     trajectory_len=height*width, 
                     hidden_size=256, 
                     temp=10, 
                     batch_size=256, 
                     replay_batch_size=256, 
                     buffer_size=100_000,
                     alpha=0.5,
                     beta=0.1,
                     device=device)
    os.makedirs(agent.output_folder, exist_ok=True)
    agent.train_gflownet(iterations=300, p=0.005)

    for i in range(20):
        trajectory, forward_probs, backward_probs, actions, reward = agent.sample_trajectory(eps=0)
        lattices = [state[:, :-1].reshape((agent.batch_size, agent.height, agent.width)) for state in trajectory]
        # visualize_trajectory(
        #     trajectory=[lattice[0] for lattice in lattices],  # Visualize only the first batch item
        #     filename=os.path.join(agent.output_folder, f"trajectory_{i}.gif"),
        #     reward=reward[0].item()
        # )
        visualize_terminal_state(
            lattice=lattices[-1][0],  # Visualize the terminal state of the first batch item
            filename=os.path.join(agent.output_folder, f"trajectory_{i}.png")
        )
    plt.show()
    print("Done")