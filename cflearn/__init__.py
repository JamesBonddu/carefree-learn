import os
import math
import tqdm
import torch
import optuna
import shutil
import logging
import onnxruntime
import cftool.ml.param_utils as pu

import numpy as np

from typing import *
from cftool.misc import *
from cftool.ml.utils import *
from cfdata.tabular import *
from cftool.ml.hpo import HPOBase
from cftool.ml import register_metric
from cfdata.tabular.misc import split_file
from cfdata.tabular.processors.base import Processor
from torch.nn.functional import one_hot
from abc import ABCMeta, abstractmethod

from .dist import *
from .bases import *
from .models import *
from .modules import *
from .misc.toolkit import *


# register


def register_initializer(name):
    def _register(f):
        Initializer.add_initializer(f, name)
        return f

    return _register


def register_processor(name):
    return Processor.register(name)


# API


def make(
    model: str = "fcnn",
    *,
    delim: str = None,
    task_type: str = None,
    skip_first: bool = None,
    cv_split: Union[float, int] = 0.1,
    min_epoch: int = None,
    num_epoch: int = None,
    max_epoch: int = None,
    batch_size: int = None,
    max_snapshot_num: int = None,
    clip_norm: float = None,
    ema_decay: float = None,
    ts_config: TimeSeriesConfig = None,
    aggregation: str = None,
    aggregation_config: Dict[str, Any] = None,
    ts_label_collator_config: Dict[str, Any] = None,
    data_config: Dict[str, Any] = None,
    read_config: Dict[str, Any] = None,
    model_config: Dict[str, Any] = None,
    metrics: Union[str, List[str]] = None,
    metric_config: Dict[str, Any] = None,
    optimizer: str = None,
    scheduler: str = None,
    optimizer_config: Dict[str, Any] = None,
    scheduler_config: Dict[str, Any] = None,
    optimizers: Dict[str, Any] = None,
    logging_file: str = None,
    logging_folder: str = None,
    trigger_logging: bool = None,
    tracker_config: Dict[str, Any] = None,
    cuda: Union[int, str] = None,
    verbose_level: int = 2,
    use_tqdm: bool = True,
    **kwargs,
) -> Wrapper:
    # wrapper general
    kwargs["model"] = model
    kwargs["cv_split"] = cv_split
    if data_config is None:
        data_config = {}
    if ts_config is not None:
        data_config["time_series_config"] = ts_config
    if task_type is not None:
        data_config["task_type"] = TaskTypes.from_str(task_type)
    if read_config is None:
        read_config = {}
    if delim is not None:
        read_config["delim"] = delim
    if skip_first is not None:
        read_config["skip_first"] = skip_first
    kwargs["data_config"] = data_config
    kwargs["read_config"] = read_config
    if model_config is not None:
        kwargs["model_config"] = model_config
    if logging_folder is not None:
        if logging_file is None:
            logging_file = f"{model}_{timestamp()}.log"
        kwargs["logging_folder"] = logging_folder
        kwargs["logging_file"] = logging_file
    if trigger_logging is not None:
        kwargs["trigger_logging"] = trigger_logging
    # pipeline general
    pipeline_config = kwargs.setdefault("pipeline_config", {})
    pipeline_config["use_tqdm"] = use_tqdm
    if min_epoch is not None:
        pipeline_config["min_epoch"] = min_epoch
    if num_epoch is not None:
        pipeline_config["num_epoch"] = num_epoch
    if max_epoch is not None:
        pipeline_config["max_epoch"] = max_epoch
    if batch_size is not None:
        pipeline_config["batch_size"] = batch_size
    if max_snapshot_num is not None:
        pipeline_config["max_snapshot_num"] = max_snapshot_num
    if clip_norm is not None:
        pipeline_config["clip_norm"] = clip_norm
    if ema_decay is not None:
        pipeline_config["ema_decay"] = ema_decay
    sampler_config = pipeline_config.setdefault("sampler_config", {})
    if aggregation is not None:
        sampler_config["aggregation"] = aggregation
    if aggregation_config is not None:
        sampler_config["aggregation_config"] = aggregation_config
    if ts_label_collator_config is not None:
        pipeline_config["ts_label_collator_config"] = ts_label_collator_config
    # metrics
    if metric_config is not None:
        if metrics is not None:
            print(
                f"{LoggingMixin.warning_prefix}`metrics` is set to '{metrics}' "
                f"but `metric_config` is provided, so `metrics` will be ignored"
            )
    elif metrics is not None:
        metric_config = {"types": metrics}
    if metric_config is not None:
        pipeline_config["metric_config"] = metric_config
    # optimizers
    if optimizers is not None:
        if optimizer is not None:
            print(
                f"{LoggingMixin.warning_prefix}`optimizer` is set to '{optimizer}' "
                f"but `optimizers` is provided, so `optimizer` will be ignored"
            )
        if optimizer_config is not None:
            print(
                f"{LoggingMixin.warning_prefix}`optimizer_config` is set to '{optimizer_config}' "
                f"but `optimizers` is provided, so `optimizer_config` will be ignored"
            )
    else:
        preset_optimizer = {}
        if optimizer is not None:
            if optimizer_config is None:
                optimizer_config = {}
            preset_optimizer = {
                "optimizer": optimizer,
                "optimizer_config": optimizer_config,
            }
        if scheduler is not None:
            if scheduler_config is None:
                scheduler_config = {}
            preset_optimizer.update(
                {"scheduler": scheduler, "scheduler_config": scheduler_config}
            )
        if preset_optimizer:
            optimizers = {"all": preset_optimizer}
    if optimizers is not None:
        pipeline_config["optimizers"] = optimizers
    return Wrapper(
        kwargs, cuda=cuda, tracker_config=tracker_config, verbose_level=verbose_level
    )


SAVING_DELIM = "^_^"
wrappers_dict_type = Dict[str, Wrapper]
wrappers_type = Union[Wrapper, List[Wrapper], wrappers_dict_type]


