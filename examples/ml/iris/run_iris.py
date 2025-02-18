# type: ignore

import os
import pickle
import cflearn

from cflearn.misc.toolkit import check_is_ci
from cflearn.misc.toolkit import seed_everything


seed_everything(123)

is_ci = check_is_ci()
file_folder = os.path.dirname(__file__)
iris_data_file = os.path.join(file_folder, "iris.data")
sklearn_runner_file = os.path.join(file_folder, "run_sklearn.py")


if __name__ == "__main__":
    processor_config = cflearn.MLBundledProcessorConfig(has_header=False, num_split=25)
    data = cflearn.MLData.init(processor_config=processor_config).fit(iris_data_file)
    config = cflearn.MLConfig(
        model_name="fcnn",
        model_config=dict(input_dim=data.num_features, output_dim=data.num_labels),
        loss_name="focal",
        metric_names=["acc", "auc"],
    )
    if is_ci:
        config.to_debug()
    m = cflearn.api.fit_ml(data, config=config)
    data = m.data
    config = m.config
    x_train = data.train_dataset.x
    print("> mean", x_train.mean(0))
    print("> std ", x_train.std(0))

    loader = data.build_loader(iris_data_file)
    rs = m.predict(loader)
    rs = m.predict(loader, return_probabilities=True)
    rs = m.predict(loader, return_classes=True)
    print(rs["predictions"][[0, 60, 100]])
    cflearn.api.evaluate(loader, dict(m=m))

    results = cflearn.api.repeat_ml(data, m.config, num_repeat=3)
    pipelines = cflearn.api.load_pipelines(results)
    cflearn.api.evaluate(loader, pipelines=pipelines)

    models = ["linear", "fcnn"]
    results = cflearn.api.repeat_ml(data, m.config, models=models, num_repeat=3)
    pipelines = cflearn.api.load_pipelines(results)
    cflearn.api.evaluate(loader, pipelines=pipelines)

    try:
        import sklearn
    except:
        msg = "`scikit-learn` is not installed, so advanced benchmark will not be executed."
        print(msg)
        exit(0)

    experiment = cflearn.dist.ml.Experiment()
    data_folder = experiment.dump_data(data)

    experiment.add_task(model="fcnn", config=config, data_folder=data_folder)
    experiment.add_task(model="linear", config=config, data_folder=data_folder)
    run_command = f"python {sklearn_runner_file}"
    common_kwargs = {"run_command": run_command, "data_folder": data_folder}
    experiment.add_task(model="decision_tree", **common_kwargs)  # type: ignore
    experiment.add_task(model="random_forest", **common_kwargs)  # type: ignore

    results = experiment.run_tasks()

    pipelines = cflearn.api.load_pipelines(results)
    for workspace, workspace_key in zip(results.workspaces, results.workspace_keys):
        model = workspace_key[0]
        if model in ["decision_tree", "random_forest"]:
            model_file = os.path.join(workspace, "sk_model.pkl")
            with open(model_file, "rb") as f:
                predictor = cflearn.SKLearnClassifier(pickle.load(f))
                pipelines[model] = cflearn.GeneralEvaluationPipeline(config, predictor)

    cflearn.api.evaluate(loader, pipelines)
