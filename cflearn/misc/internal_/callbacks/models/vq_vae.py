import os
import math

from typing import Optional

from ..generator import ArtifactCallback
from .....trainer import Trainer
from .....constants import INPUT_KEY
from .....constants import PREDICTIONS_KEY
from .....misc.toolkit import to_device
from .....misc.toolkit import interpolate
from .....misc.toolkit import save_images
from .....misc.toolkit import eval_context
from .....misc.toolkit import make_indices_visualization_map


@ArtifactCallback.register("vq_vae")
class VQVAECallback(ArtifactCallback):
    key = "images"

    def __init__(self, num_keep: int = 25, num_classes: Optional[int] = None):
        super().__init__(num_keep)
        self.num_classes = num_classes

    def log_artifacts(self, trainer: Trainer) -> None:
        if not self.is_rank_0:
            return None
        batch = next(iter(trainer.validation_loader))
        batch = to_device(batch, trainer.device)
        original = batch[INPUT_KEY]
        model = trainer.model
        state = trainer.state
        with eval_context(model):
            outputs = model(0, batch, state)
        reconstructed = outputs[PREDICTIONS_KEY]
        image_folder = self._prepare_folder(trainer)
        save_images(original, os.path.join(image_folder, "original.png"))
        save_images(reconstructed, os.path.join(image_folder, "reconstructed.png"))
        with eval_context(model):
            codes, indices = model.sample_codebook(num_samples=len(original))
        save_images(codes, os.path.join(image_folder, "codes.png"))
        indices_map = make_indices_visualization_map(indices)
        save_images(indices_map, os.path.join(image_folder, "code_indices.png"))
        if self.num_classes is not None:
            with eval_context(model):
                for i in range(self.num_classes):
                    kwargs = dict(num_samples=len(original), class_idx=i)
                    codes, indices = model.sample_codebook(**kwargs)
                    save_images(codes, os.path.join(image_folder, f"codes_{i}.png"))
                    indices_map = make_indices_visualization_map(indices)
                    i_path = os.path.join(image_folder, f"code_indices_{i}.png")
                    save_images(indices_map, i_path)
        # inspect
        sample = reconstructed[:1]
        sample_indices = outputs["indices"][0].view(-1)
        sample_map = make_indices_visualization_map(sample_indices)
        with eval_context(model):
            sample_vis = model.sample_codebook(code_indices=sample_indices)[0]
        scaled = interpolate(sample, factor=math.sqrt(len(sample_indices)))
        save_images(scaled, os.path.join(image_folder, "sampled.png"))
        save_images(sample_map, os.path.join(image_folder, "sampled_idx.png"))
        save_images(sample_vis, os.path.join(image_folder, "sampled_codes.png"))


__all__ = [
    "VQVAECallback",
]
