import os
import cflearn

import numpy as np

from cftool.misc import get_latest_workplace


file_folder = os.path.dirname(__file__)
train_file = os.path.join(file_folder, "train.csv")
test_file = os.path.join(file_folder, "test.csv")
kwargs = dict(carefree=True, cf_data_config={"label_name": "Survived"})
m = cflearn.api.fit_ml(train_file, **kwargs)  # type: ignore

idata = m.make_inference_data(test_file, contains_labels=False)
results = m.predict(idata)
predictions = results[cflearn.PREDICTIONS_KEY]

export_folder = "titanic"
m.save(export_folder)
m2 = cflearn.api.load(export_folder)
results = m2.predict(idata)
assert np.allclose(predictions, results[cflearn.PREDICTIONS_KEY])

latest = get_latest_workplace("_logs")
assert latest is not None
m3 = cflearn.api.load(cflearn.api.pack(latest))
results = m3.predict(idata)
assert np.allclose(predictions, results[cflearn.PREDICTIONS_KEY])
