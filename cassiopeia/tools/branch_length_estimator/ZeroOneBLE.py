from cassiopeia.data import CassiopeiaTree

from .BranchLengthEstimator import BranchLengthEstimator


class ZeroOneBLE(BranchLengthEstimator):
    def __init__(self, include_missing: bool = True):
        self._include_missing = include_missing

    def estimate_branch_lengths(self, tree: CassiopeiaTree) -> None:
        times = {}

        def dfs(v: str, t: int):
            times[v] = t
            for u in tree.children(v):
                dfs(
                    u,
                    t + 1 * (tree.get_number_of_mutations_along_edge(v, u, include_missing=self._include_missing) > 0),
                )

        dfs(tree.root, 0)

        tree.set_times(times)
