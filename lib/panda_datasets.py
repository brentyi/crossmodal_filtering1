import torch
import numpy as np
import scipy.stats
from tqdm import tqdm_notebook

from fannypack import utils

from . import dpf


# ['image'
# 'depth'
# 'proprio'
# 'joint_pos'
# 'joint_vel'
# 'gripper_qpos'
# 'gripper_qvel'
# 'eef_pos'
# 'eef_quat'
# 'eef_vlin'
# 'eef_vang'
# 'force'
# 'force_hi_freq'
# 'contact'
# 'robot-state'
# 'prev-act'
# 'Cylinder0_pos'
# 'Cylinder0_quat'
# 'Cylinder0_to_eef_pos'
# 'Cylinder0_to_eef_quat'
# 'Cylinder0_mass'
# 'Cylinder0_friction'
# 'object-state'
# 'action'
# 'object_z_angle'])


def load_trajectories(*paths, use_vision=True,
                      vision_interval=10, use_proprioception=True, use_haptics=True, **unused):
    """
    Loads a list of trajectories from a set of input paths, where each trajectory is a tuple
    containing...
        states: an (T, state_dim) array of state vectors
        observations: a key->(T, *) dict of observations
        controls: an (T, control_dim) array of control vectors

    Each path can either be a string or a (string, int) tuple, where int indicates the maximum
    number of timesteps to import.
    """
    trajectories = []

    for path in paths:
        count = np.float('inf')
        if type(path) == tuple:
            path, count = path
            assert type(count) == int

        with utils.TrajectoriesFile(path) as f:
            # Iterate over each trajectory
            for i, trajectory in enumerate(f):
                if i >= count:
                    break

                timesteps = len(utils.DictIterator(trajectory))

                # Define our state:  we expect this to be:
                # (x, y, cos theta, sin theta, mass, friction)
                # TODO: add mass, friction
                state_dim = 2
                states = np.full((timesteps, state_dim), np.nan)

                states[:, :2] = trajectory['Cylinder0_pos'][:, :2]  # x, y
                # states[:, 2] = np.cos(trajectory['object_z_angle'])
                # states[:, 3] = np.sin(trajectory['object_z_angle'])
                # states[:, 4] = trajectory['Cylinder0_mass'][:, 0]
                # states[:, 5] = trajectory['Cylinder0_friction'][:, 0]

                # Zero out everything but XY position
                # TODO: remove this
                states[:, 2:] *= 0

                # Pull out observations
                ## This is currently consisted of:
                ## > gripper_pos: end effector position
                ## > gripper_sensors: F/T, contact sensors
                ## > image: camera image

                observations = {}
                observations['gripper_pos'] = trajectory['eef_pos']
                assert observations['gripper_pos'].shape == (timesteps, 3)

                observations['gripper_sensors'] = np.concatenate((
                    trajectory['force'],
                    trajectory['contact'][:, np.newaxis],
                ), axis=1)
                assert observations['gripper_sensors'].shape[1] == 7

                if not use_proprioception:
                    observations['gripper_pos'][:] = 0
                if not use_haptics:
                    observations['gripper_sensors'][:] = 0

                observations['image'] = np.zeros_like(trajectory['image'])
                if use_vision:
                    for i in range(len(observations['image'])):
                        index = (i // vision_interval) * vision_interval
                        index = min(index, len(observations['image']))
                        observations['image'][i] = trajectory['image'][index]

                # Pull out controls
                ## This is currently consisted of:
                ## > previous end effector position
                ## > end effector position delta
                ## > binary contact reading
                eef_positions = trajectory['eef_pos']
                eef_positions_shifted = np.roll(eef_positions, shift=-1)
                eef_positions_shifted[-1] = eef_positions[-1]
                controls = np.concatenate([
                    eef_positions_shifted,
                    eef_positions - eef_positions_shifted,
                    trajectory['contact'][:, np.newaxis],
                ], axis=1)
                assert controls.shape == (timesteps, 7)

                # Normalization
                observations['gripper_pos'] -= np.array([[0.455967, - 0.01341514, 0.880639]])
                observations['gripper_pos'] /= np.array(
                    [[0.00735019, 0.02312305, 0.03969879]])
                observations['gripper_sensors'] -= np.array(
                    [[-1.4395459e-01, 1.3516411e+00, -3.2150087e+00,
                      -1.1090579e-01, -4.6001539e-02, 1.5583872e-03,
                      2.4166666e-01]])
                observations['gripper_sensors'] /= np.array(
                    [[1.291729, 2.461422, 0.56859004, 0.20127113, 0.09833302,
                      0.00823816, 0.42809328]])
                states -= np.array([[0.4351026, -0.0721447]])
                states /= np.array([[0.0050614, 0.01165088]])
                controls -= np.array(
                    [[-0.01127849, 0.87681806, 0.45754063, 0.46724555,
                      -0.8902338, 0.42309874, 0.24166666]])
                controls /= np.array(
                    [[0.03666912, 0.07343381, 0.02567112,
                      0.03398367, 0.06594761, 0.04507983, 0.42809328]])

            trajectories.append((states, observations, controls))

    ## Uncomment this line to generate the lines required to normalize data
    # _print_normalization(trajectories)

    return trajectories


def _print_normalization(trajectories):
    """ Helper for producing code to normalize inputs
    """
    states = []
    observations = {}
    controls = []
    for t in trajectories:
        states.extend(t[0])
        utils.DictIterator(observations).extend(t[1])
        controls.extend(t[2])

    def print_ranges(**kwargs):
        for k, v in kwargs.items():
            mean = repr(np.mean(v, axis=0, keepdims=True))
            stddev = repr(np.std(v, axis=0, keepdims=True))
            print(f"{k} -= np.{mean}")
            print(f"{k} /= np.{stddev}")

    print_ranges(
        gripper_pos=observations['gripper_pos'],
        gripper_sensors=observations['gripper_sensors'],
        states=states,
        controls=controls,
    )


class PandaDynamicsDataset(torch.utils.data.Dataset):
    """A customized data preprocessor for trajectories
    """

    def __init__(self, *paths, **kwargs):
        """
        Input:
          *paths: paths to dataset hdf5 files
        """

        trajectories = load_trajectories(*paths, **kwargs)
        active_dataset = []
        inactive_dataset = []
        for trajectory in trajectories:
            assert len(trajectory) == 3
            states, observations, controls = trajectory

            timesteps = len(states)
            assert type(observations) == dict
            assert len(controls) == timesteps

            for t in range(1, timesteps):
                # Pull out data & labels
                prev_state = states[t - 1]
                observation = utils.DictIterator(observations)[t]
                control = controls[t]
                new_state = states[t]

                # Construct sample, bring to torch, & add to dataset
                sample = (prev_state, observation, control, new_state)
                sample = tuple(utils.to_torch(x) for x in sample)

                if np.linalg.norm(new_state - prev_state) > 1e-5:
                    active_dataset.append(sample)
                else:
                    inactive_dataset.append(sample)

        print("Parsed data: {} active, {} inactive".format(
            len(active_dataset), len(inactive_dataset)))
        keep_count = min(len(active_dataset) // 2, len(inactive_dataset))
        print("Keeping:", keep_count)
        np.random.shuffle(inactive_dataset)
        self.dataset = active_dataset + inactive_dataset[:keep_count]

    def __getitem__(self, index):
        """ Get a subsequence from our dataset
        Output:
            sample: (prev_state, observation, control, new_state)
        """
        return self.dataset[index]

    def __len__(self):
        """
        Total number of samples in the dataset
        """
        return len(self.dataset)


class PandaMeasurementDataset(torch.utils.data.Dataset):
    """A customized data preprocessor for trajectories
    """
    # (x, y, cos theta, sin theta, mass, friction)
    # TODO: fix default variances for mass, friction
    # default_stddev = (0.015, 0.015, 1e-4, 1e-4, 1e-4, 1e-4)
    default_stddev = (0.005, 0.005)  # , 0.015, 0.015, 0.015, 0.015)

    def __init__(self, *paths, stddev=None, samples_per_pair=20, **kwargs):
        """
        Args:
          *paths: paths to dataset hdf5 files
        """

        trajectories = load_trajectories(*paths, **kwargs)

        if stddev is None:
            stddev = self.default_stddev
        self.stddev = np.array(stddev)
        self.samples_per_pair = samples_per_pair
        self.dataset = []
        for i, trajectory in enumerate(tqdm_notebook(trajectories)):
            assert len(trajectory) == 3
            states, observations, controls = trajectory

            timesteps = len(states)
            assert type(observations) == dict
            assert len(controls) == timesteps

            for t in range(0, timesteps):
                # Pull out data & labels
                state = states[t]
                observation = utils.DictIterator(observations)[t]

                self.dataset.append((state, observation))

        print("Loaded {} points".format(len(self.dataset)))

    def __getitem__(self, index):
        """ Get a subsequence from our dataset

        Returns:
            sample: (prev_state, observation, control, new_state)
        """

        state, observation = self.dataset[index // self.samples_per_pair]

        assert self.stddev.shape == state.shape

        # Generate half of our samples close to the mean, and the other half
        # far away
        if index % self.samples_per_pair < self.samples_per_pair * 0.5:
            noisy_state = state + \
                np.random.normal(
                    loc=0., scale=self.stddev, size=state.shape)
        else:
            noisy_state = state + \
                np.random.normal(
                    loc=0., scale=self.stddev * 10, size=state.shape)

        log_likelihood = np.asarray(scipy.stats.multivariate_normal.logpdf(
            noisy_state[:2], mean=state[:2], cov=np.diag(self.stddev[:2] ** 2)))

        return utils.to_torch((noisy_state, observation, log_likelihood))

    def __len__(self):
        """
        Total number of samples in the dataset
        """
        return len(self.dataset) * self.samples_per_pair


class PandaParticleFilterDataset(dpf.ParticleFilterDataset):
    # (x, y, cos theta, sin theta, mass, friction)
    # TODO: fix default variances for mass, friction
    default_particle_stddev = [0.02, 0.02]  # , 0.1, 0.1, 0, 0]
    default_subsequence_length = 20
    default_particle_count = 100

    def __init__(self, *paths, **kwargs):
        """
        Args:
          *paths: paths to dataset hdf5 files
        """

        trajectories = load_trajectories(*paths, **kwargs)

        # Split up trajectories into subsequences
        super().__init__(trajectories, **kwargs)

        # Post-process subsequences; differentiate between active ones and
        # inactive ones
        active_subsequences = []
        inactive_subsequences = []

        for subsequence in self.subsequences:
            start_state = subsequence[0][0]
            end_state = subsequence[0][-1]
            if np.linalg.norm(start_state - end_state) > 1e-5:
                active_subsequences.append(subsequence)
            else:
                inactive_subsequences.append(subsequence)

        print("Parsed data: {} active, {} inactive".format(
            len(active_subsequences), len(inactive_subsequences)))
        keep_count = min(
            len(active_subsequences) // 2,
            len(inactive_subsequences)
        )
        print("Keeping (inactive):", keep_count)

        np.random.shuffle(inactive_subsequences)
        self.subsequences = active_subsequences + \
            inactive_subsequences[:keep_count]
