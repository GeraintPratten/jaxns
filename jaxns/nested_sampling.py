from jax.config import config

config.update("jax_enable_x64", True)
import jax.numpy as jnp
from jax.lax import while_loop, dynamic_update_slice
from jax import random, vmap
from jax.scipy.special import logsumexp
from typing import NamedTuple, Dict
from collections import namedtuple

from jaxns.prior_transforms import PriorChain
from jaxns.param_tracking import \
    TrackedExpectation
from jaxns.utils import dict_multimap
from jaxns.likelihood_samplers import (slice_sampling,  init_slice_sampler_state,
                                       multi_ellipsoid_sampler, init_multi_ellipsoid_sampler_state)


class NestedSamplerState(NamedTuple):
    key: jnp.ndarray
    done: bool
    i: int
    num_likelihood_evaluations: int  # int, number of times logL evaluated.
    live_points_U: jnp.ndarray  # [N, M] points_U in unit cube of live points_U
    live_points: Dict  # [N, M] points_U in constrained space of live points_U in dict struture
    log_L_live: jnp.ndarray  # log likelihood of live points_U
    dead_points: Dict  # [D, M] dead points_U in dci structure
    log_X: jnp.ndarray  # [D] logX
    log_w: jnp.ndarray  # [D] dX L
    num_dead: int  # int, number of samples (dead points_U) taken so far.
    log_L_dead: jnp.ndarray  # log likelhood of dead points_U
    sampler_efficiency: jnp.ndarray  # array of efficiency per accepted sample
    status: int  # exit status: 0=good, 1=max samples reached
    sampler_state: namedtuple  # arbitrary state passed between iterations of the sampling step
    tracked_expectations_state: namedtuple  # marginalised functions states


