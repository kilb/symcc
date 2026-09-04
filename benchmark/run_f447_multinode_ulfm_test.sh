#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PREFIX=${OPENMPI_ULFM_PREFIX:-"${HOME}/.local/opt/openmpi-5.0.10-ulfm"}
PYTHON=${F447_PYTHON:-"$(command -v python3)"}
HOST_SPEC=${F447_HOST_SPEC:?Set F447_HOST_SPEC, for example node-a:1,node-b:9}
RUN_ID=${F447_RUN_ID:-"$(date -u +%Y%m%dT%H%M%SZ)-$$"}
OUTPUT=${F447_OUTPUT_DIR:-"${ROOT}/.symcc-research-evidence/f447-multinode-ulfm-${RUN_ID}"}
SHARED_UMASK=${F447_SHARED_UMASK:-}
CORPUS_FILE_MODE=${F447_CORPUS_FILE_MODE:-}
WALL_SECONDS=${F447_WALL_SECONDS:-8}
LIVE_TIMEOUT=${F447_LIVE_TIMEOUT_SECONDS:-90}
MINIMUM_HOSTS=${F447_MINIMUM_HOSTS:-2}
RANKS=${F447_RANKS:-10}
WORKERS_PER_MASTER=${F447_WORKERS_PER_MASTER:-3}
WARM_SPARES=${F447_WARM_SPARES:-2}
FAILURE_SCHEDULE=${F447_FAILURE_SCHEDULE:-"0:2,1:3"}
FAILED_RANKS=${F447_FAILED_RANKS:-"2,3"}
FAILURE_POLL_INTERVAL=${F447_FAILURE_POLL_INTERVAL:-0.05}
FS_PROBE=${F447_FS_PROBE:-1}
FS_PROBE_TIMEOUT=${F447_FS_PROBE_TIMEOUT:-10}
CLUSTER_FS_PROBE_TIMEOUT=${F447_CLUSTER_FS_PROBE_TIMEOUT:-180}
SHUTDOWN_GRACE=${F447_SHUTDOWN_GRACE_SECONDS:-${CLUSTER_FS_PROBE_TIMEOUT}}
FINALIZE_GRACE=${F447_FINALIZE_GRACE_SECONDS:-${CLUSTER_FS_PROBE_TIMEOUT}}
BTL_IF_EXCLUDE=${F447_BTL_IF_EXCLUDE:-"127.0.0.1/8,sppp"}
BTL_PORT_MIN=${F447_BTL_PORT_MIN:-43100}
BTL_PORT_RANGE=${F447_BTL_PORT_RANGE:-21}
OOB_PORTS=${F447_OOB_PORTS:-"43000-43020"}
MAP_BY=${F447_MAP_BY:-slot}
# A posted receive is the portable failure-notification operation for each
# owned worker. Open MPI's same-host shared-memory BTL can leave that receive
# pending after an abrupt peer exit, so the resilience gate deliberately uses
# TCP for local and remote ranks alike.
BTL_COMPONENTS=${F447_BTL_COMPONENTS:-"tcp,self"}
# MPI_Comm_shrink performs an agreement over the revoked communicator.  The
# ULFM-aware ftagree component must remain selected; basic agreement is not
# fault tolerant once a cross-node peer has disappeared.
COLL_COMPONENTS=${F447_COLL_COMPONENTS:-"ftagree,basic,libnbc,self"}
DRIVER="${ROOT}/util/mpi_concolic_execution.py"
ORACLE="${ROOT}/benchmark/check_f446_elastic_ulfm_oracles.py"

if [[ ! -x "${PREFIX}/bin/mpirun" || ! -x "${PYTHON}" ]]; then
  printf 'F447 requires an executable ULFM prefix and Python runtime\n' >&2
  exit 2
fi
if [[ -e "${OUTPUT}" ]]; then
  printf 'F447 output already exists: %s\n' "${OUTPUT}" >&2
  exit 2
