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

"""Solvers for constructing coresubsets."""

from collections.abc import Callable
from typing import Optional, TypeVar, Union
from warnings import warn

import equinox as eqx
import jax
import jax.numpy as jnp
import jax.random as jr
import jax.scipy as jsp
import jax.tree_util as jtu
from jaxtyping import Array, ArrayLike
from typing_extensions import override

from coreax.coreset import Coresubset
from coreax.data import Data, SupervisedData, as_data
from coreax.kernel import Kernel
from coreax.least_squares import (
    MinimalEuclideanNormSolver,
    RegularisedLeastSquaresSolver,
)
from coreax.score_matching import ScoreMatching, convert_stein_kernel
from coreax.solvers.base import (
    CoresubsetSolver,
    ExplicitSizeSolver,
    PaddingInvariantSolver,
    RefinementSolver,
)
from coreax.util import KeyArrayLike, sample_batch_indices, tree_zero_pad_leading_axis

_Data = TypeVar("_Data", bound=Data)
_SupervisedData = TypeVar("_SupervisedData", bound=SupervisedData)


class SizeWarning(Warning):
    """Custom warning to be raised when some parameter shape is too large."""


class HerdingState(eqx.Module):
    """
    Intermediate :class:`KernelHerding` solver state information.

    :param gramian_row_mean: Cached Gramian row-mean.
    """

    gramian_row_mean: Array


class RPCholeskyState(eqx.Module):
    """
    Intermediate :class:`RPCholesky` solver state information.

    :param gramian_diagonal: Cached Gramian diagonal.
    """

    gramian_diagonal: Array


class GreedyKernelInducingPointsState(eqx.Module):
    """
    Intermediate :class:`GreedyKernelInducingPoints` solver state information.

    :param feature_gramian: Cached feature kernel gramian matrix, should be padded with
        an additional row and column of zeros.
    """

    feature_gramian: Array


MSG = "'coreset_size' must be less than 'len(dataset)' by definition of a coreset"


def _initial_coresubset(
    fill_value: int, coreset_size: int, dataset: _Data
) -> Coresubset[_Data]:
    """Generate a coresubset with `fill_value` valued and zero-weighted indices."""
    initial_coresubset_indices = Data(
        jnp.full((coreset_size,), fill_value, dtype=jnp.int32), 0
    )
    try:
        return Coresubset(initial_coresubset_indices, dataset)
    except ValueError as err:
        if len(initial_coresubset_indices) > len(dataset):
            raise ValueError(MSG) from err
        raise


