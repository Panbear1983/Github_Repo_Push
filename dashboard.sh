#!/bin/bash
# Dashboard wrapper script for Github_Repo_Push
# Sets PYTHONPATH to include the src directory and runs the dashboard command

# Get the directory where this script is located
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Set PYTHONPATH to include the src directory
export PYTHONPATH="$DIR/src:$PYTHONPATH"

# Run the dashboard command
python3 -m github_repo_push.cli dashboard "$@"