def _to_saving_path(identifier: str, saving_folder: str) -> str:
    if saving_folder is None:
        saving_path = identifier
    else:
        saving_path = os.path.join(saving_folder, identifier)
    return saving_path


def _make_saving_path(name: str, saving_path: str, remove_existing: bool) -> str:
    saving_path = os.path.abspath(saving_path)
    saving_folder, identifier = os.path.split(saving_path)
    postfix = f"{SAVING_DELIM}{name}"
    if os.path.isdir(saving_folder) and remove_existing:
        for existing_model in os.listdir(saving_folder):
            if os.path.isdir(os.path.join(saving_folder, existing_model)):
                continue
            if existing_model.startswith(f"{identifier}{postfix}"):
                print(
                    f"{LoggingMixin.warning_prefix}"
                    f"'{existing_model}' was found, it will be removed"
                )
                os.remove(os.path.join(saving_folder, existing_model))
    return f"{saving_path}{postfix}"


def load_task(task: Task) -> Wrapper:
    return next(iter(load(saving_folder=task.saving_folder).values()))


def transform_experiments(experiments: Experiments) -> Dict[str, List[Wrapper]]:
    return {k: list(map(load_task, v)) for k, v in experiments.tasks.items()}


class RepeatResult(NamedTuple):
    experiments: Experiments
    data: Union[None, TabularData]
    patterns: Union[None, Dict[str, List[ModelPattern]]]

    @property
    def models(self) -> Dict[str, List[Wrapper]]:
        return {key: [m.model for m in value] for key, value in self.patterns.items()}


def repeat_with(
    x: data_type,
    y: data_type = None,
    x_cv: data_type = None,
    y_cv: data_type = None,
    *,
    models: Union[str, List[str]] = "fcnn",
    identifiers: Union[str, List[str]] = None,
    num_jobs: int = 4,
    num_repeat: int = 5,
    temp_folder: str = "__tmp__",
    return_patterns: bool = True,
    use_tqdm: bool = True,
    **kwargs,
) -> RepeatResult:

    if isinstance(models, str):
        models = [models]
    if identifiers is None:
        identifiers = models.copy()
    elif isinstance(identifiers, str):
        identifiers = [identifiers]

    kwargs.setdefault("trigger_logging", False)
    kwargs["verbose_level"] = 0

    experiments = Experiments(temp_folder, overwrite=False)
    experiments.run(
        None,
        x,
        y,
        x_cv,
        y_cv,
        models=models,
        identifiers=identifiers,
        num_repeat=num_repeat,
        num_jobs=num_jobs,
        use_tqdm=use_tqdm,
        temp_folder=temp_folder,
        **kwargs,
    )
    patterns = None
    if return_patterns:
        wrappers = transform_experiments(experiments)
        patterns = {
            model: [m.to_pattern() for m in wrappers]
            for model, wrappers in wrappers.items()
        }
    data = None
    if patterns is not None:
        data = patterns[identifiers[0]][0].model.tr_data
    return RepeatResult(experiments, data, patterns)


def tasks_to_wrappers(tasks: List[Task]) -> List[Wrapper]:
    return list(map(load_task, tasks))


def tasks_to_patterns(tasks: List[Task], **kwargs) -> List[pattern_type]:
    wrappers = tasks_to_wrappers(tasks)
    return [m.to_pattern(**kwargs) for m in wrappers]


class _Tuner:
    def __init__(
        self,
        x: data_type,
        y: data_type,
        x_cv: data_type,
        y_cv: data_type,
        task_type: TaskTypes,
        **kwargs,
    ):
        hpo_cv_split = kwargs.get("hpo_cv_split", 0.1)
        hpo_cv_split_order = kwargs.get("hpo_cv_split_order", "auto")
        need_cv_split = x_cv is None and hpo_cv_split > 0.0

        if y is not None:
            y, y_cv = map(to_2d, [y, y_cv])
            if need_cv_split:
                data = TabularData.simple(task_type).read(x, y)
                split = data.split(hpo_cv_split, order=hpo_cv_split_order)
                tr_data, cv_data = split.remained, split.split
                x, y = tr_data.raw.xy
                x_cv, y_cv = cv_data.raw.xy
        elif isinstance(x, str):
            if need_cv_split:
                print(
                    f"{LoggingMixin.warning_prefix}only random split is supported "
                    f"for file datasets, `split_order` ({hpo_cv_split_order}) "
                    "will be ignored"
                )
                x_cv, x = split_file(x, export_folder="_split", split=hpo_cv_split)
                x, x_cv = map(os.path.abspath, [x, x_cv])
            data_config = kwargs.get("data_config", {})
            data_config["task_type"] = task_type
            read_config = kwargs.get("read_config", {})
            delim = read_config.get("delim", kwargs.get("delim"))
            if delim is not None:
                read_config["delim"] = delim
            else:
                print(
                    f"{LoggingMixin.warning_prefix}delimiter of the given file dataset is not provided, "
                    "this may cause incorrect parsing"
                )
            if y is not None:
                read_config["y"] = y
            tr_data = TabularData(**data_config)
            tr_data.read(x, **read_config)
            y = tr_data.processed.y
            if x_cv is not None:
                if y_cv is None:
                    y_cv = tr_data.transform(x_cv).y
                else:
                    y_cv = tr_data.transform_labels(y_cv)
        else:
            raise ValueError("`x` should be a file when `y` is not provided")

        self.task_type = task_type
        self.x, self.x_cv = x, x_cv
        self.y, self.y_cv = y, y_cv
        self.base_params = shallow_copy_dict(kwargs)

    def make_estimators(self, metrics: Union[str, List[str]]) -> List[Estimator]:
        if metrics is None:
            if self.task_type is None:
                raise ValueError("either `task_type` or `metrics` should be provided")
            if self.task_type is TaskTypes.CLASSIFICATION:
                metrics = ["acc", "auc"]
            else:
                metrics = ["mae", "mse"]
        return list(map(Estimator, metrics))

    def train(
        self,
        model: str,
        params: Dict[str, Any],
        num_repeat: int,
        num_parallel: int,
        temp_folder: str,
    ) -> List[Task]:
        identifier = hash_code(str(params))
        params = update_dict(params, shallow_copy_dict(self.base_params))
        params["verbose_level"] = 0
        params["use_tqdm"] = False
        if isinstance(self.x, str):
            y = y_cv = None
            x, x_cv = self.x, self.x_cv
        else:
            x, x_cv = self.x.copy(), self.x_cv.copy()
            y = self.y.copy()
            y_cv = None if self.y_cv is None else self.y_cv.copy()
        results = repeat_with(
            x,
            y,
            x_cv,
            y_cv,
            num_repeat=num_repeat,
            num_jobs=num_parallel,
            models=model,
            identifiers=identifier,
            temp_folder=temp_folder,
            return_tasks=True,
            return_patterns=False,
            **params,
        )
        return results.experiments.tasks[identifier]


