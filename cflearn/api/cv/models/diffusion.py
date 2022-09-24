import torch
import random

import numpy as np
import torch.nn as nn
import torch.nn.functional as F

from PIL import Image
from tqdm import tqdm
from torch import Tensor
from typing import Any
from typing import Dict
from typing import List
from typing import Tuple
from typing import Union
from typing import Callable
from typing import Optional
from cftool.misc import safe_execute
from cftool.misc import shallow_copy_dict
from cftool.array import save_images
from cftool.types import tensor_dict_type

from .common import read_image
from .common import restrict_wh
from .common import get_suitable_size
from .common import APIMixin
from ....zoo import DLZoo
from ....data import predict_tensor_data
from ....data import TensorInferenceData
from ....pipeline import DLPipeline
from ....constants import INPUT_KEY
from ....misc.toolkit import slerp
from ....misc.toolkit import new_seed
from ....misc.toolkit import eval_context
from ....misc.toolkit import seed_everything
from ....modules.blocks import Conv2d
from ....models.cv.diffusion import LDM
from ....models.cv.diffusion import DDPM
from ....models.cv.diffusion import ISampler
from ....models.cv.ae.common import IAutoEncoder
from ....models.cv.diffusion.utils import get_timesteps
from ....models.cv.diffusion.samplers.ddim import DDIMMixin
from ....models.cv.diffusion.samplers.k_samplers import KSamplerMixin


