import torch
import torch.nn.functional as F
import numpy as np
import matplotlib.pyplot as plt
from tqdm import tqdm_notebook

from fannypack import utils
import fannypack

from . import dpf


def train_dynamics_recurrent(
        buddy, kf_model, dataloader, log_interval=10, loss_type="l2"):
    epoch_losses = []

    assert loss_type in ('l1', 'l2')

    for batch_idx, batch in enumerate(tqdm_notebook(dataloader)):
        batch_gpu = utils.to_device(batch, buddy._device)
        batch_states, batch_obs, batch_controls = batch_gpu

        N, timesteps, control_dim = batch_controls.shape
        N, timesteps, state_dim = batch_states.shape
        assert batch_controls.shape == (N, timesteps, control_dim)

        prev_states = batch_states[:, 0, :]

        losses = []
        magnitude_losses = []
        direction_losses = []

        # Compute some state deltas for debugging
        label_deltas = np.mean(utils.to_numpy(
            batch_states[:, 1:, :] - batch_states[:, :-1, :]
        ) ** 2, axis=(0, 2))
        assert label_deltas.shape == (timesteps - 1,)
        pred_deltas = []

        for t in range(1, timesteps):
            controls = batch_controls[:, t, :]
            new_states = kf_model.dynamics_model(
                prev_states,
                controls,
                noisy=False,
            )

            pred_delta = prev_states - new_states
            label_delta = batch_states[:, t - 1, :] - batch_states[:, t, :]

            # todo: maybe switch back to l2
            if loss_type == "l1":
                timestep_loss = F.l1_loss(new_states, batch_states[:, t, :])
            else:
                timestep_loss = F.mse_loss(new_states, batch_states[:, t, :])

            losses.append(timestep_loss)

            pred_deltas.append(np.mean(
                utils.to_numpy(new_states - prev_states) ** 2
            ))
            prev_states = new_states

        pred_deltas = np.array(pred_deltas)
        assert pred_deltas.shape == (timesteps - 1,)

        loss = torch.mean(torch.stack(losses))
        epoch_losses.append(loss)

        buddy.minimize(
            loss,
            optimizer_name="ekf_dynamics",
            checkpoint_interval=100)

        if buddy.optimizer_steps % log_interval == 0:
            with buddy.log_scope("dynamics_recurrent"):
                buddy.log("Training loss", loss)

                buddy.log("Label delta mean", label_deltas.mean())
                buddy.log("Label delta std", label_deltas.std())

                buddy.log("Pred delta mean", pred_deltas.mean())
                buddy.log("Pred delta std", pred_deltas.std())

                if magnitude_losses:
                    buddy.log("Magnitude loss",
                              torch.mean(torch.tensor(magnitude_losses)))
                if direction_losses:
                    buddy.log("Direction loss",
                              torch.mean(torch.tensor(direction_losses)))


def train_measurement(buddy, kf_model, dataloader, log_interval=10):
    losses = []

    for batch_idx, batch in enumerate(tqdm_notebook(dataloader)):
        noisy_state, observation, _, state = fannypack.utils.to_device(batch, buddy._device)
        #         states = states[:,0,:]
        state_update, R = kf_model.measurement_model(observation, noisy_state)
        loss = F.mse_loss(state_update, state)
        buddy.minimize(loss,
                       optimizer_name="ekf_measurement",
                       checkpoint_interval=500)
        losses.append(loss)
        with buddy.log_scope("ekf_measurement"):
            buddy.log("loss", loss)
            buddy.log("label_mean", fannypack.utils.to_numpy(state).mean())
            buddy.log("label_std", fannypack.utils.to_numpy(state).std())
            buddy.log("pred_mean", fannypack.utils.to_numpy(state_update).mean())
            buddy.log("pred_std", fannypack.utils.to_numpy(state_update).std())

    print("Epoch loss:", np.mean(losses))


