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
Functionality to perform simple, generic tasks and operations.

The functions within this module are simple solutions to various problems or
requirements that are sufficiently generic to be useful across multiple areas of the
codebase. Examples of this include computation of squared distances, definition of
class factories and checks for numerical precision.
"""

import logging
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from functools import partial, wraps
from math import log10
from typing import Any, NamedTuple, Optional, Sequence, TypeVar

import equinox as eqx
import jax.numpy as jnp
import jax.random as jr
import jax.tree_util as jtu
from jax import Array, block_until_ready, jit, vmap
from jax.typing import ArrayLike
from typing_extensions import TypeAlias, deprecated

_logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, stream=sys.stdout)

PyTreeDef: TypeAlias = Any
Leaf: TypeAlias = Any

#: JAX random key type annotations.
KeyArray: TypeAlias = Array
KeyArrayLike: TypeAlias = ArrayLike


class NotCalculatedError(Exception):
    """Raise when trying to use a variable that has not been calculated yet."""


class JITCompilableFunction(NamedTuple):
    """
    Parameters for :func:`jit_test`.

    :param fn: JIT-compilable function callable to test
    :param fn_args: Arguments passed during the calls to the passed function
    :param fn_kwargs: Keyword arguments passed during the calls to the passed function
    :param jit_kwargs: Keyword arguments that are partially applied to :func:`jax.jit`
        before being called to compile the passed function
    """

    fn: Callable
    fn_args: tuple = ()
    fn_kwargs: Optional[dict] = None
    jit_kwargs: Optional[dict] = None


class InvalidKernel:
    """
    Simple class that does not have a compute method on to test kernel.

    This is used across several testing instances to ensure the consequence of invalid
    inputs is correctly caught.
    """

    def __init__(self, x: float):
        """Initialise the invalid kernel object."""
        self.x = x


def tree_leaves_repeat(tree: PyTreeDef, length: int = 2) -> list[Leaf]:
    """
    Flatten a PyTree to its leaves and (potentially) repeat the trailing leaf.

    The PyTree 'tree' is flattened, but unlike the standard flattening, :data:`None` is
    treated as a valid leaf and the trailing leaf (potentially) repeated such that
    the length of the collection of leaves is given by the 'length' parameter.

    :param tree: The PyTree to flatten and whose trailing leaf to (potentially) repeat
    :param length: The length of the flattened PyTree after any repetition; values are
        implicitly clipped by :code:`max(len(tree_leaves), length)`
    :return: The PyTree leaves, with the trailing leaf repeated as many times as
        required for the collection of leaves to have length 'repeated_length'
    """
    tree_leaves = jtu.tree_leaves(tree, is_leaf=lambda x: x is None)
    num_repeats = length - len(tree_leaves)
    return tree_leaves + tree_leaves[-1:] * num_repeats


def tree_zero_pad_leading_axis(tree: PyTreeDef, pad_width: int) -> PyTreeDef:
    """
    Pad each array leaf of 'tree' with 'pad_width' trailing zeros.

    :param tree: The PyTree whose array leaves to pad with trailing zeros
    :param pad_width: The number of trailing zeros to pad with
    :return: A copy of the original PyTree with the array leaves padded
    """
    if int(pad_width) < 0:
        raise ValueError("'pad_width' must be a positive integer")
    leaves_to_pad, leaves_to_keep = eqx.partition(tree, eqx.is_array)

    def _pad(x: ArrayLike) -> Array:
        padding = (0, int(pad_width))
        skip_padding = ((0, 0),) * (jnp.ndim(x) - 1)
        return jnp.pad(x, (padding, *skip_padding))

    padded_leaves = jtu.tree_map(_pad, leaves_to_pad)
    return eqx.combine(padded_leaves, leaves_to_keep)


def apply_negative_precision_threshold(
    x: ArrayLike, precision_threshold: float = 1e-8
) -> Array:
    """
    Round a number to 0.0 if it is negative but within precision_threshold of 0.0.

    :param x: Scalar value we wish to compare to 0.0
    :param precision_threshold: Positive threshold we compare against for precision
    :return: ``x``, rounded to 0.0 if it is between ``-precision_threshold`` and 0.0
    """
    _x = jnp.asarray(x)
    return jnp.where((-jnp.abs(precision_threshold) < _x) & (_x < 0.0), 0.0, _x)


def pairwise(
    fn: Callable[[ArrayLike, ArrayLike], Array],
) -> Callable[[ArrayLike, ArrayLike], Array]:
    """
    Transform a function so it returns all pairwise evaluations of its inputs.

    :param fn: the function to apply the pairwise transform to.
    :returns: function that returns an array whose entries are the evaluations of `fn`
        for every pairwise combination of its input arguments.
    """

    @wraps(fn)
    def pairwise_fn(x: ArrayLike, y: ArrayLike) -> Array:
        x = jnp.atleast_2d(x)
        y = jnp.atleast_2d(y)
        return vmap(
            vmap(fn, in_axes=(0, None), out_axes=0),
            in_axes=(None, 0),
            out_axes=1,
        )(x, y)

    return pairwise_fn


@jit
def squared_distance(x: ArrayLike, y: ArrayLike) -> Array:
    """
    Calculate the squared distance between two vectors.

    :param x: First vector argument
    :param y: Second vector argument
    :return: Dot product of ``x - y`` and ``x - y``, the square distance between ``x``
        and ``y``
    """
    x = jnp.atleast_1d(x)
    y = jnp.atleast_1d(y)
    return jnp.dot(x - y, x - y)


@deprecated(
    "Use coreax.util.pairwise(coreax.util.squared_distance)(x, y);"
    "will be removed in version 0.3.0"
)
def squared_distance_pairwise(x: ArrayLike, y: ArrayLike) -> Array:
    r"""
    Calculate efficient pairwise square distance between two arrays.

    :param x: First set of vectors as a :math:`n \times d` array
    :param y: Second set of vectors as a :math:`m \times d` array
    :return: Pairwise squared distances between ``x_array`` and ``y_array`` as an
        :math:`n \times m` array
    """
    return pairwise(squared_distance)(x, y)


@jit
def difference(x: ArrayLike, y: ArrayLike) -> Array:
    """
    Calculate vector difference for a pair of vectors.

    :param x: First vector
    :param y: Second vector
    :return: Vector difference ``x - y``
    """
    x = jnp.atleast_1d(x)
    y = jnp.atleast_1d(y)
    return x - y


@deprecated(
    "Use coreax.kernels.util.median_heuristic; will be removed in version 0.3.0"
)
@jit
def median_heuristic(x: ArrayLike) -> Array:
    """
    Compute the median heuristic for setting kernel bandwidth.

    Analysis of the performance of the median heuristic can be found in
    :cite:`garreau2018median`.

    :param x: Input array of vectors
    :return: Bandwidth parameter, computed from the median heuristic, as a
        zero-dimensional array
    """
    # Format inputs
    x = jnp.atleast_2d(x)
    # Calculate square distances as an upper triangular matrix
    square_distances = jnp.triu(pairwise(squared_distance)(x, x), k=1)
    # Calculate the median of the square distances
    median_square_distance = jnp.median(
        square_distances[jnp.triu_indices_from(square_distances, k=1)]
    )

    return jnp.sqrt(median_square_distance / 2.0)


@deprecated(
    "Use coreax.util.pairwise(coreax.util.difference)(x, y);"
    "will be removed in version 0.3.0"
)
def pairwise_difference(x: ArrayLike, y: ArrayLike) -> Array:
    r"""
    Calculate efficient pairwise difference between two arrays of vectors.

    :param x: First set of vectors as a :math:`n \times d` array
    :param y: Second set of vectors as a :math:`m \times d` array
    :return: Pairwise differences between ``x_array`` and ``y_array`` as an
        :math:`n \times m \times d` array
    """
    return pairwise(difference)(x, y)


def sample_batch_indices(
    random_key: KeyArrayLike,
    max_index: int,
    batch_size: int,
    num_batches: int,
) -> Array:
    """
    Sample an array of indices of size `num_batches` x `batch_size`.

    Each row (batch) of the sampled array will contain unique elements.

    :param random_key: Key for random number generation
    :param max_index: Largest index we wish to sample
    :param batch_size: Size of the batch we wish to sample
    :param num_batches: Number of batches to sample

    :return: Array of batch indices of size `num_batches` x `batch_size`
    """
    if max_index < batch_size:
        raise ValueError("'max_index' must be greater than or equal to 'batch_size'")
    if batch_size < 0.0:
        raise ValueError("'batch_size' must be non-negative")

    batch_keys = jr.split(random_key, num_batches)
    batch_permutation = vmap(jr.permutation, in_axes=(0, None))
    return batch_permutation(batch_keys, max_index)[:, :batch_size]


def jit_test(
    fn: Callable,
    fn_args: tuple = (),
    fn_kwargs: Optional[dict] = None,
    jit_kwargs: Optional[dict] = None,
    check_hash: bool = True,
) -> tuple[float, float]:
    """
    Measure execution times of two runs of a JIT-compilable function.

    The function is called with supplied arguments twice, and timed for each run. These
    timings are returned in a 2-tuple. These timings can help verify the JIT performance
    by comparing timings of a before and after run of a function.

    :param fn: JIT-compilable function callable to test
    :param fn_args: Arguments passed during the calls to the passed function
    :param fn_kwargs: Keyword arguments passed during the calls to the passed function
    :param jit_kwargs: Keyword arguments that are partially applied to :func:`jax.jit`
        before being called to compile the passed function
    :param check_hash: If :data:`True`, check that the hash of the JITted function is
        different to the supplied function
    :return: (First run time, Second run time), in seconds
    """
    # Avoid dangerous default values - Pylint W0102
    if fn_kwargs is None:
        fn_kwargs = {}
    if jit_kwargs is None:
        jit_kwargs = {}

    @partial(jit, **jit_kwargs)
    def _fn(*args, **kwargs):
        return fn(*args, **kwargs)

    if check_hash:
        assert hash(_fn) != hash(fn), "Cannot guarantee recompilation of `fn`."

    start_time = time.perf_counter()
    block_until_ready(_fn(*fn_args, **fn_kwargs))
    end_time = time.perf_counter()
    pre_delta = end_time - start_time

    start_time = time.perf_counter()
    block_until_ready(_fn(*fn_args, **fn_kwargs))
    end_time = time.perf_counter()
    post_delta = end_time - start_time

    return pre_delta, post_delta


def format_time(num: float) -> str:
    """
    Standardise the format of the input time.

    Floats will be converted to a standard format, e.g. 0.4531 -> "453.1 ms".

    :param num: Float to be converted
    :return: Formatted time as a string
    """
    if num == 0:
        return "0 s"
    order = log10(abs(num))
    if order >= 2:  # noqa: PLR2004
        scaled_time = num / 60
        unit_string = "mins"
    if order < 2:  # noqa: PLR2004
        scaled_time = num
        unit_string = "s"
    if order < 0:  # noqa: PLR2004
        scaled_time = 1e3 * num
        unit_string = "ms"
    if order < -3:  # noqa: PLR2004
        scaled_time = 1e6 * num
        unit_string = "\u03bcs"
    if order < -6:  # noqa: PLR2004
        scaled_time = 1e9 * num
        unit_string = "ns"
    if order < -9:  # noqa: PLR2004
        scaled_time = 1e12 * num
        unit_string = "ps"

    return f"{round(scaled_time, 2)} {unit_string}"


def speed_comparison_test(
    function_setups: Sequence[JITCompilableFunction],
    num_runs: int = 10,
    log_results: bool = False,
    check_hash: bool = False,
) -> tuple[list[tuple[Array, Array]], dict[str, Array]]:
    """
    Compare compilation time and runtime of a list of JIT-able functions.

    :param function_setups: Sequence of instances of :class:`JITCompilableFunction`
    :param num_runs: Number of times to average function timings over
    :param log_results: If :data:`True`, the results are formatted and logged
    :param check_hash: If :data:`True`, check that the hash of the JITted functions are
        different to the supplied functions
    :return: List of tuples (means, standard deviations) for each function containing
        JIT compilation and execution times as array components; Dictionary with
        key function number and value array of execution time savings for each repeat
        test of a function

    """
    timings_dict = {}
    results = []
    for i, function in enumerate(function_setups):
        timings = jnp.zeros((num_runs, 2))
        for j in range(num_runs):
            timings = timings.at[j, :].set(jit_test(*function, check_hash=check_hash))
        # Compute the time just spent on compilation
        post_processed_timings = timings.at[:, 0].set(timings[:, 0] - timings[:, 1])
        timings_dict[i] = post_processed_timings
        # Compute summary statistics
        mean = post_processed_timings.mean(axis=0)
        std = post_processed_timings.std(axis=0)
        results.append((mean, std))
        if log_results:
            _logger.info("------------------- Function %s -------------------", i + 1)
            _logger.info(
                "Compilation time: "
                + f"{format_time(mean[0].item())} ± "
                + f"{format_time(std[0].item())}"
                + f" per run (mean ± std. dev. of {num_runs} runs)"
            )
            _logger.info(
                "Execution time: "
                + f"{format_time(mean[1].item())} ± "
                + f"{format_time(std[1].item())}"
                + f" per run (mean ± std. dev. of {num_runs} runs)"
            )

    return results, timings_dict


T = TypeVar("T")


class SilentTQDM:
    """
    Class implementing interface of :class:`~tqdm.tqdm` that does nothing.

    It can substitute :class:`~tqdm.tqdm` to silence all output.

    Based on `code by Pro Q <https://stackoverflow.com/a/77450937>`_.

    Additional parameters are accepted and ignored to match interface of
    :class:`~tqdm.tqdm`.

    :param iterable: Iterable of tasks to (not) indicate progress for
    """

    def __init__(self, iterable: Iterable[T], *_args, **_kwargs):
        """Store iterable."""
        self.iterable = iterable

    def __iter__(self) -> Iterator[T]:
        """
        Iterate.

        :return: Next item
        """
        return iter(self.iterable)

    def write(self, *_args, **_kwargs) -> None:
        """Do nothing instead of writing to output."""