class HPOResult(NamedTuple):
    hpo: HPOBase
    extra_config: Dict[str, Any]

    @property
    def best_param(self) -> Dict[str, Any]:
        param = shallow_copy_dict(self.hpo.best_param)
        return update_dict(param, shallow_copy_dict(self.extra_config))


def tune_with(
    x: data_type,
    y: data_type = None,
    x_cv: data_type = None,
    y_cv: data_type = None,
    *,
    model: str = "fcnn",
    hpo_method: str = "bo",
    params: pu.params_type = None,
    task_type: TaskTypes = None,
    metrics: Union[str, List[str]] = None,
    num_jobs: int = None,
    num_repeat: int = 5,
    num_parallel: int = 4,
    num_search: int = 10,
    temp_folder: str = "__tmp__",
    score_weights: Union[Dict[str, float], None] = None,
    estimator_scoring_function: Union[str, scoring_fn_type] = "default",
    search_config: Dict[str, Any] = None,
    verbose_level: int = 2,
    extra_config: Dict[str, Any] = None,
) -> HPOResult:

    if os.path.isdir(temp_folder):
        print(
            f"{LoggingMixin.warning_prefix}'{temp_folder}' already exists, it will be overwritten"
        )
        shutil.rmtree(temp_folder)

    if extra_config is None:
        extra_config = {}
    tuner = _Tuner(x, y, x_cv, y_cv, task_type, **extra_config)
    x, y, x_cv, y_cv = tuner.x, tuner.y, tuner.x_cv, tuner.y_cv

    def _creator(_, __, params_) -> Dict[str, List[Task]]:
        num_jobs_ = num_parallel if hpo.is_sequential else 0
        tasks = tuner.train(model, params_, num_repeat, num_jobs_, temp_folder)
        return {model: tasks}

    def _converter(created: List[Dict[str, List[Task]]]) -> List[pattern_type]:
        return tasks_to_patterns(created[0][model], contains_labels=True)

    if params is None:
        default_init_param = pu.Any(pu.Choice(values=[None, "truncated_normal"]))
        params = {
            "optimizer": pu.String(pu.Choice(values=["sgd", "rmsprop", "adam"])),
            "optimizer_config": {"lr": pu.Float(pu.Exponential(1e-5, 0.1))},
            "model_config": {
                "default_encoding_configs": {"init_method": default_init_param},
            },
        }

    hpo = HPOBase.make(
        hpo_method, _creator, params, converter=_converter, verbose_level=verbose_level
    )
    if hpo.is_sequential:
        if num_jobs is None:
            num_jobs = 0
        if num_jobs > 1:
            print(
                f"{LoggingMixin.warning_prefix}`num_jobs` is set but hpo is sequential, "
                "please use `num_parallel` instead"
            )
        num_jobs = 0
    if search_config is None:
        search_config = {}
    update_dict(
        {
            "num_retry": 1,
            "num_search": num_search,
            "score_weights": score_weights,
            "estimator_scoring_function": estimator_scoring_function,
        },
        search_config,
    )
    if num_jobs is not None:
        search_config["num_jobs"] = num_jobs
    search_config.setdefault(
        "parallel_logging_folder", os.path.join(temp_folder, "__hpo_parallel__")
    )
    estimators = tuner.make_estimators(metrics)
    hpo.search(x, y, estimators, x_cv, y_cv, **search_config)
    return HPOResult(hpo, extra_config)


class OptunaParam(NamedTuple):
    name: str
    values: Any
    dtype: str  # [int | float | categorical]
    config: Dict[str, Any] = None

    def pop(self, trial: optuna.trial.Trial) -> Any:
        method = getattr(trial, f"suggest_{self.dtype}")
        if self.dtype == "categorical":
            return method(self.name, self.values)
        low, high = self.values
        config = {} if self.config is None else self.config
        return method(self.name, low, high, **config)


optuna_params_type = Dict[str, Union[OptunaParam, "optuna_params_type"]]


class OptunaKeyMapping:
    def __init__(self, optuna_params: optuna_params_type):
        self.delim = SAVING_DELIM
        self.params = optuna_params
        self.optuna_key_mapping: Dict[str, str] = {}

        def _inject_mapping(d: optuna_params_type, prefix_list: List[str]):
            for k, v in d.items():
                new_prefix_list = prefix_list + [k]
                if isinstance(v, OptunaParam):
                    self.optuna_key_mapping[v.name] = self.delim.join(new_prefix_list)
                    continue
                _inject_mapping(v, new_prefix_list)

        _inject_mapping(optuna_params, [])

    def parse(self, optuna_param_values: Dict[str, Any]) -> Dict[str, Any]:
        params = {}
        for k, v in optuna_param_values.items():
            key_mapping = self.optuna_key_mapping[k]
            key_path = key_mapping.split(self.delim)
            local_param = params
            for sub_k in key_path[:-1]:
                local_param = local_param.setdefault(sub_k, {})
            local_param[key_path[-1]] = v
        return params