class DiffusionAPI(APIMixin):
    m: DDPM
    sampler: ISampler
    cond_model: Optional[nn.Module]
    first_stage: Optional[IAutoEncoder]
    latest_seed: int
    latest_variation_seed: Optional[int]

    def __init__(
        self,
        m: DDPM,
        device: torch.device,
        *,
        use_amp: bool = False,
        use_half: bool = False,
    ):
        super().__init__(m, device, use_amp=use_amp, use_half=use_half)
        self.sampler = m.sampler
        self.cond_type = m.condition_type
        # extracted the condition model so we can pre-calculate the conditions
        self.cond_model = m.condition_model
        if self.cond_model is not None:
            self.cond_model.eval()
        m.condition_model = nn.Identity()
        # pre-calculate unconditional_cond if needed
        unconditional_cond = getattr(m.sampler, "unconditional_cond", None)
        if self.cond_model is not None and unconditional_cond is not None:
            uncond = self.get_cond(m.sampler.unconditional_cond)
            m.sampler.unconditional_cond = uncond.to(self.device)
        # extract first stage
        if not isinstance(m, LDM):
            self.first_stage = None
        else:
            self.first_stage = m.first_stage.core

    def get_cond(self, cond: Any) -> Tensor:
        if self.cond_model is None:
            msg = "should not call `get_cond` when `cond_model` is not available"
            raise ValueError(msg)
        with torch.no_grad():
            with self.amp_context:
                return self.cond_model(cond)

    def switch_sampler(
        self,
        sampler: str,
        sampler_config: Optional[Dict[str, Any]] = None,
    ) -> None:
        sampler_ins = self.m.make_sampler(sampler, sampler_config)
        current_unconditional_cond = getattr(self.m.sampler, "unconditional_cond", None)
        if current_unconditional_cond is not None:
            if hasattr(sampler_ins, "unconditional_cond"):
                sampler_ins.unconditional_cond = current_unconditional_cond
        current_guidance = getattr(self.m.sampler, "unconditional_guidance_scale", None)
        if current_guidance is not None:
            if hasattr(sampler_ins, "unconditional_guidance_scale"):
                sampler_ins.unconditional_guidance_scale = current_guidance
        self.sampler = self.m.sampler = sampler_ins

    def switch_circular(self, enable: bool) -> None:
        def _inject(m: nn.Module) -> None:
            for child in m.children():
                _inject(child)
            modules.append(m)

        padding_mode = "circular" if enable else "zeros"
        modules: List[nn.Module] = []
        _inject(self.m)
        for module in modules:
            if isinstance(module, nn.Conv2d):
                module.padding_mode = padding_mode
            elif isinstance(module, Conv2d):
                module.padding = padding_mode

    def sample(
        self,
        num_samples: int,
        export_path: Optional[str] = None,
        *,
        seed: Optional[int] = None,
        # each variation contains (seed, weight)
        variations: Optional[List[Tuple[int, float]]] = None,
        variation_seed: Optional[int] = None,
        variation_strength: Optional[float] = None,
        z: Optional[Tensor] = None,
        z_ref: Optional[Tensor] = None,
        z_ref_mask: Optional[Tensor] = None,
        z_ref_noise: Optional[Tensor] = None,
        size: Optional[Tuple[int, int]] = None,
        original_size: Optional[Tuple[int, int]] = None,
        alpha: Optional[np.ndarray] = None,
        cond: Optional[Any] = None,
        num_steps: Optional[int] = None,
        clip_output: bool = True,
        callback: Optional[Callable[[Tensor], Tensor]] = None,
        batch_size: int = 1,
        verbose: bool = True,
        **kwargs: Any,
    ) -> Tensor:
        if cond is not None:
            if self.cond_type != "concat" and self.cond_model is not None:
                data = TensorInferenceData(cond, batch_size=batch_size)
                cond = predict_tensor_data(self.cond_model, data)
        if cond is not None and num_samples != len(cond):
            raise ValueError(
                f"`num_samples` ({num_samples}) should be identical with "
                f"the number of `cond` ({len(cond)})"
            )
        if alpha is not None and original_size is not None:
            alpha_h, alpha_w = alpha.shape[-2:]
            if alpha_w != original_size[0] or alpha_h != original_size[1]:
                raise ValueError(
                    f"shape of the provided `alpha` ({alpha_w}, {alpha_h}) should be "
                    f"identical with the provided `original_size` {original_size}"
                )
        unconditional = cond is None
        if unconditional:
            cond = [0] * num_samples
        iterator = TensorInferenceData(cond, batch_size=batch_size).initialize()[0]
        num_iter = len(iterator)
        if verbose and num_iter > 1:
            iterator = tqdm(iterator, desc="iter", total=num_iter)
        sampled = []
        kw = dict(num_steps=num_steps, verbose=verbose)
        kw.update(shallow_copy_dict(kwargs))
        if size is None:
            size = self.m.img_size, self.m.img_size
        else:
            if self.first_stage is None:
                factor = 1
            else:
                factor = self.first_stage.img_size // self.m.img_size
            size = tuple(map(lambda n: round(n / factor), size))  # type: ignore
        with eval_context(self.m):
            with self.amp_context:
                for batch in iterator:
                    i_kw = shallow_copy_dict(kw)
                    i_cond = batch[INPUT_KEY].to(self.device)
                    repeat = lambda t: t.repeat_interleave(len(i_cond), dim=0)
                    if z is not None:
                        i_z = repeat(z)
                    else:
                        i_z_shape = len(i_cond), self.m.in_channels, *size[::-1]
                        i_z, _ = self._set_seed_and_variations(
                            seed,
                            lambda: torch.randn(i_z_shape, device=self.device),
                            lambda noise: noise,
                            variations,
                            variation_seed,
                            variation_strength,
                        )
                    if z_ref is not None and z_ref_mask is not None:
                        if z_ref_noise is not None:
                            i_kw["ref"] = repeat(z_ref)
                            i_kw["ref_mask"] = repeat(z_ref_mask)
                            i_kw["ref_noise"] = repeat(z_ref_noise)
                    if unconditional:
                        i_cond = None
                    if self.use_half:
                        i_z = i_z.half()
                        if i_cond is not None:
                            i_cond = i_cond.half()
                        for k, v in i_kw.items():
                            if isinstance(v, torch.Tensor) and v.is_floating_point():
                                i_kw[k] = v.half()
                    i_sampled = self.m.decode(i_z, cond=i_cond, **i_kw)
                    sampled.append(i_sampled.cpu().float())
        concat = torch.cat(sampled, dim=0)
        if clip_output:
            concat = torch.clip(concat, -1.0, 1.0)
        if callback is not None:
            concat = callback(concat)
        if original_size is not None:
            with torch.no_grad():
                concat = F.interpolate(
                    concat,
                    original_size[::-1],
                    mode="bicubic",
                    antialias=True,
                )
        if alpha is not None:
            alpha = torch.from_numpy(2.0 * alpha - 1.0)
            if original_size is None:
                with torch.no_grad():
                    alpha = F.interpolate(
                        alpha,
                        concat.shape[-2:],
                        mode="nearest",
                    )
            concat = torch.cat([concat, alpha], dim=1)
        if export_path is not None:
            save_images(concat, export_path)
        return concat

    def txt2img(
        self,
        txt: Union[str, List[str]],
        export_path: Optional[str] = None,
        *,
        anchor: int = 64,
        max_wh: int = 512,
        num_samples: Optional[int] = None,
        size: Optional[Tuple[int, int]] = None,
        num_steps: Optional[int] = None,
        clip_output: bool = True,
        callback: Optional[Callable[[Tensor], Tensor]] = None,
        batch_size: int = 1,
        verbose: bool = True,
        **kwargs: Any,
    ) -> Tensor:
        input_is_str = isinstance(txt, str)
        if num_samples is None:
            num_samples = 1 if input_is_str else len(txt)
        if isinstance(txt, str):
            txt = [txt] * num_samples
        if len(txt) != num_samples:
            raise ValueError(
                f"`num_samples` ({num_samples}) should be identical with "
                f"the number of `txt` ({len(txt)})"
            )
        if size is None:
            new_size = None
        else:
            new_size = restrict_wh(*size, max_wh)
            new_size = tuple(map(get_suitable_size, new_size, (anchor, anchor)))  # type: ignore
        return self.sample(
            num_samples,
            export_path,
            size=new_size,
            original_size=size,
            cond=txt,
            num_steps=num_steps,
            clip_output=clip_output,
            callback=callback,
            batch_size=batch_size,
            verbose=verbose,
            **kwargs,
        )

    def img2img(
        self,
        image: Union[str, Image.Image],
        export_path: Optional[str] = None,
        *,
        anchor: int = 32,
        max_wh: int = 512,
        fidelity: float = 0.2,
        alpha: Optional[np.ndarray] = None,
        cond: Optional[Any] = None,
        num_steps: Optional[int] = None,
        clip_output: bool = True,
        verbose: bool = True,
        **kwargs: Any,
    ) -> Tensor:
        res = read_image(image, max_wh, anchor=anchor)
        z = self._get_z(res.image)
        return self._img2img(
            z,
            export_path,
            fidelity=fidelity,
            original_size=res.original_size,
            alpha=res.alpha if alpha is None else alpha,
            cond=cond,
            num_steps=num_steps,
            clip_output=clip_output,
            verbose=verbose,
            **kwargs,
        )

    def inpainting(
        self,
        image: Union[str, Image.Image],
        mask: Union[str, Image.Image],
        export_path: Optional[str] = None,
        *,
        anchor: int = 32,
        max_wh: int = 512,
        alpha: Optional[np.ndarray] = None,
        refine_fidelity: Optional[float] = None,
        num_steps: Optional[int] = None,
        clip_output: bool = True,
        verbose: bool = True,
        **kwargs: Any,
    ) -> Tensor:
        # inpainting callback, will not trigger in refine stage
        def callback(out: Tensor) -> Tensor:
            final = torch.from_numpy(remained_image.copy())
            final += 0.5 * (1.0 + out) * (1.0 - remained_mask)
            return 2.0 * final - 1.0

        # handle mask stuffs
        image_res = read_image(image, max_wh, anchor=anchor)
        mask = read_image(mask, max_wh, anchor=anchor, to_mask=True).image
        bool_mask = mask >= 0.5
        remained_mask = (~bool_mask).astype(np.float16 if self.use_half else np.float32)
        remained_image = remained_mask * image_res.image
        # construct condition tensor
        remained_cond = self._get_z(remained_image)
        latent_shape = remained_cond.shape[-2:]
        mask_cond = torch.where(torch.from_numpy(bool_mask), 1.0, -1.0)
        mask_cond = mask_cond.to(torch.float16 if self.use_half else torch.float32)
        mask_cond = mask_cond.to(self.device)
        mask_cond = F.interpolate(mask_cond, size=latent_shape)
        cond = torch.cat([remained_cond, mask_cond], dim=1)
        # refine with img2img
        if refine_fidelity is not None:
            z = self._get_z(image_res.image)
            return self._img2img(
                z,
                export_path,
                fidelity=refine_fidelity,
                original_size=image_res.original_size,
                alpha=image_res.alpha if alpha is None else alpha,
                cond=cond,
                num_steps=num_steps,
                clip_output=clip_output,
                verbose=verbose,
                **kwargs,
            )
        # sampling
        z = torch.randn_like(remained_cond)
        return self.sample(
            1,
            export_path,
            z=z,
            original_size=image_res.original_size,
            alpha=image_res.alpha if alpha is None else alpha,
            cond=cond,
            num_steps=num_steps,
            clip_output=clip_output,
            callback=callback,
            verbose=verbose,
            **kwargs,
        )

    def sr(
        self,
        image: Union[str, Image.Image],
        export_path: Optional[str] = None,
        *,
        anchor: int = 8,
        max_wh: int = 512,
        alpha: Optional[np.ndarray] = None,
        num_steps: Optional[int] = None,
        clip_output: bool = True,
        verbose: bool = True,
        **kwargs: Any,
    ) -> Tensor:
        if not isinstance(self.m, LDM):
            raise ValueError("`sr` is now only available for `LDM` models")
        factor = 2 ** (len(self.m.first_stage.core.channel_multipliers) - 1)
        res = read_image(image, round(max_wh / factor), anchor=anchor)
        wh_ratio = res.original_size[0] / res.original_size[1]
        zh, zw = res.image.shape[-2:]
        sr_size = (zw, zw / wh_ratio) if zw > zh else (zh * wh_ratio, zh)
        sr_size = tuple(map(lambda n: round(factor * n), sr_size))  # type: ignore
        cond = torch.from_numpy(2.0 * res.image - 1.0).to(self.device)
        z = torch.randn_like(cond)
        return self.sample(
            1,
            export_path,
            z=z,
            original_size=sr_size,
            alpha=res.alpha if alpha is None else alpha,
            cond=cond,
            num_steps=num_steps,
            clip_output=clip_output,
            verbose=verbose,
            **kwargs,
        )

    def semantic2img(
        self,
        semantic: Union[str, Image.Image],
        export_path: Optional[str] = None,
        *,
        anchor: int = 16,
        max_wh: int = 512,
        alpha: Optional[np.ndarray] = None,
        num_steps: Optional[int] = None,
        clip_output: bool = True,
        verbose: bool = True,
        **kwargs: Any,
    ) -> Tensor:
        err_fmt = "`{}` is needed for `semantic2img`"
        if self.cond_model is None:
            raise ValueError(err_fmt.format("cond_model"))
        in_channels = getattr(self.cond_model, "in_channels", None)
        if in_channels is None:
            raise ValueError(err_fmt.format("cond_model.in_channels"))
        factor = getattr(self.cond_model, "factor", None)
        if factor is None:
            raise ValueError(err_fmt.format("cond_model.factor"))
        res = read_image(
            semantic,
            max_wh,
            anchor=anchor,
            to_gray=True,
            resample=Image.NEAREST,
            normalize=False,
        )
        cond = torch.from_numpy(res.image).to(torch.long).to(self.device)
        cond = F.one_hot(cond, num_classes=in_channels)[0]
        cond = cond.half() if self.use_half else cond.float()
        cond = cond.permute(0, 3, 1, 2).contiguous()
        cond = self.get_cond(cond)
        z = torch.randn_like(cond)
        return self.sample(
            1,
            export_path,
            z=z,
            original_size=res.original_size,
            alpha=res.alpha if alpha is None else alpha,
            cond=cond,
            num_steps=num_steps,
            clip_output=clip_output,
            verbose=verbose,
            **kwargs,
        )

    @classmethod
    def from_sd(
        cls,
        device: Optional[str] = None,
        *,
        use_amp: bool = False,
        use_half: bool = False,
    ) -> "DiffusionAPI":
        return cls.from_pipeline(ldm_sd(), device, use_amp=use_amp, use_half=use_half)

    @classmethod
    def from_celeba_hq(
        cls,
        device: Optional[str] = None,
        *,
        use_amp: bool = False,
        use_half: bool = False,
    ) -> "DiffusionAPI":
        m = ldm_celeba_hq()
        return cls.from_pipeline(m, device, use_amp=use_amp, use_half=use_half)

    @classmethod
    def from_inpainting(
        cls,
        device: Optional[str] = None,
        *,
        use_amp: bool = False,
        use_half: bool = False,
    ) -> "DiffusionAPI":
        m = ldm_inpainting()
        return cls.from_pipeline(m, device, use_amp=use_amp, use_half=use_half)

    @classmethod
    def from_sr(
        cls,
        device: Optional[str] = None,
        *,
        use_amp: bool = False,
        use_half: bool = False,
    ) -> "DiffusionAPI":
        return cls.from_pipeline(ldm_sr(), device, use_amp=use_amp, use_half=use_half)

    @classmethod
    def from_semantic(
        cls,
        device: Optional[str] = None,
        *,
        use_amp: bool = False,
        use_half: bool = False,
    ) -> "DiffusionAPI":
        m = ldm_semantic()
        return cls.from_pipeline(m, device, use_amp=use_amp, use_half=use_half)

    def _get_z(self, img: np.ndarray) -> Tensor:
        img = 2.0 * img - 1.0
        z = torch.from_numpy(img)
        if self.use_half:
            z = z.half()
        z = z.to(self.device)
        z = self.m._preprocess(z, deterministic=True)
        return z

    def _set_seed_and_variations(
        self,
        seed: Optional[int],
        get_noise: Callable[[], Tensor],
        get_new_z: Callable[[Tensor], Tensor],
        variations: Optional[List[Tuple[int, float]]],
        variation_seed: Optional[int],
        variation_strength: Optional[float],
    ) -> Tuple[Tensor, Tensor]:
        if seed is None:
            seed = new_seed()
        seed = seed_everything(seed)
        self.latest_seed = seed
        noise = get_noise()
        z = get_new_z(noise)
        self.latest_variation_seed = None
        if variations is not None:
            for v_seed, v_weight in variations:
                seed_everything(v_seed)
                v_noise = get_noise()
                vz = get_new_z(v_noise)
                z = slerp(vz, z, v_weight)
        if variation_strength is not None:
            random.seed()
            if variation_seed is None:
                variation_seed = new_seed()
            variation_seed = seed_everything(variation_seed)
            self.latest_variation_seed = variation_seed
            variation_noise = get_noise()
            vz = get_new_z(variation_noise)
            z = slerp(vz, z, variation_strength)
        return z, noise

    def _img2img(
        self,
        z: Tensor,
        export_path: Optional[str] = None,
        *,
        seed: Optional[int] = None,
        # each variation contains (seed, weight)
        variations: Optional[List[Tuple[int, float]]] = None,
        variation_seed: Optional[int] = None,
        variation_strength: Optional[float] = None,
        z_ref: Optional[Tensor] = None,
        z_ref_mask: Optional[Tensor] = None,
        fidelity: float = 0.2,
        original_size: Optional[Tuple[int, int]] = None,
        alpha: Optional[np.ndarray] = None,
        cond: Optional[Any] = None,
        num_steps: Optional[int] = None,
        clip_output: bool = True,
        verbose: bool = True,
        **kwargs: Any,
    ) -> Tensor:
        # perturb z
        if num_steps is None:
            num_steps = self.sampler.default_steps
        t = round((1.0 - fidelity) * num_steps)
        ts = get_timesteps(min(t, num_steps - 1), 1, z.device)
        if isinstance(self.sampler, (DDIMMixin, KSamplerMixin)):
            kw = shallow_copy_dict(self.sampler.sample_kwargs)
            kw["total_step"] = num_steps
            safe_execute(self.sampler._reset_buffers, kw)
        z, noise = self._set_seed_and_variations(
            seed,
            lambda: torch.randn_like(z),
            lambda noise_: self.sampler.q_sample(z, ts, noise_),
            variations,
            variation_seed,
            variation_strength,
        )
        kwargs["start_step"] = num_steps - t
        return self.sample(
            1,
            export_path,
            z=z,
            z_ref=z_ref,
            z_ref_mask=z_ref_mask,
            z_ref_noise=None if z_ref is None else noise,
            original_size=original_size,
            alpha=alpha,
            cond=cond,
            num_steps=num_steps,
            clip_output=clip_output,
            verbose=verbose,
            **kwargs,
        )


