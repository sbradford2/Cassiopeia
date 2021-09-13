"""
This file stores a subclass of BranchLengthEstimator, the IIDExponentialMLE.
Briefly, this model assumes that CRISPR/Cas9 mutates each site independently
and identically, with an exponential waiting time.
"""
import cvxpy as cp
import numpy as np

from cassiopeia.data import CassiopeiaTree
from cassiopeia.mixins import IIDExponentialMLEError

import multiprocessing

from typing import List, Tuple

from .BranchLengthEstimator import BranchLengthEstimator


class IIDExponentialMLE(BranchLengthEstimator):
    """
    MLE under a model of IID memoryless CRISPR/Cas9 mutations.

    In more detail, this model assumes that CRISPR/Cas9 mutates each site
    independently and identically, with an exponential waiting time. The
    tree is assumed to have depth exactly 1, and the user can provide a
    minimum branch length. The MLE under this set of assumptions can be
    solved with a special kind of convex optimization problem known as an
    exponential cone program, which can be readily solved with off-the-shelf
    (open source) solvers.

    This estimator requires that the ancestral characters be provided (these
    can be imputed with CassiopeiaTree's reconstruct_ancestral_characters
    method if they are not known, which is usually the case for real data).

    The estimated mutation rate under will be stored as an attribute called
    `mutation_rate`. The log-likelihood will be stored in an attribute
    called `log_likelihood`.

    Missing states are treated as missing at random by the model.

    Args:
        minimum_branch_length: Estimated branch lengths will be constrained to
            have length at least this value. By default it is set to 0.01,
            since the MLE tends to collapse mutationless edges to length 0.
        solver: Convex optimization solver to use. Can be "SCS", "ECOS", or
            "MOSEK". Note that "MOSEK" solver should be installed separately.
        verbose: Verbosity level.

    Attributes:
        mutation_rate: The estimated CRISPR/Cas9 mutation rate, assuming that
            the tree has depth exactly 1.
        log_likelihood: The log-likelihood of the training data under the
            estimated model.
    """

    def __init__(
        self,
        minimum_branch_length: float = 0.01,
        verbose: bool = False,
        solver: str = "SCS",
    ):
        allowed_solvers = ["ECOS", "SCS", "MOSEK"]
        if solver not in allowed_solvers:
            raise ValueError(
                f"Solver {solver} not allowed. "
                f"Allowed solvers: {allowed_solvers}"
            )  # pragma: no cover
        self._minimum_branch_length = minimum_branch_length
        self._verbose = verbose
        self._solver = solver
        self._mutation_rate = None
        self._log_likelihood = None

    def estimate_branch_lengths(self, tree: CassiopeiaTree) -> None:
        r"""
        MLE under a model of IID memoryless CRISPR/Cas9 mutations.

        The only caveat is that this method raises an IIDExponentialMLEError
        if the underlying convex optimization solver fails, or a
        ValueError if the character matrix is degenerate (fully mutated,
        or fully unmutated).

        Raises:
            IIDExponentialMLEError
            ValueError
        """
        # Extract parameters
        minimum_branch_length = self._minimum_branch_length
        solver = self._solver
        verbose = self._verbose

        # # # # # Check that the character has at least one mutation # # # # #
        if (tree.character_matrix == 0).all().all():
            raise ValueError(
                "The character matrix has no mutations. Please check your data."
            )

        # # # # # Check that the character is not saturated # # # # #
        if (tree.character_matrix != 0).all().all():
            raise ValueError(
                "The character matrix is fully mutated. The MLE does not "
                "exist. Please check your data."
            )

        # # # # # Check that the minimum_branch_length makes sense # # # # #
        if tree.get_edge_depth() * minimum_branch_length >= 1.0:
            raise ValueError(
                "The minimum_branch_length is too large. Please reduce it."
            )

        # # # # # Create variables of the optimization problem # # # # #
        r_X_t_variables = dict(
            [
                (node_id, cp.Variable(name=f"r_X_t_{node_id}"))
                for node_id in tree.nodes
            ]
        )

        # # # # # Create constraints of the optimization problem # # # # #
        a_leaf = tree.leaves[0]
        root = tree.root
        root_has_time_0_constraint = [r_X_t_variables[root] == 0]
        minimum_branch_length_constraints = [
            r_X_t_variables[child]
            >= r_X_t_variables[parent]
            + minimum_branch_length * r_X_t_variables[a_leaf]
            for (parent, child) in tree.edges
        ]
        ultrametric_constraints = [
            r_X_t_variables[leaf] == r_X_t_variables[a_leaf]
            for leaf in tree.leaves
            if leaf != a_leaf
        ]
        all_constraints = (
            root_has_time_0_constraint
            + minimum_branch_length_constraints
            + ultrametric_constraints
        )

        # # # # # Compute the log-likelihood # # # # #
        log_likelihood = 0
        for (parent, child) in tree.edges:
            edge_length = r_X_t_variables[child] - r_X_t_variables[parent]
            num_unmutated = len(
                tree.get_unmutated_characters_along_edge(parent, child)
            )
            num_mutated = len(
                tree.get_mutations_along_edge(
                    parent, child, treat_missing_as_mutations=False
                )
            )
            log_likelihood += num_unmutated * (-edge_length)
            log_likelihood += num_mutated * cp.log(
                1 - cp.exp(-edge_length - 1e-5)  # We add eps for stability.
            )

        # # # # # Solve the problem # # # # #
        obj = cp.Maximize(log_likelihood)
        prob = cp.Problem(obj, all_constraints)
        try:
            prob.solve(solver=solver, verbose=verbose)
        except cp.SolverError:  # pragma: no cover
            raise IIDExponentialMLEError("Third-party solver failed")

        # # # # # Extract the mutation rate # # # # #
        self._mutation_rate = float(r_X_t_variables[a_leaf].value)
        if self._mutation_rate < 1e-8 or self._mutation_rate > 15.0:
            raise IIDExponentialMLEError(
                "The solver failed when it shouldn't have."
            )

        # # # # # Extract the log-likelihood # # # # #
        log_likelihood = float(log_likelihood.value)
        if np.isnan(log_likelihood):
            log_likelihood = -np.inf
        self._log_likelihood = log_likelihood

        # # # # # Populate the tree with the estimated branch lengths # # # # #
        times = {
            node: float(r_X_t_variables[node].value) / self._mutation_rate
            for node in tree.nodes
        }
        # Make sure that the root has time 0 (avoid epsilons)
        times[tree.root] = 0.0
        # We smooth out epsilons that might make a parent's time greater
        # than its child (which can happen if minimum_branch_length=0)
        for (parent, child) in tree.depth_first_traverse_edges():
            times[child] = max(times[parent], times[child])
        tree.set_times(times)

    @property
    def log_likelihood(self):
        """
        The log-likelihood of the training data under the estimated model.
        """
        return self._log_likelihood

    @property
    def mutation_rate(self):
        """
        The estimated CRISPR/Cas9 mutation rate under the given model.
        """
        return self._mutation_rate

    @staticmethod
    def model_log_likelihood(tree: CassiopeiaTree, mutation_rate: float) -> float:
        r"""
        The log-likelihood of the given character states under the model,
        up to constants. Used for cross-validation.
        """
        log_likelihood = 0
        for (parent, child) in tree.edges:
            edge_length = tree.get_time(child) - tree.get_time(parent)
            assert(edge_length >= 0)
            num_unmutated = len(
                tree.get_unmutated_characters_along_edge(parent, child)
            )
            num_mutated = len(
                tree.get_mutations_along_edge(
                    parent, child, treat_missing_as_mutations=False
                )
            )
            log_likelihood += num_unmutated * (-edge_length * mutation_rate)
            if num_mutated > 0:
                if edge_length * mutation_rate < 1e-8:
                    return -np.inf
                log_likelihood += num_mutated * np.log(
                    1 - np.exp(-edge_length * mutation_rate)
                )
        assert not np.isnan(log_likelihood)
        return log_likelihood


