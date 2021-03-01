"""
This file stores a subclass of GreedySolver, the MaxCutSolver. This subclass 
implements an inference procedure inspired by Snir and Rao (2006) that 
approximates the max-cut problem on a connectivity graph generated from the 
observed mutations on a group of samples. The connectivity graph represents a
supertree generated over phylogenetic trees for each individual character, and
encodes similarities and differences in mutations between the samples. The
goal is to find a partition on the graph that resolves triplets on the 
supertree, grouping together samples that share mutations and seperating
samples that differ in mutations.
"""
from typing import Callable, Dict, List, Optional, Tuple, Union

import itertools
import networkx as nx
import numpy as np
import pandas as pd

from cassiopeia.solver import graph_utilities, GreedySolver


class MaxCutSolver(GreedySolver.GreedySolver):
    """The MaxCutSolver implements a top-down algorithm that recursively
    partitions the sample set based on connectivity. At each recursive step,
    a connectivity graph is generated for the sample set, where edge weights
    between samples represent the number of triplets that separate those samples
    minus the number of triplets that have those samples as an ingroup. Shared
    mutations are coded as strong negative connections and differing mutations
    are coded as positive connections. Then a partition is generated by finding
    a maximum weight cut over the graph. The final partition is also improved
    upon by a greedy hill-climbing procedure that further optimizes the cut.

    Args:
        prior_transformation: A function defining a transformation on the priors
            in forming weights to scale frequencies and the contribution of
            each mutation in the connectivity graph. One of the following:
                "negative_log": Transforms each probability by the negative
                    log (default)
                "inverse": Transforms each probability p by taking 1/p
                "square_root_inverse": Transforms each probability by the
                    the square root of 1/p
        sdimension: The number of dimensions to use for the embedding space.
            Acts as a hyperparameter
        iterations: The number of iterations in updating the embeddings.
            Acts as a hyperparameter

    Attributes:
        prior_transformation: Function to use when transforming priors into
            weights.
        sdimension: The number of dimensions to use for the embedding space
        iterations: The number of iterations in updating the embeddings
    """

    def __init__(
        self,
        sdimension: Optional[int] = 3,
        iterations: Optional[int] = 50,
        prior_transformation: str = "negative_log",
    ):

        super().__init__(prior_transformation)
        self.sdimension = sdimension
        self.iterations = iterations

    def perform_split(
        self,
        character_matrix: pd.DataFrame,
        samples: List[int],
        weights: Optional[Dict[int, Dict[int, float]]] = None,
        missing_state_indicator: int = -1,
    ) -> Tuple[List[str], List[str]]:
        """Generate a partition of the samples by finding the max-cut.

        First, a connectivity graph is generated with samples as nodes such
        that samples with shared mutations have strong negative edge weight
        and samples with distant mutations have positive edge weight. Then,
        the algorithm finds a partition by using a heuristic method to find
        the max-cut on the connectivity graph. The samples are randomly
        embedded in a d-dimensional sphere and the embeddings for each node
        are iteratively updated based on neighboring edge weights in the
        connectivity graph such that nodes with stronger connectivity cluster
        together. The final partition is generated by choosing random
        hyperplanes to bisect the d-sphere and taking the one that maximizes
        the cut.

        Args:
            character_matrix: Character matrix
            samples: A list of samples to partition
            weights: Weighting of each (character, state) pair. Typically a
                transformation of the priors.
            missing_state_indicator: Character representing missing data.

        Returns:
            A tuple of lists, representing the left and right partition groups
        """
        mutation_frequencies = self.compute_mutation_frequencies(
            samples, character_matrix, missing_state_indicator
        )

        G = graph_utilities.construct_connectivity_graph(
            character_matrix,
            mutation_frequencies,
            missing_state_indicator,
            samples,
            weights=weights,
        )

        if len(G.edges) == 0:
            return samples, []

        embedding_dimension = self.sdimension + 1
        emb = {}
        for i in G.nodes():
            x = np.random.normal(size=embedding_dimension)
            x = x / np.linalg.norm(x)
            emb[i] = x

        for _ in range(self.iterations):
            new_emb = {}
            for i in G.nodes:
                cm = np.zeros(embedding_dimension, dtype=float)
                for j in G.neighbors(i):
                    cm -= (
                        G[i][j]["weight"]
                        * np.linalg.norm(emb[i] - emb[j])
                        * emb[j]
                    )
                if cm.any():
                    cm = cm / np.linalg.norm(cm)
                new_emb[i] = cm
            emb = new_emb

        return_cut = []
        best_score = 0
        for _ in range(3 * embedding_dimension):
            b = np.random.normal(size=embedding_dimension)
            b = b / np.linalg.norm(b)
            cut = []
            for i in G.nodes():
                if np.dot(emb[i], b) > 0:
                    cut.append(i)
            this_score = self.evaluate_cut(cut, G)
            if this_score > best_score:
                return_cut = cut
                best_score = this_score

        improved_left_set = graph_utilities.max_cut_improve_cut(G, return_cut)

        improved_right_set = []
        for i in samples:
            if i not in improved_left_set:
                improved_right_set.append(i)

        return improved_left_set, improved_right_set

    def evaluate_cut(self, cut: List[str], G: nx.DiGraph) -> float:
        """A simple function to evaluate the weight of a cut.

        For each edge in the graph, checks if it is in the cut, and then adds
        its edge weight to the cut if it is.

        Args:
            cut: A list of nodes that represents one of the sides of a cut
                on the graph
            G: The graph the cut is over

        Returns:
            The weight of the cut
        """
        cut_score = 0
        for e in G.edges():
            u = e[0]
            v = e[1]
            w_uv = G[u][v]["weight"]
            if graph_utilities.check_if_cut(u, v, cut):
                cut_score += float(w_uv)

        return cut_score
