import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from fannypack.nn import resblocks
from fannypack import utils
import torch.optim as optim
import torch.nn.functional as F

from lib import utility

class KalmanFusionModel(nn.Module):

    def __init__(self, image_model, force_model,
                 weight_model, fusion_type="weighted"):
        super().__init__()

        self.image_model = image_model
        self.force_model = force_model
        self.weight_model = weight_model
        self.fusion_type = fusion_type

        assert self.fusion_type in ["weighted", "poe", "sigma"]

    def forward(self, states_prev, state_sigma_prev, observations, controls, obs_only=False):

            N, state_dim = states_prev.shape 

            assert state_sigma_prev is not None
            image_state, image_state_sigma = self.image_model.forward(
                states_prev,
                state_sigma_prev,
                observations,
                controls,
                noisy_dynamics=True,
                obs_only = obs_only
            )

            force_state, force_state_sigma = self.force_model.forward(
                states_prev,
                state_sigma_prev,
                observations,
                controls,
                noisy_dynamics=True,
                obs_only = obs_only
            )

            force_beta, image_beta = self.weight_model.forward(observations)

            weights = torch.stack([image_beta[:,0:state_dim], force_beta[:, 0:state_dim]])
            weights_for_sigma = [torch.diag_embed(image_beta[:, 0:state_dim], offset=0, dim1=-2, dim2=-1), 
                                torch.diag_embed(force_beta[:, 0:state_dim], offset=0, dim1=-2, dim2=-1)]
            #todo: this only works for state dim =2 
            weights_for_sigma[0][:, 0, 1] = image_beta[:, -1]
            weights_for_sigma[0][:, 1, 0] = image_beta[:, -1]

            weights_for_sigma[1][:, 0, 1] = force_beta[:, -1]
            weights_for_sigma[1][:, 1, 0] = force_beta[:, -1]

            weights_for_sigma = torch.stack(weights_for_sigma)
            states_pred = torch.stack([image_state, force_state])
            state_sigma_pred = torch.stack([image_state_sigma, force_state_sigma])

            if self.fusion_type == "weighted":
                state = self.weighted_average(states_pred, weights)
                state_sigma = self.weighted_average(state_sigma_pred, weights_for_sigma)
            elif self.fusion_type == "poe":
                state = self.product_of_experts(states_pred, weights)
                state_sigma = self.weighted_average(state_sigma_pred, weights_for_sigma)
            elif self.fusion_type == "sigma":
                image_weight = torch.pinverse(image_state_sigma, 1e-8)
                force_weight = torch.pinverse(force_state_sigma, 1e-8)

                image_weight_normed = image_weight/(image_weight+force_weight)
                force_weight_normed = force_weight/(image_weight+force_weight)
                state = torch.bmm(image_weight_normed, image_state.unsqueeze(-1)) + torch.bmm(force_weight_normed, force_state.unsqueeze(-1))
                state = state.squeeze(-1)
                state_sigma = 1/(image_weight + force_weight)

            return state, state_sigma, force_state, image_state

    def weighted_average(self, predictions, weights):

        assert predictions.shape == weights.shape

        weights = weights / (torch.sum(weights, dim=0) + 1e-9)
        weighted_average = torch.sum(weights * predictions, dim=0)

        # print("pred: ", predictions)
        # print("weights" , weights)
        # print("avg:" , weighted_average)
        return weighted_average

    def product_of_experts(self, predictions: list, weights: list):
        assert predictions.shape == weights.shape
        T= 1.0/(weights + 1e-9)
        mu = (predictions * T).sum(0) * (1.0/ T.sum(0))
        var = (1.0/T.sum(0))
        return mu

