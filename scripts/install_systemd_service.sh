#!/usr/bin/env bash
set -Eeuo pipefail

readonly DEFAULT_SERVICE_NAME="chronoshft"
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly ROOT_DIR
readonly TEMPLATE_PATH="${ROOT_DIR}/deploy/systemd/chronoshft.service.in"
readonly PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"

RUN_USER=""
SERVICE_NAME="${DEFAULT_SERVICE_NAME}"
CONFIG_PATH="${ROOT_DIR}/config.json"
START_SERVICE=false
UNIT_TMP=""

usage() {
    cat <<'EOF'
Usage: sudo bash scripts/install_systemd_service.sh [options]

Install and enable the ChronosHFT Paper systemd service.

Options:
  --user USER          Service account (defaults to the non-root SUDO_USER)
  --service-name NAME  Unit basename (default: chronoshft)
  --config PATH        Paper manifest (default: repository config.json)
  --start              Start the service, or restart it if already active
  -h, --help           Show this help
EOF
}

die() {
    printf 'systemd install error: %s\n' "$*" >&2
    exit 1
}

cleanup() {
    if [[ -n "${UNIT_TMP}" && -f "${UNIT_TMP}" ]]; then
        rm -f -- "${UNIT_TMP}"
    fi
}
trap cleanup EXIT

require_value() {
    local option="$1"
    local value="${2:-}"
    [[ -n "${value}" ]] || die "${option} requires a value"
}

validate_template_value() {
    local label="$1"
    local value="$2"
    case "${value}" in
        *$'\n'*|*$'\r'*|*'"'*|*'\'*|*'%'*|*'$'*)
            die "${label} contains characters unsafe for a systemd unit"
            ;;
        *@USER@*|*@GROUP@*|*@ROOT_DIR@*|*@PYTHON_BIN@*|*@CONFIG_PATH@*)
            die "${label} contains a reserved template token"
            ;;
    esac
}

render_unit() {
    local rendered
    rendered="$(<"${TEMPLATE_PATH}")"
    shopt -u patsub_replacement 2>/dev/null || true
    rendered="${rendered//@USER@/${RUN_USER}}"
    rendered="${rendered//@GROUP@/${RUN_GROUP}}"
    rendered="${rendered//@ROOT_DIR@/${ROOT_DIR}}"
    rendered="${rendered//@PYTHON_BIN@/${PYTHON_BIN}}"
    rendered="${rendered//@CONFIG_PATH@/${CONFIG_PATH}}"
    printf '%s\n' "${rendered}" > "${UNIT_TMP}"
}

effective_property() {
    local property="$1"
    systemctl show "${UNIT_NAME}" --property="${property}" --value
}

verify_effective_unit() {
    local watchdog_usec tasks_max memory_high memory_max memory_swap_max
    watchdog_usec="$(effective_property WatchdogUSec)"
    tasks_max="$(effective_property TasksMax)"
    memory_high="$(effective_property MemoryHigh)"
    memory_max="$(effective_property MemoryMax)"
    memory_swap_max="$(effective_property MemorySwapMax)"

    case "${watchdog_usec}" in
        ""|0|0s|infinity)
            die "systemd did not enable the configured service watchdog"
            ;;
    esac
    [[ "${tasks_max}" == "128" ]] \
        || die "effective TasksMax is ${tasks_max:-missing}, expected 128"
    [[ "${memory_high}" == "1468006400" ]] \
        || die "effective MemoryHigh is ${memory_high:-missing}, expected 1400M"
    [[ "${memory_max}" == "1677721600" ]] \
        || die "effective MemoryMax is ${memory_max:-missing}, expected 1600M"
    [[ "${memory_swap_max}" == "0" ]] \
        || die "effective MemorySwapMax is ${memory_swap_max:-missing}, expected 0"
}

