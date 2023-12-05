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
Functionality to validate data passed throughout coreax.

The functions within this module are intended to be used as a means to validate inputs
passed to classes, functions and methods throughout the coreax codebase.
"""

# Support annotations with | in Python < 3.10
# TODO: Remove once no longer supporting old code
from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


def validate_in_range(
    x: T,
    object_name: str,
    strict_inequalities: bool,
    lower_bound: T | None = None,
    upper_bound: T | None = None,
) -> None:
    """
    Verify that a given input is in a specified range.

    :param x: Variable we wish to verify lies in the specified range
    :param object_name: Name of ``x`` to display if limits are broken
    :param strict_inequalities: If true, checks are applied using strict inequalities,
        otherwise they are not
    :param lower_bound: Lower limit placed on ``x``, or :data:`None`
    :param upper_bound: Upper limit placed on ``x``, or :data:`None`
    :raises ValueError: Raised if ``x`` does not fall between ``lower_limit`` and
        ``upper_limit``
    :raises TypeError: Raised if x cannot be compared to a value using >, >=, < or <=
    """
    try:
        if strict_inequalities:
            if lower_bound is not None and not x > lower_bound:
                raise ValueError(
                    f"{object_name} must be strictly above {lower_bound}. "
                    f"Given value {x}."
                )
            if upper_bound is not None and not x < upper_bound:
                raise ValueError(
                    f"{object_name} must be strictly below {lower_bound}. "
                    f"Given value {x}."
                )
        else:
            if lower_bound is not None and not x >= lower_bound:
                raise ValueError(
                    f"{object_name} must be {lower_bound} or above. Given value {x}."
                )
            if upper_bound is not None and not x <= upper_bound:
                raise ValueError(
                    f"{object_name} must be {lower_bound} or lower. Given value {x}."
                )
    except TypeError:
        raise TypeError(
            f"{object_name} must have a valid comparison <, <=, > and >= implemented."
        )


def validate_is_instance(x: T, object_name: str, expected_type: type[T]) -> None:
    """
    Verify that a given object is of a given type.

    :param x: Variable we wish to verify lies in the specified range
    :param object_name: Name of ``x`` to display if limits are broken
    :param expected_type: The expected type of ``x``
    :raises TypeError: Raised if ``x`` is not of type ``expected_type``
    """
    if not isinstance(x, expected_type):
        raise TypeError(f"{object_name} must be of type {expected_type}.")


def cast_as_type(x: Any, object_name: str, type_caster: Callable) -> Any:
    """
    Cast an object as a specified type.

    :param x: Variable to cast as specified type
    :param object_name: Name of the object being considered
    :param type_caster: Callable that ``x`` will be passed
    :return: ``x``, but cast as the type specified by ``type_caster``
    :raises TypeError: Raised if ``x`` cannot be cast using ``type_caster``
    """
    try:
        return type_caster(x)
    except (TypeError, ValueError) as e:
        error_text = f"{object_name} cannot be cast using {type_caster}. \n"
        if hasattr(e, "message"):
            error_text += e.message
        else:
            error_text += str(e)

        if isinstance(e, TypeError):
            raise TypeError(error_text)
        else:
            raise ValueError(error_text)
