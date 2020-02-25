import torch
import torch.nn.functional as F
import numpy as np
import matplotlib.pyplot as plt
from IPython import get_ipython
from lib import utility

def in_ipynb():
    try:
        cfg = get_ipython().config
        if cfg['IPKernelApp']['parent_appname'] == 'ipython-notebook':
            return True
        else:
            return False
    except NameError:
        return False
try:
    if in_ipynb():
        print("notebook")
        from tqdm import tqdm_notebook as tqdm
    else:
        print("nope")
        from tqdm import tqdm
except:
    from tqdm import tqdm

from fannypack import utils
import fannypack
from . import dpf

def train_dynamics_recurrent(
        buddy, kf_model, dataloader, log_interval=10,
        loss_type="l2",
        optim_name="ekf_dynamics", checkpoint_interval=50, init_state_noise=0.5):
    epoch_losses = []

    assert loss_type in ('l1', 'l2')

    for batch_idx, batch in enumerate(tqdm(dataloader)):
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
            optimizer_name= optim_name,
            checkpoint_interval=checkpoint_interval)

        if buddy.optimizer_steps % log_interval == 0:
            with buddy.log_scope(optim_name):
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


def train_measurement(buddy, kf_model, dataloader, log_interval=10,
                      optim_name="ekf_measurement", checkpoint_interval=500):
    losses = []

    for batch_idx, batch in enumerate(dataloader):
        noisy_state, observation, _, state = fannypack.utils.to_device(batch, buddy._device)
        #         states = states[:,0,:]
        state_update, R = kf_model.measurement_model(observation, noisy_state)
        loss = F.mse_loss(state_update, state)
        buddy.minimize(loss,
                       optimizer_name= optim_name,
                       checkpoint_interval=checkpoint_interval)
        losses.append(utils.to_numpy(loss))
        with buddy.log_scope(optim_name):
            buddy.log("loss", loss)
            buddy.log("label_mean", fannypack.utils.to_numpy(state).mean())
            buddy.log("label_std", fannypack.utils.to_numpy(state).std())
            buddy.log("pred_mean", fannypack.utils.to_numpy(state_update).mean())
            buddy.log("pred_std", fannypack.utils.to_numpy(state_update).std())

    print("Epoch loss:", np.mean(losses))

def train_fusion(buddy, fusion_model, dataloader, log_interval=2,
                 optim_name="fusion", obs_only=False, init_state_noise=0.5):
    for batch_idx, batch in enumerate(dataloader):
        # Transfer to GPU and pull out batch data
        batch_gpu = utils.to_device(batch, buddy._device)
        _, batch_states, batch_obs, batch_controls = batch_gpu
        # N = batch size
        N, timesteps, control_dim = batch_controls.shape
        N, timesteps, state_dim = batch_states.shape
        assert batch_controls.shape == (N, timesteps, control_dim)

        state = batch_states[:, 0, :]
        state_sigma = torch.eye(state.shape[-1], device=buddy._device) * init_state_noise
        state_sigma = state_sigma.unsqueeze(0).repeat(N, 1, 1)

        if obs_only:
            state, state_sigma, force_state, image_state = fusion_model.forward(
                state,
                state_sigma,
                utils.DictIterator(batch_obs)[:, 0, :],
                batch_controls[:, 0, :],
                obs_only = obs_only,
            )
        else:
            dist = torch.distributions.Normal(
                torch.tensor([0.]), torch.ones(state.shape)*init_state_noise)
            noise = dist.sample().to(state.device)
            state += noise

        losses_image = []
        losses_force = []
        losses_fused = []
        losses_total = []

        for t in range(1, timesteps-1):
            prev_state = state
            prev_state_sigma = state_sigma

            state, state_sigma, force_state, image_state = fusion_model.forward(
                prev_state,
                prev_state_sigma,
                utils.DictIterator(batch_obs)[:, t, :],
                batch_controls[:, t, :],
            )

            loss_image = torch.mean((image_state - batch_states[:, t, :]) ** 2)
            loss_force = torch.mean((force_state - batch_states[:, t, :]) ** 2)
            loss_fused = torch.mean((state - batch_states[:, t, :]) ** 2)

            losses_force.append(loss_force)
            losses_image.append(loss_image)
            losses_fused.append(loss_fused)
            losses_total.append(loss_image + loss_force +loss_fused)

        buddy.minimize(
            torch.mean(torch.stack(losses_total)),
            optimizer_name= optim_name,
            checkpoint_interval=50)

        if buddy.optimizer_steps % log_interval == 0:
            with buddy.log_scope("fusion"):
                buddy.log("Training loss",  torch.mean(torch.stack(losses_total)))
                buddy.log("Image loss",  torch.mean(torch.stack(losses_image)))
                buddy.log("Force loss",  torch.mean(torch.stack(losses_force)))
                buddy.log("Fused loss",  torch.mean(torch.stack(losses_fused)))