find_project_python_pids() {
    local proc_dir proc_uid proc_exe proc_cwd arg
    local -a argv

    for proc_dir in /proc/[0-9]*; do
        [[ -r "${proc_dir}/cmdline" ]] || continue
        proc_uid="$(stat -c '%u' "${proc_dir}" 2>/dev/null || true)"
        [[ "${proc_uid}" == "${RUN_UID}" ]] || continue

        argv=()
        mapfile -d '' -t argv < "${proc_dir}/cmdline" 2>/dev/null || true
        [[ ${#argv[@]} -gt 0 ]] || continue
        proc_exe="$(readlink -f "${proc_dir}/exe" 2>/dev/null || true)"
        proc_cwd="$(readlink -f "${proc_dir}/cwd" 2>/dev/null || true)"
        if [[ "${proc_exe}" == "${PYTHON_REAL}" && "${proc_cwd}" == "${ROOT_DIR}" ]]; then
            printf '%s ' "${proc_dir##*/}"
            continue
        fi
        for arg in "${argv[@]}"; do
            case "${arg}" in
                "${ROOT_DIR}/main.py"|"${ROOT_DIR}/launcher.py")
                    printf '%s ' "${proc_dir##*/}"
                    break
                    ;;
            esac
        done
    done
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)
            require_value "$1" "${2:-}"
            RUN_USER="$2"
            shift 2
            ;;
        --service-name)
            require_value "$1" "${2:-}"
            SERVICE_NAME="$2"
            shift 2
            ;;
        --config)
            require_value "$1" "${2:-}"
            CONFIG_PATH="$2"
            shift 2
            ;;
        --start)
            START_SERVICE=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
done

[[ "$(uname -s)" == "Linux" ]] || die "this installer supports Linux only"
[[ "${EUID}" -eq 0 ]] || die "run this installer with sudo"
command -v systemctl >/dev/null 2>&1 || die "systemctl is not installed"
command -v systemd-analyze >/dev/null 2>&1 || die "systemd-analyze is not installed"
command -v runuser >/dev/null 2>&1 || die "runuser is not installed"
command -v realpath >/dev/null 2>&1 || die "realpath is not installed"
command -v readlink >/dev/null 2>&1 || die "readlink is not installed"
[[ -d /run/systemd/system ]] || die "systemd is not the active service manager"

if [[ -z "${RUN_USER}" ]]; then
    if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
        RUN_USER="${SUDO_USER}"
    else
        die "--user is required when the installer is run directly as root"
    fi
fi

[[ "${SERVICE_NAME}" =~ ^[A-Za-z0-9][A-Za-z0-9_.@-]*$ ]] \
    || die "invalid service name: ${SERVICE_NAME}"
[[ "${SERVICE_NAME}" != *.service ]] \
    || die "pass a unit basename without the .service suffix"
id "${RUN_USER}" >/dev/null 2>&1 || die "service user does not exist: ${RUN_USER}"
RUN_GROUP="$(id -gn "${RUN_USER}")"
RUN_UID="$(id -u "${RUN_USER}")"
readonly RUN_USER RUN_GROUP RUN_UID

[[ -f "${TEMPLATE_PATH}" ]] || die "unit template not found: ${TEMPLATE_PATH}"
[[ -f "${ROOT_DIR}/main.py" ]] || die "main.py not found under ${ROOT_DIR}"
[[ -x "${PYTHON_BIN}" ]] \
    || die "${PYTHON_BIN} is missing; run bash scripts/bootstrap_aws.sh first"
PYTHON_REAL="$(realpath -e -- "${PYTHON_BIN}")"
readonly PYTHON_REAL

