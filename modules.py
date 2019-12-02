import inspect
import sys

import numpy as np
import torch
from torch import nn
import torch.nn.functional as F
from torch.nn import init
import math
from torch.nn import Sequential as Seq, Linear as Lin, ReLU, BatchNorm1d as BN
from torch_geometric.datasets import ModelNet
import torch_geometric.transforms as T
from torch_geometric.data import DataLoader
from torch_geometric.nn import PointConv, fps, radius, global_max_pool, MessagePassing
from torch.nn.parameter import Parameter
from kernel_utils import kernel_point_optimization_debug

special_args = [
    'edge_index', 'edge_index_i', 'edge_index_j', 'size', 'size_i', 'size_j'
]
is_python2 = sys.version_info[0] < 3
getargspec = inspect.getargspec if is_python2 else inspect.getfullargspec
class PointKernel(MessagePassing):

    def __init__(self, num_points, in_features, out_features, radius=1, kernel_dim=3, fixed='center', ratio=1, KP_influence='linear'):
        super(PointKernel, self).__init__()
        # PointKernel parameters
        self.in_features = in_features
        self.out_features = out_features
        self.num_points = num_points
        self.radius = radius
        self.kernel_dim = kernel_dim
        self.fixed = fixed
        self.ratio = ratio
        self.KP_influence = KP_influence
        
        # Radius of the initial positions of the kernel points
        self.KP_extent = radius / 1.5

        # Point position in kernel_dim
        self.kernel = Parameter(torch.Tensor(1, num_points, kernel_dim))
        
        # Associated weights
        self.kernel_weight = Parameter(torch.Tensor(num_points, in_features, out_features))

        self.reset_parameters()

    def reset_parameters(self):
        init.kaiming_uniform_(self.kernel_weight, a=math.sqrt(5))

        # Init the kernel using attrative + repulsion forces
        kernel, _ = kernel_point_optimization_debug(self.radius, self.num_points, num_kernels=1, \
            dimension=self.kernel_dim, fixed=self.fixed, ratio=self.ratio, verbose=False)
        self.kernel.data = torch.from_numpy(kernel)
    
    def get_message_argument(self):
        self.__message_args__ = getargspec(self.message)[0][1:]
        self.__special_args__ = [(i, arg)
                                 for i, arg in enumerate(self.__message_args__)
                                 if arg in special_args]
        self.__message_args__ = [
            arg for arg in self.__message_args__ if arg not in special_args
        ]   

    def define_message(self, x):
        if not hasattr(self, "messsage_is_defined"):
            self.x_is_none = x is None
            if self.x_is_none:
                self.message = self.message_pos
                self.get_message_argument()
                self.messsage_is_defined = True
            else:
                self.message = self.message_x_and_pos
                self.get_message_argument()
                self.messsage_is_defined = True      

    def forward(self, x, pos, edge_index):
        self.define_message(x)
        if self.x_is_none:
            return self.propagate(edge_index, pos=pos)
        else:
            return self.propagate(edge_index, x=x, pos=pos[0])

    def message_pos(self, pos_i, pos_j):
        return self.message_forward(pos_i=pos_i, pos_j=pos_j)

    def message_x_and_pos(self, x_i, x_j, pos_i, pos_j):
        return self.message_forward(x_i=x_i, x_j=x_j, pos_i=pos_i, pos_j=pos_j)

    def message_forward(self, **kwargs):
        if self.x_is_none:
            x_i = pos_i = kwargs.get("pos_i")
            x_j = pos_j = kwargs.get("pos_j")
        else:
            x_i = kwargs.get("x_i")
            x_j = kwargs.get("x_j")
            pos_i = kwargs.get("pos_i")
            pos_j = kwargs.get("pos_j")

        #  # Center every neighborhood [n_points, n_neighbors, dim]
        neighbors = (pos_i -  pos_j).view((-1, self.num_points, 3))

        # Number of support points
        n_points = neighbors.shape[0]
        
        #Get points kernels
        K_points = self.kernel

        # Get all difference matrices [[n_points, n_neighbors, n_kpoints, dim]
        neighbors = neighbors.unsqueeze(2)

        differences = neighbors - K_points.float().view((-1, 3)).unsqueeze(0).unsqueeze(0)
        sq_distances = (differences**2).sum(-1)

        # Get Kernel point influences [n_points, n_kpoints, n_neighbors]
        if self.KP_influence == 'constant':
            # Every point get an influence of 1.
            all_weights = torch.ones_like(sq_distances)

        elif self.KP_influence == 'linear':
            # Influence decrease linearly with the distance, and get to zero when d = KP_extent.
            all_weights = 1. - (torch.sqrt(sq_distances) / self.KP_extent)
            all_weights[all_weights < 0] = 0.0
        else:
            raise ValueError('Unknown influence function type (config.KP_influence)')
        
        neighbors_1nn = torch.argmin(sq_distances, dim=-1)
        one_hot = torch.zeros_like(all_weights)
        one_hot.scatter_(2, neighbors_1nn.unsqueeze(-1), 1)
        all_weights *= one_hot
        
        K_weights = self.kernel_weight
        K_weights = torch.index_select(K_weights, 0, neighbors_1nn.view(-1)).view((n_points, -1, self.in_features, self.out_features))

        # Get the features of each neighborhood [n_points, n_neighbors, in_fdim]
        features = x_j.view((-1, self.num_points, self.in_features))

        # Apply distance weights [n_points, n_kpoints, in_fdim]
        weighted_features = torch.matmul(all_weights, features)

        # Apply network weights [n_kpoints, n_points, out_fdim]
        out = torch.matmul(weighted_features.unsqueeze(-2), \
            K_weights)
        out = out.view(-1, self.out_features)
        return out

    def update(self, aggr_out):
        return aggr_out

