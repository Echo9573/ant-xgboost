# coding: utf-8
# pylint: disable=too-many-locals, too-many-arguments, invalid-name
# pylint: disable=too-many-branches, too-many-statements
"""Training Library containing training routines."""
from __future__ import absolute_import

import warnings
import numpy as np
from .core import Booster, STRING_TYPES, XGBoostError, CallbackEnv, EarlyStopException
from .compat import (SKLEARN_INSTALLED, XGBStratifiedKFold)
from .automl_core import ConvergenceTester, check_xgb_parameter, get_optimization_direction
from . import rabit
from . import callback

def _param_rewrite(params, n, p):
    """Internal function to rewrite some parameters"""
    col_sample_bytree = params.get('colsample_bytree', 1)
    # When feature dimension is too large, building trees will become
    # super expensive, limiting feature input will add randomness
    # (and therefore diversity) and improve overall training efficiency.
    if p * col_sample_bytree > 2000:
        params['colsample_bytree'] = 2000.0 / p
        if rabit.get_rank() == 0:
            msg = "[ParamRewrite] colsample_bytree = {}\n"
            rabit.tracker_print(msg.format(params['colsample_bytree']))

    # When # of points are too large, automatically switch to stochastic
    # gradient boosting.
    subsample = params.get('subsample', 1)
    if n * subsample > 1e8:
        params['subsample'] = 1e6 * 1.0 / n
        if rabit.get_rank() == 0:
            msg = "[ParamRewrite] subsample = {}\n"
            rabit.tracker_print(msg.format(params['subsample']))

    if 'convergence_criteria' not in params:
        params['convergence_criteria'] = '10:0:0.8'
        if rabit.get_rank() == 0:
            msg = "[ParamRewrite] convergence_criteria = {}\n"
            rabit.tracker_print(msg.format(params['convergence_criteria']))

def _auto_train_internal(params, dtrain,
                         num_boost_round=1000, evals=(),
                         obj=None, feval=None,
                         xgb_model=None, callbacks=None):
    """Internal function to automatically train xgboost"""
    maximize = params['maximize_eval_metric'].lower() == 'true'
    _param_rewrite(params, dtrain.num_row(), dtrain.num_col())
    depths = range(2, 11)
    learning_rates = [0.5, 0.2, 0.1, 0.05, 0.02, 0.01]
    if 'eta' in params:
        # If learning rate is specified, skip tuning.
        learning_rates = [float(params['eta'])]
    ct_d = ConvergenceTester(1, 2, 1.0)
    ct_d.reset(maximize)
    ct_l = ConvergenceTester(1, 2, 1.0)
    if maximize:
        best_so_far = float('-inf')
    else:
        best_so_far = float('inf')
    for d in depths:
        ct_l.reset(maximize)
        params['max_depth'] = str(d)
        for l in learning_rates:
            params['eta'] = str(l)
            model = train(params, dtrain,
                          num_boost_round=num_boost_round,
                          evals=evals)
            best_score = float(model.attr('best_score'))
            ct_l.add(best_score)
            rabit.tracker_print("depth = {}, learning rate = {}, best_score = {}\n\n".format(d, l, best_score))
            if (maximize and best_score > best_so_far) or \
                ((not maximize) and best_score < best_so_far):
                best_so_far = best_score
                best_model = model
                best_depth = d
                best_learning_rate = l

            if ct_l.is_converged():
                break
        ct_d.add(ct_l.get_best_so_far())
        if ct_d.is_converged():
            break
    best_model.set_attr(best_depth=str(best_depth),
                        best_learning_rate=str(best_learning_rate))
    return best_model