def _ldm(
    model: str,
    latent_size: int,
    latent_in_channels: int,
    latent_out_channels: int,
    **kwargs: Any,
) -> DLPipeline:
    kwargs["img_size"] = latent_size
    kwargs["in_channels"] = latent_in_channels
    kwargs["out_channels"] = latent_out_channels
    model_config = kwargs.setdefault("model_config", {})
    first_stage_kw = model_config.setdefault("first_stage_config", {})
    first_stage_kw.setdefault("report", False)
    first_stage_kw.setdefault("pretrained", True)
    first_stage_model_config = first_stage_kw.setdefault("model_config", {})
    use_loss = first_stage_model_config.setdefault("use_loss", False)
    if not use_loss:

        def state_callback(states: tensor_dict_type) -> tensor_dict_type:
            for key in list(states.keys()):
                if key.startswith("core.loss"):
                    states.pop(key)
            return states

        first_stage_kw["pretrained_state_callback"] = state_callback
    return DLZoo.load_pipeline(model, **kwargs)


def ldm(
    latent_size: int = 32,
    latent_in_channels: int = 4,
    latent_out_channels: int = 4,
    **kwargs: Any,
) -> DLPipeline:
    return _ldm(
        "diffusion/ldm",
        latent_size,
        latent_in_channels,
        latent_out_channels,
        **kwargs,
    )


