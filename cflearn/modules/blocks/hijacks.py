import torch.nn as nn

from torch import Tensor
from typing import Any
from typing import Optional

from .hooks import IHook


class IHijackMixin:
    def __init__(self, *args: Any, hook: Optional[IHook] = None, **kwargs: Any):
        self.args = args
        self.kwargs = shallow_copy_dict(kwargs)
        super().__init__(*args, **kwargs)
        self.hook = hook

    def forward(self, net: Tensor) -> Tensor:
        inp = net
        net = super().forward(net)  # type: ignore
        if self.hook is not None:
            net = self.hook.callback(inp, net)
        return net


class HijackLinear(IHijackMixin, nn.Linear):
    pass


class HijackConv1d(IHijackMixin, nn.Conv1d):
    pass


class HijackConv2d(IHijackMixin, nn.Conv2d):
    pass


class HijackConv3d(IHijackMixin, nn.Conv3d):
    pass