class OptunaResult(NamedTuple):
    tuner: _Tuner
    study: optuna.study.Study
    optuna_key_mapping: OptunaKeyMapping
    extra_config: Dict[str, Any]

    @property
    def best_param(self) -> Dict[str, Any]:
        param = shallow_copy_dict(self.tuner.base_params)
        optuna_param = self.optuna_key_mapping.parse(self.study.best_params)
        update_dict(optuna_param, param)
        self.get_hidden_units(param, None)
        return update_dict(param, shallow_copy_dict(self.extra_config))

    @staticmethod
    def get_hidden_units(
        params: Dict[str, Any],
        trial: Union[optuna.trial.Trial, None],
    ) -> Union[List[int], None]:
        hidden_units = None
        model_config = params.setdefault("model_config", {})
        num_layers = model_config.pop("num_layers", None)
        if num_layers is not None:
            hidden_units = []
            if trial is not None:
                max_layers = num_layers.values[1]
                num_layers = num_layers.pop(trial)
                for i in range(num_layers, max_layers):
                    model_config.pop(f"hidden_unit_{i}", None)
            for i in range(num_layers):
                key = f"hidden_unit_{i}"
                hidden_unit = model_config.pop(key, None)
                if hidden_unit is None:
                    raise ValueError(f"'{key}' is not found in `model_config`")
                if trial is not None:
                    hidden_unit = hidden_unit.pop(trial)
                hidden_units.append(hidden_unit)
            if trial is None:
                model_config["hidden_units"] = hidden_units
        return hidden_units


def optuna_tune(
    x: data_type,
    y: data_type = None,
    x_cv: data_type = None,
    y_cv: data_type = None,
    *,
    model: str = "fcnn",
    task_type: TaskTypes = None,
    params: optuna_params_type = None,
    study_config: Dict[str, Any] = None,
    metrics: Union[str, List[str]] = None,
    num_jobs: int = 1,
    num_trial: int = 10,
    num_repeat: int = 5,
    num_parallel: int = 4,
    timeout: float = None,
    score_weights: Union[Dict[str, float], None] = None,
    estimator_scoring_function: Union[str, scoring_fn_type] = "default",
    temp_folder: str = "__tmp__",
    extra_config: Dict[str, Any] = None,
) -> OptunaResult:
    if params is None:
        lr_param = OptunaParam("lr", [1e-5, 0.1], "float", {"log": True})
        optim_param = OptunaParam(
            "optimizer", ["sgd", "rmsprop", "adam"], "categorical"
        )
        default_init_param = OptunaParam(
            "default_init_method", [None, "truncated_normal"], "categorical"
        )
        params = {
            "optimizer": optim_param,
            "optimizer_config": {"lr": lr_param},
            "model_config": {
                "default_encoding_configs": {"init_method": default_init_param},
            },
        }
    key_mapping = OptunaKeyMapping(params)

    if extra_config is None:
        extra_config = {}
    tuner = _Tuner(x, y, x_cv, y_cv, task_type, **extra_config)
    estimators = tuner.make_estimators(metrics)

    def objective(trial: optuna.trial.Trial) -> float:
        optuna_params = shallow_copy_dict(params)
        current_params = shallow_copy_dict(tuner.base_params)
        # handle hidden units
        model_config = current_params.setdefault("model_config", {})
        hidden_units = OptunaResult.get_hidden_units(optuna_params, trial)
        if hidden_units is not None:
            model_config["hidden_units"] = hidden_units
        # get other suggestions
        def _inject_suggestion(d: optuna_params_type, current: dict):
            for k, v in d.items():
                if isinstance(v, dict):
                    _inject_suggestion(v, current.setdefault(k, {}))
                    continue
                current[k] = v.pop(trial)

        _inject_suggestion(optuna_params, current_params)
        # train & estimate
        args = model, current_params, num_repeat, num_parallel, temp_folder
        patterns = tasks_to_patterns(tuner.train(*args), contains_labels=True)
        comparer = Comparer({model: patterns}, estimators)
        comparer.compare(
            tuner.x_cv,
            tuner.y_cv,
            scoring_function=estimator_scoring_function,
            verbose_level=6,
        )
        scores = {k: v[model] for k, v in comparer.final_scores.items()}
        if score_weights is None:
            score = sum(scores.values()) / len(scores)
        else:
            weighted = sum(score * score_weights[k] for k, score in scores.items())
            score = weighted / sum(score_weights.values())
        return score

    if study_config is None:
        study_config = {}
    study_config["direction"] = "maximize"
    study_config.setdefault("study_name", f"{model}_optuna")
    study = optuna.create_study(**study_config)
    study.optimize(objective, num_trial, timeout, num_jobs)

    return OptunaResult(tuner, study, key_mapping, extra_config)


def _to_wrappers(wrappers: wrappers_type) -> wrappers_dict_type:
    if not isinstance(wrappers, dict):
        if not isinstance(wrappers, list):
            wrappers = [wrappers]
        names = [wrapper.model.__identifier__ for wrapper in wrappers]
        if len(set(names)) != len(wrappers):
            raise ValueError(
                "wrapper names are not provided but identical wrapper.model is detected"
            )
        wrappers = dict(zip(names, wrappers))
    return wrappers