if [[ "${CONFIG_PATH}" != /* ]]; then
    CONFIG_PATH="$(pwd -P)/${CONFIG_PATH}"
fi
[[ -f "${CONFIG_PATH}" ]] || die "configuration not found: ${CONFIG_PATH}"
CONFIG_PATH="$(realpath -e -- "${CONFIG_PATH}")"
readonly CONFIG_PATH

validate_template_value "service user" "${RUN_USER}"
validate_template_value "service group" "${RUN_GROUP}"
validate_template_value "repository path" "${ROOT_DIR}"
validate_template_value "Python path" "${PYTHON_BIN}"
validate_template_value "configuration path" "${CONFIG_PATH}"

runuser -u "${RUN_USER}" -- test -r "${ROOT_DIR}/main.py" \
    || die "${RUN_USER} cannot read main.py"
runuser -u "${RUN_USER}" -- test -x "${PYTHON_BIN}" \
    || die "${RUN_USER} cannot execute ${PYTHON_BIN}"
runuser -u "${RUN_USER}" -- test -r "${CONFIG_PATH}" \
    || die "${RUN_USER} cannot read ${CONFIG_PATH}"

CONFIG_CHECK_OUTPUT=""
if ! CONFIG_CHECK_OUTPUT="$(
    runuser -u "${RUN_USER}" -- env \
        PYTHONUNBUFFERED=1 \
        PYTHONDONTWRITEBYTECODE=1 \
        "${PYTHON_BIN}" "${ROOT_DIR}/main.py" \
        --config "${CONFIG_PATH}" --check-config 2>&1
)"; then
    printf '%s\n' "${CONFIG_CHECK_OUTPUT}" >&2
    die "offline configuration check failed"
fi
printf '%s\n' "${CONFIG_CHECK_OUTPUT}"
case "${CONFIG_CHECK_OUTPUT}" in
    *"CONFIG_OK mode=paper "*) ;;
    *) die "this service installer accepts Paper configuration only" ;;
esac

install -d -m 0700 -o "${RUN_USER}" -g "${RUN_GROUP}" \
    "${ROOT_DIR}/logs" "${ROOT_DIR}/storage" "${ROOT_DIR}/storage/paper"
runuser -u "${RUN_USER}" -- test -w "${ROOT_DIR}/logs" \
    || die "${RUN_USER} cannot write ${ROOT_DIR}/logs"
runuser -u "${RUN_USER}" -- test -w "${ROOT_DIR}/storage" \
    || die "${RUN_USER} cannot write ${ROOT_DIR}/storage"
runuser -u "${RUN_USER}" -- test -w "${ROOT_DIR}/storage/paper" \
    || die "${RUN_USER} cannot write ${ROOT_DIR}/storage/paper"

UNIT_NAME="${SERVICE_NAME}.service"
UNIT_PATH="/etc/systemd/system/${UNIT_NAME}"
readonly UNIT_NAME UNIT_PATH
UNIT_TMP="$(mktemp --tmpdir "${SERVICE_NAME}.XXXXXX.service")"
render_unit
systemd-analyze verify "${UNIT_TMP}"
install -m 0644 -o root -g root "${UNIT_TMP}" "${UNIT_PATH}"
systemctl daemon-reload
systemctl enable "${UNIT_NAME}"

if [[ "${START_SERVICE}" == true ]]; then
    CURRENT_STATE="$(systemctl is-active "${UNIT_NAME}" 2>/dev/null || true)"
    case "${CURRENT_STATE}" in
        active|activating|reloading)
            systemctl restart "${UNIT_NAME}"
            ;;
        *)
            EXISTING_PIDS="$(find_project_python_pids)"
            if [[ -n "${EXISTING_PIDS}" ]]; then
                die "project Python processes are already running (PIDs: ${EXISTING_PIDS}); inspect them before starting systemd"
            fi
            systemctl start "${UNIT_NAME}"
            ;;
    esac
    if ! systemctl is-active --quiet "${UNIT_NAME}"; then
        journalctl -u "${UNIT_NAME}" -n 50 --no-pager >&2 || true
        die "${UNIT_NAME} did not remain active"
    fi
    verify_effective_unit
    printf '\n%s is installed, enabled, and active.\n' "${UNIT_NAME}"
else
    printf '\n%s is installed and enabled but was not started.\n' "${UNIT_NAME}"
    if systemctl is-active --quiet "${UNIT_NAME}"; then
        printf 'The running service was not restarted; use systemctl restart %s to apply the new unit.\n' "${UNIT_NAME}"
        printf 'Effective runtime limits were not checked because the old process is still active.\n'
    else
        verify_effective_unit
    fi
fi

printf 'Status:  sudo systemctl status %s\n' "${SERVICE_NAME}"
printf 'Logs:    sudo journalctl -u %s -f\n' "${SERVICE_NAME}"
printf 'Start:   sudo systemctl start %s\n' "${SERVICE_NAME}"
printf 'Stop:    sudo systemctl stop %s\n' "${SERVICE_NAME}"
printf 'Restart: sudo systemctl restart %s\n' "${SERVICE_NAME}"
