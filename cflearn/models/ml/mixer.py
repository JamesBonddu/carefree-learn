from torch import Tensor
from typing import Any
from typing import Dict
from typing import Optional

from .protocol import register_ml_module
from .protocol import MixedStackedModel
from ..bases import BAKEBase
from ..bases import RDropoutBase
from ...types import tensor_dict_type
from ...constants import LATENT_KEY
from ...constants import PREDICTIONS_KEY


@register_ml_module("mixer")
class Mixer(MixedStackedModel):
    def __init__(
        self,
        in_dim: int,
        out_dim: int,
        num_history: int,
        latent_dim: int = 256,
        *,
        num_layers: int = 4,
        dropout: float = 0.0,
        norm_type: Optional[str] = "batch_norm",
    ):
        super().__init__(
            in_dim,
            out_dim,
            num_history,
            latent_dim,
            token_mixing_type="mlp",
            num_layers=num_layers,
            dropout=dropout,
            norm_type=norm_type,
            feedforward_dim_ratio=1.0,
            use_head_token=False,
            use_positional_encoding=False,
        )


@register_ml_module("mixer_bake")
class MixerWithBAKE(BAKEBase):
    def __init__(
        self,
        in_dim: int,
        out_dim: int,
        num_history: int,
        latent_dim: int = 256,
        *,
        num_layers: int = 4,
        dropout: float = 0.1,
        norm_type: Optional[str] = "batch_norm",
        lb: float = 0.1,
        bake_loss: str = "auto",
        bake_loss_config: Optional[Dict[str, Any]] = None,
        w_ensemble: float = 0.5,
        is_classification: bool,
    ):
        super().__init__()
        self.mixer = Mixer(
            in_dim,
            out_dim,
            num_history,
            latent_dim,
            num_layers=num_layers,
            dropout=dropout,
            norm_type=norm_type,
        )
        self._init_bake(lb, bake_loss, bake_loss_config, w_ensemble, is_classification)

    def forward(self, net: Tensor) -> tensor_dict_type:
        net = self.mixer.to_encoder(net)
        latent = self.mixer.encoder(net)
        net = self.mixer.head(latent)
        return {LATENT_KEY: latent, PREDICTIONS_KEY: net}


@register_ml_module("mixer_r_dropout")
class MixerWithRDropout(RDropoutBase):
    def __init__(
        self,
        in_dim: int,
        out_dim: int,
        num_history: int,
        latent_dim: int = 256,
        *,
        num_layers: int = 4,
        dropout: float = 0.1,
        norm_type: Optional[str] = "batch_norm",
        lb: float = 0.1,
        is_classification: bool,
    ):
        super().__init__()
        self.mixer = Mixer(
            in_dim,
            out_dim,
            num_history,
            latent_dim,
            num_layers=num_layers,
            dropout=dropout,
            norm_type=norm_type,
        )
        # R-Dropout
        self.lb = lb
        self.is_classification = is_classification

    def forward(self, net: Tensor) -> tensor_dict_type:
        return self.mixer(net)


__all__ = [
    "Mixer",
    "MixerWithBAKE",
    "MixerWithRDropout",
]