def auto_train(params, dtrain, num_boost_round=1000, evals=(), obj=None, feval=None,
               maximize=False, early_stopping_rounds=None, evals_result=None,
               verbose_eval=True, xgb_model=None, callbacks=None, learning_rates=None):
    # pylint: disable=too-many-statements,too-many-branches, attribute-defined-outside-init
    """Automatically train a booster.

    Parameters
    ----------
    params : dict
        Booster params.
    dtrain : DMatrix
        Data to be trained.
    num_boost_round: int
        Number of boosting iterations.
    evals: list of pairs (DMatrix, string)
        List of items to be evaluated during training, this allows user to watch
        performance on the validation set.
    obj : function
        Customized objective function.
    feval : function
        Customized evaluation function.
    maximize : bool
        Whether to maximize feval.
    early_stopping_rounds: int
        Activates early stopping. Validation error needs to decrease at least
        every **early_stopping_rounds** round(s) to continue training.
        Requires at least one item in **evals**.
        If there's more than one, will use the last.
        Returns the model from the last iteration (not the best one).
        If early stopping occurs, the model will have three additional fields:
        ``bst.best_score``, ``bst.best_iteration`` and ``bst.best_ntree_limit``.
        (Use ``bst.best_ntree_limit`` to get the correct value if
        ``num_parallel_tree`` and/or ``num_class`` appears in the parameters)
    evals_result: dict
        This dictionary stores the evaluation results of all the items in watchlist.

        Example: with a watchlist containing
        ``[(dtest,'eval'), (dtrain,'train')]`` and
        a parameter containing ``('eval_metric': 'logloss')``,
        the **evals_result** returns

        .. code-block:: python

            {'train': {'logloss': ['0.48253', '0.35953']},
             'eval': {'logloss': ['0.480385', '0.357756']}}

    verbose_eval : bool or int
        Requires at least one item in **evals**.
        If **verbose_eval** is True then the evaluation metric on the validation set is
        printed at each boosting stage.
        If **verbose_eval** is an integer then the evaluation metric on the validation set
        is printed at every given **verbose_eval** boosting stage. The last boosting stage
        / the boosting stage found by using **early_stopping_rounds** is also printed.
        Example: with ``verbose_eval=4`` and at least one item in **evals**, an evaluation metric
        is printed every 4 boosting stages, instead of every boosting stage.
    learning_rates: list or function (deprecated - use callback API instead)
        List of learning rate for each boosting round
        or a customized function that calculates eta in terms of
        current number of round and the total number of boosting round (e.g. yields
        learning rate decay)
    xgb_model : file name of stored xgb model or 'Booster' instance
        Xgb model to be loaded before training (allows training continuation).
    callbacks : list of callback functions
        List of callback functions that are applied at end of each iteration.
        It is possible to use predefined callbacks by using
        :ref:`Callback API <callback_api>`.
        Example:

        .. code-block:: python

            [xgb.callback.reset_learning_rate(custom_rates)]

    Returns
    -------
    Booster : a trained booster model
    """
    params = check_xgb_parameter(params, num_boost_round, skip_list=['max_depth', 'eta'])

    return _auto_train_internal(params, dtrain,
                                num_boost_round=num_boost_round,
                                evals=evals,
                                obj=obj, feval=feval,
                                xgb_model=xgb_model, callbacks=callbacks)

