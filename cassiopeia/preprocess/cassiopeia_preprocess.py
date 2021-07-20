"""
Main logic behind Cassiopeia-preprocess.

This file stores the main entry point for Cassiopeia-preprocess, and makes
heavy use of the high level functionality in
cassiopeia.preprocess.pipeline. Here, we assume that the user
has already run CellRanger Count, or some equivalent, to obtain a BAM file that
relates cell barcodes and UMIs to sequences.

TODO(mattjones315@): include invocation instructions & pipeline specifics.
"""
import os

import argparse
import configparser
import pandas as pd
from typing import Any, Dict

from cassiopeia.mixins import logger, PreprocessError
from cassiopeia.preprocess import pipeline, setup_utilities, utilities

STAGES = {
    "convert": pipeline.convert_fastqs_to_unmapped_bam,
    "filter": pipeline.filter_bam,
    "error_correct_barcodes": pipeline.error_correct_barcodes,
    "collapse": pipeline.collapse_umis,
    "resolve": pipeline.resolve_umi_sequence,
    "align": pipeline.align_sequences,
    "call_alleles": pipeline.call_alleles,
    "error_correct_intbcs": pipeline.error_correct_intbcs,
    "error_correct_umis": pipeline.error_correct_umis,
    "filter_molecule_table": pipeline.filter_molecule_table,
    "call_lineages": pipeline.call_lineage_groups,
}


@logger.namespaced("main")
@utilities.log_runtime
def main():

    # --------------- Create Argument Parser & Read in Arguments -------------- #
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "config", type=str, help="Specify a config file for analysis."
    )

    args = parser.parse_args()

    config_filepath = args.config

    with open(config_filepath, "r") as f:
        pipeline_parameters = setup_utilities.parse_config(f.read())

    # pull out general parameters
    name = pipeline_parameters["general"]["name"]
    output_directory = pipeline_parameters["general"]["output_directory"]
    data_filepaths = pipeline_parameters["general"]["input_files"]
    entry_point = pipeline_parameters["general"]["entry"]
    exit_point = pipeline_parameters["general"]["exit"]

    # set up output directory
    setup_utilities.setup(output_directory)

    # create pipeline plan
    pipeline_stages = setup_utilities.create_pipeline(
        entry_point, exit_point, STAGES
    )
    if entry_point == "convert":
        data = data_filepaths
    else:
        if len(data_filepaths) != 1:
            raise PreprocessError(
                "`input_files` must contain exactly one input file for pipeline "
                f"stage `{entry_point}`"
            )

        if entry_point in ("filter", "error_correct_barcodes", "collapse"):
            data = data_filepaths[0]
        else:
            data = pd.read_csv(data_filepaths[0], sep="\t")

    # ---------------------- Run Pipeline ---------------------- #
    for stage in pipeline_stages:
        # Skip barcode correction if whitelist_fp was not provided
        if stage == "error_correct_barcodes" and not pipeline_parameters[
            stage
        ].get("whitelist_fp"):
            logger.warning(
                "Skipping barcode error correction because no whitelist was "
                "provided in the configuration."
            )
            continue
        # Skip intBC correction to whitelist if whitelist_fp was not provided
        if stage == "error_correct_intbcs" and not pipeline_parameters[
            stage
        ].get("whitelist_fp"):
            logger.warning(
                "Skipping intBC error correction because no whitelist was "
                "provided in the configuration."
            )
            continue

        procedure = STAGES[stage]
        data = procedure(data, **pipeline_parameters[stage])

        # Write to CSV only if it is a pandas dataframe
        if isinstance(data, pd.DataFrame):
            data.to_csv(
                os.path.join(output_directory, name + f".{stage}.txt"),
                sep="\t",
                index=False,
            )


if __name__ == "__main__":
    main()
