from copy import deepcopy
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd
from ray import tune

from cassiopeia.data import CassiopeiaTree
from cassiopeia.mixins.errors import BranchLengthEstimatorError
from cassiopeia.tools import (
    BranchLengthEstimator,
    IIDExponentialBayesian,
    IIDExponentialMLE,
)

from .CharacterLevelCV import CharacterLevelCV, CharacterLevelCVError


def _create_space_iid_exponential_bayesian(tree: CassiopeiaTree) -> Dict:
    mle = IIDExponentialMLE()
    mle.estimate_branch_lengths(deepcopy(tree))
    space = {}
    space["mutation_rate"] = tune.loguniform(
        mle.mutation_rate / 2.0, mle.mutation_rate * 2.0
    )
    space["e_pop_size"] = tune.loguniform(
        tree.n_cell / 10.0, tree.n_cell * 10.0
    )
    space["sampling_probability"] = tune.loguniform(0.0000001, 1.0)
    return space


def _transform_hyperparameters_iid_exponential_bayesian(
    hyperparameters: Dict,
) -> Dict:
    return {
        "mutation_rate": hyperparameters["mutation_rate"],
        "birth_rate": np.log(hyperparameters["e_pop_size"])
        + np.log(1.0 / hyperparameters["sampling_probability"]),
        "sampling_probability": hyperparameters["sampling_probability"],
        "discretization_level": 600,
    }


class IIDExponentialBayesianEmpiricalBayes(
    BranchLengthEstimator, CharacterLevelCV
):
    """
    Empirical Bayes for IIDExponentialBayesian.

    We use n_folds=0 since we are using a training metric.
    """

    def __init__(
        self,
        n_hyperparams: int = 60,
        n_parallel_hyperparams: int = 6,
        random_seed: int = 0,
        verbose: bool = False,
    ) -> None:
        super().__init__(
            n_hyperparams=n_hyperparams,
            n_parallel_hyperparams=n_parallel_hyperparams,
            n_folds=0,
            n_parallel_folds=1,
            random_seed=random_seed,
            verbose=verbose,
        )

    @staticmethod
    def _create_space(tree: CassiopeiaTree):
        return _create_space_iid_exponential_bayesian(tree)

    @staticmethod
    def _cv_metric(
        args: Tuple[Dict, CassiopeiaTree, Optional[pd.DataFrame]]
    ) -> float:
        hyperparameters, tree_train, cm_valid = args
        model = IIDExponentialBayesian(
            **_transform_hyperparameters_iid_exponential_bayesian(
                hyperparameters
            )
        )
        try:
            model.estimate_branch_lengths(tree_train)
            return model.log_likelihood
        except:
            return -np.inf
        return

    def estimate_branch_lengths(self, tree: CassiopeiaTree) -> None:
        try:
            hyperparameters = self._tune(tree)
        except CharacterLevelCVError:
            raise BranchLengthEstimatorError("Ray tune failed.")
        model = IIDExponentialBayesian(
            **_transform_hyperparameters_iid_exponential_bayesian(
                hyperparameters
            )
        )
        model.estimate_branch_lengths(tree)
        self.mutation_rate = model.mutation_rate
        self.birth_rate = model.birth_rate
        self.sampling_probability = model.sampling_probability
        self.discretization_level = model.discretization_level


def _cv_metric_ble(
    ble_class, args: Tuple[Dict, CassiopeiaTree, Optional[pd.DataFrame]]
) -> float:
    hyperparameters, tree_train, cm_valid = args
    model = ble_class(**hyperparameters)
    try:
        tree_train.set_character_states_at_leaves(tree_train.character_matrix)
        tree_train.reconstruct_ancestral_characters()
        tree_train.set_character_states(
            tree_train.root, [0] * tree_train.n_character
        )
        model.estimate_branch_lengths(tree_train)
    except:
        return -np.inf
    if cm_valid is not None:
        tree_valid = CassiopeiaTree(
            character_matrix=cm_valid, tree=tree_train.get_tree_topology()
        )
        tree_valid.set_character_states_at_leaves(cm_valid)
        tree_valid.set_times(tree_train.get_times())
        tree_valid.reconstruct_ancestral_characters()
        tree_valid.set_character_states(
            tree_valid.root, [0] * tree_valid.n_character
        )
        return IIDExponentialMLE.model_log_likelihood(
            tree_valid, model.mutation_rate
        )
    else:
        # No CV - training log-likelihood
        return IIDExponentialMLE.model_log_likelihood(
            tree_train, model.mutation_rate
        )


class IIDExponentialBayesianCrossValidated(
    BranchLengthEstimator, CharacterLevelCV
):
    """
    Cross-validated IIDExponentialBayesian.

    Uses the IIDExponentialMLE model log-likelihood as the metric.
    """

    @staticmethod
    def _create_space(tree: CassiopeiaTree):
        return _create_space_iid_exponential_bayesian(tree)

    @staticmethod
    def _cv_metric(
        args: Tuple[Dict, CassiopeiaTree, Optional[pd.DataFrame]]
    ) -> float:
        hyperparameters, tree_train, cm_valid = args
        hyperparameters = _transform_hyperparameters_iid_exponential_bayesian(
            hyperparameters
        )
        args = (hyperparameters, tree_train, cm_valid)
        return _cv_metric_ble(ble_class=IIDExponentialBayesian, args=args)

    def estimate_branch_lengths(self, tree: CassiopeiaTree) -> None:
        try:
            hyperparameters = self._tune(tree)
        except CharacterLevelCVError:
            raise BranchLengthEstimatorError("Ray tune failed.")
        model = IIDExponentialBayesian(
            **_transform_hyperparameters_iid_exponential_bayesian(
                hyperparameters
            )
        )
        model.estimate_branch_lengths(tree)
        self.mutation_rate = model.mutation_rate


class IIDExponentialMLECrossValidated(BranchLengthEstimator, CharacterLevelCV):
    """
    Cross-validated IIDExponentialMLE.

    Uses the IIDExponentialMLE model log-likelihood as the metric.
    """

    @staticmethod
    def _create_space(tree: CassiopeiaTree):
        space = {
            "minimum_branch_length": tune.loguniform(
                0.0000001, 1.0 / (tree.get_edge_depth() + 1e-8)
            )
        }
        return space

    @staticmethod
    def _cv_metric(
        args: Tuple[Dict, CassiopeiaTree, Optional[pd.DataFrame]]
    ) -> float:
        return _cv_metric_ble(ble_class=IIDExponentialMLE, args=args)

    def estimate_branch_lengths(self, tree: CassiopeiaTree) -> None:
        try:
            best_config = self._tune(tree)
        except CharacterLevelCVError:
            raise BranchLengthEstimatorError("Ray tune failed.")
        model = IIDExponentialMLE(**best_config)
        model.estimate_branch_lengths(tree)
        self.mutation_rate = model.mutation_rate