def _train_internal(params, dtrain,
                    num_boost_round=10, evals=(),
                    obj=None, feval=None,
                    xgb_model=None, callbacks=None):
    """internal training function"""
    callbacks = [] if callbacks is None else callbacks
    evals = list(evals)
    if isinstance(params, dict) \
            and 'eval_metric' in params \
            and isinstance(params['eval_metric'], list):
        params = dict((k, v) for k, v in params.items())
        eval_metrics = params['eval_metric']
        params.pop("eval_metric", None)
        params = list(params.items())
        for eval_metric in eval_metrics:
            params += [('eval_metric', eval_metric)]

    bst = Booster(params, [dtrain] + [d[0] for d in evals])
    nboost = 0
    num_parallel_tree = 1

    if xgb_model is not None:
        if not isinstance(xgb_model, STRING_TYPES):
            xgb_model = xgb_model.save_raw()
        bst = Booster(params, [dtrain] + [d[0] for d in evals], model_file=xgb_model)
        nboost = len(bst.get_dump())

    _params = dict(params) if isinstance(params, list) else params

    if 'num_parallel_tree' in _params:
        num_parallel_tree = _params['num_parallel_tree']
        nboost //= num_parallel_tree
    if 'num_class' in _params:
        nboost //= _params['num_class']

    # Distributed code: Load the checkpoint from rabit.
    version = bst.load_rabit_checkpoint()
    assert rabit.get_world_size() != 1 or version == 0
    rank = rabit.get_rank()
    start_iteration = int(version / 2)
    nboost += start_iteration

    callbacks_before_iter = [
        cb for cb in callbacks if cb.__dict__.get('before_iteration', False)]
    callbacks_after_iter = [
        cb for cb in callbacks if not cb.__dict__.get('before_iteration', False)]
    callbacks_early_stop = [
        cb for cb in callbacks if cb.__dict__.get('early_stop', False)]

    # If early stopping enabled, create booster to store the best round model.
    if len(callbacks_early_stop) > 0:
        best_bst = bst.copy()
        best_iteration = bst.attr('best_iteration')

    for i in range(start_iteration, num_boost_round):
        for cb in callbacks_before_iter:
            cb(CallbackEnv(model=bst,
                           cvfolds=None,
                           iteration=i,
                           begin_iteration=start_iteration,
                           end_iteration=num_boost_round,
                           rank=rank,
                           evaluation_result_list=None))
        # Distributed code: need to resume to this point.
        # Skip the first update if it is a recovery step.
        if version % 2 == 0:
            bst.update(dtrain, i, obj)
            bst.save_rabit_checkpoint()
            version += 1

        assert rabit.get_world_size() == 1 or version == rabit.version_number()

        nboost += 1
        evaluation_result_list = []
        # check evaluation result.
        if evals:
            bst_eval_set = bst.eval_set(evals, i, feval)
            if isinstance(bst_eval_set, STRING_TYPES):
                msg = bst_eval_set
            else:
                msg = bst_eval_set.decode()
            res = [x.split(':') for x in msg.split()]
            evaluation_result_list = [(k, float(v)) for k, v in res[1:]]
        try:
            for cb in callbacks_after_iter:
                cb(CallbackEnv(model=bst,
                               cvfolds=None,
                               iteration=i,
                               begin_iteration=start_iteration,
                               end_iteration=num_boost_round,
                               rank=rank,
                               evaluation_result_list=evaluation_result_list))
        except EarlyStopException:
            break
        finally:
            if len(callbacks_early_stop) > 0 and best_iteration != bst.attr('best_iteration'):
                best_bst = bst.copy()
                best_iteration = bst.attr('best_iteration')
        # do checkpoint after evaluation, in case evaluation also updates booster.
        bst.save_rabit_checkpoint()
        version += 1

    if bst.attr('best_score') is not None:
        bst.best_score = float(bst.attr('best_score'))
        bst.best_iteration = int(bst.attr('best_iteration'))
    else:
        bst.best_iteration = nboost - 1
    bst.best_ntree_limit = (bst.best_iteration + 1) * num_parallel_tree
    if len(callbacks_early_stop) > 0:
        return best_bst
    return bst


