#!/usr/bin/env bash
set -Eeuo pipefail

readonly UV_VERSION="0.11.8"
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly ROOT_DIR
PYTHON_VERSION="$(tr -d '[:space:]' < "${ROOT_DIR}/.python-version")"
readonly PYTHON_VERSION
readonly TOOLS_DIR="${ROOT_DIR}/.tools"
readonly DEFAULT_UV_BIN="${TOOLS_DIR}/uv"

die() {
    printf 'bootstrap error: %s\n' "$*" >&2
    exit 1
}

download_uv_installer() {
    local destination="$1"
    local url="https://astral.sh/uv/${UV_VERSION}/install.sh"

    if command -v curl >/dev/null 2>&1; then
        curl --fail --location --silent --show-error "${url}" --output "${destination}"
    elif command -v wget >/dev/null 2>&1; then
        wget --quiet "${url}" --output-document="${destination}"
    else
        die "curl or wget is required to install uv"
    fi
}

install_uv() {
    local installer
    installer="$(mktemp)"
    download_uv_installer "${installer}"
    UV_INSTALL_DIR="${TOOLS_DIR}" UV_NO_MODIFY_PATH=1 sh "${installer}"
    rm -f "${installer}"
}

main() {
    cd "${ROOT_DIR}"
    mkdir -p "${TOOLS_DIR}" logs storage/paper
    export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-${TOOLS_DIR}/python}"
    export UV_PYTHON_BIN_DIR="${UV_PYTHON_BIN_DIR:-${TOOLS_DIR}/bin}"
    export UV_CACHE_DIR="${UV_CACHE_DIR:-${TOOLS_DIR}/cache}"

    local uv_bin="${CHRONOSHFT_UV_BIN:-${DEFAULT_UV_BIN}}"
    if [[ ! -x "${uv_bin}" ]]; then
        if [[ -n "${CHRONOSHFT_UV_BIN:-}" ]]; then
            die "CHRONOSHFT_UV_BIN is not executable: ${uv_bin}"
        fi
        printf 'Installing uv %s into %s\n' "${UV_VERSION}" "${TOOLS_DIR}"
        install_uv
    fi

    "${uv_bin}" python install "${PYTHON_VERSION}"
    "${uv_bin}" sync --frozen --no-dev --python "${PYTHON_VERSION}"

    local python_bin="${UV_PROJECT_ENVIRONMENT:-${ROOT_DIR}/.venv}/bin/python"
    [[ -x "${python_bin}" ]] || die "virtual environment was not created: ${python_bin}"
    "${python_bin}" main.py --config config.json --check-config

    printf '\nAWS Paper environment is ready.\n'
    printf 'Install service: sudo bash scripts/install_systemd_service.sh --start\n'
    printf 'Interactive fallback: %s launcher.py\n' "${python_bin}"
    printf 'Dashboard: http://127.0.0.1:8765/ (use an SSH tunnel)\n'
}

main "$@"