def _greedy_kernel_selection(
    coresubset: Coresubset[_Data],
    selection_function: Callable[[int, ArrayLike], Array],
    output_size: int,
    kernel: Kernel,
    unique: bool,
    block_size: Optional[Union[int, tuple[Optional[int], Optional[int]]]],
    unroll: Union[int, bool, tuple[Union[int, bool], Union[int, bool]]],
) -> Coresubset[_Data]:
    """
    Iterative-greedy coresubset point selection loop.

    Primarily intended for use with :class`KernelHerding` and :class:`SteinThinning`.

    :param coresubset: The initialisation
    :param selection_function: Greedy selection function/objective
    :param output_size: The size of the resultant coresubset
    :param kernel: The kernel used to compute the penalty
    :param unique: If each index in the resulting coresubset should be unique
    :param block_size: Block size passed to :meth:`~coreax.kernel.Kernel.compute_mean`
    :param unroll: Unroll parameter passed to :meth:`~coreax.kernel.Kernel.compute_mean`
    :return: Coresubset generated by iterative-greedy selection
    """
    # If the initialisation coresubset is too small, pad its nodes up to 'output_size'
    # with zero valued and weighted indices.
    padding = max(0, output_size - len(coresubset))
    padded_indices = tree_zero_pad_leading_axis(coresubset.nodes, padding)
    padded_coresubset = eqx.tree_at(lambda x: x.nodes, coresubset, padded_indices)
    # The kernel similarity penalty must be computed for the initial coreset. If we did
    # not support refinement, the penalty could be initialised to the zeros vector, and
    # the result would be invariant to the initial coresubset.
    data, weights = jtu.tree_leaves(coresubset.pre_coreset_data)
    kernel_similarity_penalty = kernel.compute_mean(
        data,
        padded_coresubset.coreset,
        axis=1,
        block_size=block_size,
        unroll=unroll,
    )

    def _greedy_body(i: int, val: tuple[Array, Array]) -> tuple[Array, ArrayLike]:
        coreset_indices, kernel_similarity_penalty = val
        valid_kernel_similarity_penalty = jnp.where(
            weights > 0, kernel_similarity_penalty, jnp.nan
        )
        updated_coreset_index = selection_function(i, valid_kernel_similarity_penalty)
        updated_coreset_indices = coreset_indices.at[i].set(updated_coreset_index)
        penalty_update = jnp.ravel(kernel.compute(data, data[updated_coreset_index]))
        updated_penalty = kernel_similarity_penalty + penalty_update
        if unique:
            # Prevent the same 'updated_coreset_index' from being selected on a
            # subsequent iteration, by setting the penalty to infinity.
            updated_penalty = updated_penalty.at[updated_coreset_index].set(jnp.inf)
        return updated_coreset_indices, updated_penalty

    init_state = (padded_coresubset.unweighted_indices, kernel_similarity_penalty)
    output_state = jax.lax.fori_loop(0, output_size, _greedy_body, init_state)
    updated_coreset_indices = output_state[0][:output_size]
    return eqx.tree_at(lambda x: x.nodes, coresubset, as_data(updated_coreset_indices))


class KernelHerding(
    RefinementSolver[_Data, HerdingState], ExplicitSizeSolver, PaddingInvariantSolver
):
    r"""
    Kernel Herding - an explicitly sized coresubset refinement solver.

    Solves the coresubset problem by taking a deterministic, iterative, and greedy
    approach to minimizing the (weighted) Maximum Mean Discrepancy (MMD) between the
    coresubset (the solution) and the problem dataset.

    Given one has selected :math:`T` data points for their compressed representation of
    the original dataset, kernel herding selects the next point using Equation 8 of
    :cite:`chen2012herding`:

    .. math::

        x_{T+1} = \arg\max_{x} \left( \mathbb{E}[k(x, x')] -
            \frac{1}{T+1}\sum_{t=1}^T k(x, x_t) \right)

    where :math:`k` is the kernel used, the expectation :math:`\mathbb{E}` is taken over
    the entire dataset, and the search is over the entire dataset. This can informally
    be seen as a balance between using points at which the underlying density is high
    (the first term) and exploration of distinct regions of the space (the second term).

    :param coreset_size: The desired size of the solved coreset
    :param kernel: :class:`~coreax.kernel.Kernel` instance implementing a kernel
        function :math:`k: \mathbb{R}^d \times \mathbb{R}^d \rightarrow \mathbb{R}`
    :param unique: Boolean that ensures the resulting coresubset will only contain
        unique elements
    :param block_size: Block size passed to :meth:`~coreax.kernel.Kernel.compute_mean`
    :param unroll: Unroll parameter passed to :meth:`~coreax.kernel.Kernel.compute_mean`
    """

    kernel: Kernel
    unique: bool = True
    block_size: Optional[Union[int, tuple[Optional[int], Optional[int]]]] = None
    unroll: Union[int, bool, tuple[Union[int, bool], Union[int, bool]]] = 1

    @override
    def reduce(
        self,
        dataset: _Data,
        solver_state: Optional[HerdingState] = None,
    ) -> tuple[Coresubset[_Data], HerdingState]:
        initial_coresubset = _initial_coresubset(0, self.coreset_size, dataset)
        return self.refine(initial_coresubset, solver_state)

    def refine(
        self,
        coresubset: Coresubset[_Data],
        solver_state: Optional[HerdingState] = None,
    ) -> tuple[Coresubset[_Data], HerdingState]:
        """
        Refine a coresubset with 'Kernel Herding'.

        We first compute the kernel's Gramian row-mean if it is not given in the
        'solver_state', and then iteratively swap points with the initial coreset,
        balancing selecting points in high density regions with selecting points far
        from those already in the coreset.

        :param coresubset: The coresubset to refine
        :param solver_state: Solution state information, primarily used to cache
            expensive intermediate solution step values.
        :return: A refined coresubset and relevant intermediate solver state information
        """
        if solver_state is None:
            x, bs, un = coresubset.pre_coreset_data, self.block_size, self.unroll
            gramian_row_mean = self.kernel.gramian_row_mean(x, block_size=bs, unroll=un)
        else:
            gramian_row_mean = solver_state.gramian_row_mean

        def selection_function(i: int, _kernel_similarity_penalty: ArrayLike) -> Array:
            """Greedy selection criterion - Equation 8 of :cite:`chen2012herding`."""
            return jnp.nanargmax(
                gramian_row_mean - _kernel_similarity_penalty / (i + 1)
            )

        refined_coreset = _greedy_kernel_selection(
            coresubset,
            selection_function,
            self.coreset_size,
            self.kernel,
            self.unique,
            self.block_size,
            self.unroll,
        )
        return refined_coreset, HerdingState(gramian_row_mean)