def train(params, dtrain, num_boost_round=10, evals=(), obj=None, feval=None,
          maximize=False, early_stopping_rounds=None, evals_result=None,
          verbose_eval=True, xgb_model=None, callbacks=None, learning_rates=None):
    # pylint: disable=too-many-statements,too-many-branches, attribute-defined-outside-init
    """Train a booster with given parameters.

    Parameters
    ----------
    params : dict
        Booster params.
    dtrain : DMatrix
        Data to be trained.
    num_boost_round: int
        Number of boosting iterations.
    evals: list of pairs (DMatrix, string)
        List of validation sets for which metrics will evaluated during training.
        Validation metrics will help us track the performance of the model.
    obj : function
        Customized objective function.
    feval : function
        Customized evaluation function.
    maximize : bool
        Whether to maximize feval. If check_params is turned on, this parameter will
        be automatically determined by objective and eval_metric and might be overwritten
        accordingly.
    early_stopping_rounds: int
        Activates early stopping. Validation metric needs to improve at least once in
        every **early_stopping_rounds** round(s) to continue training.
        Requires at least one item in **evals**.
        The method returns the model from the last iteration (not the best one).
        If there's more than one item in **evals**, the last entry will be used
        for early stopping.
        If there's more than one metric in the **eval_metric** parameter given in
        **params**, the last metric will be used for early stopping.
        If early stopping occurs, the model will have three additional fields:
        ``bst.best_score``, ``bst.best_iteration`` and ``bst.best_ntree_limit``.
        (Use ``bst.best_ntree_limit`` to get the correct value if
        ``num_parallel_tree`` and/or ``num_class`` appears in the parameters)
    evals_result: dict
        This dictionary stores the evaluation results of all the items in watchlist.

        Example: with a watchlist containing
        ``[(dtest,'eval'), (dtrain,'train')]`` and
        a parameter containing ``('eval_metric': 'logloss')``,
        the **evals_result** returns

        .. code-block:: python

            {'train': {'logloss': ['0.48253', '0.35953']},
             'eval': {'logloss': ['0.480385', '0.357756']}}

    verbose_eval : bool or int
        Requires at least one item in **evals**.
        If **verbose_eval** is True then the evaluation metric on the validation set is
        printed at each boosting stage.
        If **verbose_eval** is an integer then the evaluation metric on the validation set
        is printed at every given **verbose_eval** boosting stage. The last boosting stage
        / the boosting stage found by using **early_stopping_rounds** is also printed.
        Example: with ``verbose_eval=4`` and at least one item in **evals**, an evaluation metric
        is printed every 4 boosting stages, instead of every boosting stage.
    learning_rates: list or function (deprecated - use callback API instead)
        List of learning rate for each boosting round
        or a customized function that calculates eta in terms of
        current number of round and the total number of boosting round (e.g. yields
        learning rate decay)
    xgb_model : file name of stored xgb model or 'Booster' instance
        Xgb model to be loaded before training (allows training continuation).
    callbacks : list of callback functions
        List of callback functions that are applied at end of each iteration.
        It is possible to use predefined callbacks by using
        :ref:`Callback API <callback_api>`.
        Example:

        .. code-block:: python

            [xgb.callback.reset_learning_rate(custom_rates)]

    Returns
    -------
    Booster : a trained booster model
    """
    callbacks = [] if callbacks is None else callbacks

    maximize_metric = get_optimization_direction(params)
    op_direction = maximize if maximize_metric is None else maximize_metric

    # Most of legacy advanced options becomes callbacks
    if isinstance(verbose_eval, bool) and verbose_eval:
        callbacks.append(callback.print_evaluation())
    else:
        if isinstance(verbose_eval, int):
            callbacks.append(callback.print_evaluation(verbose_eval))

    if early_stopping_rounds is not None:
        callbacks.append(callback.early_stop(early_stopping_rounds,
                                             maximize=op_direction,
                                             verbose=bool(verbose_eval)))

    if isinstance(params, dict) and params.get('convergence_criteria') is not None:
        callbacks.append(callback.convergence_test(params.get('convergence_criteria'),
                                                   maximize=op_direction,
                                                   verbose=bool(verbose_eval)))

    if evals_result is not None:
        callbacks.append(callback.record_evaluation(evals_result))

    if learning_rates is not None:
        warnings.warn("learning_rates parameter is deprecated - use callback API instead",
                      DeprecationWarning)
        callbacks.append(callback.reset_learning_rate(learning_rates))

    return _train_internal(params, dtrain,
                           num_boost_round=num_boost_round,
                           evals=evals,
                           obj=obj, feval=feval,
                           xgb_model=xgb_model, callbacks=callbacks)


