"""
This file stores generally important functionality for the Cassiopeia-Preprocess
pipeline.
"""
import os
import logging

from collections import defaultdict, OrderedDict
import Levenshtein
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pysam
from typing import Dict, List, Optional, Tuple

from tqdm.auto import tqdm


def generate_log_output(df: pd.DataFrame, begin: bool = False):
    """A function for the logging of the number of filtered elements.

    Simple function that logs the number of total reads, the number of unique
    UMIs, and the number of unique cellBCs in a DataFrame.

    Args:
        df: A DataFrame

    Returns:
        None, logs elements to log file
    """

    if begin:
        logging.info("Before filtering:")
    else:
        logging.info("After this filtering step:")
        logging.info("# Reads: " + str(sum(df["readCount"])))
        logging.info(f"# UMIs: {df.shape[0]}")
        logging.info("# Cell BCs: " + str(len(np.unique(df["cellBC"]))))


def filter_cells(
    molecule_table: pd.DataFrame,
    min_umi_per_cell: int = 10,
    min_avg_reads_per_umi: float = 2.0,
    verbose: bool = False,
) -> pd.DataFrame:
    """Filter out cell barcodes that have too few UMIs or too few reads/UMI

    Args:
        molecule_table: A molecule table of cellBC-UMI pairs to be filtered
        min_umi_per_cell: Minimum number of UMIs per cell for cell to not be
            filtered
        min_avg_reads_per_umi: Minimum coverage (i.e. average) reads per
            UMI in a cell needed in order for that cell not to be filtered
        verbose: Indicates whether to log the number of cellBCs and UMIs
            remaining after filtering

    Returns:
        A filtered molecule table
    """

    tooFewUMI_UMI = []

    # Create a cell-filter dictionary for hash lookup later on when filling
    # in the table
    cell_filter = {}

    for n, group in molecule_table.groupby(["cellBC"]):
        if group["UMI"].dtypes == object:
            umi_per_cellBC_n = group.shape[0]
        else:
            umi_per_cellBC_n = group.agg({"UMI": "sum"}).UMI
        reads_per_cellBC_n = group.agg({"readCount": "sum"}).readCount
        avg_reads_per_UMI_n = float(reads_per_cellBC_n) / float(
            umi_per_cellBC_n
        )
        if (umi_per_cellBC_n <= min_umi_per_cell) or (
            avg_reads_per_UMI_n <= min_avg_reads_per_umi
        ):
            cell_filter[n] = True
            tooFewUMI_UMI.append(group.shape[0])
        else:
            cell_filter[n] = False

    # apply the filter using the hash table created above
    molecule_table["filter"] = molecule_table["cellBC"].map(cell_filter)

    n_umi_filt = molecule_table[molecule_table["filter"] == True].shape[0]
    n_cells_filt = len(
        molecule_table.loc[molecule_table["filter"] == True, "cellBC"].unique()
    )

    logging.info(f"Filtered out {n_cells_filt} cells with too few UMIs.")
    logging.info(f"Filtered out {n_umi_filt} UMIs as a result.")

    filt_molecule_table = molecule_table[
        molecule_table["filter"] == False
    ].copy()
    filt_molecule_table.drop(columns=["filter"], inplace=True)

    if verbose:
        generate_log_output(filt_molecule_table)

    return filt_molecule_table


def filter_umis(
    moleculetable: pd.DataFrame,
    readCountThresh: int = 100,
    verbose: bool = False,
) -> pd.DataFrame:
    """
    Filters out UMIs with too few reads.

    Filters out all UMIs with a read count <= readCountThresh.

    Args:
        moleculetable: A molecule table of cellBC-UMI pairs to be filtered
        readCountThresh: The minimum read count needed for a UMI to not be
            filtered
        verbose: Indicates whether to log the number of cellBCs and UMIs
            remaining after filtering

    Returns:
        A filtered molecule table
    """

    # filter based on status & reindex
    n_moleculetable = moleculetable[
        moleculetable["readCount"] > readCountThresh
    ]
    n_moleculetable.index = [i for i in range(n_moleculetable.shape[0])]

    if verbose:
        generate_log_output(n_moleculetable)

    return n_moleculetable


