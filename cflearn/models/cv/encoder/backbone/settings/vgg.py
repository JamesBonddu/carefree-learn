from typing import List

from ..api import Preset


remove_layers: List[str] = []
target_layers = {
    "slice1": "stage1",
    "slice2": "stage2",
    "slice3": "stage3",
    "slice4": "stage4",
}


@Preset.register_settings()
class VGGPreset(Preset):
    remove_layers = {
        "vgg16": remove_layers,
        "vgg19": remove_layers,
    }
    target_layers = {
        "vgg16": target_layers,
        "vgg19": target_layers,
    }
    latent_dims = {
        "vgg16": 512,
        "vgg19": 512,
    }
    num_downsample = {
        "vgg16": 4,
        "vgg19": 4,
    }
    increment_configs = {
        "vgg16": {"out_channels": [64, 128, 256, 512]},
        "vgg19": {"out_channels": [64, 128, 256, 512]},
    }


__all__ = ["VGGPreset"]
