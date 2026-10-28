LOCAL RAW-SEQUENCING CHECKSUM AND QC WORKFLOW

Scope
This Linux/WSL package verifies supplied MD5 checksums for compressed FASTQ files,
runs FastQC separately for each sample/lane/mate, compares paired read counts,
and builds MultiQC from validated outputs. It does not trim reads, align them,
call variants, infer relationships, or perform clinical interpretation.

This code-only distribution contains no actual sequencing files, private
input manifest, participant metadata, pedigree, logs, or results.
Example identifiers are synthetic. The placeholder example manifest fails
validation until replaced with real values.

Requirements
- Linux x86_64, including Ubuntu under WSL2.
- Python 3.10 or newer for the launcher.
- Existing Conda, discovered from CONDA_EXE, PATH, or common home directories.
- The pinned tools in environment.yml. Bootstrap can create an isolated project
  environment without sudo or modifying Conda base.
- Readable input storage throughout processing and an awake computer.

Configuration
Keep private input metadata outside this code directory. Use absolute paths:

  export WES_QC_PROJECT="$HOME/wes_qc_project"
  export WES_QC_MANIFEST="/absolute/private/path/inputs.json"

Optional:
  export WES_QC_OUTPUT="/absolute/private/path/qc-results"
  export WES_QC_CONDA_EXE="/absolute/path/to/conda"
  export WES_QC_BOOTSTRAP_PYTHON="/absolute/path/to/python3"

The default output is "$WES_QC_PROJECT/results". Tools, installation logs,
environment selection, caches and locks live under WES_QC_PROJECT.
Use a Linux filesystem for the project and locks. Results may be on mounted
storage. Keep project and result directories outside the manifest raw_root.

Private manifest
Use examples/inputs.example.json as a schema guide, not runnable data.
Set raw_root, expected_samples and expected_files. Every file entry requires:
sample_id, batch, lane_label, mate (R1 or R2), absolute path, compressed
size_bytes, and expected 32-character MD5 from the provider or another trusted
source. Each sample/lane needs both mates. Paths and basenames must be unique.
Keep identifiers stable between restarts. Do not replace an expected checksum
with one calculated from a suspect file simply to make a failure pass.

Commands
From the directory containing start_qc.sh:

  bash start_qc.sh start
  bash start_qc.sh status
  bash start_qc.sh stop

Foreground:
  bash start_qc.sh run

start/run inspect available environments, then install the pinned tools from
conda-forge and Bioconda if necessary. Installation requires network access
and writes within the configured project area. Review bootstrap.sh first.

Environment inspection only:
  bash bootstrap.sh inspect

Direct QC with an existing compatible environment:
  /absolute/env/bin/python run_qc.py \
    --manifest /absolute/private/path/inputs.json \
    --env-prefix /absolute/env \
    --out-dir /absolute/private/path/qc-results \
    --lock-dir "$HOME/wes_qc_project/runtime/locks"

Results and restarting
The detached launcher records launcher_status.json and log paths in the result
directory. status.json records raw-QC progress. Read both for an environment
or launch failure. stop verifies the recorded process identity before signalling.

Full compressed-file MD5 runs sequentially before FastQC. Two FastQC processes
run concurrently, each with one thread and 1024 MB Java allocation.
No --casava mode, trimming, filtering, or read rewriting is performed.
Successful per-file states are reused only when input identity/checksum, tool
recipe and validated report contents still match. Incomplete attempts remain
for diagnosis and are not considered successful. Restart with the same command.

Aggregation checks exact names, source ZIP files and read counts.
FastQC WARN/FAIL modules are findings, not execution failures. Completed QC
does not mean every biological quality criterion passes.
Matching R1/R2 counts do not establish complete read-ID ordering.
MD5 agreement does not establish sample identity or medical validity.

Privacy and future publication
MultiQC version checks, built-in AI summaries and MegaQC uploads are disabled.
Installation downloads software; raw-data upload is not part of this workflow.
Keep your project, real manifest, logs and outputs outside a public repository.
.gitignore is a code-only allowlist. Review all tracked content before publishing.
Preparing this folder does not create a Git repository or GitHub project.
Choose a license before publication.

Validation
  python3 tests/test_qc_runner.py

Tests use synthetic files and mocked tool execution. They cover checksum/cache
failures, environment-prefix changes, input mutation, interruptions, invalid
reports, pair counts and MultiQC accounting. They do not replace a real Linux
integration run on a small local dataset.

Sources
https://www.bioinformatics.babraham.ac.uk/projects/fastqc/
https://github.com/s-andrews/FastQC/blob/v0.12.1/fastqc
https://docs.seqera.io/multiqc/getting_started/running_multiqc
https://docs.seqera.io/multiqc/config_schema

Derivation
QC algorithms and pinned dependencies are preserved from the operational
workflow. Machine-specific paths and private manifest locations are replaced
with configuration. Runtime metadata writes are relocated from the code
directory to the configured project directory.