class NestedSampler(object):
    """
    Nested sampling class, which implements a general framework for performing nested sampling with
    arbitrary (jax-compatible) constrained likelihood samplers.
    """

    _available_samplers = ['slice', 'multi_ellipsoid']

    def __init__(self, loglikelihood, prior_chain: PriorChain, sampler_name='slice', **marginalised):
        self.sampler_name = sampler_name
        if self.sampler_name not in self._available_samplers:
            raise ValueError("sampler {} should be one of {}.".format(self.sampler_name, self._available_samplers))

        def fixed_likelihood(**x):
            log_L = loglikelihood(**x)
            if log_L.shape != ():
                raise ValueError("Shape of likelihood should be scalar, got {}".format(log_L.shape))
            return jnp.where(jnp.isnan(log_L), -jnp.inf, log_L)

        self.loglikelihood = fixed_likelihood
        self.prior_chain = prior_chain

        def loglikelihood_from_U(U):
            return fixed_likelihood(**prior_chain(U))

        self.loglikelihood_from_U = loglikelihood_from_U
        self.marginalised = marginalised if len(marginalised) > 0 else None
        test_input = dict_multimap(lambda shape: jnp.zeros(shape), prior_chain.to_shapes)
        self.marginalised_shapes = {k: marg(**test_input).shape for k, marg in marginalised.items()} if len(
            marginalised) > 0 else None

    def _filter_prior_chain(self, d):
        """
        Filters a dict's keys to only those where prior variable of same name is tracked.
        Used for removing untracked priors.

        Args:
            d: dict

        Returns: dict with only keys that correspond to names being tracked.
        """
        return {name: d[name] for name, prior in self.prior_chain.prior_chain.items() if prior.tracked}

    def initial_state(self, key, num_live_points, max_samples, collect_samples: bool, only_marginalise: bool,
                      sampler_kwargs):
        """
        Initialises the state of samplers.
        """
        # get initial live points_U
        def single_sample(key):
            U = random.uniform(key, shape=(self.prior_chain.U_ndims,))
            constrained = self.prior_chain(U)
            log_L = self.loglikelihood(**constrained)
            return U, constrained, log_L

        key, init_key = random.split(key, 2)
        live_points_U, live_points, log_L_live = vmap(single_sample)(random.split(init_key, num_live_points))

        if not only_marginalise:
            if collect_samples:
                dead_points = dict_multimap(lambda shape: jnp.zeros((max_samples,) + shape),
                                            self._filter_prior_chain(self.prior_chain.to_shapes))
            else:
                dead_points = None
            log_L_dead = jnp.zeros((max_samples,))
            sampler_efficiency = jnp.ones((max_samples,))
            log_X = -jnp.inf * jnp.ones((max_samples,))  # [D] logX
            log_w = -jnp.inf * jnp.ones((max_samples,))  # [D] dX L
        else:
            dead_points = None
            log_L_dead = None
            sampler_efficiency = None
            log_X = None
            log_w = None

        tracked_expectations = TrackedExpectation(self.marginalised, self.marginalised_shapes)

        # select cluster to spawn into
        if self.sampler_name == 'slice':
            key, init_sampler_state_key = random.split(key, 2)
            depth = sampler_kwargs.get('depth', 3)
            num_slices = sampler_kwargs.get('num_slices', 1)
            sampler_state = init_slice_sampler_state(init_sampler_state_key, live_points_U, depth,
                                                     tracked_expectations.state.X.log_value, num_slices)
        elif self.sampler_name == 'multi_ellipsoid':
            if sampler_kwargs is None:
                sampler_kwargs = dict()
            key, init_sampler_state_key = random.split(key, 2)
            depth = sampler_kwargs.get('depth', 3)
            sampler_state = init_multi_ellipsoid_sampler_state(
                init_sampler_state_key, live_points_U, depth, tracked_expectations.state.X.log_value)
        else:
            raise ValueError("Invalid sampler name {}".format(self.sampler_name))

        state = NestedSamplerState(
            key=key,
            done=jnp.array(False),
            i=jnp.array(0),
            num_likelihood_evaluations=num_live_points,
            live_points=live_points,
            live_points_U=live_points_U,
            log_L_live=log_L_live,
            dead_points=dead_points,
            log_L_dead=log_L_dead,
            sampler_efficiency=sampler_efficiency,
            num_dead=jnp.array(0),
            status=jnp.array(0),
            sampler_state=sampler_state,
            tracked_expectations_state=tracked_expectations.state,
            log_X=log_X,
            log_w=log_w
        )

        return state

    def _one_step(self, state: NestedSamplerState, collect_samples: bool, only_marginalise: bool, sampler_kwargs):
        """
        Performs one step of the algorthim.
        """
        # get next dead point
        i_min = jnp.argmin(state.log_L_live)
        dead_point = dict_multimap(lambda x: x[i_min, ...], state.live_points)
        log_L_min = state.log_L_live[i_min]

        N = state.log_L_live.shape[0]

        # update tracking
        tracked_expectations = TrackedExpectation(self.marginalised, self.marginalised_shapes,
                                                  state=state.tracked_expectations_state)

        tracked_expectations.update(dead_point, N, log_L_min)
        if not only_marginalise:
            log_X = dynamic_update_slice(state.log_X,
                                         tracked_expectations.state.X.log_value[None],
                                         [state.num_dead])
            log_w = dynamic_update_slice(state.log_w,
                                         tracked_expectations.state.dw.log_value[None],
                                         [state.num_dead])
            log_L_dead = dynamic_update_slice(state.log_L_dead,
                                              log_L_min[None],
                                              [state.num_dead])

            state = state._replace(log_X=log_X, log_w=log_w, log_L_dead=log_L_dead)

            if collect_samples:
                dead_points = dict_multimap(lambda x, y: dynamic_update_slice(x,
                                                                              y.astype(x.dtype)[None, ...],
                                                                              [state.num_dead] + [0] * len(y.shape)),
                                            state.dead_points, dead_point)
                state = state._replace(dead_points=dead_points)

        # select cluster to spawn into
        if self.sampler_name == 'slice':
            num_slices = sampler_kwargs.get('num_slices', 1)
            sampler_results = slice_sampling(state.key,
                                             log_L_constraint=log_L_min,
                                             live_points_U=state.live_points_U,
                                             num_slices=num_slices,
                                             loglikelihood_from_constrained=self.loglikelihood,
                                             prior_transform=self.prior_chain,
                                             i_min=i_min,
                                             log_X=tracked_expectations.state.X.log_value,
                                             sampler_state=state.sampler_state)
        elif self.sampler_name == 'multi_ellipsoid':
            sampler_results = multi_ellipsoid_sampler(state.key,
                                                      log_L_min,
                                                      state.live_points_U,
                                                      self.loglikelihood,
                                                      self.prior_chain,
                                                      state.sampler_state,
                                                      tracked_expectations.state.X.log_value,
                                                      i_min)
        else:
            raise ValueError("Invalid sampler name {}".format(self.sampler_name))
        #
        log_L_live = dynamic_update_slice(state.log_L_live, sampler_results.log_L_new[None], [i_min])
        live_points = dict_multimap(lambda x, y: dynamic_update_slice(x, y.astype(x.dtype)[None, ...],
                                                                      [i_min] + [0] * len(y.shape)),
                                    state.live_points,
                                    sampler_results.x_new)
        live_points_U = dynamic_update_slice(state.live_points_U, sampler_results.u_new[None, :],
                                             [i_min, 0])

        if not only_marginalise:
            sampler_efficiency = dynamic_update_slice(state.sampler_efficiency,
                                                      1. / sampler_results.num_likelihood_evaluations[None],
                                                      [state.num_dead])
            state = state._replace(sampler_efficiency=sampler_efficiency)

        state = state._replace(key=sampler_results.key,
                               num_likelihood_evaluations=state.num_likelihood_evaluations +
                                                          sampler_results.num_likelihood_evaluations,
                               log_L_live=log_L_live,
                               live_points=live_points,
                               live_points_U=live_points_U,
                               sampler_state=sampler_results.sampler_state,
                               tracked_expectations_state=tracked_expectations.state,
                               num_dead=state.num_dead + 1
                               )

        return state

    def __call__(self, key, num_live_points,
                 max_samples=1e5,
                 collect_samples=True,
                 termination_frac=0.01,
                 only_marginalise=False,
                 sampler_kwargs=None):
        """
        Perform nested sampling.

        Args:
            key: PRNG
            num_live_points: int, number of live points to use in the computation.
                Rule of thumb=(D+1)*(# posterior modes)*O(50), i.e. you want at least D+1 points per mode
                to be able to detect the mode with ellipsoidal clustering, and you need several times more than that
                to be accurate.
            max_samples: int, the maximum number of samples to take.
                Rule of thumb=(information gain)*(num_live_points)*(a few), where information gain can be measured by
                running a low accuracy run with fewer live points. Just make sure this number is big enough.
            collect_samples: bool, whether to keep any tracked variables (tracked=True in the prior transform).
                Consumes much more memory, as the samples need to be allocated space.
            termination_frac: float, the algorthim is terminated when this much of current evidence estimate is greater
                than the amount left in live points.
            only_marginalise: bool, by setting this to true even the diagnostics are not recorded. So plot_diagnostics
                is not possible.
            sampler_kwargs: dict of parameters to pass to the likelihood constrained sampler.

        Returns: NestedSamplingResult
        """
        if sampler_kwargs is None:
            sampler_kwargs = dict()
        max_samples = int(max_samples)
        num_live_points = int(num_live_points)
        state = self.initial_state(key, num_live_points,
                                   max_samples=max_samples,
                                   collect_samples=collect_samples,
                                   only_marginalise=only_marginalise,
                                   sampler_kwargs=sampler_kwargs)

        def body(state: NestedSamplerState):
            # do one sampling step
            state = self._one_step(state, collect_samples=collect_samples, only_marginalise=only_marginalise,
                                   sampler_kwargs=sampler_kwargs)

            tracked_expectations = TrackedExpectation(self.marginalised, self.marginalised_shapes,
                                                      state=state.tracked_expectations_state)
            # Z_live = <L> X_i = exp(logsumexp(log_L_live) - log(N) + log(X))
            logZ_live = logsumexp(state.log_L_live) - jnp.log(
                state.log_L_live.shape[0]) + tracked_expectations.state.X.log_value
            # Z_live < f * Z => logZ_live < log(f) + logZ
            done = (logZ_live < jnp.log(termination_frac) + tracked_expectations.evidence_mean()) \
                   | ((state.i + 1) >= max_samples)
            state = state._replace(done=done,
                                   i=state.i + 1)
            return state

        state = while_loop(lambda state: ~state.done,
                           body,
                           state)
        results = self._finalise_results(state, collect_samples=collect_samples,
                                         only_marginalise=only_marginalise,
                                         max_samples=max_samples)
        return results

    def _finalise_results(self, state: NestedSamplerState, collect_samples: bool,
                          only_marginalise: bool,
                          max_samples: int):
        """
        Produces the NestedSamplingResult.
        """
        collect = ['logZ',
                   'logZerr',
                   'ESS',
                   'ESS_err',
                   'H',
                   'H_err',
                   'num_likelihood_evaluations',
                   'efficiency',
                   'marginalised',
                   'marginalised_uncert',
                   'log_L_samples',
                   'n_per_sample',
                   'log_p',
                   'log_X',
                   'sampler_efficiency',
                   'num_samples'
                   ]

        if collect_samples and not only_marginalise:
            collect.append('samples')

        NestedSamplerResults = namedtuple('NestedSamplerResults', collect)
        tracked_expectations = TrackedExpectation(self.marginalised, self.marginalised_shapes,
                                                  state=state.tracked_expectations_state)
        live_update_results = tracked_expectations.update_from_live_points(state.live_points, state.log_L_live)
        if self.marginalised is not None:
            marginalised = tracked_expectations.marg_mean()
            marginalised_uncert = None  # tracked_expectations.marg_variance() not stable
        else:
            marginalised = None
            marginalised_uncert = None

        num_live_points = state.log_L_live.shape[0]
        if not only_marginalise:
            n_per_sample = jnp.where(jnp.arange(max_samples) < state.num_dead, num_live_points, jnp.inf)
            n_per_sample = dynamic_update_slice(n_per_sample,
                                                num_live_points - jnp.arange(num_live_points, dtype=n_per_sample.dtype),
                                                [state.num_dead])
            sampler_efficiency = dynamic_update_slice(state.sampler_efficiency,
                                                      jnp.ones(num_live_points),
                                                      [state.num_dead])
            log_w = dynamic_update_slice(state.log_w,
                                         live_update_results[3],
                                         [state.num_dead])
            log_p = log_w - logsumexp(log_w)
            log_X = dynamic_update_slice(state.log_X,
                                         live_update_results[2],
                                         [state.num_dead])
            log_L_samples = dynamic_update_slice(state.log_L_dead,
                                                 live_update_results[1],
                                                 [state.num_dead])
        else:
            n_per_sample = None
            log_p = None
            log_X = None
            log_L_samples = None
            sampler_efficiency = None

        num_samples = state.num_dead + num_live_points

        data = dict(
            logZ=tracked_expectations.evidence_mean(),
            logZerr=jnp.sqrt(tracked_expectations.evidence_variance()),
            ESS=tracked_expectations.effective_sample_size(),
            ESS_err=None,
            H=tracked_expectations.information_gain_mean(),
            H_err=jnp.sqrt(tracked_expectations.information_gain_variance()),
            num_likelihood_evaluations=state.num_likelihood_evaluations,
            efficiency=(state.num_dead + state.log_L_live.shape[0]) / state.num_likelihood_evaluations,
            marginalised=marginalised,
            marginalised_uncert=marginalised_uncert,
            n_per_sample=n_per_sample,
            log_p=log_p,
            log_X=log_X,
            log_L_samples=log_L_samples,
            num_samples=num_samples,
            sampler_efficiency=sampler_efficiency
        )
        if collect_samples and not only_marginalise:
            ar = jnp.argsort(state.log_L_live)
            samples = dict_multimap(lambda dead_points, live_points:
                                    dynamic_update_slice(dead_points,
                                                         live_points.astype(dead_points.dtype)[ar, ...],
                                                         [state.num_dead] + [0] * (len(dead_points.shape) - 1)),
                                    state.dead_points, state.live_points)
            data['samples'] = samples
        return NestedSamplerResults(**data)