class CVPack(object):
    """"Auxiliary datastruct to hold one fold of CV."""
    def __init__(self, dtrain, dtest, param):
        """"Initialize the CVPack"""
        self.dtrain = dtrain
        self.dtest = dtest
        self.watchlist = [(dtrain, 'train'), (dtest, 'test')]
        self.bst = Booster(param, [dtrain, dtest])

    def update(self, iteration, fobj):
        """"Update the boosters for one iteration"""
        self.bst.update(self.dtrain, iteration, fobj)

    def eval(self, iteration, feval):
        """"Evaluate the CVPack for one iteration."""
        return self.bst.eval_set(self.watchlist, iteration, feval)


def groups_to_rows(groups, boundaries):
    """
    Given group row boundaries, convert ground indexes to row indexes
    :param groups: list of groups for testing
    :param boundaries: rows index limits of each group
    :return: row in group
    """
    return np.concatenate([np.arange(boundaries[g], boundaries[g+1]) for g in groups])


def mkgroupfold(dall, nfold, param, evals=(), fpreproc=None, shuffle=True):
    """
    Make n folds for cross-validation maintaining groups
    :return: cross-validation folds
    """
    # we have groups for pairwise ranking... get a list of the group indexes
    group_boundaries = dall.get_uint_info('group_ptr')
    group_sizes = np.diff(group_boundaries)

    if shuffle is True:
        idx = np.random.permutation(len(group_sizes))
    else:
        idx = np.arange(len(group_sizes))
    # list by fold of test group indexes
    out_group_idset = np.array_split(idx, nfold)
    # list by fold of train group indexes
    in_group_idset = [np.concatenate([out_group_idset[i] for i in range(nfold) if k != i])
                      for k in range(nfold)]
    # from the group indexes, convert them to row indexes
    in_idset = [groups_to_rows(in_groups, group_boundaries) for in_groups in in_group_idset]
    out_idset = [groups_to_rows(out_groups, group_boundaries) for out_groups in out_group_idset]

    # build the folds by taking the appropriate slices
    ret = []
    for k in range(nfold):
        # perform the slicing using the indexes determined by the above methods
        dtrain = dall.slice(in_idset[k], allow_groups=True)
        dtrain.set_group(group_sizes[in_group_idset[k]])
        dtest = dall.slice(out_idset[k], allow_groups=True)
        dtest.set_group(group_sizes[out_group_idset[k]])
        # run preprocessing on the data set if needed
        if fpreproc is not None:
            dtrain, dtest, tparam = fpreproc(dtrain, dtest, param.copy())
        else:
            tparam = param
        plst = list(tparam.items()) + [('eval_metric', itm) for itm in evals]
        ret.append(CVPack(dtrain, dtest, plst))
    return ret