class RandomSample(CoresubsetSolver[_Data, None], ExplicitSizeSolver):
    """
    Reduce a dataset by randomly sampling a fixed number of points.

    :param coreset_size: The desired size of the solved coreset
    :param random_key: Key for random number generation
    :param weighted: If to use dataset weights as selection probabilities
    :param unique: If to sample without replacement
    """

    random_key: KeyArrayLike
    weighted: bool = False
    unique: bool = True

    @override
    def reduce(
        self, dataset: _Data, solver_state: None = None
    ) -> tuple[Coresubset, None]:
        selection_weights = dataset.weights if self.weighted else None
        try:
            random_indices = jr.choice(
                self.random_key,
                len(dataset),
                (self.coreset_size,),
                p=selection_weights,
                replace=not self.unique,
            )
            return Coresubset(random_indices, dataset), solver_state
        except ValueError as err:
            if self.coreset_size > len(dataset) and self.unique:
                raise ValueError(MSG) from err
            raise


class RPCholesky(CoresubsetSolver[_Data, RPCholeskyState], ExplicitSizeSolver):
    r"""
    Randomly Pivoted Cholesky - an explicitly sized coresubset refinement solver.

    Solves the coresubset problem by taking a stochastic, iterative, and greedy approach
    to approximating the Gramian of a given kernel, evaluated on the original dataset.

    :param coreset_size: The desired size of the solved coreset
    :param random_key: Key for random number generation
    :param kernel: :class:`~coreax.kernel.Kernel` instance implementing a kernel
        function :math:`k: \mathbb{R}^d \times \mathbb{R}^d \rightarrow \mathbb{R}`
    :param unique: If each index in the resulting coresubset should be unique
    """

    random_key: KeyArrayLike
    kernel: Kernel
    unique: bool = True

    def reduce(
        self, dataset: _Data, solver_state: Optional[RPCholeskyState] = None
    ) -> tuple[Coresubset[_Data], RPCholeskyState]:
        """
        Reduce 'dataset' to a :class:`~coreax.coreset.Coresubset` with 'RPCholesky'.

        This is done by first computing the kernel Gram matrix of the original data, and
        isolating the diagonal of this. A 'pivot point' is then sampled, where sampling
        probabilities correspond to the size of the elements on this diagonal. The
        data-point corresponding to this pivot point is added to the coreset, and the
        diagonal of the Gram matrix is updated to add a repulsion term of sorts -
        encouraging the coreset to select a range of distinct points in the original
        data. The pivot sampling and diagonal updating steps are repeated until
        :math:`M` points have been selected.

        :param dataset: The dataset to reduce to a coresubset
        :param solver_state: Solution state information, primarily used to cache
            expensive intermediate solution step values.
        :return: a refined coresubset and relevant intermediate solver state information
        """
        x = dataset.data
        if solver_state is None:
            gramian_diagonal = jax.vmap(self.kernel.compute_elementwise)(x, x)
        else:
            gramian_diagonal = solver_state.gramian_diagonal
        initial_coresubset = _initial_coresubset(0, self.coreset_size, dataset)
        coreset_indices = initial_coresubset.unweighted_indices
        num_data_points = len(x)

        def _greedy_body(
            i: int, val: tuple[Array, Array, Array]
        ) -> tuple[Array, Array, Array]:
            """RPCholesky iteration - Algorithm 1 of :cite:`chen2023randomly`."""
            residual_diagonal, approximation_matrix, coreset_indices = val
            key = jr.fold_in(self.random_key, i)
            pivot_point = jr.choice(
                key, num_data_points, (), p=residual_diagonal, replace=False
            )
            updated_coreset_indices = coreset_indices.at[i].set(pivot_point)
            # Remove overlap with previously chosen columns
            g = (
                self.kernel.compute(x, x[pivot_point])
                - (approximation_matrix @ approximation_matrix[pivot_point])[:, None]
            )
            updated_approximation_matrix = approximation_matrix.at[:, i].set(
                jnp.ravel(g / jnp.sqrt(g[pivot_point]))
            )
            # Track diagonal of residual matrix and ensure it remains non-negative
            updated_residual_diagonal = jnp.clip(
                residual_diagonal - jnp.square(approximation_matrix[:, i]), min=0
            )
            if self.unique:
                # Ensures that index selected_pivot_point can't be drawn again in future
                updated_residual_diagonal = updated_residual_diagonal.at[
                    pivot_point
                ].set(0.0)
            return (
                updated_residual_diagonal,
                updated_approximation_matrix,
                updated_coreset_indices,
            )

        approximation_matrix = jnp.zeros((num_data_points, self.coreset_size))
        init_state = (gramian_diagonal, approximation_matrix, coreset_indices)
        output_state = jax.lax.fori_loop(0, self.coreset_size, _greedy_body, init_state)
        _, _, updated_coreset_indices = output_state
        updated_coreset = Coresubset(updated_coreset_indices, dataset)
        return updated_coreset, RPCholeskyState(gramian_diagonal)


