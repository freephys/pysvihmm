from __future__ import division

import os
import itertools
import glob
import copy
import numpy as np

from experiment import ExperimentSequential as ExpSeq

from pybasicbayes.distributions import Gaussian
from pybasicbayes.util.stats import sample_invwishart
import hmmsgd_metaobs as HMM
from gen_synthetic import generate_data_smoothing

try:
    from numpy.random import multivariate_normal as mvnrand
except IOError:
    from util import mvnrand

# True parameter values
K = 4
D = 2
T = 101  # 5001
miss = 0.2

# Small just for debugging
maxit = 5

left = T // 2  # No missing data in first half of data

A_true = np.array([[0.35, 0.35, 0.15, 0.15],
                   [0.35, 0.35, 0.15, 0.15],
                   [0.15, 0.15, 0.35, 0.35],
                   [0.15, 0.15, 0.35, 0.35]])

mu_true = np.array([[5, 5], [5, -5], [-5, 5], [-5, -5]], dtype='float64')

sigma_true = [np.eye(2) for i in xrange(K)]
emit_true = [Gaussian(mu=mu_true[i,:], sigma=sigma_true[i],
                      mu_0=mu_true[i,:], sigma_0=sigma_true[i],
                      kappa_0=1, nu_0=4) for i in xrange(K)]

pi_prior = np.ones(K)
A_prior = np.ones((K,K))

taus = [1.]
kappas = [0.7, 0.8, 0.9]
Ls = [1,  5, 10, 25]


def run_exper(obs, par, mask):
    """ This actually runs the experiment.

        If you needed other arguments for this function you could either
        declare the values outside of the function and make a closure, or you
        could use functools.partial (or a lambda function) to curry the
        arguments so that the signature matches.
    """
    # Initialize object and run inference.  Have to have arguments in this
    # order.  Or could add mask as a key to par and then wouldn't need the mask
    # argument.
    hmm = HMM.VBHMM(obs, mask=mask, **par)
    hmm.infer()

    # Return hmm object as result, but remove reference to observations and
    # masks so we don't store lots of copies of the data.  Be careful, don't
    # call functions that depend on the data unless you reload it and reset
    # these values.  See the `set_data` method in hmmbase.
    hmm.obs = None
    hmm.mask = None

    return hmm


def main(name, datadir, datafn, expdir=None, nfolds=1, nrestarts=1, seed=None):
    """ Run experiment on 4 state, two group synthetic data.

        name :

        datadir : Path to directory containing data.

        datafn : Prefix name to files that data and missing masks are stored
                 in.

        expdir : Path to directory to store experiment results.  If None
                 (default), then a directory, `name`_results, is made in the
                 current directory.

        nfolds : Number of folds to generate if datafn is None.

        nrestarts : Number of random initial parameters.

        seed : Random number seed.
    """

    # Set seed for reproducibility
    np.random.seed(seed)

    # Generate/Load data and folds (missing masks)
    # These are the emission distributions for the following tests
    if not os.path.exists(datadir):
        try:
            os.makedirs(datadir)
        except IOError:
            raise RuntimeError("Could not create datadir: %s" % (datadir,))
    else:
        if not os.path.isdir(datadir):
            raise RuntimeError("datadir: %s exists but is not a directory" % (datadir,))

    nodata = True
    if datafn is None:
        datafn = name

    dpath = os.path.join(datadir, datafn + ".data")
    mpath = os.path.join(datadir, datafn + "_*.txt")
    try:
        X = np.loadtxt(dpath)
        nodata = False
    except IOError:
        if os.path.exists(dpath) and not os.path.isdir(dpath):
            raise RuntimeError("Could not load data: %s" % (dpath,))

    if nodata:
        print "Could not find data, generating it..."
        X, sts, masks = generate_data_smoothing(A_true, emit_true, T, miss,
                                                left, nfolds)

        dpath = os.path.join(datadir, datafn + "_data.txt")
        np.savetxt(dpath, X)

        spath = os.path.join(datadir, datafn + "_states.txt")
        np.savetxt(spath, sts)

        for i in xrange(nfolds):
            mpath = os.path.join(datadir, datafn + "_fold%d.txt" % (i,))
            np.savetxt(mpath, masks[i])
        print "Data saved to %s" % (dpath,)

    masks = glob.glob(mpath)
    if len(masks) == 0:
        masks = [None]

    # Initialize parameter possibilities

    obs_mean = np.mean(X, axis=0)
    mu_0 = obs_mean
    sigma_0 = 0.75*np.cov(X.T)
    kappa_0 = 0.01
    nu_0 = 4

    prior_init = np.ones(K)
    prior_tran = np.ones((K,K))

    rand_starts = list()
    for r in xrange(nrestarts):
        init_means = np.empty((K,D))
        init_cov = list()
        for k in xrange(K):
            init_means[k,:] = mvnrand(mu_0, cov=sigma_0)
            init_cov.append(sample_invwishart(np.linalg.inv(sigma_0), nu_0))
        # We use prior b/c mu and sigma are sampled here
        prior_emit = np.array([Gaussian(mu=init_means[k,:], sigma=sigma_0,
                                       mu_0=mu_0, sigma_0=sigma_0,
                                       kappa_0=kappa_0, nu_0=nu_0)
                              for k in xrange(K)])

        init_init = np.random.rand(K)
        init_init /= np.sum(init_init)

        init_tran = np.random.rand(K,K)
        init_tran /= np.sum(init_tran, axis=1)[:,np.newaxis]

        # Make dict with initial parameters to pass to experiment.
        pd = {'init_init': init_init, 'init_tran': init_tran,
              'prior_init': prior_init, 'prior_tran': prior_tran,
              'prior_emit': prior_emit, 'maxit': maxit}
        rand_starts.append(pd)

    # Compute Cartesian product of random starts with other possible parameter
    # values, make a generator to fill in entries in the par dicts created
    # above, and then construct the par_list by calling the generator with the
    # Cartesian product iterator.
    par_prod_iter = itertools.product(rand_starts, taus, kappas, Ls)

    def gen_par(par_tuple):
        d = copy.copy(par_tuple[0])
        d['tau'] = par_tuple[1]
        d['kappa'] = par_tuple[2]
        d['metaobs_half'] = par_tuple[3]
        return d

    # Call gen_par on each par product to pack into dictionary to pass to
    # experiment.
    par_list = itertools.imap(gen_par, par_prod_iter)

    # Create ExperimentSequential and call run_exper
    dname = os.path.join(datadir, datafn + "_data.txt")
    exp = ExpSeq(name, dname, run_exper, par_list,
                 masks=masks, exper_dir=expdir)
    exp.run()


if __name__ == "__main__":
    """ This test consists of two groups of states: 1,2 and 3,4, which switch
        between each other often but rarely switch between groups.
    """

    name = 'exper_hmmsgd_metaobs_synth_twogroups'
    datadir = '../data'
    datafn = 'synth_twogroups_T1000'
    expdir = None
    nfolds = 3
    nrestarts = 2
    seed = 8675309

    main(name, datadir, datafn, expdir, nfolds, nrestarts, seed)