def train_e2e(buddy, ekf_model, dataloader,
              log_interval=2, optim_name="ekf",
              obs_only=False,
              checkpoint_interval = 50,
              init_state_noise=0.5,
              ):
    # Train for 1 epoch
    for batch_idx, batch in enumerate(dataloader):
        # Transfer to GPU and pull out batch data
        batch_gpu = utils.to_device(batch, buddy._device)
        _, batch_states, batch_obs, batch_controls = batch_gpu
        # N = batch size, M = particle count
        N, timesteps, control_dim = batch_controls.shape
        N, timesteps, state_dim = batch_states.shape
        assert batch_controls.shape == (N, timesteps, control_dim)
        state, _ = ekf_model.measurement_model.forward(utils.DictIterator(batch_obs)[:, 0, :], batch_states[:, 0, :]  )
        state_sigma = torch.eye(state.shape[-1], device=buddy._device) * init_state_noise
        state_sigma = state_sigma.unsqueeze(0).repeat(N, 1, 1)

        if obs_only:
            state, state_sigma = ekf_model.forward(
                state,
                state_sigma,
                utils.DictIterator(batch_obs)[:, 0, :],
                batch_controls[:, 0, :],
                obs_only=obs_only,
            )
        else:
            dist = torch.distributions.Normal(
                torch.tensor([0.]), torch.ones(state.shape)*init_state_noise)
            noise = dist.sample().to(state.device)
            state += noise

        ekf_model.measurement_model.use_states = True

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

        loss = torch.mean(torch.stack(losses))
        buddy.minimize(
            torch.mean(torch.stack(losses)),
            optimizer_name= optim_name,
            checkpoint_interval=checkpoint_interval)

        if buddy.optimizer_steps % log_interval == 0:
            with buddy.log_scope(optim_name):
                buddy.log("Training loss", loss)

def rollout_kf(kf_model, trajectories, start_time=0, max_timesteps=300,
               noisy_dynamics=True, true_initial=False):
    # To make things easier, we're going to cut all our trajectories to the
    # same length :)
    end_time = np.min([len(s) for s, _, _ in trajectories] +
                      [start_time + max_timesteps])
    actual_states = [states[start_time:end_time]
                     for states, _, _ in trajectories]

    state_dim = len(actual_states[0][0])
    N = len(trajectories)
    controls_dim = trajectories[0][-1, 0].shape

    device = next(kf_model.parameters()).device

    initial_states = np.zeros((N, state_dim))
    initial_sigmas = np.ones((N, state_dim, state_dim)) * 0.5
    initial_obs = {}

    if true_initial:
        for i in range(N):
            initial_states[i] = trajectories[i][0, 0]
    else:
        # Put into measurement model!
        dummy_controls = torch.ones((N,)+controls_dim,).to(device)
        for i in range(N):
            utils.DictIterator(initial_obs).append(utils.DictIterator(trajectories[i][1, 0]))

        utils.DictIterator(initial_obs).convert_to_numpy()

        (initial_obs_torch,
         initial_states,
         initial_sigmas) = utils.to_torch((initial_obs,
                                           initial_states,
                                           initial_sigmas), device=device)

        initial_states, initial_sigmas = kf_model.forward(
            initial_states,
            initial_sigmas,
            initial_obs,
            dummy_controls,
        )

    states = initial_states
    sigmas = initial_sigmas

    predicted_states = [[initial_states[i]]
                        for i in range(len(trajectories))]

    for t in tqdm_notebook(range(start_time + 1, end_time)):
        s = []
        o = {}
        c = []

        for i, traj in enumerate(trajectories):
            states, observations, controls = traj

            o_t = utils.DictIterator(observations)[t]
            utils.DictIterator(o).append(o_t)
            c.append(controls[t])

        s = np.array(s)
        utils.DictIterator(o).convert_to_numpy()
        c = np.array(c)
        (s, o, c) = utils.to_torch((s, o, c), device=device)

        state_estimates, sigma_estimates = kf_model.forward(
            states,
            sigmas,
            o,
            c,
        )

        states = state_estimates
        sigmas = sigma_estimates

        for i in range(len(trajectories)):
            predicted_states[i].append(
                utils.to_numpy(
                    state_estimates[i]))

    predicted_states = np.array(predicted_states)
    actual_states = np.array(actual_states)

    return predicted_states, actual_states

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