class SteinThinning(
    RefinementSolver[_Data, None], ExplicitSizeSolver, PaddingInvariantSolver
):
    r"""
    Stein Thinning - an explicitly sized coresubset refinement solver.

    Solves the coresubset problem by taking a deterministic, iterative, and greedy
    approach to minimizing the Kernelised Stein Discrepancy (KSD) between the empirical
    distribution of the coresubset (the solution) and the distribution of the problem
    dataset, as characterised by the score function of the Stein Kernel.

    Given one has selected :math:`T` data points for their compressed representation of
    the original dataset, (regularised) Stein thinning selects the next point using the
    equations in Section 3.1 of :cite:`benard2023kernel`:

    .. math::

        x_{T+1} = \arg\min_{x} \left( k_P(x, x) / 2 + \Delta^+ \log p(x) -
            \lambda T \log p(x) + \frac{1}{T+1}\sum_{t=1}^T k_P(x, x_t) \right)

    where :math:`k` is the Stein kernel induced by the supplied base kernel,
    :math:`\Delta^+` is the non-negative Laplace operator, :math:`\lambda` is a
    regularisation parameter, and the search is over the entire dataset.

    :param coreset_size: The desired size of the solved coreset
    :param kernel: :class:`~coreax.kernel.Kernel` instance implementing a kernel
        function :math:`k: \mathbb{R}^d \times \mathbb{R}^d \rightarrow \mathbb{R}`;
        if 'kernel' is a :class:`~coreax.kernel.SteinKernel` and :code:`score_matching
        is not None`, a new instance of the kernel will be generated where the score
        function is given by :code:`score_matching.match(...)`
    :param score_matching: Specifies/overwrite the score function of the implied/passed
       :class:`~coreax.kernel.SteinKernel`; if :data:`None`, default to
       :class:`~coreax.score_matching.KernelDensityMatching` unless 'kernel' is a
       :class:`~coreax.kernel.SteinKernel`, in which case the kernel's existing score
       function is used.
    :param unique: If each index in the resulting coresubset should be unique
    :param regularise: Boolean that enforces regularisation, see Section 3.1 of
        :cite:`benard2023kernel`.
    :param block_size: Block size passed to :meth:`~coreax.kernel.Kernel.compute_mean`
    :param unroll: Unroll parameter passed to :meth:`~coreax.kernel.Kernel.compute_mean`
    """

    kernel: Kernel
    score_matching: Optional[ScoreMatching] = None
    unique: bool = True
    regularise: bool = True
    block_size: Optional[Union[int, tuple[Optional[int], Optional[int]]]] = None
    unroll: Union[int, bool, tuple[Union[int, bool], Union[int, bool]]] = 1

    @override
    def reduce(
        self, dataset: _Data, solver_state: None = None
    ) -> tuple[Coresubset[_Data], None]:
        initial_coresubset = _initial_coresubset(0, self.coreset_size, dataset)
        return self.refine(initial_coresubset, solver_state)

    def refine(
        self, coresubset: Coresubset[_Data], solver_state: None = None
    ) -> tuple[Coresubset[_Data], None]:
        r"""
        Refine a coresubset with 'Stein thinning'.

        We first compute a score function, and then the Stein kernel. This is used to
        greedily choose points in the coreset to minimise kernel Stein discrepancy
        (KSD).

        :param coresubset: The coresubset to refine
        :param solver_state: Solution state information, primarily used to cache
            expensive intermediate solution step values.
        :return: a refined coresubset and relevant intermediate solver state information
        """
        x, w_x = jtu.tree_leaves(coresubset.pre_coreset_data)
        kernel = convert_stein_kernel(x, self.kernel, self.score_matching)
        stein_kernel_diagonal = jax.vmap(self.kernel.compute_elementwise)(x, x)
        if self.regularise:
            # Cannot guarantee that kernel.base_kernel has a 'length_scale' attribute
            bandwidth_method = getattr(kernel.base_kernel, "length_scale", None)
            kde = jsp.stats.gaussian_kde(x.T, weights=w_x, bw_method=bandwidth_method)
            # Use regularisation parameter suggested in :cite:`benard2023kernel`
            regulariser_lambda = 1 / len(coresubset)
            regularised_log_pdf = regulariser_lambda * kde.logpdf(x.T)

            @jax.vmap
            def _laplace_positive(x_):
                r"""Evaluate Laplace positive operator  :math:`\Delta^+ \log p(x)`."""
                hessian = jax.jacfwd(kernel.score_function)(x_)
                return jnp.clip(jnp.diag(hessian), min=0.0).sum()

            laplace_correction = _laplace_positive(x)
        else:
            laplace_correction, regularised_log_pdf = 0.0, 0.0

        def selection_function(i: int, _kernel_similarity_penalty: ArrayLike) -> Array:
            """
            Greedy selection criterion - Section 3.1 :cite:`benard2023kernel`.

            Argmin of the Laplace corrected and regularised Kernel Stein Discrepancy.
            """
            ksd = stein_kernel_diagonal + 2.0 * _kernel_similarity_penalty
            return jnp.nanargmin(ksd + laplace_correction - i * regularised_log_pdf)

        refined_coreset = _greedy_kernel_selection(
            coresubset,
            selection_function,
            self.coreset_size,
            self.kernel,
            self.unique,
            self.block_size,
            self.unroll,
        )
        return refined_coreset, solver_state