class IIDExponentialMLEGridSearchCV(BranchLengthEstimator):
    r"""
    Like IIDExponentialMLE but with automatic tuning of hyperparameters.

    This class fits the hyperparameters of IIDExponentialMLE based on
    character-level held-out log-likelihood. It leaves out one character at a
    time, fitting the data on all the remaining characters. Thus, the number
    of models trained by this class is #characters * grid size.

    Args:
        minimum_branch_lengths: The grid of minimum_branch_length to use.
        verbose: Verbosity level.
    """

    def __init__(
        self,
        minimum_branch_lengths: Tuple[float] = (0, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5),
        processes: int = 5,
        n_fold: int = 5,
        verbose: bool = False,
    ):
        self.minimum_branch_lengths = minimum_branch_lengths
        self.processes = processes
        self.n_fold = n_fold
        self.verbose = verbose

    def estimate_branch_lengths(self, tree: CassiopeiaTree) -> None:
        r"""
        See base class. The only caveat is that this method raises if it fails
        to solve the underlying optimization problem for any reason.

        Raises:
            cp.error.SolverError
        """
        # Extract parameters
        minimum_branch_lengths = self.minimum_branch_lengths
        verbose = self.verbose

        held_out_log_likelihoods = []  # type: List[Tuple[float, List]]
        grid = np.zeros(
            shape=(len(minimum_branch_lengths))
        )
        random_char_indices = list(range(tree.n_character))
        np.random.shuffle(random_char_indices)
        for i, minimum_branch_length in enumerate(minimum_branch_lengths):
            cv_log_likelihood = self._cv_log_likelihood(
                tree=tree,
                minimum_branch_length=minimum_branch_length,
                random_char_indices=random_char_indices,
            )
            held_out_log_likelihoods.append(
                (
                    cv_log_likelihood,
                    [minimum_branch_length],
                )
            )
            grid[i] = cv_log_likelihood

        # Refit model on full dataset with the best hyperparameters
        held_out_log_likelihoods.sort(reverse=True)
        (
            best_minimum_branch_length,
        ) = held_out_log_likelihoods[0][1]
        if verbose:
            print(f"grid = {grid}")
            print(
                f"Refitting full model with:\n"
                f"minimum_branch_length={best_minimum_branch_length}"
            )
        final_model = IIDExponentialMLE(
            minimum_branch_length=best_minimum_branch_length,
        )
        final_model.estimate_branch_lengths(tree)
        self.minimum_branch_length = best_minimum_branch_length
        self.log_likelihood = final_model.log_likelihood
        self.grid = grid

    def _cv_log_likelihood(
        self,
        tree: CassiopeiaTree,
        minimum_branch_length: float,
        random_char_indices: List[int],
    ) -> float:
        r"""
        Given the tree and the parameters of the model, returns the
        cross-validated log-likelihood of the model. This is done by holding out
        one character at a time, fitting the model on the remaining characters,
        and evaluating the log-likelihood on the held-out character. As a
        consequence, #character models are fit by this method. The mean held-out
        log-likelihood over the #character folds is returned.
        """
        verbose = self.verbose
        processes = self.processes
        n_fold = self.n_fold
        if n_fold == -1:
            n_fold = tree.n_character
        if verbose:
            print(
                f"Cross-validating hyperparameters:"
                f"\nminimum_branch_length={minimum_branch_length}"
            )
        n_characters = tree.n_character
        params = []
        split_size = int((n_characters + n_fold - 1) / n_fold)
        for split_id in range(n_fold):
            held_out_character_idxs = random_char_indices[(split_id * split_size): ((split_id + 1) * split_size)]
            train_tree, valid_tree = self._cv_split(
                tree=tree, held_out_character_idxs=held_out_character_idxs
            )
            model = IIDExponentialMLE(
                minimum_branch_length=minimum_branch_length,
            )
            params.append((model, train_tree, valid_tree))
        with multiprocessing.Pool(processes=processes) as pool:
            map_fn = pool.map if processes > 1 else map
            log_likelihood_folds = list(map_fn(_fit_model, params))
        if verbose:
            print(f"log_likelihood_folds = {log_likelihood_folds}")
            print(f"mean log likelihood = {np.mean(log_likelihood_folds)}")
        return np.mean(np.array(log_likelihood_folds))

    def _cv_split(
        self, tree: CassiopeiaTree, held_out_character_idxs: List[int]
    ) -> Tuple[CassiopeiaTree, CassiopeiaTree]:
        r"""
        Creates a training and a cross validation tree by hiding the
        character at position held_out_character_idx.
        """
        if self.verbose:
            print(f"IIDExponentialMLEGridSearchCV held_out_character_idxs = {held_out_character_idxs}")
        tree_topology = tree.get_tree_topology()
        train_states = {}
        valid_states = {}
        for node in tree.nodes:
            state = tree.get_character_states(node)
            train_state = [state[i] for i in range(len(state)) if i not in held_out_character_idxs]
            valid_state = [state[i] for i in held_out_character_idxs]
            train_states[node] = train_state
            valid_states[node] = valid_state
        train_tree = CassiopeiaTree(tree=tree_topology)
        valid_tree = CassiopeiaTree(tree=tree_topology)
        train_tree.set_all_character_states(train_states)
        valid_tree.set_all_character_states(valid_states)
        return train_tree, valid_tree


def _fit_model(args):
    r"""
    This is used by IIDExponentialMLEGridSearchCV to
    parallelize the CV folds. It must be defined here (at the top level of
    the module) for multiprocessing to be able to pickle it. (This is why
    coverage misses it)
    """
    model, train_tree, valid_tree = args
    try:
        model.estimate_branch_lengths(train_tree)
        valid_tree.set_times(train_tree.get_times())
        held_out_log_likelihood = IIDExponentialMLE.model_log_likelihood(valid_tree, mutation_rate=model.mutation_rate)
    except (IIDExponentialMLEError, ValueError):
        held_out_log_likelihood = -np.inf
    return held_out_log_likelihood