def estimate(
    x: data_type,
    y: data_type = None,
    *,
    contains_labels: bool = False,
    wrappers: wrappers_type = None,
    wrapper_predict_config: Dict[str, Any] = None,
    metrics: Union[str, List[str]] = None,
    other_patterns: Dict[str, patterns_type] = None,
    comparer_verbose_level: Union[int, None] = 1,
) -> Comparer:
    patterns = {}
    if isinstance(metrics, str):
        metrics = [metrics]
    if wrappers is None:
        if y is None:
            raise ValueError("either `wrappers` or `y` should be provided")
        if metrics is None:
            raise ValueError("either `wrappers` or `metrics` should be provided")
        if other_patterns is None:
            raise ValueError("either `wrappers` or `other_patterns` should be provided")
    else:
        wrappers = _to_wrappers(wrappers)
        if wrapper_predict_config is None:
            wrapper_predict_config = {}
        for name, wrapper in wrappers.items():
            if y is not None:
                y = to_2d(y)
            else:
                x, y = wrapper.tr_data.read_file(x, contains_labels=contains_labels)
                y = wrapper.tr_data.transform(x, y).y
            if metrics is None:
                metrics = [
                    k for k, v in wrapper.pipeline.metrics.items() if v is not None
                ]
            with eval_context(wrapper.model):
                patterns[name] = wrapper.to_pattern(**wrapper_predict_config)
    if other_patterns is not None:
        for other_name in other_patterns.keys():
            if other_name in patterns:
                prefix = LoggingMixin.warning_prefix
                print(
                    f"{prefix}'{other_name}' is found in `other_patterns`, it will be overwritten"
                )
        update_dict(other_patterns, patterns)
    estimators = list(map(Estimator, metrics))
    comparer = Comparer(patterns, estimators)
    comparer.compare(x, y, verbose_level=comparer_verbose_level)
    return comparer


def save(
    wrappers: wrappers_type,
    identifier: str = "cflearn",
    saving_folder: str = None,
) -> wrappers_dict_type:
    wrappers = _to_wrappers(wrappers)
    saving_path = _to_saving_path(identifier, saving_folder)
    for name, wrapper in wrappers.items():
        wrapper.save(_make_saving_path(name, saving_path, True), compress=True)
    return wrappers


def _fetch_saving_paths(
    identifier: str = "cflearn",
    saving_folder: str = None,
) -> Dict[str, str]:
    paths = {}
    saving_path = _to_saving_path(identifier, saving_folder)
    saving_path = os.path.abspath(saving_path)
    base_folder = os.path.dirname(saving_path)
    for existing_model in os.listdir(base_folder):
        if not os.path.isfile(os.path.join(base_folder, existing_model)):
            continue
        existing_model, existing_extension = os.path.splitext(existing_model)
        if existing_extension != ".zip":
            continue
        if SAVING_DELIM in existing_model:
            *folder, name = existing_model.split(SAVING_DELIM)
            if os.path.join(base_folder, SAVING_DELIM.join(folder)) != saving_path:
                continue
            paths[name] = _make_saving_path(name, saving_path, False)
    return paths


def load(identifier: str = "cflearn", saving_folder: str = None) -> wrappers_dict_type:
    paths = _fetch_saving_paths(identifier, saving_folder)
    wrappers = {k: Wrapper.load(v, compress=True) for k, v in paths.items()}
    if not wrappers:
        raise ValueError(
            f"'{identifier}' models not found with `saving_folder`={saving_folder}"
        )
    return wrappers


def _remove(identifier: str = "cflearn", saving_folder: str = None) -> None:
    for path in _fetch_saving_paths(identifier, saving_folder).values():
        path = f"{path}.zip"
        print(f"{LoggingMixin.info_prefix}removing {path}...")
        os.remove(path)


# zoo

zoo_dict: Dict[str, Type["ZooBase"]] = {}


class ZooBase(LoggingMixin, metaclass=ABCMeta):
    def __init__(
        self,
        *,
        model_type: str = "default",
        increment_config: Dict[str, Any] = None,
    ):
        self._model_type = model_type
        self._increment_config = increment_config

    @property
    @abstractmethod
    def benchmarks(self) -> Dict[str, dict]:
        """
        this method should return a dict of configs (which represent benchmarks)
        * Note that "default" key should always be included in the returned dict
        """
        raise NotImplementedError

    @property
    def config(self) -> dict:
        """ return corresponding config of self._model_type, update with increment_config if provided """
        config_dict = self.benchmarks
        assert "default" in config_dict, "'default' should be included in config_dict"
        config = config_dict.get(self._model_type)
        if config is None:
            if self._model_type != "default":
                self.log_msg(
                    f"model_type '{self._model_type}' is not recognized, 'default' model_type will be used",
                    self.warning_prefix,
                    2,
                    msg_level=logging.WARNING,
                )
                self._model_type = "default"
            config = self.benchmarks["default"]
        config = shallow_copy_dict(config)
        if self._increment_config is not None:
            update_dict(self._increment_config, config)
        return config

    @property
    def model(self) -> str:
        return self._model_type

    @property
    def m(self) -> Wrapper:
        """ return corresponding model of self.config """
        return make(self.model, **self.config)

    def switch(self, model_type) -> "ZooBase":
        """ switch to another model_type """
        self._model_type = model_type
        return self

    @classmethod
    def register(cls, name: str):
        global zoo_dict

        def before(cls_):
            cls_.__identifier__ = name

        return register_core(name, zoo_dict, before_register=before)


@ZooBase.register("fcnn")
class FCNNZoo(ZooBase):
    @property
    def benchmarks(self) -> Dict[str, dict]:
        return {
            "default": {},
            "light_bn": {"model_config": {"hidden_units": [128]}},
            "on_large": {
                "model_config": {
                    "mapping_configs": {"dropout": 0.1, "batch_norm": False}
                }
            },
            "light": {
                "model_config": {
                    "hidden_units": [128],
                    "mapping_configs": {"batch_norm": False},
                }
            },
            "on_sparse": {
                "optimizer_config": {"lr": 1e-4},
                "model_config": {
                    "hidden_units": [128],
                    "mapping_configs": {"dropout": 0.9, "batch_norm": False},
                },
            },
        }


@ZooBase.register("tree_dnn")
class TreeDNNZoo(ZooBase):
    @property
    def benchmarks(self) -> Dict[str, dict]:
        return {
            "default": {},
            "on_large": {
                "model_config": {
                    "dndf_config": None,
                    "mapping_configs": {"dropout": 0.1},
                }
            },
            "light": {
                "model_config": {
                    "dndf_config": None,
                    "mapping_configs": {"batch_norm": False},
                    "default_encoding_configs": {"embedding_dim": 8},
                }
            },
            "on_sparse": {
                "optimizer_config": {"lr": 1e-4},
                "model_config": {
                    "dndf_config": None,
                    "mapping_configs": {
                        "dropout": 0.9,
                        "batch_norm": False,
                        "pruner_config": None,
                    },
                    "default_encoding_configs": {"embedding_dim": 8},
                },
            },
        }


