#!/bin/bash
# Daily: read Meta, Microsoft and Netflix in Chrome, then copy the result to the server.
# Needs ~/.hermes-browser/config.env with HERMES_VM_HOST and HERMES_VM_KEY (kept out of the repository).
set -euo pipefail
cd "$(dirname "$0")/../.."
# shellcheck disable=SC1090
source "$HOME/.hermes-browser/config.env"
"$HOME/.venvs/hermes-browser/bin/python" browser_reader.py
scp -i "$HERMES_VM_KEY" -q "$HOME/.hermes-browser/browser_jobs.json" "azureuser@$HERMES_VM_HOST:/home/azureuser/browser_jobs.json"
ssh -i "$HERMES_VM_KEY" "azureuser@$HERMES_VM_HOST" \
  'sudo install -o hermes -g hermes -m 644 /home/azureuser/browser_jobs.json /home/hermes/app/browser_jobs.json && rm -f /home/azureuser/browser_jobs.json'
echo "browser jobs copied to the server at $(date)"
