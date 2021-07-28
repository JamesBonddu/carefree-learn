import os
import torch

import numpy as np

from ...types import tensor_dict_type
from ...trainer import Trainer
from ...constants import INPUT_KEY
from ...constants import LABEL_KEY
from ...misc.toolkit import to_numpy
from ...misc.toolkit import to_torch
from ...misc.toolkit import to_device
from ...misc.toolkit import save_images
from ...misc.toolkit import eval_context
from ...misc.toolkit import normalize_image
from ...misc.internal_.callback import ArtifactCallback


class AlphaSegmentationCallback(ArtifactCallback):
    key = "images"

    def _save_seg_results(
        self,
        trainer: Trainer,
        batch: tensor_dict_type,
        seg_map: torch.Tensor,
    ) -> None:
        original = batch[INPUT_KEY]
        label = batch[LABEL_KEY].float()
        image_folder = self._prepare_folder(trainer)
        save_images(original, os.path.join(image_folder, "original.png"))
        save_images(label, os.path.join(image_folder, "label.png"))
        save_images(seg_map, os.path.join(image_folder, "mask.png"))
        bundle = [original, label, seg_map]
        np_original, np_label, np_mask = map(normalize_image, map(to_numpy, bundle))
        rgba = np.concatenate([np_original, np_label], axis=1)
        rgba_pred = np.concatenate([np_original, np_mask], axis=1)
        save_images(to_torch(rgba), os.path.join(image_folder, "rgba.png"))
        save_images(to_torch(rgba_pred), os.path.join(image_folder, "rgba_pred.png"))

    def log_artifacts(self, trainer: Trainer) -> None:
        if not self.is_rank_0:
            return None
        batch = next(iter(trainer.validation_loader))
        batch = to_device(batch, trainer.device)
        original = batch[INPUT_KEY]
        image_folder = self._prepare_folder(trainer)
        save_images(original, os.path.join(image_folder, "original.png"))
        label = batch[LABEL_KEY].float()
        save_images(label, os.path.join(image_folder, "label.png"))
        with eval_context(trainer.model):
            seg_map = trainer.model.generate_from(original)
            save_images(seg_map, os.path.join(image_folder, "segmentation.png"))


__all__ = [
    "AlphaSegmentationCallback",
]