def error_correct_intbc(
    moleculetable: pd.DataFrame,
    prop: float = 0.5,
    umiCountThresh: int = 10,
    bcDistThresh: int = 1,
    verbose: bool = False,
) -> pd.DataFrame:
    """
    Error corrects close intBCs with small enough unique UMI counts.

    Considers each pair of intBCs sharing a cellBC in the DataFrame for
    correction. For a pair of intBCs, changes all instances of one to other if:
        1. They have the same allele.
        2. The Levenshtein distance between their sequences is <= bcDistThresh.
        3. The number of UMIs of the intBC to be changed is <= umiCountThresh.
        4. The proportion of the UMI count in the intBC to be changed out of the
        total UMI count in both intBCs <= prop.
    Note: Prop should be <= 0.5, as this algorithm only corrects intBCs with
    fewer/equal UMIs towards intBCs with more UMIs. Additionally, if multiple
    intBCs are within the distance threshold of an intBC, it corrects the intBC
    towards the intBC with the most UMIs.

    Args:
        moleculetable: A molecule table of cellBC-UMI pairs to be filtered
        prop: proportion by which to filter integration barcodes
        umiCountThresh: maximum umi count for which to correct barcodes
        bcDistThresh: barcode distance threshold, to decide what's similar
            enough to error correct
        verbose: Indicates whether to log every cellBC correction and the
            number of cellBCs and UMIs remaining after filtering

    Returns:
        Filtered molecule table with error corrected intBCs
    """

    # create index filter hash map
    index_filter = {}
    for n in moleculetable.index.values:
        index_filter[n] = "good"

    recovered = 0
    numUMI_corrected = 0
    for name, grp in tqdm(
        moleculetable.groupby(["cellBC"]), desc="Error Correcting intBCs"
    ):

        x1 = (
            grp.groupby(["intBC", "allele"])
            .agg({"UMI": "count", "readCount": "sum"})
            .sort_values("UMI", ascending=False)
            .reset_index()
        )

        if x1.shape[0] > 1:
            for r1 in range(x1.shape[0]):
                iBC1, allele1 = x1.loc[r1, "intBC"], x1.loc[r1, "allele"]
                for r2 in range(r1 + 1, x1.shape[0]):
                    iBC2, allele2 = x1.loc[r2, "intBC"], x1.loc[r2, "allele"]
                    bclDist = Levenshtein.distance(iBC1, iBC2)
                    if bclDist <= bcDistThresh and allele1 == allele2:
                        totalCount = x1.loc[[r1, r2], "UMI"].sum()
                        umiCounts = x1.loc[[r1, r2], "UMI"]
                        props = umiCounts / totalCount

                        # if the alleles are the same and the proportions are good, then let's error correct
                        if props[r2] < prop and umiCounts[r2] <= umiCountThresh:
                            bad_locs = moleculetable[
                                (moleculetable["cellBC"] == name)
                                & (moleculetable["intBC"] == iBC2)
                                & (moleculetable["allele"] == allele2)
                            ]
                            recovered += 1
                            numUMI_corrected += len(bad_locs.index.values)
                            moleculetable.loc[
                                bad_locs.index.values, "intBC"
                            ] = iBC1

                            if verbose:
                                logging.info(
                                    f"In cellBC {name}, intBC {iBC2} corrected to {iBC1},"
                                    + "correcting UMI "
                                    + str({x1.loc[r2, "UMI"]})
                                    + "to "
                                    + str({x1.loc[r1, "UMI"]})
                                )

    moleculetable.index = [i for i in range(moleculetable.shape[0])]

    if verbose:
        generate_log_output(moleculetable)

    return moleculetable


def record_stats(
    moleculetable: pd.DataFrame,
) -> Tuple[np.array, np.array, np.array]:
    """
    Simple function to record the number of UMIs.

    Args:
        moleculetable: A DataFrame of alignments

    Returns:
        Read counts for each alignment, number of unique UMIs per intBC, number
            of UMIs per cellBC
    """

    # Count UMI per intBC
    umi_per_ibc = np.array([])
    for n, g in moleculetable.groupby(["cellBC"]):
        x = g.groupby(["intBC"]).agg({"UMI": "nunique"})["UMI"]
        if x.shape[0] > 0:
            umi_per_ibc = np.concatenate([umi_per_ibc, np.array(x)])

    # Count UMI per cellBC
    umi_per_cbc = (
        moleculetable.groupby(["cellBC"])
        .agg({"UMI": "count"})
        .sort_values("UMI", ascending=False)["UMI"]
    )

    return (
        np.array(moleculetable["readCount"]),
        umi_per_ibc,
        np.array(umi_per_cbc),
    )


def convert_bam_to_df(
    data_fp: str, out_fp: str, create_pd: bool = False
) -> pd.DataFrame:
    """Converts a BAM file to a dataframe.

    Saves the contents of a BAM file to a tab-delimited table saved to a text
    file. Rows represent alignments with relevant fields such as the CellBC,
    UMI, read count, sequence, and sequence qualities.

    Args:
        data_fp: The input filepath for the BAM file to be converted.
        out_fp: The output filepath specifying where the resulting dataframe is to
            be stored and its name.
        create_pd: Specifies whether to generate and return a pd.Dataframe.

    Returns:
        If create_pd: a pd.Dataframe containing the BAM information.
        Else: None, output saved to file

    """
    f = open(out_fp, "w")
    f.write("cellBC\tUMI\treadCount\tgrpFlag\tseq\tqual\treadName\n")

    als = []

    bam_fh = pysam.AlignmentFile(
        data_fp, ignore_truncation=True, check_sq=False
    )
    for al in bam_fh:
        cellBC, UMI, readCount, grpFlag = al.query_name.split("_")
        seq = al.query_sequence
        qual = al.query_qualities
        # Pysam qualities are represented as an array of unsigned chars,
        # so they are converted to the ASCII-encoded format that are found
        # in the typical SAM formatting.
        encode_qual = "".join(map(lambda x: chr(x + 33), qual))
        f.write(
            cellBC
            + "\t"
            + UMI
            + "\t"
            + readCount
            + "\t"
            + grpFlag
            + "\t"
            + seq
            + "\t"
            + encode_qual
            + "\t"
            + al.query_name
            + "\n"
        )

        if create_pd:
            als.append(
                [
                    cellBC,
                    UMI,
                    int(readCount),
                    grpFlag,
                    seq,
                    encode_qual,
                    al.query_name,
                ]
            )

    f.close()

    if create_pd:
        df = pd.DataFrame(als)
        df = df.rename(
            columns={
                0: "cellBC",
                1: "UMI",
                2: "readCount",
                3: "grpFlag",
                4: "seq",
                5: "qual",
                6: "readName",
            }
        )
        return df


