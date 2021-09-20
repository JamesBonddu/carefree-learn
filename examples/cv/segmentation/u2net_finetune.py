# type: ignore

import os
import cv2
import cflearn

import numpy as np

from typing import List
from cflearn.misc.toolkit import check_is_ci
from cflearn.misc.toolkit import download_dataset
from cflearn.misc.toolkit import min_max_normalize


is_ci = check_is_ci()


def prepare(ci: bool) -> str:
    def label_fn(hierarchy: List[str]) -> str:
        file_id = os.path.splitext(hierarchy[-1])[0]
        os.makedirs(label_folder, exist_ok=True)
        label_path = os.path.abspath(os.path.join(label_folder, f"{file_id}.npy"))
        if os.path.isfile(label_path):
            return label_path
        rgba_path = os.path.abspath(os.path.join(src_rgba_folder, f"{file_id}.png"))
        alpha = cv2.imread(rgba_path, cv2.IMREAD_UNCHANGED)[..., -1:]
        alpha = min_max_normalize(alpha.astype(np.float32), global_norm=True)
        np.save(label_path, alpha)
        return label_path

    data_root = "../data" if ci else "data"
    dataset = "products-10k_tiny"
    if not ci:
        data_folder = data_root
    else:
        data_folder = os.path.join(data_root, dataset)
    src_folder = os.path.join(data_folder, "raw")
    src_rgba_folder = os.path.join(data_folder, "rgba")
    tgt_folder = os.path.join(data_folder, "products-10k")
    label_folder = os.path.join(data_folder, "products-10k_labels")
    if ci and not os.path.isdir(src_folder):
        download_dataset(dataset, root=data_root)
    cflearn.cv.prepare_image_folder(
        src_folder,
        tgt_folder,
        to_index=False,
        label_fn=label_fn,
        make_labels_in_parallel=not ci,
        num_jobs=0 if ci else 8,
    )
    return tgt_folder


pretrained_ckpt = "pretrained/model_lite.pt"

if __name__ == "__main__":
    data = cflearn.cv.ImageFolderData(
        prepare(is_ci),
        batch_size=16,
        num_workers=2 if is_ci else 4,
        transform=cflearn.cv.ABundleTransform(label_alias="mask"),
        test_transform=cflearn.cv.ABundleTestTransform(label_alias="mask"),
    )
    pipeline_config = dict(
        model_name="u2net",
        model_config={
            "in_channels": 3,
            "out_channels": 1,
            "lite": True,
            # "upsample_mode": "nearest",
        },
        loss_name="multi_stage:bce,iou",
        loss_metrics_weights={"bce0": 0.2, "iou0": 0.8},
        callback_names=["u2net", "mlflow"],
        callback_configs={"mlflow": {"experiment_name": "lite_pretrain"}},
        # lr=2.0e-3,
        scheduler_name="none",
        fixed_steps=0 if is_ci else None,
    )
    if not is_ci:
        pipeline_config["finetune_config"] = {
            # "pretrained_ckpt": "pretrained/model.pt",
            "pretrained_ckpt": pretrained_ckpt,
            # "freeze_except": r"(.*\.side_blocks\..*|.*\.out\..*)",
        }
    else:
        import torch

        m = cflearn.cv.CarefreePipeline(**pipeline_config).fit(data)
        os.makedirs(os.path.dirname(pretrained_ckpt), exist_ok=True)
        torch.save(m.model.state_dict(), pretrained_ckpt)
        pipeline_config["fixed_steps"] = 1
        pipeline_config["finetune_config"] = {"pretrained_ckpt": pretrained_ckpt}
    m = cflearn.cv.CarefreePipeline(**pipeline_config)
    m.fit(data, cuda=None if is_ci else 0)
    # m.ddp(data, cuda_list=[1, 2, 3, 4])
