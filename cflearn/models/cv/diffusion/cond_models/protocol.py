import torch.nn as nn

from abc import abstractmethod
from abc import ABCMeta
from torch import Tensor
from typing import Any
from typing import Dict
from typing import Type
from cftool.misc import WithRegister


condition_models: Dict[str, Type["IConditionModel"]] = {}
specialized_condition_models: Dict[str, Type[nn.Module]] = {}


class IConditionModel(nn.Module, WithRegister, metaclass=ABCMeta):
    d = condition_models

    def __init__(self, m: nn.Module):
        super().__init__()
        self.m = m

    @abstractmethod
    def forward(self, cond: Any) -> Tensor:
        pass


class ISpecializedConditionModel(nn.Module, WithRegister, metaclass=ABCMeta):
    d = specialized_condition_models

    @abstractmethod
    def forward(self, cond: Any) -> Tensor:
        pass


__all__ = [
    "condition_models",
    "specialized_condition_models",
    "IConditionModel",
    "ISpecializedConditionModel",
]