@ZooBase.register("ddr")
class DDRZoo(ZooBase):
    @property
    def benchmarks(self) -> Dict[str, dict]:
        return {
            "default": {},
            "disjoint": {"joint_training": False},
            "q_only": {"fetches": ["quantile"]},
        }


def zoo(
    model: str = "fcnn",
    *,
    model_type: str = "default",
    increment_config: Dict[str, Any] = None,
) -> ZooBase:
    return zoo_dict[model](model_type=model_type, increment_config=increment_config)


# benchmark


class BenchmarkResults(NamedTuple):
    data: TabularData
    best_configs: Dict[str, Dict[str, Any]]
    best_methods: Dict[str, str]
    experiments: Experiments
    comparer: Comparer


class Benchmark(LoggingMixin):
    def __init__(
        self,
        task_name: str,
        task_type: TaskTypes,
        *,
        temp_folder: str = None,
        project_name: str = "carefree-learn",
        models: Union[str, List[str]] = "fcnn",
        increment_config: Dict[str, Any] = None,
        data_config: Dict[str, Any] = None,
        read_config: Dict[str, Any] = None,
        use_cuda: bool = True,
    ):
        self.data = None
        if data_config is None:
            data_config = {}
        if read_config is None:
            read_config = {}
        self.data_config, self.read_config = data_config, read_config
        self.task_name, self.task_type = task_name, task_type
        if temp_folder is None:
            temp_folder = f"__{task_name}__"
        self.temp_folder, self.project_name = temp_folder, project_name
        if isinstance(models, str):
            models = [models]
        self.models = models
        if increment_config is None:
            increment_config = {}
        self.increment_config = increment_config
        self.use_cuda = use_cuda
        self.experiments = None

    @property
    def identifier(self) -> str:
        return hash_code(
            f"{self.project_name}{self.task_name}{self.models}{self.increment_config}"
        )

    @property
    def data_tasks(self) -> List[Task]:
        return next(iter(self.experiments.data_tasks.values()))

    def _add_tasks(
        self,
        iterator_name: str,
        data_tasks: List[Task],
        experiments: Experiments,
        benchmarks: Dict[str, Dict[str, Dict[str, Any]]],
    ) -> None:
        self.configs = {}
        for i in range(len(data_tasks)):
            for model in self.models:
                model_benchmarks = benchmarks.get(model)
                if model_benchmarks is None:
                    model_benchmarks = zoo(model).benchmarks
                for model_type, config in model_benchmarks.items():
                    identifier = f"{model}_{self.task_name}_{model_type}"
                    task_name = f"{identifier}_{iterator_name}{i}"
                    increment_config = shallow_copy_dict(self.increment_config)
                    config = update_dict(increment_config, config)
                    self.configs.setdefault(identifier, config)
                    tracker_config = {
                        "project_name": self.project_name,
                        "task_name": task_name,
                        "overwrite": True,
                    }
                    experiments.add_task(
                        model=model,
                        data_task=data_tasks[i],
                        identifier=identifier,
                        tracker_config=tracker_config,
                        **config,
                    )

    def _run_tasks(
        self,
        num_jobs: int = 4,
        run_tasks: bool = True,
        predict_config: Dict[str, Any] = None,
    ) -> BenchmarkResults:
        results = self.experiments.run_tasks(
            num_jobs=num_jobs, run_tasks=run_tasks, load_task=load_task
        )
        comparer_list = []
        for i, data_task in enumerate(self.data_tasks):
            wrappers = {}
            x_te, y_te = data_task.fetch_data("_te")
            for identifier, ms in results.items():
                wrappers[identifier] = ms[i]
            comparer = estimate(
                x_te,
                y_te,
                wrappers=wrappers,
                wrapper_predict_config=predict_config,
                comparer_verbose_level=None,
            )
            comparer_list.append(comparer)
        comparer = Comparer.merge(comparer_list)
        best_methods = comparer.best_methods
        best_configs = {
            metric: self.configs[identifier]
            for metric, identifier in best_methods.items()
        }
        return BenchmarkResults(
            self.data, best_configs, best_methods, self.experiments, comparer
        )

    def _pre_process(self, x: data_type, y: data_type = None) -> TabularDataset:
        data_config = shallow_copy_dict(self.data_config)
        task_type = data_config.pop("task_type", None)
        if task_type is not None:
            assert task_type is self.task_type
        self.data = TabularData.simple(self.task_type, **data_config).read(
            x, y, **self.read_config
        )
        return self.data.to_dataset()

    def _k_core(
        self,
        k_iterator: Iterable,
        num_jobs: int,
        run_tasks: bool,
        predict_config: Dict[str, Any],
        benchmarks: Dict[str, Dict[str, Dict[str, Any]]],
    ) -> BenchmarkResults:
        if benchmarks is None:
            benchmarks = {}
        self.experiments = Experiments(self.temp_folder, use_cuda=self.use_cuda)
        data_tasks = []
        for i, (train_split, test_split) in enumerate(k_iterator):
            train_dataset, test_dataset = train_split.dataset, test_split.dataset
            x_tr, y_tr = train_dataset.xy
            x_te, y_te = test_dataset.xy
            data_task = Task.data_task(i, self.identifier, self.experiments)
            data_task.dump_data(x_tr, y_tr)
            data_task.dump_data(x_te, y_te, "_te")
            data_tasks.append(data_task)
        self._iterator_name = type(k_iterator).__name__
        self._add_tasks(self._iterator_name, data_tasks, self.experiments, benchmarks)
        return self._run_tasks(num_jobs, run_tasks, predict_config)

    def k_fold(
        self,
        k: int,
        x: data_type,
        y: data_type = None,
        *,
        num_jobs: int = 4,
        run_tasks: bool = True,
        predict_config: Dict[str, Any] = None,
        benchmarks: Dict[str, Dict[str, Dict[str, Any]]] = None,
    ) -> BenchmarkResults:
        dataset = self._pre_process(x, y)
        return self._k_core(
            KFold(k, dataset), num_jobs, run_tasks, predict_config, benchmarks
        )

    def k_random(
        self,
        k: int,
        num_test: Union[int, float],
        x: data_type,
        y: data_type = None,
        *,
        num_jobs: int = 4,
        run_tasks: bool = True,
        predict_config: Dict[str, Any] = None,
        benchmarks: Dict[str, Dict[str, Dict[str, Any]]] = None,
    ) -> BenchmarkResults:
        dataset = self._pre_process(x, y)
        return self._k_core(
            KRandom(k, num_test, dataset),
            num_jobs,
            run_tasks,
            predict_config,
            benchmarks,
        )

    def save(
        self,
        saving_folder: str,
        *,
        simplify: bool = True,
        compress: bool = True,
    ) -> "Benchmark":
        abs_folder = os.path.abspath(saving_folder)
        base_folder = os.path.dirname(abs_folder)
        with lock_manager(base_folder, [saving_folder]):
            Saving.prepare_folder(self, saving_folder)
            Saving.save_dict(
                {
                    "task_name": self.task_name,
                    "task_type": self.task_type.value,
                    "project_name": self.project_name,
                    "models": self.models,
                    "increment_config": self.increment_config,
                    "use_cuda": self.use_cuda,
                    "iterator_name": self._iterator_name,
                    "temp_folder": self.temp_folder,
                    "configs": self.configs,
                },
                "kwargs",
                abs_folder,
            )
            experiments_folder = os.path.join(abs_folder, "__experiments__")
            self.experiments.save(
                experiments_folder, simplify=simplify, compress=compress
            )
            if compress:
                Saving.compress(abs_folder, remove_original=True)
        return self

    @classmethod
    def load(
        cls,
        saving_folder: str,
        *,
        predict_config: Dict[str, Any] = None,
        compress: bool = True,
    ) -> Tuple["Benchmark", BenchmarkResults]:
        abs_folder = os.path.abspath(saving_folder)
        base_folder = os.path.dirname(abs_folder)
        with lock_manager(base_folder, [saving_folder]):
            with Saving.compress_loader(abs_folder, compress, remove_extracted=False):
                kwargs = Saving.load_dict("kwargs", abs_folder)
                configs = kwargs.pop("configs")
                iterator_name = kwargs.pop("iterator_name")
                kwargs["task_type"] = TaskTypes.from_str(kwargs["task_type"])
                benchmark = cls(**kwargs)
                benchmark.configs = configs
                benchmark._iterator_name = iterator_name
                benchmark.experiments = Experiments.load(
                    os.path.join(abs_folder, "__experiments__")
                )
                results = benchmark._run_tasks(0, False, predict_config)
        return benchmark, results


