# Copyright 2022 Mathias Lechner and Ramin Hasani
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
try:
    import torch
except:
    raise ImportWarning(
        "It seems like the PyTorch package is not installed\n"
        "Installation instructions: https://pytorch.org/get-started/locally/\n",
    )
import torch.nn as nn
import numpy as np
import torch.nn.functional as F
from typing import Optional, Union


class LeCun(nn.Module):
    def __init__(self):
        super(LeCun, self).__init__()
        self.tanh = nn.Tanh()

    def forward(self, x):
        return 1.7159 * self.tanh(0.666 * x)


class CfCCell(nn.Module):
    def __init__(
        self,
        input_size,
        hidden_size,
        mode="default",
        backbone_activation="lecun_tanh",
        backbone_units=128,
        backbone_layers=1,
        backbone_dropout=0.0,
        sparsity_mask=None,
        neuromod_network_dims=None,
        neuromod_network_activation=nn.Tanh,
    ):
        """A `Closed-form Continuous-time <https://arxiv.org/abs/2106.13898>`_ cell.

        .. Note::
            This is an RNNCell that process single time-steps. To get a full RNN that can process sequences see `ncps.torch.CfC`.



        :param input_size:
        :param hidden_size:
        :param mode:
        :param backbone_activation:
        :param backbone_units:
        :param backbone_layers:
        :param backbone_dropout:
        :param sparsity_mask:
        """

        super(CfCCell, self).__init__()

        self.input_size = input_size
        self.hidden_size = hidden_size
        allowed_modes = ["default", "pure", "no_gate", "neuromodulated"]
        if mode not in allowed_modes:
            raise ValueError(
                f"Unknown mode '{mode}', valid options are {str(allowed_modes)}"
            )
        self.sparsity_mask = (
            None
            if sparsity_mask is None
            else torch.nn.Parameter(
                data=torch.from_numpy(np.abs(sparsity_mask.T).astype(np.float32)),
                requires_grad=False,
            )
        )

        self.mode = mode
        self.neuromod_network_dims = neuromod_network_dims

        if backbone_activation == "silu":
            backbone_activation = nn.SiLU
        elif backbone_activation == "relu":
            backbone_activation = nn.ReLU
        elif backbone_activation == "tanh":
            backbone_activation = nn.Tanh
        elif backbone_activation == "gelu":
            backbone_activation = nn.GELU
        elif backbone_activation == "lecun_tanh":
            backbone_activation = LeCun
        else:
            raise ValueError(f"Unknown activation {backbone_activation}")

        self.backbone = None
        self.backbone_layers = backbone_layers
        if backbone_layers > 0:
            layer_list = [
                nn.Linear(input_size + hidden_size, backbone_units),
                backbone_activation(),
            ]
            for i in range(1, backbone_layers):
                layer_list.append(nn.Linear(backbone_units, backbone_units))
                layer_list.append(backbone_activation())
                if backbone_dropout > 0.0:
                    layer_list.append(torch.nn.Dropout(backbone_dropout))
            self.backbone = nn.Sequential(*layer_list)
        self.tanh = nn.Tanh()
        self.sigmoid = nn.Sigmoid()
        cat_shape = int(
            self.hidden_size + input_size if backbone_layers == 0 else backbone_units
        )

        self.ff1 = nn.Linear(cat_shape, hidden_size)
        if self.mode == "pure" or self.mode == "neuromodulated":
            self.w_tau = torch.nn.Parameter(
                data=torch.zeros(1, self.hidden_size), requires_grad=True
            )
            self.A = torch.nn.Parameter(
                data=torch.ones(1, self.hidden_size), requires_grad=True
            )

            if self.mode == "neuromodulated":
                assert neuromod_network_dims is not None, "Neuromodulation network dimensions must be set"
                assert neuromod_network_dims[-1] == hidden_size, "Last layer of neuromodulation network must have the same size as the hidden state"
                
                layer_list = []
                for i in range(len(neuromod_network_dims) - 1):
                    layer_list.append(nn.Linear(neuromod_network_dims[i], neuromod_network_dims[i + 1]))
                    layer_list.append(neuromod_network_activation())
                self.neuromod = nn.Sequential(*layer_list)
        else:
            self.ff2 = nn.Linear(cat_shape, hidden_size)
            self.time_a = nn.Linear(cat_shape, hidden_size)
            self.time_b = nn.Linear(cat_shape, hidden_size)

        # Create a vector to store tau system values. It is just for
        # storing calculated values, these values aren't learned.
        self.tau_system = torch.nn.Parameter(
            torch.ones((self.hidden_size,)), requires_grad=False
        )

        self.init_weights()

    def init_weights(self):
        for w in self.parameters():
            if w.dim() == 2 and w.requires_grad:
                torch.nn.init.xavier_uniform_(w)


    # Function to replace the neuromodulation network
    # by a different neuromodulation network. 
    def change_neuromodulation_network(self, network):
        
        # Check if actually operating in neuromodulated mode
        assert self.mode == "neuromodulated", "Neuromodulation network can only be set in neuromodulated mode"
        
        # Check if the input and output sizes of the new
        # network are correct.
        correct_input_size = self.neuromod_network_dims[0]
        input_tensor = torch.rand((1, correct_input_size))
        try:
            output = network(input_tensor)
            assert output.shape[1] == self.neuromod_network_dims[-1], "Neuromodulation network doesn't have correct output size"
        except:
            raise ValueError(f"New neuromodulation network doesn't have correct input size of {correct_input_size}")
        
        self.neuromod = network



    def forward(self, input, hx, ts):
        # If neuromodulation is used, the input should actually be a tuple
        # containing the policy input and the neuromodulation input.
        # Otherwise, the input is just the policy input.
        if self.mode == "neuromodulated":
            assert isinstance(input, tuple), "Input must be a tuple (policy_input, neuromod_input)"
            x = torch.cat([input[0], hx], 1)

        else:
            x = torch.cat([input, hx], 1)

        if self.backbone_layers > 0:
            x = self.backbone(x)
        
        if self.sparsity_mask is not None:
            ff1 = F.linear(x, self.ff1.weight * self.sparsity_mask, self.ff1.bias)
        else:
            ff1 = self.ff1(x)
        
        if self.mode == "pure":
            # Solution
            new_hidden = (
                -self.A
                * torch.exp(-ts * (torch.abs(self.w_tau) + torch.abs(ff1)))
                * ff1
                + self.A
            )

            # This calculation of the tau system seems to be in accordance
            # with equations 1, 2, and 3 in "Closed-form Continuous-time
            # Neural Networks".
            self.tau_system.data = 1.0 / (torch.abs(self.w_tau) + torch.abs(ff1))
        elif self.mode == "neuromodulated":
            neuromod_signal = self.neuromod(input[1])

            new_hidden = (
                -self.A
                * torch.exp(-ts * (torch.abs(self.w_tau) + torch.abs(neuromod_signal)))
                * ff1
                + self.A
            )

            # This calculation of the tau system seems to be in accordance
            # with equations 1, 2, and 3 in "Closed-form Continuous-time
            # Neural Networks".
            self.tau_system.data = 1.0 / (torch.abs(self.w_tau) + torch.abs(neuromod_signal))
        else:
            # Cfc
            if self.sparsity_mask is not None:
                ff2 = F.linear(x, self.ff2.weight * self.sparsity_mask, self.ff2.bias)
            else:
                ff2 = self.ff2(x)
            ff1 = self.tanh(ff1)
            ff2 = self.tanh(ff2)
            t_a = self.time_a(x)
            t_b = self.time_b(x)
            t_interp = self.sigmoid(t_a * ts + t_b)
            if self.mode == "no_gate":
                new_hidden = ff1 + t_interp * ff2
            else:
                new_hidden = ff1 * (1.0 - t_interp) + t_interp * ff2
        return new_hidden, new_hidden