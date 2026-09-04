#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PREFIX=${OPENMPI_ULFM_PREFIX:-"${HOME}/.local/opt/openmpi-5.0.10-ulfm"}
PYTHON=${F446_PYTHON:-"$(command -v python3)"}
RUN_ID=${F446_RUN_ID:-"$(date -u +%Y%m%dT%H%M%SZ)-$$"}
OUTPUT=${F446_OUTPUT_DIR:-"${ROOT}/.symcc-research-evidence/f446-elastic-ulfm-${RUN_ID}"}
WALL_SECONDS=${F446_WALL_SECONDS:-6}
LIVE_TIMEOUT=${F446_LIVE_TIMEOUT_SECONDS:-60}
DRIVER="${ROOT}/util/mpi_concolic_execution.py"
ORACLE="${ROOT}/benchmark/check_f446_elastic_ulfm_oracles.py"

if [[ ! -x "${PREFIX}/bin/mpirun" ]]; then
  printf 'Missing ULFM runtime at %s\n' "${PREFIX}" >&2
  exit 2
fi
if [[ ! -x "${PYTHON}" ]]; then
  printf 'Missing ULFM-qualified Python at %s\n' "${PYTHON}" >&2
  exit 2
fi
if [[ -e "${OUTPUT}" ]]; then
  printf 'F446 output already exists: %s\n' "${OUTPUT}" >&2
  exit 2
fi
mkdir -p "${OUTPUT}"
export PATH="${PREFIX}/bin:${PATH}"
export LD_LIBRARY_PATH="${PREFIX}/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
export PYTHONPATH="${ROOT}/util${PYTHONPATH:+:${PYTHONPATH}}"

run_case() {
  local label=$1
  local ranks=$2
  local workers_per_master=$3
  local warm_spares=$4
  local failure_schedule=$5
  local failed_ranks=$6
  local case_root="${OUTPUT}/${label}"
  mkdir -p "${case_root}/input" "${case_root}/corpus"
  printf 'seed-%s-a' "${label}" >"${case_root}/input/a"
  printf 'seed-%s-b' "${label}" >"${case_root}/input/b"

  set +e
  SYMCC_ULFM_HOT_PATH=1 \
  SYMCC_ULFM_WARM_SPARES="${warm_spares}" \
  SYMCC_ULFM_TEST_FAILURE_SCHEDULE="${failure_schedule}" \
  SYMCC_ULFM_FAILURE_POLL_INTERVAL=0.05 \
  SYMCC_SHUTDOWN_GRACE_SEC=15 \
  SYMCC_FINALIZE_GRACE_SEC=15 \
  timeout --signal=TERM --kill-after=10s "${LIVE_TIMEOUT}" \
    "${PREFIX}/bin/mpirun" --allow-run-as-root --with-ft ulfm \
    --mca pml ob1 --mca btl tcp,self \
    --prtemca errmgr_detector_heartbeat_period 0.1 \
    --prtemca errmgr_detector_heartbeat_timeout 1.0 \
    -n "${ranks}" "${PYTHON}" "${DRIVER}" \
    -i "${case_root}/input" -o "${case_root}/corpus" \
    -t 1 --max-idle 2 --wall-timeout "${WALL_SECONDS}" \
    --workers-per-master "${workers_per_master}" \
    --simulate -- /bin/true @@ >"${case_root}/campaign.log" 2>&1
  local launcher_exit=$?
  set -e
  if [[ "${launcher_exit}" -ne 86 ]]; then
    printf 'Unexpected %s launcher exit: %s\n' \
      "${label}" "${launcher_exit}" >&2
    exit 3
  fi
  printf '%s\n' "${launcher_exit}" >"${case_root}/launcher-exit.txt"
  "${PYTHON}" "${ORACLE}" \
    --campaign-output "${case_root}/corpus" \
    --log "${case_root}/campaign.log" \
    --failed-ranks "${failed_ranks}" \
    --warm-spares "${warm_spares}" \
    --minimum-hosts 1 \
    --output "${case_root}/oracle.json"
  "${PYTHON}" "${ORACLE}" --verify "${case_root}/oracle.json" \
    >"${case_root}/verified.json"
}

run_case multi-worker-failure 8 3 0 0:2 2
run_case multi-root-failure 8 3 0 0:0 0
run_case warm-spare-promotion 10 3 2 0:2 2
run_case continuous-failure 10 3 2 0:2,1:3 2,3

find "${OUTPUT}" -type f ! -name SHA256SUMS.txt -print0 \
  | sort -z | xargs -0 sha256sum >"${OUTPUT}/SHA256SUMS.txt"
printf 'F446 elastic ULFM gates passed: %s\n' "${OUTPUT}"
