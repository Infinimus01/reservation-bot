#!/bin/bash
# Run the old FlareSolverr-based availability checker locally (one scan only)
cd "$(dirname "$0")"

FLARESOLVERR_URLS=http://164.90.229.229:8191/v1 \
MASTER_REQUIRE_AVAILABILITY_TRIGGER=false \
venv/bin/python -m master.availability_checker --once