def train_e2e(buddy, ekf_model, dataloader, log_interval=2):
    # Train for 1 epoch
    for batch_idx, batch in enumerate(tqdm_notebook(dataloader)):
        # Transfer to GPU and pull out batch data
        batch_gpu = utils.to_device(batch, buddy._device)
        _, batch_states, batch_obs, batch_controls = batch_gpu
        # N = batch size, M = particle count
        N, timesteps, control_dim = batch_controls.shape
        N, timesteps, state_dim = batch_states.shape
        assert batch_controls.shape == (N, timesteps, control_dim)

        state = batch_states[:, 0, :]
        state_sigma = torch.eye(state.shape[-1], device=buddy._device) * 0.001
        state_sigma = state_sigma.unsqueeze(0).repeat(N, 1, 1)

        # Accumulate losses from each timestep
        losses = []
        for t in range(1, timesteps - 1):
            prev_state = state
            prev_state_sigma = state_sigma

            state, state_sigma = ekf_model.forward(
                prev_state,
                prev_state_sigma,
                utils.DictIterator(batch_obs)[:, t, :],
                batch_controls[:, t, :],
                noisy_dynamics=True
            )

            assert state.shape == batch_states[:, t, :].shape
            loss = torch.mean((state - batch_states[:, t, :]) ** 2)
            losses.append(loss)

        buddy.minimize(
            torch.mean(torch.stack(losses)),
            optimizer_name="ekf",
            checkpoint_interval=500)

        if buddy.optimizer_steps % log_interval == 0:
            with buddy.log_scope("ekf"):
                buddy.log("Training loss", loss)

#todo: write the rollout code!

# def rollout_kf(kf_model, trajectories, start_time=0, max_timesteps=300,
#             particle_count=100, noisy_dynamics=True, true_initial=False):
#     # To make things easier, we're going to cut all our trajectories to the
#     # same length :)
#     end_time = np.min([len(s) for s, _, _ in trajectories] +
#                       [start_time + max_timesteps])
#     actual_states = [states[start_time:end_time]
#                      for states, _, _ in trajectories]
#
#     state_dim = len(actual_states[0][0])
#     N = len(trajectories)
#     M = particle_count
#
#     device = next(kf_model.parameters()).device
#
#     # particles = np.zeros((N, M, state_dim))
#     # if true_initial:
#     #     for i in range(N):
#     #         particles[i, :] = trajectories[i][0, 0]
#     # else:
#     #     # Distribute initial particles randomly
#     #     particles += np.random.normal(0, 1.0, size=particles.shape)
#
#     # Populate the initial state estimate as just the estimate of our particles
#     # This is a little hacky
#     # predicted_states = [[np.mean(particles[i], axis=0)]
#     #                     for i in range(len(trajectories))]
#     #
#     # particles = utils.to_torch(particles, device=device)
#     # log_weights = torch.ones((N, M), device=device) * (-np.log(M))
#
#     for t in tqdm_notebook(range(start_time + 1, end_time)):
#         s = []
#         o = {}
#         c = []
#         for i, traj in enumerate(trajectories):
#             states, observations, controls = traj
#
#             s.append(predicted_states[i][t - start_time - 1])
#             o_t = utils.DictIterator(observations)[t]
#             utils.DictIterator(o).append(o_t)
#             c.append(controls[t])
#
#         s = np.array(s)
#         utils.DictIterator(o).convert_to_numpy()
#         c = np.array(c)
#         (s, o, c) = utils.to_torch((s, o, c), device=device)
#
#         state_estimates, new_particles, new_log_weights = pf_model.forward(
#             particles,
#             log_weights,
#             o,
#             c,
#             resample=True,
#             noisy_dynamics=noisy_dynamics
#         )
#
#         particles = new_particles
#         log_weights = new_log_weights
#
#         for i in range(len(trajectories)):
#             predicted_states[i].append(
#                 utils.to_numpy(
#                     state_estimates[i]))
#
#     predicted_states = np.array(predicted_states)
#     actual_states = np.array(actual_states)
#     return predicted_states, actual_states


def eval_rollout(predicted_states, actual_states, plot=False):
    if plot:
        timesteps = len(actual_states[0])

        def color(i):
            colors = ['b', 'g', 'r', 'c', 'm', 'y', 'k']
            return colors[i % len(colors)]

        state_dim = actual_states.shape[-1]
        for j in range(state_dim):
            plt.figure(figsize=(8, 6))
            for i, (pred, actual) in enumerate(
                    zip(predicted_states, actual_states)):
                predicted_label_arg = {}
                actual_label_arg = {}
                if i == 0:
                    predicted_label_arg['label'] = "Predicted"
                    actual_label_arg['label'] = "Ground Truth"
                plt.plot(range(timesteps),
                         pred[:, j],
                         c=color(i),
                         alpha=0.3,
                         **predicted_label_arg)
                plt.plot(range(timesteps),
                         actual[:, j],
                         c=color(i),
                         **actual_label_arg)

            rmse = np.mean(
                (predicted_states[:, :, j] - actual_states[:, :, j]) ** 2)

            plt.title(f"State #{j} // RMSE = {rmse}")
            plt.xlabel("Timesteps")
            plt.ylabel("Value")
            plt.legend()
            plt.show()
