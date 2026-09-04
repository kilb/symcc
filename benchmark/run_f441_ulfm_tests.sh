#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PREFIX=${OPENMPI_ULFM_PREFIX:-"${HOME}/.local/opt/openmpi-5.0.10-ulfm"}
OUTPUT=${F441_OUTPUT_DIR:-"${ROOT}/.symcc-research-evidence/f441-ulfm-current"}
RANKS=${F441_MPI_RANKS:-4}
TIMEOUT=${F441_LIVE_TIMEOUT_SECONDS:-30}

if [[ ! -x "${PREFIX}/bin/mpirun" ]]; then
  printf 'Missing ULFM runtime at %s; run benchmark/install_openmpi_ulfm_5_0_10.sh\n' \
    "${PREFIX}" >&2
  exit 2
fi
if (( RANKS < 3 )); then
  printf 'F441_MPI_RANKS must be at least 3\n' >&2
  exit 2
fi

mkdir -p "${OUTPUT}"
export PATH="${PREFIX}/bin:${PATH}"
export LD_LIBRARY_PATH="${PREFIX}/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
ORACLE="${ROOT}/benchmark/check_mpi_ulfm_recovery_oracles.py"

python3 "${ORACLE}" --mode deterministic \
  --output "${OUTPUT}/deterministic.json"
python3 "${ORACLE}" --verify "${OUTPUT}/deterministic.json"

"${PREFIX}/bin/mpirun" --allow-run-as-root --with-ft ulfm \
  -n "${RANKS}" python3 "${ORACLE}" --mode capability \
  --output "${OUTPUT}/capability.json"
python3 "${ORACLE}" --verify "${OUTPUT}/capability.json"
python3 -c \
  'import json,sys; assert json.load(open(sys.argv[1]))["all_ranks_available"]' \
  "${OUTPUT}/capability.json"

set +e
F441_LIVE_TRACE=1 timeout --signal=TERM --kill-after=5s "${TIMEOUT}" \
  "${PREFIX}/bin/mpirun" --allow-run-as-root --with-ft ulfm \
  --mca pml ob1 --mca btl tcp,self \
  --prtemca errmgr_detector_heartbeat_period 0.1 \
  --prtemca errmgr_detector_heartbeat_timeout 1.0 \
  -n "${RANKS}" python3 "${ORACLE}" --mode live-failure \
  --output "${OUTPUT}/live-failure.json" \
  >"${OUTPUT}/live-failure.log" 2>&1
LIVE_EXIT=$?
set -e

# The intentionally terminated last rank exits with 86.  Recovery is proven by
# the independently verified survivor artifact, not by coercing mpirun to zero.
if [[ "${LIVE_EXIT}" -ne 86 ]]; then
  printf 'Unexpected F441 live-failure launcher exit: %s\n' "${LIVE_EXIT}" >&2
  exit 3
fi
python3 "${ORACLE}" --verify "${OUTPUT}/live-failure.json"
printf '%s\n' "${LIVE_EXIT}" >"${OUTPUT}/live-failure-launcher-exit.txt"

sha256sum \
  "${OUTPUT}/deterministic.json" \
  "${OUTPUT}/capability.json" \
  "${OUTPUT}/live-failure.json" \
  "${OUTPUT}/live-failure.log" \
  "${OUTPUT}/live-failure-launcher-exit.txt" \
  >"${OUTPUT}/SHA256SUMS.txt"
printf 'F441 ULFM protocol, capability, and live-failure gates passed: %s\n' \
  "${OUTPUT}"