def ldm_vq(
    latent_size: int = 64,
    latent_in_channels: int = 3,
    latent_out_channels: int = 3,
    **kwargs: Any,
) -> DLPipeline:
    return _ldm(
        "diffusion/ldm.vq",
        latent_size,
        latent_in_channels,
        latent_out_channels,
        **kwargs,
    )


def ldm_sd(pretrained: bool = True) -> DLPipeline:
    return _ldm("diffusion/ldm.sd", 64, 4, 4, pretrained=pretrained)


def ldm_celeba_hq(pretrained: bool = True) -> DLPipeline:
    return ldm_vq(
        pretrained=pretrained,
        download_name="ldm_celeba_hq",
        model_config=dict(
            ema_decay=None,
            first_stage_config=dict(
                pretrained=False,
            ),
        ),
    )


def ldm_inpainting(pretrained: bool = True) -> DLPipeline:
    return ldm_vq(
        pretrained=pretrained,
        latent_in_channels=7,
        download_name="ldm_inpainting",
        model_config=dict(
            ema_decay=None,
            start_channels=256,
            num_heads=8,
            num_head_channels=None,
            resample_with_resblock=True,
            condition_type="concat",
            first_stage_config=dict(
                pretrained=False,
                model_config=dict(
                    attention_type="none",
                ),
            ),
        ),
    )


def ldm_sr(pretrained: bool = True) -> DLPipeline:
    return ldm_vq(
        pretrained=pretrained,
        latent_in_channels=6,
        download_name="ldm_sr",
        model_config=dict(
            ema_decay=None,
            start_channels=160,
            attention_downsample_rates=[8, 16],
            channel_multipliers=[1, 2, 2, 4],
            condition_type="concat",
            first_stage_config=dict(
                pretrained=False,
            ),
        ),
    )


def ldm_semantic(pretrained: bool = True) -> DLPipeline:
    return ldm_vq(
        pretrained=pretrained,
        latent_size=128,
        latent_in_channels=6,
        download_name="ldm_semantic",
        model_config=dict(
            ema_decay=None,
            start_channels=128,
            num_heads=8,
            num_head_channels=None,
            attention_downsample_rates=[8, 16, 32],
            channel_multipliers=[1, 4, 8],
            condition_type="concat",
            condition_model="rescaler",
            condition_config=dict(
                num_stages=2,
                in_channels=182,
                out_channels=3,
            ),
            first_stage_config=dict(
                pretrained=False,
            ),
        ),
    )


__all__ = [
    "DiffusionAPI",
]
