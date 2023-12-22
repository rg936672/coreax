# © Crown Copyright GCHQ
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Classes and associated functionality to define neural networks.

Neural networks are used throughout the codebase as functional approximators.
"""

from collections.abc import Callable

from flax import linen as nn
from flax.linen import Module
from flax.training.train_state import TrainState
from jax import numpy as jnp
from jax import random
from jax.typing import ArrayLike


class ScoreNetwork(nn.Module):
    """
    A feed-forward neural network for use in sliced score matching.

    See :class:'~coreax.score_matching.SlicedScoreMatching' for an example usage of this
    class.
    """

    hidden_dim: int
    output_dim: int

    @nn.compact
    def __call__(self, x: ArrayLike) -> ArrayLike:
        r"""
        Compute forward pass through a three-layer network with softplus activations.

        :param x: Batch input data :math:`b \times m \times n`
        :return: Network output on batch :math:`b \times` ``self.output_dim``
        """
        x = nn.Dense(self.hidden_dim)(x)
        x = nn.softplus(x)
        x = nn.Dense(self.hidden_dim)(x)
        x = nn.softplus(x)
        x = nn.Dense(self.hidden_dim)(x)
        x = nn.softplus(x)
        x = nn.Dense(self.output_dim)(x)
        return x


def create_train_state(
    module: Module,
    learning_rate: float,
    data_dimension: int,
    optimiser: Callable,
    random_key: random.PRNGKey = random.PRNGKey(0),
) -> TrainState:
    """
    Create a flax :class:`~flax.training.train_state.TrainState` for learning with.

    :param module: Subclass of :class:`~flax.nn.Module`
    :param learning_rate: Optimiser learning rate
    :param data_dimension: Data dimension
    :param optimiser: optax optimiser, e.g. :class:`~optax.adam`
    :param random_key: Key for random number generation
    :return: :class:`~flax.training.train_state.TrainState` object
    """
    params = module.init(random_key, jnp.ones((1, data_dimension)))["params"]
    tx = optimiser(learning_rate)
    return TrainState.create(apply_fn=module.apply, params=params, tx=tx)
