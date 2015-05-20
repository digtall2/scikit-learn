"""Gaussian processes regression. """

# Authors: Jan Hendrik Metzen <jhm@informatik.uni-bremen.de>
#
# License: BSD 3 clause

from operator import itemgetter

import numpy as np
from scipy.linalg import cholesky, cho_solve, solve, solve_triangular
from scipy.optimize import fmin_l_bfgs_b

from sklearn.base import BaseEstimator, RegressorMixin, clone
from sklearn.gaussian_process.kernels import RBF
from sklearn.utils import check_random_state
from sklearn.utils.validation import check_X_y, check_array


class GaussianProcessRegressor(BaseEstimator, RegressorMixin):
    """Gaussian process regression (GPR).

    The implementation is based on Algorithm 2.1 of ``Gaussian Processes
    for Machine Learning'' (GPML) by Rasmussen and Williams.

    In addition to standard sklearn estimator API, GaussianProcessRegressor:

       * allows prediction without prior fitting (based on the GP prior)
       * provides an additional method sample_y(X), which evaluates samples
         drawn from the GPR (prior or posterior) at given inputs
       * exposes a method log_marginal_likelihood(theta), which can be used
         externally for other ways of selecting hyperparameters, e.g., via
         Markov chain Monte Carlo.

    Parameters
    ----------
    kernel : kernel object
        The kernel specifying the covariance function of the GP. If None is
        passed, the kernel "1.0 * RBF(1.0)" is used as default. Note that
        the kernel's hyperparameters are optimized during fitting.

    sigma_squared_n : float or array-like, optional (default: 1e-10)
        Value added to the diagonal of the kernel matrix during fitting.
        Larger values correspond to increased noise level in the observations
        and reduce potential numerical issue during fitting. If an array is
        passed, it must have the same number of entries as the data used for
        fitting and is used as datapoint-dependent noise level.

    optimizer : string, optional (default: "fmin_l_bfgs_b")
        A string specifying the optimization algorithm used for optimizing the
        kernel's parameters. Default uses 'fmin_l_bfgs_b' algorithm from
        scipy.optimize. If None, the kernel's parameters are kept fixed.
        Available optimizers are::

            'fmin_l_bfgs_b'

    n_restarts_optimizer: int, optional (default: 1)
        The number of restarts of the optimizer for finding the kernel's
        parameters which maximize the log-marginal likelihood. The first run
        of the optimizer is performed from the kernel's initial parameters,
        the remaining ones (if any) from thetas sampled log-uniform randomly
        from the space of allowed theta-values. If greater than 1, all bounds
        must be finite.

    normalize_y: boolean, optional (default: False)
        Whether the target values y are normalized, i.e., mean and standard
        deviation of observed target values become zero and one, respectively.
        This parameter should be set to True if the target values' mean is
        expected to differ considerable from zero or if the standard deviation
        of the target values is very small or large. When enabled, the
        normalization effectively modifies the GP's prior based on the data,
        which contradicts the likelihood principle; normalization is thus
        disabled per default.

    random_state : integer or numpy.RandomState, optional
        The generator used to initialize the centers. If an integer is
        given, it fixes the seed. Defaults to the global numpy random
        number generator.

    Attributes
    ----------
    X_fit_ : array-like, shape = (n_samples, n_features)
        Feature values in training data (also required for prediction)

    y_fit_: array-like, shape = (n_samples, [n_output_dims])
        Target values in training data (also required for prediction)

    kernel_: kernel object
        The kernel used for prediction. The structure of the kernel is the
        same as the one passed as parameter but with optimized hyperparameters

    theta_: array-like, shape =(n_kernel_params,)
        Selected kernel hyperparameters

    L_: array-like, shape = (n_samples, n_samples)
        Lower-triangular Cholesky decomposition of the kernel in X_fit_

    alpha_: array-like, shape = (n_samples,)
        Dual coefficients of training data points in kernel space
    """
    def __init__(self, kernel=None, sigma_squared_n=1e-10,
                 optimizer="fmin_l_bfgs_b", n_restarts_optimizer=1,
                 normalize_y=False, random_state=None):
        self.kernel = kernel
        self.sigma_squared_n = sigma_squared_n
        self.optimizer = optimizer
        self.n_restarts_optimizer = n_restarts_optimizer
        self.normalize_y = normalize_y
        self.rng = check_random_state(random_state)

    def fit(self, X, y):
        """Fit Gaussian process regression model

        Parameters
        ----------
        X : array-like, shape = (n_samples, n_features)
            Training data

        y : array-like, shape = (n_samples, [n_output_dims])
            Target values

        Returns
        -------
        self : returns an instance of self.
        """
        if self.kernel is None:  # Use an RBF kernel as default
            self.kernel_ = 1.0 * RBF(1.0)
        else:
            self.kernel_ = clone(self.kernel)

        X, y = check_X_y(X, y, multi_output=True)

        # Normalize target value
        if self.normalize_y:
            self.y_fit_mean = np.mean(y, axis=0)
            self.y_fit_std = np.atleast_1d(np.std(y))  # XXX: std per dim?
            self.y_fit_std[self.y_fit_std == 0.] = 1.
            # center and scale y (and sigma_squared_n)
            y = (y - self.y_fit_mean) / self.y_fit_std
            self.sigma_squared_n /= self.y_fit_std**2
        else:
            self.y_fit_mean = np.zeros(1)
            self.y_fit_std = np.ones(1)

        if np.iterable(self.sigma_squared_n) \
           and self.sigma_squared_n.shape[0] != y.shape[0]:
            if self.sigma_squared_n.shape[0] == 1:
                self.sigma_squared_n = self.sigma_squared_n[0]
            else:
                raise ValueError("sigma_n_squared must be a scalar or an array"
                                 " with same number of entries as y.(%d != %d)"
                                 % (self.sigma_squared_n.shape[0], y.shape[0]))

        self.X_fit_ = X
        self.y_fit_ = y

        if self.optimizer in ["fmin_l_bfgs_b"]:
            # Choose hyperparameters based on maximizing the log-marginal
            # likelihood (potentially starting from several initial values)
            def obj_func(theta):
                lml, grad = self.log_marginal_likelihood(theta,
                                                         eval_gradient=True)
                return -lml, -grad

            # First optimize starting from theta specified in kernel
            optima = [(self._constrained_optimization(obj_func,
                                                      self.kernel_.theta,
                                                      self.kernel_.bounds))]

            # Additional runs are performed from log-uniform chosen initial
            # theta
            if self.n_restarts_optimizer > 1:
                if not np.isfinite(self.kernel_.bounds).all():
                    raise ValueError(
                        "Multiple optimizer restarts (n_restarts_optimizer>1) "
                        "requires that all bounds are finite.")
                log_bounds = np.log(self.kernel_.bounds)
                for iteration in range(1, self.n_restarts_optimizer):
                    theta_initial = np.exp(self.rng.uniform(log_bounds[:, 0],
                                                            log_bounds[:, 1]))
                    optima.append(
                        self._constrained_optimization(obj_func, theta_initial,
                                                       self.kernel_.bounds))
            # Select result from run with minimal (negative) log-marginal
            # likelihood
            self.theta_ = optima[np.argmin(map(itemgetter(1), optima))][0]
            self.kernel_.theta = self.theta_
        elif self.optimizer is None:
            # Use initially provided hyperparameters
            self.theta_ = self.kernel_.theta
        else:
            raise ValueError("Unknown optimizer %s." % self.optimizer)

        # Precompute quantities required for predictions which are independent
        # of actual query points
        K = self.kernel_(self.X_fit_)
        K[np.diag_indices_from(K)] += self.sigma_squared_n
        self.L_ = cholesky(K, lower=True)  # Line 2
        self.alpha_ = cho_solve((self.L_, True), self.y_fit_)  # Line 3

        return self

    def predict(self, X, return_std=False, return_cov=False):
        """Predict using the Gaussian process regression model

        We can also predict based on an unfitted model by using the GP prior.
        In addition to the mean of the predictive distribution, also its
        standard deviation (return_std=True) or covariance (return_cov=True).
        Note that at most one of the two can be requested.

        Parameters
        ----------
        X : array-like, shape = (n_samples, n_features)
            Query points where the GP is evaluated

        return_std : bool, default: False
            If True, the standard-deviation of the predictive distribution at
            the query points is returned along with the mean.

        return_cov : bool, default: False
            If True, the covariance of the joint predictive distribution at
            the query points is returned along with the mean

        Returns
        -------
        y_mean : array, shape = (n_samples, [n_output_dims])
            Mean of predictive distribution a query points

        y_std : array, shape = (n_samples,), optional
            Standard deviation of predictive distribution at query points.
            Only returned when return_std is True.

        y_cov : array, shape = (n_samples, n_samples), optional
            Covariance of joint predictive distribution a query points.
            Only returned when return_cov is True.
        """
        if return_std and return_cov:
            raise RuntimeError(
                "Not returning standard deviation of predictions when "
                "returning full covariance.")

        X = check_array(X)

        if not hasattr(self, "X_fit_"):  # Unfitted; predict based on GP prior
            y_mean = np.zeros(X.shape[0])
            if return_cov:
                y_cov = self.kernel(X)
                return y_mean, y_cov
            elif return_std:
                y_var = self.kernel.diag(X)
                return y_mean, np.sqrt(y_var)
            else:
                return y_mean
        else:  # Predict based on GP posterior
            K_trans = self.kernel_(X, self.X_fit_)
            y_mean = K_trans.dot(self.alpha_)  # Line 4 (y_mean = f_star)
            y_mean = self.y_fit_mean + self.y_fit_std * y_mean  # undo normal.
            if return_cov:
                v = cho_solve((self.L_, True), K_trans.T)  # Line 5
                y_cov = self.kernel_(X) - K_trans.dot(v)  # Line 6
                y_cov *= self.y_fit_std ** 2  # undo normalization
                return y_mean, y_cov
            elif return_std:
                # compute inverse K_inv of K based on its Cholesky
                # decomposition L and its inverse L_inv
                L_inv = solve_triangular(self.L_.T, np.eye(self.L_.shape[0]))
                K_inv = L_inv.dot(L_inv.T)
                # Compute variance of predictive distribution
                y_var = self.kernel_.diag(X)
                y_var -= np.sum(K_trans.T[:, np.newaxis] * K_trans.T
                                * K_inv[:, :, np.newaxis],
                                axis=0).sum(axis=0)  # axis=(0, 1) in np >= 1.7
                y_std = np.sqrt(y_var) * self.y_fit_std  # undo normalization
                return y_mean, y_std
            else:
                return y_mean

    def sample_y(self, X, n_samples=1, random_state=0):
        """Draw samples from Gaussian process and evaluate at X.

        Parameters
        ----------
        X : array-like, shape = (n_samples_X, n_features)
            Query points where the GP samples are evaluated

        n_samples : int, default: 1
            The number of samples drawn from the Gaussian process

        random_state: RandomState or an int seed (0 by default)
            A random number generator instance

        Returns
        -------
        y_samples : array, shape = (n_samples_X, [n_output_dims], n_samples)
            Values of n_samples samples drawn from Gaussian process and
            evaluated at query points.
        """
        rng = check_random_state(random_state)

        y_mean, y_cov = self.predict(X, return_cov=True)
        if y_mean.ndim == 1:
            y_samples = rng.multivariate_normal(y_mean, y_cov, n_samples).T
        else:
            y_samples = \
                [rng.multivariate_normal(y_mean[:, i], y_cov,
                                         n_samples).T[:, np.newaxis]
                 for i in range(y_mean.shape[1])]
            y_samples = np.hstack(y_samples)
        return y_samples

    def log_marginal_likelihood(self, theta, eval_gradient=False):
        """Returns log-marginal likelihood of theta for training data.

        Parameters
        ----------
        theta : array-like, shape = (n_kernel_params,)
            Kernel hyperparameters for which the log-marginal likelihood is
            evaluated

        eval_gradient : bool, default: False
            If True, the gradient of the log-marginal likelihood with respect
            to the kernel hyperparameters at position theta is returned
            additionally.

        Returns
        -------
        log_likelihood : float
            Log-marginal likelihood of theta for training data.

        log_likelihood_gradient : array, shape = (n_kernel_params,), optional
            Gradient of the log-marginal likelihood with respect to the kernel
            hyperparameters at position theta.
            Only returned when eval_gradient is True.
        """
        kernel = self.kernel_.clone_with_theta(theta)

        if eval_gradient:
            K, K_gradient = kernel(self.X_fit_, eval_gradient=True)
        else:
            K = kernel(self.X_fit_)

        K[np.diag_indices_from(K)] += self.sigma_squared_n
        try:
            L = cholesky(K, lower=True)  # Line 2
        except np.linalg.LinAlgError:
            return (-np.inf, np.zeros_like(theta)) \
                if eval_gradient else -np.inf

        log_likelihood = 0
        if eval_gradient:
            log_likelihood_gradient = 0

        # Iterate over output dimensions of self.y_fit_
        y_fit = self.y_fit_
        if y_fit.ndim == 1:
            y_fit = y_fit[:, np.newaxis]
        for i in range(y_fit.shape[1]):
            alpha = cho_solve((L, True), y_fit[:, i])  # Line 3

            # Compute log-likelihood of output dimension (compare line 7)
            log_likelihood_dim = -0.5 * y_fit[:, i].dot(alpha)
            log_likelihood_dim -= np.log(np.diag(L)).sum()
            log_likelihood_dim -= K.shape[0] / 2 * np.log(2 * np.pi)

            log_likelihood += log_likelihood_dim

            if eval_gradient:  # compare Equation 5.9 from GPML
                tmp = np.outer(alpha, alpha)
                tmp -= cho_solve((L, True), np.eye(K.shape[0]))
                # Compute "0.5 * trace(tmp.dot(K_gradient))" without
                # constructing the full matrix tmp.dot(K_gradient) since only
                # its diagonal is required
                log_likelihood_gradient_dim = \
                    0.5 * np.einsum("ij,ijk->k", tmp, K_gradient)
                log_likelihood_gradient += log_likelihood_gradient_dim

        if eval_gradient:
            return log_likelihood, log_likelihood_gradient
        else:
            return log_likelihood

    def _constrained_optimization(self, obj_func, initial_theta, bounds):
        if self.optimizer in ["fmin_l_bfgs_b"]:
            theta_opt, func_min, _ = \
                fmin_l_bfgs_b(obj_func, initial_theta, bounds=bounds)
        return theta_opt, func_min