def mknfold(dall, nfold, param, seed, evals=(), fpreproc=None, stratified=False,
            folds=None, shuffle=True):
    """
    Make an n-fold list of CVPack from random indices.
    """
    evals = list(evals)
    np.random.seed(seed)

    if stratified is False and folds is None:
        # Do standard k-fold cross validation. Automatically determine the folds.
        if len(dall.get_uint_info('group_ptr')) > 1:
            return mkgroupfold(dall, nfold, param, evals=evals, fpreproc=fpreproc, shuffle=shuffle)

        if shuffle is True:
            idx = np.random.permutation(dall.num_row())
        else:
            idx = np.arange(dall.num_row())
        out_idset = np.array_split(idx, nfold)
        in_idset = [np.concatenate([out_idset[i] for i in range(nfold) if k != i])
                    for k in range(nfold)]
    elif folds is not None:
        # Use user specified custom split using indices
        try:
            in_idset = [x[0] for x in folds]
            out_idset = [x[1] for x in folds]
        except TypeError:
            # Custom stratification using Sklearn KFoldSplit object
            splits = list(folds.split(X=dall.get_label(), y=dall.get_label()))
            in_idset = [x[0] for x in splits]
            out_idset = [x[1] for x in splits]
        nfold = len(out_idset)
    else:
        # Do standard stratefied shuffle k-fold split
        sfk = XGBStratifiedKFold(n_splits=nfold, shuffle=True, random_state=seed)
        splits = list(sfk.split(X=dall.get_label(), y=dall.get_label()))
        in_idset = [x[0] for x in splits]
        out_idset = [x[1] for x in splits]
        nfold = len(out_idset)

    ret = []
    for k in range(nfold):
        # perform the slicing using the indexes determined by the above methods
        dtrain = dall.slice(in_idset[k])
        dtest = dall.slice(out_idset[k])
        # run preprocessing on the data set if needed
        if fpreproc is not None:
            dtrain, dtest, tparam = fpreproc(dtrain, dtest, param.copy())
        else:
            tparam = param
        plst = list(tparam.items()) + [('eval_metric', itm) for itm in evals]
        ret.append(CVPack(dtrain, dtest, plst))
    return ret


def aggcv(rlist):
    # pylint: disable=invalid-name
    """
    Aggregate cross-validation results.

    If verbose_eval is true, progress is displayed in every call. If
    verbose_eval is an integer, progress will only be displayed every
    `verbose_eval` trees, tracked via trial.
    """
    cvmap = {}
    idx = rlist[0].split()[0]
    for line in rlist:
        arr = line.split()
        assert idx == arr[0]
        for metric_idx, it in enumerate(arr[1:]):
            if not isinstance(it, STRING_TYPES):
                it = it.decode()
            k, v = it.split(':')
            if (metric_idx, k) not in cvmap:
                cvmap[(metric_idx, k)] = []
            cvmap[(metric_idx, k)].append(float(v))
    msg = idx
    results = []
    for (metric_idx, k), v in sorted(cvmap.items(), key=lambda x: x[0][0]):
        v = np.array(v)
        if not isinstance(msg, STRING_TYPES):
            msg = msg.decode()
        mean, std = np.mean(v), np.std(v)
        results.extend([(k, mean, std)])
    return results


