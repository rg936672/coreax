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
Example coreset generation using randomly generated point clouds.

This example showcases how a coreset can be generated from a dataset containing ``n``
points sampled from ``k`` clusters in space.

A coreset is generated using kernel herding, with a Squared Exponential kernel. To
reduce computational demand, we approximate the kernel matrix row sum mean (also known
as the mean of the Gram matrix row sum). This coreset is compared to a coreset generated
via uniform random sampling. Coreset quality is measured using maximum mean discrepancy
(MMD).
"""

# Support annotations with | in Python < 3.10
from __future__ import annotations

from pathlib import Path

import jax.numpy as jnp
import matplotlib.pyplot as plt
import numpy as np
from jax import random
from sklearn.datasets import make_blobs

from coreax import (
    MMD,
    ArrayData,
    KernelHerding,
    RandomSample,
    SizeReduce,
    SquaredExponentialKernel,
)
from coreax.approximation import ANNchorApproximator
from coreax.kernel import median_heuristic


# Examples are written to be easy to read, copy and paste by users, so we ignore the
# pylint warnings raised that go against this approach
# pylint: disable=duplicate-code
# pylint: disable=too-many-locals
def main(out_path: Path | None = None) -> tuple[float, float]:
    """
    Run the tabular data herding example with an approximate kernel matrix row sum mean.

    Generate a set of points from distinct clusters in a plane. Generate a coreset via
    kernel herding, where computation time is reduced by approximating the kernel matrix
    row sum mean, rather than computing it exactly with all data-points. Compare results
    to coresets generated via uniform random sampling. Coreset quality is measured using
    maximum mean discrepancy (MMD).

    :param out_path: Path to save output to, if not :data:`None`, assumed relative to
        this module file unless an absolute path is given
    :return: Coreset MMD, random sample MMD
    """
    # Create some data. Here we'll use 10,000 points in 2D from 6 distinct clusters. 2D
    # for plotting below.
    num_data_points = 10_000
    num_features = 2
    num_cluster_centers = 6
    random_seed = 1_989
    x, _, _centers = make_blobs(
        num_data_points,
        n_features=num_features,
        centers=num_cluster_centers,
        random_state=random_seed,
        return_centers=True,
    )

    # Request 100 coreset points
    coreset_size = 100

    # Setup the original data object
    data = ArrayData.load(x)

    # Set the bandwidth parameter of the kernel using a median heuristic derived from at
    # most 1000 random samples in the data.
    num_samples_length_scale = min(num_data_points, 1_000)
    generator = np.random.default_rng(random_seed)
    idx = generator.choice(num_data_points, num_samples_length_scale, replace=False)
    length_scale = median_heuristic(x[idx])

    # Define a kernel to use
    herding_kernel = SquaredExponentialKernel(length_scale=length_scale)

    print("Computing coreset...")
    # Compute a coreset using kernel herding with a Squared exponential kernel.
    herding_key, approximator_key, sample_key = random.split(random.key(random_seed), 3)
    herding_object = KernelHerding(
        herding_key,
        kernel=herding_kernel,
        approximator=ANNchorApproximator(
            approximator_key,
            kernel=herding_kernel,
            num_kernel_points=500,
            num_train_points=500,
        ),
    )
    herding_object.fit(
        original_data=data, strategy=SizeReduce(coreset_size=coreset_size)
    )

    print("Choosing random subset...")
    # Generate a coreset via uniform random sampling for comparison
    random_sample_object = RandomSample(sample_key, unique=True)
    random_sample_object.fit(
        original_data=data, strategy=SizeReduce(coreset_size=coreset_size)
    )

    # Define a reference kernel to use for comparisons of MMD. We'll use a normalised
    # SquaredExponentialKernel (which is also a Gaussian kernel)
    print("Computing MMD...")
    mmd_kernel = SquaredExponentialKernel(
        length_scale=length_scale,
        output_scale=1.0 / (length_scale * jnp.sqrt(2.0 * jnp.pi)),
    )

    # Compute the MMD between the original data and the coreset generated via herding
    metric_object = MMD(kernel=mmd_kernel)
    maximum_mean_discrepancy_herding = herding_object.compute_metric(metric_object)

    # Compute the MMD between the original data and the coreset generated via random
    # sampling
    maximum_mean_discrepancy_random = random_sample_object.compute_metric(metric_object)

    # Print the MMD values
    print(f"Random sampling coreset MMD: {maximum_mean_discrepancy_random}")
    print(f"Herding coreset MMD: {maximum_mean_discrepancy_herding}")

    # Produce some scatter plots (assume 2-dimensional data)
    plt.scatter(x[:, 0], x[:, 1], s=2.0, alpha=0.1)
    plt.scatter(
        herding_object.coreset[:, 0],
        herding_object.coreset[:, 1],
        s=10,
        color="red",
    )
    plt.axis("off")
    plt.title(
        f"Stein kernel herding, m={coreset_size}, "
        f"MMD={round(float(maximum_mean_discrepancy_herding), 6)}"
    )
    plt.show()

    plt.scatter(x[:, 0], x[:, 1], s=2.0, alpha=0.1)
    plt.scatter(
        random_sample_object.coreset[:, 0],
        random_sample_object.coreset[:, 1],
        s=10,
        color="red",
    )
    plt.title(
        f"Random, m={coreset_size}, "
        f"MMD={round(float(maximum_mean_discrepancy_random), 6)}"
    )
    plt.axis("off")

    if out_path is not None:
        if not out_path.is_absolute():
            out_path = Path(__file__).parent / out_path
        plt.savefig(out_path)

    plt.show()

    return (
        float(maximum_mean_discrepancy_herding),
        float(maximum_mean_discrepancy_random),
    )


# pylint: enable=duplicate-code
# pylint: enable=too-many-locals


if __name__ == "__main__":
    main()