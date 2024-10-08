import os
import json
import re
import subprocess
import sys

from ccbr_tools.pipeline.util import (
    get_hpcname,
    get_tmp_dir,
)
from ccbr_tools.pipeline.cache import image_cache

from .util import get_version


def setup(sub_args, ifiles, repo_path, output_path):
    """Setup the pipeline for execution and creates config file from templates
    @param sub_args <parser.parse_args() object>:
        Parsed arguments for run sub-command
    @param repo_path <str>:
        Path to RENEE source code and its templates
    @param output_path <str>:
        Pipeline output path, created if it does not exist
    @return config <dict>:
         Config dictionary containing metadata to run the pipeline
    @return hpcname <str>:
    """
    # Resolves PATH to template for genomic reference files to select from a
    # bundled reference genome or a user generated reference genome built via
    # renee build subcommand
    hpcname = get_hpcname()
    ty_message = "Thank you for running RENEE"
    ty_message += f" on {hpcname.upper()}!" if hpcname else "!"
    print(ty_message)

    if sub_args.genome.endswith(".json"):
        # Provided a custom reference genome generated by renee build
        genome_config = os.path.abspath(sub_args.genome)
    else:
        genome_config = os.path.join(
            output_path, "config", "genomes", hpcname, sub_args.genome + ".json"
        )
    if not os.path.exists(genome_config):
        raise FileNotFoundError(f"Genome config file does not exist: {genome_config}")

    required = {
        # Template for project-level information
        "project": os.path.join(output_path, "config", "templates", "project.json"),
        # Template for genomic reference files
        # User provided argument --genome is used to select the template
        "genome": genome_config,
        # Template for tool information
        "tools": os.path.join(output_path, "config", "templates", "tools.json"),
    }

    # Global config file for pipeline, config.json
    config = join_jsons(required.values())  # uses templates in the renee repo
    # Update cluster-specific paths for fastq screen & kraken db
    if hpcname == "biowulf" or hpcname == "frce":
        db_json_filename = os.path.join(
            output_path, "config", "templates", f"dbs_{hpcname}.json"
        )
        with open(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), db_json_filename),
            "r",
        ) as json_file:
            config["bin"]["rnaseq"]["tool_parameters"].update(json.load(json_file))

    config = add_user_information(config)
    config = add_rawdata_information(sub_args, config, ifiles)

    # Resolves if an image needs to be pulled from an OCI registry or
    # a local SIF generated from the renee cache subcommand exists
    config = image_cache(sub_args, config)

    # Add other cli collected info
    config["project"]["annotation"] = sub_args.genome
    config["project"]["version"] = get_version()
    config["project"]["pipelinehome"] = os.path.dirname(__file__)
    config["project"]["workpath"] = os.path.abspath(sub_args.output)
    genome_annotation = sub_args.genome
    config["project"]["organism"] = genome_annotation.split("_")[0]

    # Add optional cli workflow steps
    config["options"] = {}
    config["options"]["star_2_pass_basic"] = sub_args.star_2_pass_basic
    config["options"]["small_rna"] = sub_args.small_rna
    config["options"]["tmp_dir"] = get_tmp_dir(sub_args.tmp_dir, output_path)
    config["options"]["shared_resources"] = sub_args.shared_resources
    if sub_args.wait:
        config["options"]["wait"] = "True"
    else:
        config["options"]["wait"] = "False"
    if sub_args.create_nidap_folder:
        config["options"]["create_nidap_folder"] = "True"
    else:
        config["options"]["create_nidap_folder"] = "False"

    # Get latest git commit hash
    git_hash = get_repo_git_commit_hash(repo_path)
    config["project"]["git_commit_hash"] = git_hash

    if sub_args.shared_resources:
        # Update paths to shared resources directory
        config["bin"]["rnaseq"]["tool_parameters"]["KRAKENBACDB"] = os.path.join(
            sub_args.shared_resources, "20180907_standard_kraken2"
        )

    # Save config to output directory
    print(
        "\nGenerating config file in '{}'... ".format(
            os.path.join(output_path, "config.json")
        ),
        end="",
    )
    with open(os.path.join(output_path, "config.json"), "w") as fh:
        json.dump(config, fh, indent=4, sort_keys=True)
    print("Done!")

    return config


def add_user_information(config):
    """Adds username and user's home directory to config.
    @params config <dict>:
        Config dictionary containing metadata to run pipeline
    @return config <dict>:
         Updated config dictionary containing user information (username and home directory)
    """
    # Get PATH to user's home directory
    # Method is portable across unix-like OS and Windows
    home = os.path.expanduser("~")

    # Get username from home directory PATH
    username = os.path.split(home)[-1]

    # Update config with home directory and username
    config["project"]["userhome"] = home
    config["project"]["username"] = username

    return config


def add_rawdata_information(sub_args, config, ifiles):
    """Adds information about rawdata provided to pipeline.
    Determines whether the dataset is paired-end or single-end and finds the set of all
    rawdata directories (needed for -B option when running singularity). If a user provides
    paired-end data, checks to see if both mates (R1 and R2) are present for each sample.
    @param sub_args <parser.parse_args() object>:
        Parsed arguments for run sub-command
    @params ifiles list[<str>]:
        List containing pipeline input files (renamed symlinks)
    @params config <dict>:
        Config dictionary containing metadata to run pipeline
    @return config <dict>:
         Updated config dictionary containing user information (username and home directory)
    """
    # Determine whether dataset is paired-end or single-ends
    # Updates config['project']['nends']: 1 = single-end, 2 = paired-end
    nends = get_nends(ifiles)  # Checks PE data for both mates (R1 and R2)
    config["project"]["nends"] = nends

    # Finds the set of rawdata directories to bind
    rawdata_paths = get_rawdata_bind_paths(input_files=sub_args.input)
    config["project"]["datapath"] = ",".join(rawdata_paths)

    # Add each sample's basename, label and group info
    config = add_sample_metadata(input_files=ifiles, config=config)

    return config