class KPConv(nn.Module):
    def __init__(self, ratio, radius, in_features, out_features, num_points=16):
        super(KPConv, self).__init__()       
        self.ratio = ratio
        self.radius = radius
        self.in_features = in_features
        self.out_features = out_features
        self.num_points = num_points

        self.conv = PointKernel(self.num_points, self.in_features, self.out_features, radius=self.radius)

    def forward(self, x, pos, batch):
        idx = fps(pos, batch, ratio=self.ratio)
        row, col = radius(pos, pos[idx], self.radius, batch, batch[idx],
                          max_num_neighbors=self.num_points)
        edge_index = torch.stack([col, row], dim=0)
        x = self.conv(x, (pos, pos[idx]), edge_index)
        pos, batch = pos[idx], batch[idx]
        return x, pos, batch

class SAModule(torch.nn.Module):
    def __init__(self, ratio, r, nn):
        super(SAModule, self).__init__()
        self.ratio = ratio
        self.r = r
        self.conv = PointConv(nn)

    def forward(self, x, pos, batch):
        idx = fps(pos, batch, ratio=self.ratio)
        row, col = radius(pos, pos[idx], self.r, batch, batch[idx],
                          max_num_neighbors=64)
        edge_index = torch.stack([col, row], dim=0)
        x = self.conv(x, (pos, pos[idx]), edge_index)
        pos, batch = pos[idx], batch[idx]
        return x, pos, batch


class GlobalSAModule(torch.nn.Module):
    def __init__(self, nn):
        super(GlobalSAModule, self).__init__()
        self.nn = nn

    def forward(self, x, pos, batch):
        x = self.nn(torch.cat([x, pos], dim=1))
        x = global_max_pool(x, batch)
        pos = pos.new_zeros((x.size(0), 3))
        batch = torch.arange(x.size(0), device=batch.device)
        return x, pos, batch


def MLP(channels, batch_norm=True):
    return Seq(*[
        Seq(Lin(channels[i - 1], channels[i]), ReLU(), BN(channels[i]))
        for i in range(1, len(channels))
    ])
