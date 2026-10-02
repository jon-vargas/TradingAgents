#!/bin/bash
# TradingAgents Quick Setup Script
# Installs dependencies, creates .env, and verifies the environment

set -e  # Exit on error

echo ""
echo "============================================================"
echo "        TradingAgents Quick Setup"
echo "============================================================"
echo ""

# Check Python version
echo "Checking Python version..."
python_version=$(python3 --version 2>&1 | awk '{print $2}')
echo "Found Python $python_version"

major=$(echo "$python_version" | cut -d. -f1)
minor=$(echo "$python_version" | cut -d. -f2)
if [ "$major" -lt 3 ] || ([ "$major" -eq 3 ] && [ "$minor" -lt 10 ]); then
    echo "ERROR: Python 3.10+ is required (found $python_version)"
    exit 1
fi

# Navigate to script directory
cd "$(dirname "$0")"

# Check if .env exists, if not create from example
if [ ! -f .env ]; then
    echo ""
    echo "Creating .env file from template..."
    if [ -f .env.example ]; then
        cp .env.example .env
        echo "  Created .env file"
        echo ""
        echo "  IMPORTANT: Edit .env and add your API keys before running!"
        echo "  Required: OPENAI_API_KEY, FINNHUB_API_KEY"
        echo ""
    else
        echo "ERROR: .env.example not found"
        exit 1
    fi
else
    echo "  .env file already exists"
fi

# Upgrade pip
echo ""
echo "Upgrading pip, setuptools, and wheel..."
pip install --upgrade pip setuptools wheel

# Install dependencies
echo ""
echo "Installing dependencies from requirements.txt..."
echo "This may take a few minutes..."
pip install -r requirements.txt

# Install package in editable mode
echo ""
echo "Installing tradingagents package..."
pip install -e . 2>/dev/null || echo "  (editable install skipped — run 'pip install -e .' manually if needed)"

# Create output directory
mkdir -p research_output

# Run verification
echo ""
echo "Running setup verification..."
python3 scripts/verify_setup.py

echo ""
echo "============================================================"
echo "        Setup Complete!"
echo "============================================================"
echo ""
echo "Next steps:"
echo "  1. Edit .env and add your API keys"
echo "  2. Start the web UI:    make ui"
echo "  3. Or run from CLI:     make analyze TICKER=NVDA"
echo "  4. See all commands:    make help"
echo ""