# ensemble


def ensemble(
    patterns: List[ModelPattern],
    *,
    pattern_weights: np.ndarray = None,
    ensemble_method: Union[str, collate_fn_type] = None,
) -> EnsemblePattern:
    if ensemble_method is None:
        if pattern_weights is None:
            ensemble_method = "default"
        else:
            pattern_weights = pattern_weights.reshape([-1, 1, 1])

            def ensemble_method(
                arrays: List[np.ndarray], requires_prob: bool
            ) -> np.ndarray:
                predictions = np.array(arrays).reshape(
                    [len(arrays), len(arrays[0]), -1]
                )
                if requires_prob or not is_int(predictions):
                    return (predictions * pattern_weights).sum(axis=0)
                encodings = one_hot(to_torch(predictions).to(torch.long).squeeze())
                encodings = encodings.to(torch.float32)
                weighted = (encodings * pattern_weights).sum(dim=0)
                return to_numpy(weighted.argmax(1)).reshape([-1, 1])

    return EnsemblePattern(patterns, ensemble_method)


class EnsembleResults(NamedTuple):
    data: TabularData
    pattern: EnsemblePattern
    experiments: Union[Experiments, None]


class Ensemble:
    def __init__(self, task_type: TaskTypes, config: Dict[str, Any] = None):
        self.task_type = task_type
        if config is None:
            config = {}
        self.config = config

    def bagging(
        self,
        x: data_type,
        y: data_type = None,
        *,
        k: int = 10,
        num_test: Union[int, float] = 0.1,
        num_jobs: int = 4,
        run_tasks: bool = True,
        predict_config: Dict[str, Any] = None,
        temp_folder: str = None,
        project_name: str = "carefree-learn",
        task_name: str = "bagging",
        models: Union[str, List[str]] = "fcnn",
        increment_config: Dict[str, Any] = None,
        use_cuda: bool = True,
    ) -> EnsembleResults:
        if isinstance(models, str):
            models = [models]

        data_config, read_config = map(
            self.config.get, ["data_config", "read_config"], [{}, {}]
        )
        benchmark = Benchmark(
            task_name,
            self.task_type,
            temp_folder=temp_folder,
            project_name=project_name,
            models=models,
            increment_config=increment_config,
            use_cuda=use_cuda,
            data_config=data_config,
            read_config=read_config,
        )
        dataset = benchmark._pre_process(x, y)
        k_bootstrap = KBootstrap(k, num_test, dataset)
        benchmark_results = benchmark._k_core(
            k_bootstrap,
            num_jobs,
            run_tasks,
            predict_config,
            {model: {"config": shallow_copy_dict(self.config)} for model in models},
        )

        def _pre_process(x_):
            return benchmark_results.data.transform(x_, contains_labels=False).x

        experiments = benchmark_results.experiments
        ms_dict = transform_experiments(experiments)
        all_models = sum(ms_dict.values(), [])
        all_patterns = [m.to_pattern(pre_process=_pre_process) for m in all_models]
        ensemble_pattern = ensemble(all_patterns)

        return EnsembleResults(benchmark_results.data, ensemble_pattern, experiments)

    def adaboost(
        self,
        x: data_type,
        y: data_type = None,
        *,
        k: int = 10,
        eps: float = 1e-12,
        model: str = "fcnn",
        increment_config: Dict[str, Any] = None,
        sample_weights: Union[np.ndarray, None] = None,
        num_test: Union[int, float] = 0.1,
    ) -> EnsembleResults:
        if increment_config is None:
            increment_config = {}
        config = shallow_copy_dict(self.config)
        update_dict(increment_config, config)
        config["cv_split"] = num_test
        config.setdefault("use_tqdm", False)
        config.setdefault("verbose_level", 0)

        data = None
        patterns, pattern_weights = [], []
        for _ in tqdm.tqdm(list(range(k))):
            m = make(model=model, **config)
            m.fit(x, y, sample_weights=sample_weights)
            predictions = m.predict(x, contains_labels=True).astype(np.float32)
            target = m._original_data.processed.y.astype(np.float32)
            errors = (predictions != target).ravel()
            if sample_weights is None:
                e = errors.mean()
            else:
                e = errors.dot(sample_weights) / len(errors)
            em = min(max(e, eps), 1.0 - eps)
            am = 0.5 * math.log(1.0 / em - 1.0)
            if sample_weights is None:
                sample_weights = np.ones_like(predictions).ravel()
            target[target == 0.0] = predictions[predictions == 0.0] = -1.0
            sample_weights *= np.exp(-am * target * predictions).ravel()
            sample_weights /= np.mean(sample_weights)
            patterns.append(m.to_pattern())
            pattern_weights.append(am)
            if data is None:
                data = m._original_data

        pattern_weights = np.array(pattern_weights, np.float32)
        ensemble_pattern = ensemble(patterns, pattern_weights=pattern_weights)
        return EnsembleResults(data, ensemble_pattern, None)


