# This file is part of the pyMOR project (https://www.pymor.org).
# Copyright 2013-2021 pyMOR developers and contributors. All rights reserved.
# License: BSD 2-Clause License (https://opensource.org/licenses/BSD-2-Clause)

from numbers import Number
import numpy as np
import pytest

from hypothesis import given

from pymor.algorithms.basic import almost_equal
from pymor.algorithms.timestepping import ImplicitEulerTimeStepper, ExplicitEulerTimeStepper, ExplicitRungeKuttaTimeStepper
from pymor.discretizers.builtin.fv import discretize_instationary_fv

from pymortests.base import runmodule
from pymortests.fixtures.analyticalproblem import linear_transport_problems


def assert_all_iterator_variants_work(fom, mu=None, expected_len=None, additional_check=lambda U: True):
    # variant returning an iterator yielding U(t)
    U_iter = fom.solution_space.empty()
    for U in fom.solve(mu=mu, return_iter=True):
        U_iter.append(U)
    if expected_len:
        assert len(U_iter) == expected_len
    assert additional_check(U_iter)
    # variant returning an iterator yielding U(t), t
    U_iter_times = fom.solution_space.empty()
    for U, t in fom.solve(return_iter=True, return_times=True):
        assert isinstance(t, Number)
        assert 0. <= t and t <= fom.T
        U_iter_times.append(U)
    if expected_len:
        assert len(U_iter_times) == expected_len
    assert additional_check(U_iter_times)
    # variant returning the full trajectory U(0), ..., U(T)
    U_no_iter = fom.solve()
    if expected_len:
        assert len(U_no_iter) == expected_len
    assert additional_check(U_no_iter)


@pytest.mark.parametrize('num_values_factor', [None, 0.3, 1, 17])
def test_ExplicitEuler_linear_transport_exact(num_values_factor):
    nt = 10
    fom, _ = discretize_instationary_fv(linear_transport_problems[0], diameter=1/nt, nt=nt)
    if num_values_factor:
        num_values = num_values_factor*nt + 1
        fom = fom.with_time_stepper(num_values=num_values)
    else:
        num_values = nt + 1

    assert_all_iterator_variants_work(
            fom, mu=None, expected_len=num_values, additional_check=lambda U: almost_equal(U[0], U[-1]))


if __name__ == "__main__":
    runmodule(filename=__file__)