def convert_alleletable_to_character_matrix(
    alleletable: pd.DataFrame,
    ignore_intbcs: List[str] = [],
    allele_rep_thresh: float = 1.0,
    missing_data_state: str = "-",
    mutation_priors: Optional[pd.DataFrame] = None,
) -> Tuple[
    pd.DataFrame, Dict[int, Dict[str, float]], Dict[int, Dict[str, str]]
]:
    """Converts an alleletable into a character matrix.

    Given an alleletable storing the observed mutations for each intBC / cellBC
    combination, create a character matrix for input into a CassiopeiaSolver
    object. By default, we codify uncut mutations as '0' and missing data items
    as '-'. The function also have the ability to ignore certain intBC sets as
    well as cut sites with too little diversity. 

    Args:
        alleletable: Allele Table to be converted into a character matrix
        ignore_intbcs: A set of intBCs to ignore
        allele_rep_thresh: A threshold for removing target sites that have an
            allele represented by this proportion
        missing_data_state: A state to use for missing data.
        mutation_priors: A table storing the prior probability of a mutation
            occurring. This table is used to create a character matrix-specific
            probability dictionary for reconstruction. 

	Returns:
        A character matrix, a probability dictionary, and a dictionary mapping
            states to the original mutation.
	"""

    filtered_samples = defaultdict(OrderedDict)
    for sample in alleletable.index:
        cell = alleletable.loc[sample, "cellBC"]
        intBC = alleletable.loc[sample, "intBC"]
        cut_sites = ["_r1", "_r2", "_r3"]

        to_add = []
        i = 1
        for c in cut_sites:
            if intBC not in ignore_intbcs:
                to_add.append(("intBC", c, "r" + str(i)))

            i += 1

        for ent in to_add:
            filtered_samples[cell][
                alleletable.loc[sample, ent[0]] + ent[1]
            ] = alleletable.loc[sample, ent[2]]

    character_strings = defaultdict(list)
    allele_counter = defaultdict(OrderedDict)

    _intbc_uniq = []
    allele_dist = defaultdict(list)
    for s in filtered_samples:
        for key in filtered_samples[s]:
            if key not in _intbc_uniq:
                _intbc_uniq.append(key)
            allele_dist[key].append(filtered_samples[s][key])

    # remove intBCs that are not diverse enough
    intbc_uniq = []
    dropped = []
    for key in allele_dist.keys():

        props = np.unique(allele_dist[key], return_counts=True)[1]
        props = props / len(allele_dist[key])
        if np.any(props > allele_rep_thresh):
            dropped.append(key)
        else:
            intbc_uniq.append(key)

    print(
        "Dropping the following intBCs due to lack of diversity with threshold "
        + str(allele_rep_thresh)
        + ": "
        + str(dropped)
    )

    prior_probs = defaultdict(dict)
    indel_to_charstate = defaultdict(dict)
    # for all characters
    for i in tqdm(range(len(list(intbc_uniq))), desc="Processing characters"):

        c = list(intbc_uniq)[i]

        # for all samples, construct a character string
        for sample in filtered_samples.keys():

            if c in filtered_samples[sample]:

                state = filtered_samples[sample][c]

                if type(state) != str and np.isnan(state):
                    character_strings[sample].append(missing_data_state)
                    continue

                if state == "NONE" or "None" in state:
                    character_strings[sample].append("0")
                else:
                    if state in allele_counter[c]:
                        character_strings[sample].append(
                            str(allele_counter[c][state])
                        )
                    else:
                        # if this is the first time we're seeing the state for this character,
                        # add a new entry to the allele_counter
                        allele_counter[c][state] = len(allele_counter[c]) + 1
                        character_strings[sample].append(
                            str(allele_counter[c][state])
                        )

                        # add a new entry to the character's probability map
                        if mutation_priors is not None:
                            prob = np.mean(mutation_priors.loc[state, "freq"])
                            prior_probs[i][
                                str(len(allele_counter[c]))
                            ] = float(prob)
                            indel_to_charstate[i][
                                str(len(allele_counter[c]))
                            ] = state
            else:
                character_strings[sample].append(missing_data_state)

    character_matrix = pd.DataFrame.from_dict(
        character_strings,
        orient="index",
        columns=[f"r{i}" for i in range(1, len(intbc_uniq)+1)],
    )

    return character_matrix, prior_probs, indel_to_charstate