class CrossModalWeights(nn.Module):

    def __init__(self, state_dim=2, units=32, use_softmax=True):
        super().__init__()

        obs_pose_dim = 3
        obs_sensors_dim = 7
        self.state_dim = state_dim
        self.use_softmax = use_softmax

        self.observation_image_layers = nn.Sequential(
            nn.Conv2d(
                in_channels=1,
                out_channels=32,
                kernel_size=5,
                padding=2),
            nn.ReLU(inplace=True),
            resblocks.Conv2d(channels=32, kernel_size=3),
            nn.Conv2d(
                in_channels=32,
                out_channels=16,
                kernel_size=3,
                padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(
                in_channels=16,
                out_channels=2,
                kernel_size=3,
                padding=1),
            nn.Flatten(),  # 32 * 32 * 8
            nn.Linear(2 * 32 * 32, units),
            nn.ReLU(inplace=True),
            resblocks.Linear(units),
        )
        self.observation_pose_layers = nn.Sequential(
            nn.Linear(obs_pose_dim, units),
            resblocks.Linear(units, activation='leaky_relu'),
        )
        self.observation_sensors_layers = nn.Sequential(
            nn.Linear(obs_sensors_dim, units),
            resblocks.Linear(units, activation='leaky_relu'),
        )

        if self.use_softmax:
            self.shared_layers = nn.Sequential(
                nn.Linear(units * 3, units),
                nn.ReLU(inplace=True),
                resblocks.Linear(units, units),
                resblocks.Linear(units, units),
                resblocks.Linear(units, units),
                nn.Linear(units, 2 * self.state_dim),
            )
        else:
            self.shared_layers = nn.Sequential(
                nn.Linear(units * 3, units * 3),
                nn.ReLU(inplace=True),
                resblocks.Linear(3 * units),
            )

            self.force_prop_layer = nn.Sequential(
                nn.Linear(units, units),
                nn.ReLU(inplace=True),
                nn.Linear(units, self.state_dim),
                nn.Sigmoid(),
            )

            self.image_prop_layer = nn.Sequential(
                nn.Linear(units, units),
                nn.ReLU(inplace=True),
                nn.Linear(units, self.state_dim),
                nn.Sigmoid(),
            )

            self.fusion_layer = nn.Sequential(
                nn.Linear(units, units),
                nn.ReLU(inplace=True),
                nn.Linear(units, self.state_dim+1),
                nn.Sigmoid(),
            )

            #todo: the +1 only works for state dim =2
            # it should be + (state_dim)(state_dim-1)/2 

        for m in self.modules():
            if isinstance(m, nn.Conv2d) or isinstance(m, nn.ConvTranspose2d):
                nn.init.kaiming_normal_(m.weight.data)
                if m.bias is not None:
                    m.bias.data.zero_()
                elif isinstance(m, nn.BatchNorm2d):
                    m.weight.data.fill_(1)
                m.bias.data.zero_()

        self.units = units

    def forward(self, observations):
        assert type(observations) == dict

        # N := distinct trajectory count (batch size)

        N = observations['image'].shape[0]

        # Construct observations feature vector
        # (N, obs_dim)
        observation_features = torch.cat((
            self.observation_image_layers(
                observations['image'][:, np.newaxis, :, :]),
            self.observation_pose_layers(observations['gripper_pos']),
            self.observation_sensors_layers(
                observations['gripper_sensors']),
        ), dim=1)
        assert observation_features.shape == (N, self.units * 3)

        shared_features = self.shared_layers(observation_features)

        if self.use_softmax:
            assert shared_features.shape == (N, 2 * self.state_dim)
            log_softmax = F.log_softmax(
                shared_features.reshape((N, 2, self.state_dim)),
                dim=1
            )
            force_prop_beta = log_softmax[:, 0, :]
            image_beta = log_softmax[:, 1, :]
        else:
            assert shared_features.shape == (N, self.units * 3)
            force_prop_beta = self.force_prop_layer(
                shared_features[:, :self.units+1])
            image_beta = self.image_prop_layer(
                shared_features[:, self.units+1:(self.units+1) * 2])

        return image_beta, force_prop_beta