def cv(params, dtrain, num_boost_round=10, nfold=3, stratified=False, folds=None,
       metrics=(), obj=None, feval=None, maximize=False, early_stopping_rounds=None,
       fpreproc=None, as_pandas=True, verbose_eval=None, show_stdv=True,
       seed=0, callbacks=None, shuffle=True):
    # pylint: disable = invalid-name
    """Cross-validation with given parameters.

    Parameters
    ----------
    params : dict
        Booster params.
    dtrain : DMatrix
        Data to be trained.
    num_boost_round : int
        Number of boosting iterations.
    nfold : int
        Number of folds in CV.
    stratified : bool
        Perform stratified sampling.
    folds : a KFold or StratifiedKFold instance or list of fold indices
        Sklearn KFolds or StratifiedKFolds object.
        Alternatively may explicitly pass sample indices for each fold.
        For ``n`` folds, **folds** should be a length ``n`` list of tuples.
        Each tuple is ``(in,out)`` where ``in`` is a list of indices to be used
        as the training samples for the ``n`` th fold and ``out`` is a list of
        indices to be used as the testing samples for the ``n`` th fold.
    metrics : string or list of strings
        Evaluation metrics to be watched in CV.
    obj : function
        Custom objective function.
    feval : function
        Custom evaluation function.
    maximize : bool
        Whether to maximize feval.
    early_stopping_rounds: int
        Activates early stopping. Cross-Validation metric (average of validation
        metric computed over CV folds) needs to improve at least once in
        every **early_stopping_rounds** round(s) to continue training.
        The last entry in the evaluation history will represent the best iteration.
        If there's more than one metric in the **eval_metric** parameter given in
        **params**, the last metric will be used for early stopping.
    fpreproc : function
        Preprocessing function that takes (dtrain, dtest, param) and returns
        transformed versions of those.
    as_pandas : bool, default True
        Return pd.DataFrame when pandas is installed.
        If False or pandas is not installed, return np.ndarray
    verbose_eval : bool, int, or None, default None
        Whether to display the progress. If None, progress will be displayed
        when np.ndarray is returned. If True, progress will be displayed at
        boosting stage. If an integer is given, progress will be displayed
        at every given `verbose_eval` boosting stage.
    show_stdv : bool, default True
        Whether to display the standard deviation in progress.
        Results are not affected, and always contains std.
    seed : int
        Seed used to generate the folds (passed to numpy.random.seed).
    callbacks : list of callback functions
        List of callback functions that are applied at end of each iteration.
        It is possible to use predefined callbacks by using
        :ref:`Callback API <callback_api>`.
        Example:

        .. code-block:: python

            [xgb.callback.reset_learning_rate(custom_rates)]
    shuffle : bool
        Shuffle data before creating folds.

    Returns
    -------
    evaluation history : list(string)
    """
    if stratified is True and not SKLEARN_INSTALLED:
        raise XGBoostError('sklearn needs to be installed in order to use stratified cv')

    if isinstance(metrics, str):
        metrics = [metrics]

    if isinstance(params, list):
        _metrics = [x[1] for x in params if x[0] == 'eval_metric']
        params = dict(params)
        if 'eval_metric' in params:
            params['eval_metric'] = _metrics
    else:
        params = dict((k, v) for k, v in params.items())

    if (not metrics) and 'eval_metric' in params:
        if isinstance(params['eval_metric'], list):
            metrics = params['eval_metric']
        else:
            metrics = [params['eval_metric']]

    params.pop("eval_metric", None)

    results = {}
    cvfolds = mknfold(dtrain, nfold, params, seed, metrics, fpreproc,
                      stratified, folds, shuffle)

    # setup callbacks
    callbacks = [] if callbacks is None else callbacks
    if early_stopping_rounds is not None:
        callbacks.append(callback.early_stop(early_stopping_rounds,
                                             maximize=maximize,
                                             verbose=False))

    if isinstance(verbose_eval, bool) and verbose_eval:
        callbacks.append(callback.print_evaluation(show_stdv=show_stdv))
    else:
        if isinstance(verbose_eval, int):
            callbacks.append(callback.print_evaluation(verbose_eval, show_stdv=show_stdv))

    callbacks_before_iter = [
        cb for cb in callbacks if cb.__dict__.get('before_iteration', False)]
    callbacks_after_iter = [
        cb for cb in callbacks if not cb.__dict__.get('before_iteration', False)]

    for i in range(num_boost_round):
        for cb in callbacks_before_iter:
            cb(CallbackEnv(model=None,
                           cvfolds=cvfolds,
                           iteration=i,
                           begin_iteration=0,
                           end_iteration=num_boost_round,
                           rank=0,
                           evaluation_result_list=None))
        for fold in cvfolds:
            fold.update(i, obj)
        res = aggcv([f.eval(i, feval) for f in cvfolds])

        for key, mean, std in res:
            if key + '-mean' not in results:
                results[key + '-mean'] = []
            if key + '-std' not in results:
                results[key + '-std'] = []
            results[key + '-mean'].append(mean)
            results[key + '-std'].append(std)
        try:
            for cb in callbacks_after_iter:
                cb(CallbackEnv(model=None,
                               cvfolds=cvfolds,
                               iteration=i,
                               begin_iteration=0,
                               end_iteration=num_boost_round,
                               rank=0,
                               evaluation_result_list=res))
        except EarlyStopException as e:
            for k in results:
                results[k] = results[k][:(e.best_iteration + 1)]
            break
    if as_pandas:
        try:
            import pandas as pd
            results = pd.DataFrame.from_dict(results)
        except ImportError:
            pass
    return results
