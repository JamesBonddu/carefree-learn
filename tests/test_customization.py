import torch
import cflearn
import unittest

import numpy as np


class TestCustomization(unittest.TestCase):
    def test_customize_model(self) -> None:
        @cflearn.MLModel.register("foo")
        class _(cflearn.MLModel):
            def __init__(self) -> None:
                super().__init__()
                self.dummy = torch.nn.Parameter(torch.tensor([1.0]))

            def forward(self, net: torch.Tensor) -> torch.Tensor:
                net = net.new_empty([net.shape[0], 1]).fill_(1.0)
                return {cflearn.PREDICTIONS_KEY: net}

        x = np.random.random([1000, 10])
        y = np.random.random([1000, 1])
        config = cflearn.MLConfig(model_name="foo", loss_name="mae", fixed_steps=0)
        m = cflearn.MLTrainingPipeline.init(config)
        data = cflearn.MLData.init().fit(x, y)
        m.fit(data)
        loader = m.data.build_loader(x)
        predictions = m.predict(loader)[cflearn.PREDICTIONS_KEY]
        self.assertTrue(np.allclose(predictions, np.ones_like(y)))
        params = list(m.build_model.model.parameters())[0]
        self.assertTrue(params is m.build_model.model.dummy)


if __name__ == "__main__":
    unittest.main()
