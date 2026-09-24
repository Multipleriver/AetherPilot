#!/usr/bin/env bash
set -euo pipefail

project_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$project_root"
export PYTHONDONTWRITEBYTECODE=1
exec "${PYTHON:-python3}" -m unittest tests.test_backend tests.test_public_surface tests.test_parameters tests.test_results tests.test_signals
