#!/bin/bash
# ==============================================================================
# Market Confluence Pipeline - Universal Local Verification Gate
# ==============================================================================
set -e

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
VENV_PATH="$SCRIPT_DIR/.venv"

if [ -d "$VENV_PATH" ]; then
    echo "⚙️ Found local .venv at $VENV_PATH... Activating."
    source "$VENV_PATH/bin/activate"
    echo "🐍 Python Location: $(which python)"
elif [ -d "$SCRIPT_DIR/../quant-pwa/gateway/.venv" ]; then
    echo "⚙️ Activating gateway .venv for local testing..."
    source "$SCRIPT_DIR/../quant-pwa/gateway/.venv/bin/activate"
fi

export PYTHONPATH="$SCRIPT_DIR/src:$SCRIPT_DIR/../common-lib:$PYTHONPATH"
echo "📂 PYTHONPATH set to: $PYTHONPATH"

# Run pytest using python -m
python -m pytest tests/ -v -ra --showlocals