def _greedy_kernel_inducing_points_loss(
    candidate_coresets: Array,
    responses: Array,
    feature_gramian: Array,
    regularisation_parameter: float,
    identity: Array,
    least_squares_solver: RegularisedLeastSquaresSolver,
) -> Array:
    """
    Given an array of candidate coreset indices, compute the greedy KIP loss for each.

    Primarily intended for use with :class:`GreedyKernelInducingPoints`.

    :param candidate_coresets: Array of indices representing all possible "next"
        coresets
    :param responses: Array of responses
    :param feature_gramian: Feature kernel gramian
    :param regularisation_parameter: Regularisation parameter for stable inversion of
        array, negative values will be converted to positive
    :param identity: Identity array used to regularise the feature gramians
        corresponding to each coreset. For :meth:`GreedyKernelInductionPoints.reduce`
        this array is a matrix of zeros except for ones on the diagonal up to the
        current size of the coreset. For :meth:`GreedyKernelInductionPoints.refine` this
        array is a standard identity array.
    :param least_squares_solver: Instance of
        :class:`coreax.least_squares.RegularisedLeastSquaresSolver`

    :return: :class`GreedyKernelInducingPoints` loss for each candidate coreset
    """
    # Extract all the possible "next" coreset feature gramians, cross feature gramians
    # and coreset response vectors.
    coreset_gramians = feature_gramian[
        (candidate_coresets[:, :, None], candidate_coresets[:, None, :])
    ]
    coreset_cross_gramians = feature_gramian[candidate_coresets, :-1]
    coreset_responses = responses[candidate_coresets]

    # Solve for the kernel ridge regression coefficients for each possible coreset
    coefficients = least_squares_solver.solve_stack(
        arrays=coreset_gramians,
        regularisation_parameter=regularisation_parameter,
        targets=coreset_responses,
        identity=identity,
    )

    # Compute the loss function, making sure that we remove the padding on the responses
    predictions = (coreset_cross_gramians * coefficients).sum(axis=1)
    loss = ((predictions - 2 * responses[:-1, 0]) * predictions).sum(axis=1)
    return loss


