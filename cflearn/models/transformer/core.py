import torch

import torch.nn as nn

from typing import *

from ..fcnn.core import FCNNCore
from ...misc.toolkit import Activations
from ...modules.blocks import Linear
from ...modules.blocks import Dropout
from ...modules.blocks import Attention


class TransformerLayer(nn.Module):
    def __init__(
        self,
        input_dim: int,
        num_heads: int,
        *,
        dropout: float = 0.1,
        latent_dim: int = 2048,
        activation: str = "ReLU",
        attention_config: Optional[Dict[str, Any]] = None,
        activation_config: Optional[Dict[str, Any]] = None,
        to_latent_config: Optional[Dict[str, Any]] = None,
        from_latent_config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__()
        if attention_config is None:
            attention_config = {}
        attention_config["is_self_attention"] = True
        attention_config.setdefault("dropout", dropout)
        self.self_attn = Attention(input_dim, num_heads, **attention_config)
        if to_latent_config is None:
            to_latent_config = {}
        self.to_latent = Linear(input_dim, latent_dim, **to_latent_config)
        self.dropout = Dropout(dropout)
        if from_latent_config is None:
            from_latent_config = {}
        self.from_latent = Linear(latent_dim, input_dim, **from_latent_config)

        self.norm1 = nn.LayerNorm(input_dim)
        self.norm2 = nn.LayerNorm(input_dim)
        self.dropout1 = Dropout(dropout)
        self.dropout2 = Dropout(dropout)
        self.activation = Activations.make(activation, activation_config)

    def forward(self, net: torch.Tensor, mask: torch.Tensor = None) -> torch.Tensor:
        new = self.self_attn(net, net, net, mask=mask).output
        net = net + self.dropout1(new)
        net = self.norm1(net)
        new = self.from_latent(self.dropout(self.activation(self.to_latent(net))))
        net = net + self.dropout2(new)
        net = self.norm2(net)
        return net


class TransformerCore(nn.Module):
    def __init__(
        self,
        in_dim: int,
        out_dim: int,
        num_heads: int,
        num_layers: int,
        num_history: int,
        hidden_units: List[int],
        to_latent: bool = False,
        norm: Optional[Callable] = None,
        latent_dim: Optional[int] = None,
        input_linear_config: Optional[Dict[str, Any]] = None,
        transformer_layer_config: Optional[Dict[str, Any]] = None,
        mapping_configs: Optional[Union[Dict[str, Any], List[Dict[str, Any]]]] = None,
        final_mapping_config: Optional[Dict[str, Any]] = None,
    ):
        super().__init__()
        # latent projection
        if latent_dim is not None and not to_latent:
            msg = "`latent_dim` is provided but `to_latent` is set to False"
            raise ValueError(msg)
        if latent_dim is None:
            latent_dim = 256 if to_latent else in_dim
        if not to_latent:
            self.input_linear = None
        else:
            if input_linear_config is None:
                input_linear_config = {}
            input_linear_config.setdefault("bias", False)
            self.input_linear = Linear(in_dim, latent_dim, **input_linear_config)
            in_dim = latent_dim
        # transformer blocks
        self.norm = norm
        if transformer_layer_config is None:
            transformer_layer_config = {}
        self.layers = nn.ModuleList(
            [
                TransformerLayer(
                    in_dim,
                    num_heads,
                    **transformer_layer_config,
                )
                for _ in range(num_layers)
            ]
        )
        # final projection
        self.fcnn = FCNNCore(
            in_dim * num_history,
            out_dim,
            hidden_units,
            mapping_configs,
            final_mapping_config,
        )

    def forward(self, net: torch.Tensor, mask: Optional[torch.Tensor]) -> torch.Tensor:
        if self.input_linear is not None:
            net = self.input_linear(net)
        for layer in self.layers:
            net = layer(net, mask=mask)
        if self.norm is not None:
            net = self.norm(net)
        return self.fcnn(net.view(net.shape[0], -1))


__all__ = ["TransformerCore"]