# others


class ONNX:
    def __init__(
        self,
        wrapper: Wrapper,
        onnx_path: str = None,
    ):
        self.wrapper = wrapper
        self.model = wrapper.model.cpu()
        self.ort_session = None
        if onnx_path is not None:
            self._init_onnx_session(onnx_path)
        # initialize
        self.input_sample = self.model.input_sample
        with eval_context(self.model):
            outputs = self.model(self.input_sample)
        self.input_names = sorted(self.input_sample.keys())
        self.output_names = sorted(outputs.keys())
        self.model.to(self.model.device)

    def _init_onnx_session(self, onnx_path: str) -> "ONNX":
        self.ort_session = onnxruntime.InferenceSession(onnx_path)

    def to_onnx(
        self,
        onnx_path: str,
        dynamic_axes: Union[List[int], Dict[int, str]] = None,
        **kwargs,
    ) -> "ONNX":
        kwargs["input_names"] = self.input_names
        kwargs["output_names"] = self.output_names
        kwargs["opset_version"] = 11
        kwargs["export_params"] = True
        kwargs["do_constant_folding"] = True
        if dynamic_axes is None:
            dynamic_axes = {}
        elif isinstance(dynamic_axes, list):
            dynamic_axes = {axis: f"axis.{axis}" for axis in dynamic_axes}
        dynamic_axes[0] = "batch_size"
        dynamic_axes_settings = {}
        for name in self.input_names + self.output_names:
            dynamic_axes_settings[name] = dynamic_axes
        kwargs["dynamic_axes"] = dynamic_axes_settings
        model = self.model.cpu()
        with eval_context(model):
            torch.onnx.export(model, self.input_sample, onnx_path, **kwargs)
        self._init_onnx_session(onnx_path)
        model.to(model.device)
        return self

    def inject_onnx(self) -> "ONNX":
        self.wrapper.pipeline.onnx = self
        del self.wrapper.model, self.wrapper.pipeline.model

    def inference(self, new_inputs: Dict[str, np.ndarray]) -> Dict[str, torch.Tensor]:
        ort_inputs = {
            node.name: to_standard(new_inputs[node.name])
            for node in self.ort_session.get_inputs()
        }
        outputs = dict(zip(self.output_names, self.ort_session.run(None, ort_inputs)))
        return {k: to_torch(v) for k, v in outputs.items()}


def make_toy_model(
    model: str = "fcnn",
    config: Dict[str, Any] = None,
    *,
    task_type: str = "reg",
    data_tuple: Tuple[data_type, data_type] = None,
) -> Wrapper:
    if config is None:
        config = {}
    if data_tuple is None:
        if task_type == "reg":
            data_tuple = [[0]], [[1]]
        else:
            data_tuple = [[0], [1]], [[1], [0]]
    base_config = {
        "model": model,
        "model_config": {
            "hidden_units": [100],
            "mapping_configs": {"dropout": 0.0, "batch_norm": False},
        },
        "cv_split": 0.0,
        "trigger_logging": False,
        "min_epoch": 250,
        "num_epoch": 500,
        "max_epoch": 1000,
        "optimizer": "sgd",
        "optimizer_config": {"lr": 0.01},
        "task_type": task_type,
        "data_config": {
            "valid_columns": list(range(len(data_tuple[0]))),
            "label_process_method": "identical",
        },
        "verbose_level": 0,
    }
    config = update_dict(config, base_config)
    return make(**config).fit(*data_tuple)


__all__ = [
    "load_task",
    "transform_experiments",
    "register_metric",
    "register_optimizer",
    "register_scheduler",
    "make",
    "save",
    "load",
    "estimate",
    "ensemble",
    "repeat_with",
    "tune_with",
    "OptunaParam",
    "optuna_tune",
    "make_toy_model",
    "Task",
    "Experiments",
    "Benchmark",
    "ModelBase",
    "Pipeline",
    "Wrapper",
    "Initializer",
    "register_initializer",
    "Processor",
    "register_processor",
    "ONNX",
]
