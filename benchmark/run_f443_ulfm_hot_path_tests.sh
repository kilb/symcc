#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PREFIX=${OPENMPI_ULFM_PREFIX:-"${HOME}/.local/opt/openmpi-5.0.10-ulfm"}
RUN_ID=${F443_RUN_ID:-"$(date -u +%Y%m%dT%H%M%SZ)-$$"}
OUTPUT=${F443_OUTPUT_DIR:-"${ROOT}/.symcc-research-evidence/f443-ulfm-hot-path-${RUN_ID}"}
WALL_SECONDS=${F443_WALL_SECONDS:-5}
LIVE_TIMEOUT=${F443_LIVE_TIMEOUT_SECONDS:-30}
DRIVER="${ROOT}/util/mpi_concolic_execution.py"
ORACLE="${ROOT}/benchmark/check_f443_ulfm_hot_path_oracles.py"

if [[ ! -x "${PREFIX}/bin/mpirun" ]]; then
  printf 'Missing ULFM runtime at %s; run benchmark/install_openmpi_ulfm_5_0_10.sh\n' \
    "${PREFIX}" >&2
  exit 2
fi
if [[ -e "${OUTPUT}" ]]; then
  printf 'F443 output already exists: %s\n' "${OUTPUT}" >&2
  exit 2
fi
mkdir -p "${OUTPUT}"
export PATH="${PREFIX}/bin:${PATH}"
export LD_LIBRARY_PATH="${PREFIX}/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
export PYTHONPATH="${ROOT}/util${PYTHONPATH:+:${PYTHONPATH}}"

run_case() {
  local label=$1
  local ranks=$2
  local failed_rank=$3
  local case_root="${OUTPUT}/${label}"
  mkdir -p "${case_root}/input" "${case_root}/corpus"
  printf 'seed-%s-a' "${label}" >"${case_root}/input/a"
  printf 'seed-%s-b' "${label}" >"${case_root}/input/b"

  set +e
  SYMCC_ULFM_HOT_PATH=1 \
  SYMCC_ULFM_TEST_FAIL_INITIAL_RANK="${failed_rank}" \
  SYMCC_ULFM_FAILURE_POLL_INTERVAL=0.05 \
  SYMCC_SHUTDOWN_GRACE_SEC=15 \
  SYMCC_FINALIZE_GRACE_SEC=15 \
  timeout --signal=TERM --kill-after=5s "${LIVE_TIMEOUT}" \
    "${PREFIX}/bin/mpirun" --allow-run-as-root --with-ft ulfm \
    --mca pml ob1 --mca btl tcp,self \
    --prtemca errmgr_detector_heartbeat_period 0.1 \
    --prtemca errmgr_detector_heartbeat_timeout 1.0 \
    -n "${ranks}" python3 "${DRIVER}" \
    -i "${case_root}/input" -o "${case_root}/corpus" \
    -t 1 --max-idle 2 --wall-timeout "${WALL_SECONDS}" \
    --simulate -- /bin/true @@ >"${case_root}/campaign.log" 2>&1
  local launcher_exit=$?
  set -e
  if [[ "${launcher_exit}" -ne 86 ]]; then
    printf 'Unexpected %s launcher exit: %s\n' \
      "${label}" "${launcher_exit}" >&2
    exit 3
  fi
  printf '%s\n' "${launcher_exit}" >"${case_root}/launcher-exit.txt"
  python3 "${ORACLE}" \
    --campaign-output "${case_root}/corpus" \
    --log "${case_root}/campaign.log" \
    --expected-failed-rank "${failed_rank}" \
    --output "${case_root}/oracle.json"
  python3 "${ORACLE}" --verify "${case_root}/oracle.json" \
    >"${case_root}/verified.json"
}

run_case worker-failure 3 1
run_case master-failure 4 0

find "${OUTPUT}" -type f ! -name SHA256SUMS.txt -print0 \
  | sort -z | xargs -0 sha256sum \
  >"${OUTPUT}/SHA256SUMS.txt"
printf 'F443 production ULFM worker/master failure gates passed: %s\n' "${OUTPUT}"