def get_nends(ifiles):
    """Determines whether the dataset is paired-end or single-end.
    If paired-end data, checks to see if both mates (R1 and R2) are present for each sample.
    If single-end, nends is set to 1. Else if paired-end, nends is set to 2.
    @params ifiles list[<str>]:
        List containing pipeline input files (renamed symlinks)
    @return nends_status <int>:
         Integer reflecting nends status: 1 = se, 2 = pe
    """
    # Determine if dataset contains paired-end data
    paired_end = False
    nends_status = 1
    for file in ifiles:
        if file.endswith(".R2.fastq.gz"):
            paired_end = True
            nends_status = 2
            break  # dataset is paired-end

    # Check to see if both mates (R1 and R2) are present paired-end data
    if paired_end:
        nends = {}  # keep count of R1 and R2 for each sample
        for file in ifiles:
            # Split sample name on file extension
            sample = re.split("\.R[12]\.fastq\.gz", os.path.basename(file))[0]
            if sample not in nends:
                nends[sample] = 0

            nends[sample] += 1

        # Check if samples contain both read mates
        missing_mates = [sample for sample, count in nends.items() if count == 1]
        if missing_mates:
            # Missing an R1 or R2 for a provided input sample
            raise NameError(
                """\n\tFatal: Detected pair-end data but user failed to provide
               both mates (R1 and R2) for the following samples:\n\t\t{}\n
            Please check that the basename for each sample is consistent across mates.
            Here is an example of a consistent basename across mates:
              consistent_basename.R1.fastq.gz
              consistent_basename.R2.fastq.gz

            Please do not run the pipeline with a mixture of single-end and paired-end
            samples. This feature is currently not supported within {}, and it is
            not recommended either. If this is a priority for your project, please run
            paired-end samples and single-end samples separately (in two separate output directories).
            If you feel like this functionality should exist, feel free to open an issue on Github.
            """.format(
                    missing_mates, sys.argv[0]
                )
            )

    return nends_status


def get_rawdata_bind_paths(input_files):
    """
    Gets rawdata bind paths of user provided fastq files.
    @params input_files list[<str>]:
        List containing user-provided input fastq files
    @return bindpaths <set>:
        Set of rawdata bind paths
    """
    bindpaths = []
    for file in input_files:
        # Get directory of input file
        rawdata_src_path = os.path.dirname(os.path.abspath(os.path.realpath(file)))
        if rawdata_src_path not in bindpaths:
            bindpaths.append(rawdata_src_path)

    return bindpaths


def add_sample_metadata(input_files, config, group=None):
    """Adds sample metadata such as sample basename, label, and group information.
    If sample sheet is provided, it will default to using information in that file.
    If no sample sheet is provided, it will only add sample basenames and labels.
    @params input_files list[<str>]:
        List containing pipeline input fastq files
    @params config <dict>:
        Config dictionary containing metadata to run pipeline
    @params group <str>:
        Sample sheet containing basename, group, and label for each sample
    @return config <dict>:
        Updated config with basenames, labels, and groups (if provided)
    """
    # TODO: Add functionality for basecase when user has samplesheet
    added = []
    for file in input_files:
        # Split sample name on file extension
        sample = re.split("\.R[12]\.fastq\.gz", os.path.basename(file))[0]
        if sample not in added:
            # Only add PE sample information once
            added.append(sample)
            config["project"]["groups"]["rsamps"].append(sample)
            config["project"]["groups"]["rgroups"].append(sample)
            config["project"]["groups"]["rlabels"].append(sample)

    return config


def join_jsons(templates):
    """Joins multiple JSON files to into one data structure
    Used to join multiple template JSON files to create a global config dictionary.
    @params templates <list[str]>:
        List of template JSON files to join together
    @return aggregated <dict>:
        Dictionary containing the contents of all the input JSON files
    """
    # Get absolute PATH to templates in renee git repo
    repo_path = os.path.dirname(os.path.abspath(__file__))
    aggregated = {}

    for file in templates:
        with open(os.path.join(repo_path, file), "r") as fh:
            aggregated.update(json.load(fh))

    return aggregated


def get_repo_git_commit_hash(repo_path):
    """Gets the git commit hash of the RENEE repo.
    @param repo_path <str>:
        Path to RENEE git repo
    @return githash <str>:
        Latest git commit hash
    """
    try:
        githash = (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], stderr=subprocess.STDOUT, cwd=repo_path
            )
            .strip()
            .decode("utf-8")
        )
        # Typecast to fix python3 TypeError (Object of type bytes is not JSON serializable)
        # subprocess.check_output() returns a byte string
        githash = str(githash)
    except Exception as e:
        # Github releases are missing the .git directory,
        # meaning you cannot get a commit hash, set the
        # commit hash to indicate its from a GH release
        githash = "github_release"

    return githash