fi
if [[ -n "${SHARED_UMASK}" ]]; then
  if [[ ! "${SHARED_UMASK}" =~ ^[0-7]{3,4}$ ]]; then
    printf 'F447_SHARED_UMASK must be a three- or four-digit octal mask\n' >&2
    exit 2
  fi
  # Test-only network shares may map clients to different server identities.
  # Let the operator opt into a campaign-wide creation mask without weakening
  # the framework's production defaults.
  umask "${SHARED_UMASK}"
fi

mkdir -p "${OUTPUT}/input" "${OUTPUT}/corpus"
printf 'f447-multinode-seed-a' >"${OUTPUT}/input/a"
printf 'f447-multinode-seed-b' >"${OUTPUT}/input/b"
export PATH="${PREFIX}/bin:${PATH}"
export LD_LIBRARY_PATH="${PREFIX}/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
export PYTHONPATH="${ROOT}/util${PYTHONPATH:+:${PYTHONPATH}}"

MPI_ARGS=(
  --with-ft ulfm
  --prefix "${PREFIX}"
  --wdir "${ROOT}"
  --host "${HOST_SPEC}"
  --map-by "${MAP_BY}"
  --oversubscribe
  --prtemca oob_tcp_dynamic_ipv4_ports "${OOB_PORTS}"
  --prtemca errmgr_detector_heartbeat_period 0.1
  --prtemca errmgr_detector_heartbeat_timeout 1.0
  --mca pml ob1
  --mca btl "${BTL_COMPONENTS}"
  --mca coll "${COLL_COMPONENTS}"
  --mca btl_tcp_if_exclude "${BTL_IF_EXCLUDE}"
  --mca btl_tcp_port_min_v4 "${BTL_PORT_MIN}"
  --mca btl_tcp_port_range_v4 "${BTL_PORT_RANGE}"
  -x "PYTHONPATH=${PYTHONPATH}"
  -x "SYMCC_ULFM_HOT_PATH=1"
  -x "SYMCC_ULFM_WARM_SPARES=${WARM_SPARES}"
  -x "SYMCC_ULFM_TEST_FAILURE_SCHEDULE=${FAILURE_SCHEDULE}"
  -x "SYMCC_ULFM_FAILURE_POLL_INTERVAL=${FAILURE_POLL_INTERVAL}"
  -x "SYMCC_SHARED_STATE_FS_PROBE=${FS_PROBE}"
  -x "SYMCC_SHARED_STATE_FS_PROBE_TIMEOUT=${FS_PROBE_TIMEOUT}"
  -x "SYMCC_SHARED_STATE_CLUSTER_PROBE_TIMEOUT=${CLUSTER_FS_PROBE_TIMEOUT}"
  -x "SYMCC_SHUTDOWN_GRACE_SEC=${SHUTDOWN_GRACE}"
  -x "SYMCC_FINALIZE_GRACE_SEC=${FINALIZE_GRACE}"
)
if [[ -n "${SYMCC_MPI_STARTUP_TRACE:-}" ]]; then
  MPI_ARGS+=(-x "SYMCC_MPI_STARTUP_TRACE=${SYMCC_MPI_STARTUP_TRACE}")
fi
if [[ -n "${F447_LD_PRELOAD:-}" ]]; then
  MPI_ARGS+=(
    -x "LD_PRELOAD=${F447_LD_PRELOAD}"
    -x "SYMCC_MPI_NAT_SOURCE_MAP=${F447_NAT_SOURCE_MAP:?}"
    -x "SYMCC_MPI_NAT_INTERFACE_MAP=${F447_NAT_INTERFACE_MAP:?}"
  )
  if [[ -n "${F447_NAT_LOCAL_SOURCE:-}" ]]; then
    MPI_ARGS+=(
      -x "SYMCC_MPI_NAT_LOCAL_SOURCE=${F447_NAT_LOCAL_SOURCE}"
    )
  fi