# pylint: disable=too-many-locals
class GreedyKernelInducingPoints(
    RefinementSolver[_SupervisedData, GreedyKernelInducingPointsState],
    ExplicitSizeSolver,
):
    r"""
    Apply `GreedyKernelInducingPoints` to a supervised dataset.

    `GreedyKernelInducingPoints` is a deterministic, iterative and greedy approach to
    build a coreset.

    Given one has an original dataset :math:`\mathcal{D}^{(1)} = \{(x_i, y_i)\}_{i=1}^n`
    of :math:`n` pairs with :math:`x\in\mathbb{R}^d` and :math:`y\in\mathbb{R}^p`, and
    one has selected :math:`m` data pairs :math:`\mathcal{D}^{(2)} = \{(\tilde{x}_i,
    \tilde{y}_i)\}_{i=1}^m` already for their compressed representation of the original
    dataset, `GreedyKernelInducingPoints` selects the next point to minimise the loss

    .. math::

        L(\mathcal{D}^{(1)}, \mathcal{D}^{(2)}) = ||y^{(1)} -
        K^{(12)}(K^{(22)} + \lambda I_m)^{-1}y^{(2)} ||^2_{\mathbb{R}^n}

    where :math:`y^{(1)}\in\mathbb{R}^n` is the vector of responses from
    :math:`\mathcal{D}^{(1)}`, :math:`y^{(2)}\in\mathbb{R}^n` is the vector of responses
    from :math:`\mathcal{D}^{(2)}`,  :math:`K^{(12)} \in \mathbb{R}^{n\times m}` is the
    cross-matrix of kernel evaluations between :math:`\mathcal{D}^{(1)}` and
    :math:`\mathcal{D}^{(2)}`, :math:`K^{(22)} \in \mathbb{R}^{m\times m}` is the
    kernel matrix on :math:`\mathcal{D}^{(2)}`,
    :math:`\lambda I_m \in \mathbb{R}^{m \times m}` is the identity matrix and
    :math:`\lambda \in \mathbb{R}_{>0}` is a regularisation parameter.

    This class works with all children of :class:`~coreax.kernel.Kernel`. Note that
    `GreedyKernelInducingPoints` does not support non-uniform weights and will only
    return coresubsets with uniform weights.

    :param random_key: Key for random number generation
    :param feature_kernel: :class:`~coreax.kernel.Kernel` instance implementing a kernel
        function :math:`k: \mathbb{R}^d \times \mathbb{R}^d \rightarrow \mathbb{R}`
        on the feature space
    :param regularisation_parameter: Regularisation parameter for stable inversion of
        the feature Gramian
    :param unique: If :data:`False`, the resulting coresubset may contain the same point
        multiple times. If :data:`True` (default), the resulting coresubset will not
        contain any duplicate points
    :param batch_size: An integer representing the size of the batches of data pairs
        sampled at each iteration for consideration for adding to the coreset. If
        :data:`None` (default), the search is performed over the entire dataset
    :param least_squares_solver: Instance of
        :class:`coreax.least_squares.RegularisedLeastSquaresSolver`, default value of
        :data:`None` uses :class:`coreax.least_squares.MinimalEuclideanNormSolver`
    """

    random_key: KeyArrayLike
    feature_kernel: Kernel
    regularisation_parameter: float = 1e-6
    unique: bool = True
    batch_size: Optional[int] = None
    least_squares_solver: Optional[RegularisedLeastSquaresSolver] = None

    @override
    def reduce(
        self,
        dataset: _SupervisedData,
        solver_state: Optional[GreedyKernelInducingPointsState] = None,
    ) -> tuple[Coresubset[_SupervisedData], GreedyKernelInducingPointsState]:
        initial_coresubset = _initial_coresubset(-1, self.coreset_size, dataset)
        return self.refine(initial_coresubset, solver_state)

    def refine(  # noqa: PLR0915
        self,
        coresubset: Coresubset[_SupervisedData],
        solver_state: Union[GreedyKernelInducingPointsState, None] = None,
    ) -> tuple[Coresubset[_SupervisedData], GreedyKernelInducingPointsState]:
        """
        Refine a coresubset with 'GreedyKernelInducingPointsState'.

        We first compute the various factors if they are not given in the
        `solver_state`, and then iteratively swap points with the initial coreset,
        selecting points which minimise the loss.

        :param coresubset: The coresubset to refine
        :param solver_state: Solution state information, primarily used to cache
            expensive intermediate solution step values.
        :return: A refined coresubset and relevant intermediate solver state information
        """
        # Handle default value of None
        if self.least_squares_solver is None:
            least_squares_solver = MinimalEuclideanNormSolver()
        else:
            least_squares_solver = self.least_squares_solver

        # If the initialisation coresubset is too small, pad its nodes up to
        # 'output_size' with -1 valued indices. If it is too large, raise a warning and
        # clip off the indices at the end.
        if self.coreset_size > len(coresubset):
            pad_size = max(0, self.coreset_size - len(coresubset))
            coreset_indices = jnp.hstack(
                (
                    coresubset.unweighted_indices,
                    -1 * jnp.ones(pad_size, dtype=jnp.int32),
                )
            )
        elif self.coreset_size < len(coresubset):
            warn(
                "Requested coreset size is smaller than input 'coresubset', clipping"
                + " to the correct size and proceeding...",
                SizeWarning,
                stacklevel=2,
            )
            coreset_indices = coresubset.unweighted_indices[: self.coreset_size]
        else:
            coreset_indices = coresubset.unweighted_indices

        # Extract features and responses
        dataset = coresubset.pre_coreset_data
        num_data_pairs = len(dataset)
        x, y = dataset.data, dataset.supervision

        # Pad the response array with an additional zero to allow us to
        # extract sub-arrays and fill in elements with zeros simultaneously.
        padded_responses = jnp.vstack((y, jnp.array([[0]])))

        if solver_state is None:
            # Pad the gramian with zeros in an additional column and row
            padded_feature_gramian = jnp.pad(
                self.feature_kernel.compute(x, x), [(0, 1)], mode="constant"
            )
        else:
            padded_feature_gramian = solver_state.feature_gramian

        # Sample the indices to be considered at each iteration ahead of time.
        if self.batch_size is not None and self.batch_size < num_data_pairs:
            batch_size = self.batch_size
        else:
            batch_size = num_data_pairs
        batch_indices = sample_batch_indices(
            random_key=self.random_key,
            max_index=num_data_pairs,
            batch_size=batch_size,
            num_batches=self.coreset_size,
        )

        # Initialise an array that will let us extract arrays corresponding to every
        # possible candidate coreset.
        initial_candidate_coresets = (
            jnp.tile(coreset_indices, (batch_size, 1)).at[:, 0].set(batch_indices[0, :])
        )

        # Adaptively initialise an "identity matrix". For reduction we need this to
        # be a zeros matrix which we will iteratively add ones to on the diagonal. While
        # for refinement we need an actual identity matrix.
        identity_helper = jnp.hstack((jnp.ones(num_data_pairs), jnp.array([0])))
        identity = jnp.diag(identity_helper[coreset_indices])

        def _greedy_body(
            i: int, val: tuple[Array, Array, Array]
        ) -> tuple[Array, Array, Array]:
            """Execute main loop of GreedyKernelInducingPoints."""
            coreset_indices, identity, candidate_coresets = val

            # Update the identity matrix to allow for sub-array inversion in the
            # case of reduction (no effect when refining).
            updated_identity = identity.at[i, i].set(1)

            # Compute the loss corresponding to each candidate coreset. Note that we do
            # not compute the first term as it is an invariant quantity wrt the coreset.
            loss = _greedy_kernel_inducing_points_loss(
                candidate_coresets,
                padded_responses,
                padded_feature_gramian,
                self.regularisation_parameter,
                updated_identity,
                least_squares_solver,
            )

            # Find the optimal replacement coreset index, ensuring we don't pick an
            # already chosen point if we want the indices to be unique. Note we
            # must set the ith index to -1 for refinement purposes, as we are happy for
            # the current index to be retained if it is the best.
            if self.unique:
                already_chosen_indices_mask = jnp.isin(
                    candidate_coresets[:, i],
                    coreset_indices.at[i].set(-1),
                )
                loss += jnp.where(already_chosen_indices_mask, jnp.inf, 0)
            index_to_include_in_coreset = candidate_coresets[loss.argmin(), i]

            # Repeat the chosen coreset index into the ith column of the array of
            # candidate coreset indices. Replace the (i+1)th column with the next batch
            # of possible coreset indices.
            updated_candidate_coresets = (
                candidate_coresets.at[:, i]
                .set(index_to_include_in_coreset)
                .at[:, i + 1]
                .set(batch_indices[i + 1, :])
            )

            # Add the chosen coreset index to the current coreset indices
            updated_coreset_indices = coreset_indices.at[i].set(
                index_to_include_in_coreset
            )
            return updated_coreset_indices, updated_identity, updated_candidate_coresets

        # Greedily refine coreset points
        updated_coreset_indices, _, _ = jax.lax.fori_loop(
            lower=0,
            upper=self.coreset_size,
            body_fun=_greedy_body,
            init_val=(
                coreset_indices,
                identity,
                initial_candidate_coresets,
            ),
        )

        return Coresubset(
            updated_coreset_indices, dataset
        ), GreedyKernelInducingPointsState(padded_feature_gramian)