fi
if [[ -n "${F447_FLOCK_PROXY_ADDR:-}" ]]; then
  MPI_ARGS+=(
    -x "SYMCC_FLOCK_PROXY_ADDR=${F447_FLOCK_PROXY_ADDR}"
    -x "SYMCC_FLOCK_PROXY_PREFIX=${F447_FLOCK_PROXY_PREFIX:?}"
  )
fi
if [[ -n "${F447_SNAPSHOT_FILE_MODE:-}" ]]; then
  MPI_ARGS+=(
    -x "SYMCC_ULFM_SNAPSHOT_FILE_MODE=${F447_SNAPSHOT_FILE_MODE}"
  )
fi
if [[ -n "${CORPUS_FILE_MODE}" ]]; then
  if [[ ! "${CORPUS_FILE_MODE}" =~ ^[0-7]{3,4}$ ]]; then
    printf 'F447_CORPUS_FILE_MODE must be a three- or four-digit octal mode\n' >&2
    exit 2
  fi
  MPI_ARGS+=(
    -x "SYMCC_SHARED_CORPUS_FILE_MODE=${CORPUS_FILE_MODE}"
  )
fi

set +e
SYMCC_ULFM_HOT_PATH=1 \
SYMCC_ULFM_WARM_SPARES="${WARM_SPARES}" \
SYMCC_ULFM_TEST_FAILURE_SCHEDULE="${FAILURE_SCHEDULE}" \
SYMCC_ULFM_FAILURE_POLL_INTERVAL="${FAILURE_POLL_INTERVAL}" \
SYMCC_SHARED_STATE_FS_PROBE="${FS_PROBE}" \
SYMCC_SHARED_STATE_FS_PROBE_TIMEOUT="${FS_PROBE_TIMEOUT}" \
SYMCC_SHARED_STATE_CLUSTER_PROBE_TIMEOUT="${CLUSTER_FS_PROBE_TIMEOUT}" \
SYMCC_SHUTDOWN_GRACE_SEC="${SHUTDOWN_GRACE}" \
SYMCC_FINALIZE_GRACE_SEC="${FINALIZE_GRACE}" \
timeout --signal=TERM --kill-after=10s "${LIVE_TIMEOUT}" \
  "${PREFIX}/bin/mpirun" "${MPI_ARGS[@]}" -n "${RANKS}" \
  "${PYTHON}" "${DRIVER}" \
  -i "${OUTPUT}/input" -o "${OUTPUT}/corpus" \
  -t 1 --max-idle 2 --wall-timeout "${WALL_SECONDS}" \
  --workers-per-master "${WORKERS_PER_MASTER}" \
  --simulate -- /bin/true @@ >"${OUTPUT}/campaign.log" 2>&1
launcher_exit=$?
set -e
if [[ "${launcher_exit}" -ne 86 ]]; then
  printf 'Unexpected F447 launcher exit: %s\n' "${launcher_exit}" >&2
  exit 3
fi
printf '%s\n' "${launcher_exit}" >"${OUTPUT}/launcher-exit.txt"

"${PYTHON}" "${ORACLE}" \
  --campaign-output "${OUTPUT}/corpus" \
  --log "${OUTPUT}/campaign.log" \
  --failed-ranks "${FAILED_RANKS}" \
  --warm-spares "${WARM_SPARES}" \
  --minimum-hosts "${MINIMUM_HOSTS}" \
  --output "${OUTPUT}/oracle.json"
"${PYTHON}" "${ORACLE}" --verify "${OUTPUT}/oracle.json" \
  >"${OUTPUT}/verified.json"

find "${OUTPUT}" -type f ! -name SHA256SUMS.txt -print0 \
  | sort -z | xargs -0 sha256sum >"${OUTPUT}/SHA256SUMS.txt"
printf 'F447 multi-node continuous-failure gate passed: %s\n' "${OUTPUT}"
