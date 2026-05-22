#!/usr/bin/env bash

set -Eeuo pipefail

DEFAULT_REPO_URL="https://github.com/FayaaDev/rakkib.git"
DEFAULT_BRANCH="main"
REPO_URL="${RAKKIB_REPO:-$DEFAULT_REPO_URL}"
BRANCH="${RAKKIB_BRANCH:-$DEFAULT_BRANCH}"
REPO_URL_OVERRIDDEN=0
[[ -n "${RAKKIB_REPO:-}" ]] && REPO_URL_OVERRIDDEN=1
UPDATE_MODE="${RAKKIB_UPDATE_MODE:-skip}"
VENV_INSTALL_IN_PROGRESS=0
PLATFORM=""
PYTHON_CMD="${RAKKIB_PYTHON:-}"

log()  { printf '==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
command_exists() { command -v "$1" >/dev/null 2>&1; }

is_legacy_repo_url() {
  case "$1" in
    https://github.com/FayaaDev/Rakkib|https://github.com/FayaaDev/Rakkib.git|git@github.com:FayaaDev/Rakkib.git|ssh://git@github.com/FayaaDev/Rakkib.git) return 0 ;;
    *) return 1 ;;
  esac
}

run_quiet() {
  local label="$1"
  shift

  local log_file
  log_file="$(mktemp "${TMPDIR:-/tmp}/rakkib-install.XXXXXX")" \
    || die "Failed to create a temporary log file. Check write access to ${TMPDIR:-/tmp} and rerun."

  if "$@" >"$log_file" 2>&1; then
    rm -f "$log_file"
    return 0
  fi

  printf 'ERROR: %s failed.\n' "$label" >&2
  printf 'Log: %s\n' "$log_file" >&2
  cat "$log_file" >&2
  return 1
}

warn_incomplete_venv() {
  local status=$?
  if [[ "${VENV_INSTALL_IN_PROGRESS:-0}" -eq 1 ]]; then
    warn "Setup was interrupted. To retry cleanly, run: rm -rf '${INSTALL_DIR}/.venv' && rerun install.sh"
  fi
  exit "$status"
}

trap warn_incomplete_venv INT TERM ERR

_prompt() {
  local var_name="$1" prompt_text="$2"
  if { true < /dev/tty; } 2>/dev/null; then
    printf '%s' "$prompt_text" > /dev/tty
    IFS= read -r "$var_name" < /dev/tty
  else
    printf '%s' "$prompt_text"
    IFS= read -r "$var_name"
  fi
}

detect_platform() {
  case "$(uname -s 2>/dev/null || true)" in
    Linux) PLATFORM="linux" ;;
    Darwin) PLATFORM="mac" ;;
    *) die "unsupported OS; expected Linux or macOS" ;;
  esac
}

# Pick install directory
SUDO_USER_HOME=""
if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
  if command_exists getent; then
    SUDO_USER_HOME="$(getent passwd "$SUDO_USER" 2>/dev/null | cut -d: -f6 || true)"
  elif [[ -d "/Users/${SUDO_USER}" ]]; then
    SUDO_USER_HOME="/Users/${SUDO_USER}"
  fi
fi

if [[ -z "${RAKKIB_DIR:-}" && -f "pyproject.toml" && -d ".git" ]]; then
  INSTALL_DIR="$(pwd)"
elif [[ -n "${RAKKIB_DIR:-}" ]]; then
  INSTALL_DIR="${RAKKIB_DIR}"
elif [[ "${EUID:-$(id -u)}" -eq 0 && -n "$SUDO_USER_HOME" ]]; then
  INSTALL_DIR="${SUDO_USER_HOME}/Rakkib"
elif [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
  INSTALL_DIR="/opt/rakkib"
else
  INSTALL_DIR="${HOME}/Rakkib"
fi

usage() {
  cat <<'USAGE'
Usage: install.sh [--dir <path>] [--repo <url>] [--branch <name>]

Rakkib installer. Sets up the rakkib command on this machine.

Environment overrides:
  RAKKIB_DIR       target checkout path   (default: $HOME/Rakkib)
  RAKKIB_REPO      git repo URL           (default: https://github.com/FayaaDev/rakkib.git)
                   only FayaaDev/rakkib or FayaaDev/rakkib-dev are accepted
  RAKKIB_BRANCH    git branch             (default: main)
                   must match [A-Za-z0-9_][A-Za-z0-9._/-]*
  RAKKIB_UPDATE_MODE  reset|skip          (default: skip; non-destructive)
                   reset discards local changes before pulling
USAGE
}

parse_args() {
  while [[ "$#" -gt 0 ]]; do
    case "$1" in
      --dir)    [[ "$#" -ge 2 ]] || die "missing value for --dir";    INSTALL_DIR="$2"; shift 2 ;;
      --repo)   [[ "$#" -ge 2 ]] || die "missing value for --repo";   REPO_URL="$2"; REPO_URL_OVERRIDDEN=1; shift 2 ;;
      --branch) [[ "$#" -ge 2 ]] || die "missing value for --branch"; BRANCH="$2";      shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown argument: $1" ;;
    esac
  done
}

# Defense against `RAKKIB_REPO=https://attacker.example/rakkib curl ... | bash`.
# Only the public runtime repo and the private dev repo are accepted.
validate_repo_url() {
  case "$1" in
    https://github.com/FayaaDev/rakkib|https://github.com/FayaaDev/rakkib.git) return 0 ;;
    https://github.com/FayaaDev/rakkib-dev|https://github.com/FayaaDev/rakkib-dev.git) return 0 ;;
    git@github.com:FayaaDev/rakkib.git|git@github.com:FayaaDev/rakkib-dev.git) return 0 ;;
    ssh://git@github.com/FayaaDev/rakkib.git|ssh://git@github.com/FayaaDev/rakkib-dev.git) return 0 ;;
  esac
  die "Refusing to install from untrusted repo: $1. Set RAKKIB_REPO to FayaaDev/rakkib or FayaaDev/rakkib-dev. To install from a fork, git clone manually and run ./install.sh from the checkout."
}

# Defense against `RAKKIB_BRANCH=--upload-pack=evil` style git argument injection.
# Branch must start with an alphanumeric or underscore, then only
# alphanumerics, dot, underscore, slash, or hyphen.
validate_branch() {
  case "$1" in
    "") die "RAKKIB_BRANCH is empty." ;;
    [!A-Za-z0-9_]*) die "RAKKIB_BRANCH must start with [A-Za-z0-9_], got: $1" ;;
    *[!A-Za-z0-9._/-]*) die "RAKKIB_BRANCH may only contain [A-Za-z0-9._/-], got: $1" ;;
  esac
}

confirm_root() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    if [[ ! -t 0 ]]; then
      warn "running Rakkib as root in non-interactive mode; continuing because stdin is not a terminal."
      return
    fi
    local answer
    [[ -e /dev/tty ]] && printf 'WARNING: You are running Rakkib as root.\n' > /dev/tty
    _prompt answer 'Are you sure you want to continue? (y/N) ' || exit 1
    case "$answer" in y|Y) ;; *) exit 1 ;; esac
  fi
}

ensure_tooling() {
  command_exists curl || die "curl is required. Install curl and rerun."
  if [[ "${PLATFORM:-}" == "mac" ]]; then
    ensure_macos_tooling
    return
  fi
  git_usable || die "git is required. Install git and rerun."
}

git_path() {
  command -v git 2>/dev/null || true
}

git_usable() {
  local path
  path="$(git_path)"
  [[ -n "$path" ]] || return 1

  if [[ "${PLATFORM:-}" == "mac" && "$path" == "/usr/bin/git" ]]; then
    command_exists xcode-select || return 1
    xcode-select -p >/dev/null 2>&1 || return 1
  fi

  return 0
}

xcode_clt_installed() {
  command_exists xcode-select || return 1
  xcode-select -p >/dev/null 2>&1
}

select_xcode_command_line_tools() {
  local clt_dir="/Library/Developer/CommandLineTools"
  local waited=0
  while [[ ! -d "$clt_dir" && $waited -lt 60 ]]; do
    sleep 5
    waited=$((waited + 5))
  done
  [[ -d "$clt_dir" ]] || return 1
  run_root xcode-select --switch "$clt_dir"
}

run_root() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

install_xcode_command_line_tools() {
  xcode_clt_installed && return 0
  command_exists softwareupdate || die "softwareupdate is required to install Xcode Command Line Tools on macOS."

  local marker label
  marker="/tmp/.com.apple.dt.CommandLineTools.installondemand.in-progress"
  touch "$marker" || die "Failed to request Xcode Command Line Tools install. Check write access to /tmp and rerun."
  label="$(softwareupdate -l 2>/dev/null | awk -F': ' '/Label: Command Line Tools/ {print $2}' | tail -n 1 || true)"

  if [[ -z "$label" ]]; then
    rm -f "$marker"
    die "Xcode Command Line Tools are required, but macOS did not expose an install package via softwareupdate. Run 'xcode-select --install', complete the Apple installer, then rerun."
  fi

  log "Installing Xcode Command Line Tools..."
  if ! run_quiet "Installing Xcode Command Line Tools" run_root softwareupdate -i "$label" --verbose; then
    rm -f "$marker"
    die "Xcode Command Line Tools installation failed. Run 'xcode-select --install', complete the Apple installer, then rerun."
  fi
  rm -f "$marker"
  select_xcode_command_line_tools || die "Xcode Command Line Tools installed, but macOS did not create /Library/Developer/CommandLineTools. Run 'xcode-select --install', complete the Apple installer, then rerun."
  xcode_clt_installed || die "Xcode Command Line Tools installed, but xcode-select is still not configured. Run 'sudo xcode-select --reset' and rerun."
}

homebrew_path() {
  if command_exists brew; then
    command -v brew
    return 0
  fi
  if [[ -x /opt/homebrew/bin/brew ]]; then
    printf '%s\n' /opt/homebrew/bin/brew
    return 0
  fi
  if [[ -x /usr/local/bin/brew ]]; then
    printf '%s\n' /usr/local/bin/brew
    return 0
  fi
  return 1
}

load_homebrew_env() {
  local brew_bin
  brew_bin="$(homebrew_path || true)"
  [[ -n "$brew_bin" ]] || return 1
  eval "$("$brew_bin" shellenv)"
}

ensure_homebrew() {
  if load_homebrew_env; then
    return 0
  fi

  log "Installing Homebrew..."
  run_quiet "Installing Homebrew" /bin/bash -lc 'NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"' \
    || die "Homebrew installation failed. Check the log above, then rerun install.sh."
  load_homebrew_env || die "Homebrew installed, but brew is not available on PATH. Open a new terminal and rerun install.sh."
}

ensure_brew_package() {
  local package="$1" binary="$2"
  local force="${3:-0}"
  if [[ "$force" != "1" ]] && command_exists "$binary"; then
    return 0
  fi
  log "Installing ${package} via Homebrew..."
  run_quiet "Installing ${package} via Homebrew" brew install "$package" \
    || die "Failed to install ${package} with Homebrew. Check the log above and rerun."
  command_exists "$binary" || die "Homebrew installed ${package}, but ${binary} is not on PATH. Open a new terminal and rerun."
}

ensure_macos_tooling() {
  install_xcode_command_line_tools
  ensure_homebrew
  if ! git_usable; then
    ensure_brew_package git git 1
  fi
  git_usable || die "Git installed, but it is not usable. Open a new terminal and rerun install.sh."
}

# Block until all dpkg/apt lock files are free.
# On fresh Ubuntu 24.04, unattended-upgrades holds these at first boot.
# flock is always present (util-linux), so this is safe on bare metal.
wait_for_apt_locks() {
  local lock_files=(
    /var/lib/dpkg/lock-frontend
    /var/lib/dpkg/lock
    /var/lib/apt/lists/lock
    /var/cache/apt/archives/lock
  )
  local waited=0
  while true; do
    local busy=0
    for f in "${lock_files[@]}"; do
      if [[ -e "$f" ]] && ! run_root flock -n "$f" true 2>/dev/null; then
        busy=1
        break
      fi
    done
    [[ $busy -eq 0 ]] && return 0
    if [[ $waited -eq 0 ]]; then
      log "Ubuntu automatic updates are running; waiting for apt/dpkg to become available..."
    fi
    sleep 5
    waited=$((waited + 5))
    [[ $waited -ge 900 ]] && die "Timed out after 15 min waiting for apt/dpkg locks. Ubuntu automatic updates or another package manager is still running. Wait for it to finish and rerun install.sh; if it is stuck, run 'sudo systemctl stop unattended-upgrades' and rerun."
  done
}

apt_get() {
  local timeout="${RAKKIB_APT_LOCK_TIMEOUT:-900}"
  run_root env \
    DEBIAN_FRONTEND=noninteractive \
    APT_LISTCHANGES_FRONTEND=none \
    NEEDRESTART_MODE=a \
    NEEDRESTART_SUSPEND=1 \
    UCF_FORCE_CONFFOLD=1 \
    apt-get -o "DPkg::Lock::Timeout=${timeout}" "$@"
}

python_has_venv() {
  local candidate="$1"
  [[ -n "$candidate" ]] || return 1
  "$candidate" -c "import venv, ensurepip" >/dev/null 2>&1
}

select_python_cmd() {
  local candidates=()
  [[ -n "${RAKKIB_PYTHON:-}" ]] && candidates+=("${RAKKIB_PYTHON}")
  if command_exists python3; then
    candidates+=("$(command -v python3)")
  fi
  local candidate
  for candidate in "${candidates[@]}"; do
    if [[ -x "$candidate" ]] && python_has_venv "$candidate"; then
      PYTHON_CMD="$candidate"
      return 0
    fi
  done
  return 1
}

ensure_python_macos() {
  ensure_homebrew
  log "Installing Python via Homebrew..."
  run_quiet "Installing Python via Homebrew" brew install python \
    || die "Failed to install Python with Homebrew. Check the log above and rerun."
  select_python_cmd || die "Homebrew Python installed, but setup cannot use it yet. Open a new terminal and rerun."
}

# Install python3 + python3-venv via the system package manager.
ensure_python3_and_venv() {
  if select_python_cmd; then
    return 0
  fi

  if command_exists apt-get; then
    local need_python need_venv
    need_python=0; need_venv=0
    command_exists python3 || need_python=1
    python3 -c "import venv, ensurepip" 2>/dev/null || need_venv=1
    local pkgs=()
    [[ $need_python -eq 1 ]] && pkgs+=(python3)
    if [[ $need_venv -eq 1 ]]; then
      local pyver
      pyver=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || echo "")
      pkgs+=(python3-venv)
      [[ -n "$pyver" ]] && pkgs+=("python${pyver}-venv")
    fi
    wait_for_apt_locks
    log "Refreshing apt index..."
    run_quiet "Refreshing apt index" apt_get update -qq -o Acquire::Retries=3 \
      || warn "apt-get update failed; continuing with existing index."
    log "Installing ${pkgs[*]} via apt-get..."
    wait_for_apt_locks
    run_quiet "Installing ${pkgs[*]} via apt-get" apt_get install -y -qq --no-install-recommends "${pkgs[@]}" \
      || die "Failed to install ${pkgs[*]}. Run 'sudo apt-get update && sudo apt-get install --no-install-recommends ${pkgs[*]}' and rerun install.sh."
  elif command_exists dnf; then
    local pkgs=()
    command_exists python3 || pkgs+=(python3)
    # venv ships with python3 on Fedora/RHEL
    [[ ${#pkgs[@]} -gt 0 ]] && sudo dnf install -y "${pkgs[@]}"
  elif command_exists pacman; then
    command_exists python3 || sudo pacman -Sy --noconfirm python
  elif [[ "${PLATFORM:-}" == "mac" ]]; then
    ensure_python_macos
  elif command_exists brew; then
    brew install python
  else
    die "Could not find a package manager. Install Python 3 manually and rerun."
  fi

  select_python_cmd || die "Python setup is unavailable. Install Python 3 support and rerun."
}

is_empty_dir() {
  [[ -d "$1" ]] || return 1
  [[ -z "$(ls -A "$1" 2>/dev/null)" ]]
}

repo_has_local_changes() {
  [[ -n "$(git -C "$INSTALL_DIR" status --porcelain 2>/dev/null)" ]]
}

repo_local_changes() {
  git -C "$INSTALL_DIR" status --short 2>/dev/null || true
}

discard_repo_local_changes() {
  git -C "$INSTALL_DIR" reset --hard HEAD
  git -C "$INSTALL_DIR" clean -fd
}

ensure_origin_url() {
  local current_url target_url=""
  current_url="$(git -C "$INSTALL_DIR" remote get-url origin 2>/dev/null || true)"
  [[ -n "$current_url" ]] || return 0

  if [[ "$REPO_URL_OVERRIDDEN" -eq 1 ]]; then
    target_url="$REPO_URL"
  elif is_legacy_repo_url "$current_url"; then
    target_url="$DEFAULT_REPO_URL"
  fi

  [[ -n "$target_url" ]] || return 0
  [[ "$current_url" == "$target_url" ]] && return 0

  log "Setting origin to ${target_url}"
  run_quiet "Setting origin to ${target_url}" git -C "$INSTALL_DIR" remote set-url origin "$target_url" \
    || die "Failed to update git origin to ${target_url}. Resolve the checkout state or set RAKKIB_DIR to a new path."
}

prepare_repo() {
  if [[ -d "${INSTALL_DIR}/.git" ]]; then
    git_usable || die "Existing git checkout at ${INSTALL_DIR} requires usable git for updates. Install Git/Xcode Command Line Tools or set RAKKIB_DIR to a new path."
    log "Using existing checkout: ${INSTALL_DIR}"
    ensure_origin_url
    if repo_has_local_changes; then
      case "$UPDATE_MODE" in
        reset)
          warn "Existing checkout has local changes; discarding them before update because RAKKIB_UPDATE_MODE=reset."
          repo_local_changes >&2
          discard_repo_local_changes
          ;;
        skip)
          warn "Existing checkout has local changes; skipping automatic update because RAKKIB_UPDATE_MODE=skip."
          warn "Set RAKKIB_UPDATE_MODE=reset to discard checkout changes and pull the latest code."
          repo_local_changes >&2
          return 0
          ;;
        *)
          die "invalid RAKKIB_UPDATE_MODE '${UPDATE_MODE}'. Use 'reset' or 'skip'."
          ;;
      esac
    fi
    log "Updating from origin/${BRANCH}"
    run_quiet "Fetching origin/${BRANCH}" git -C "$INSTALL_DIR" fetch origin "$BRANCH" \
      || die "Failed to fetch origin/${BRANCH}. Check git access and rerun install.sh."
    if git -C "$INSTALL_DIR" show-ref --verify --quiet "refs/heads/${BRANCH}"; then
      run_quiet "Switching to ${BRANCH}" git -C "$INSTALL_DIR" switch "$BRANCH" \
        || die "Failed to switch to branch ${BRANCH}. Resolve the checkout state and rerun install.sh."
    else
      run_quiet "Creating branch ${BRANCH}" git -C "$INSTALL_DIR" switch -c "$BRANCH" "origin/${BRANCH}" \
        || die "Failed to create local branch ${BRANCH} from origin/${BRANCH}. Check the repository state and rerun install.sh."
    fi
    case "$UPDATE_MODE" in
      reset)
        run_quiet "Resetting ${BRANCH} to origin/${BRANCH}" git -C "$INSTALL_DIR" reset --hard "origin/${BRANCH}" \
          || die "Failed to reset ${BRANCH} to origin/${BRANCH}. Resolve the checkout state and rerun install.sh."
        ;;
      skip)
        run_quiet "Pulling origin/${BRANCH}" git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH" \
          || die "Failed to fast-forward ${BRANCH} from origin/${BRANCH}. Resolve the checkout state and rerun install.sh."
        ;;
      *)
        die "invalid RAKKIB_UPDATE_MODE '${UPDATE_MODE}'. Use 'reset' or 'skip'."
        ;;
    esac
    return 0
  fi

  if ! git_usable; then
    die "git is required. Install git and rerun."
  fi

  if [[ -e "$INSTALL_DIR" ]] && ! is_empty_dir "$INSTALL_DIR"; then
    die "target path exists and is not an empty git checkout: ${INSTALL_DIR}"
  fi

  mkdir -p "$(dirname "$INSTALL_DIR")"
  log "Cloning ${REPO_URL} into ${INSTALL_DIR}"
  run_quiet "Cloning ${REPO_URL}" git clone --depth 1 --single-branch --no-tags --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR" \
    || die "Failed to clone ${REPO_URL}. Check git access and rerun install.sh."
}

ensure_venv_install() {
  local venv_dir="${INSTALL_DIR}/.venv"
  local bin_dir="${HOME}/.local/bin"
  local target="${bin_dir}/rakkib"
  VENV_INSTALL_IN_PROGRESS=1

  if [[ ! -d "$venv_dir" ]]; then
    log "Preparing Rakkib..."
    run_quiet "Preparing Rakkib" "$PYTHON_CMD" -m venv "$venv_dir" \
      || run_quiet "Preparing Rakkib" "$PYTHON_CMD" -m venv --without-pip "$venv_dir" \
      || die "Failed to prepare Rakkib. Install python3-venv and rerun."
  fi

  if [[ ! -x "${venv_dir}/bin/pip" ]]; then
    log "Preparing Python tools..."
    command_exists curl || die "curl is required to bootstrap pip. Install curl and rerun."
    run_quiet "Preparing Python tools" bash -lc 'curl -fsSL https://bootstrap.pypa.io/get-pip.py | "$1"' -- "${venv_dir}/bin/python" \
      || die "Failed to bootstrap pip. Check network and rerun."
  fi

  log "Finishing setup..."
  run_quiet "Finishing setup" "${venv_dir}/bin/pip" install -q "${INSTALL_DIR}" \
    || die "pip install failed. Check the error above and rerun."

  mkdir -p "$bin_dir"
  # Overwrite symlink if it points elsewhere (e.g. stale pipx path)
  if [[ -L "$target" || ! -e "$target" ]]; then
    ln -sf "${venv_dir}/bin/rakkib" "$target"
    log "Added rakkib to PATH."
  else
    warn "${target} exists and is not a symlink; skipping link creation."
  fi
  VENV_INSTALL_IN_PROGRESS=0
}

ensure_shell_path() {
  local marker="# Added by Rakkib: user-local bin on PATH"
  local files=()
  if [[ "${PLATFORM:-}" == "mac" ]]; then
    files=("${HOME}/.zshrc" "${HOME}/.zprofile" "${HOME}/.profile")
  else
    [[ -f "${HOME}/.bashrc"  ]] && files+=("${HOME}/.bashrc")
    [[ -f "${HOME}/.zshrc"   ]] && files+=("${HOME}/.zshrc")
    [[ -f "${HOME}/.profile" ]] && files+=("${HOME}/.profile")
    [[ ${#files[@]} -eq 0 ]] && files=("${HOME}/.bashrc")
  fi

  for profile in "${files[@]}"; do
    grep -Fq "$marker" "$profile" 2>/dev/null && continue
    touch "$profile"
    {
      printf '\n%s\n' "$marker"
      printf '%s\n' 'case ":$PATH:" in'
      printf '%s\n' '  *":$HOME/.local/bin:"*) ;;'
      printf '%s\n' '  *) export PATH="$HOME/.local/bin:$PATH" ;;'
      printf '%s\n' 'esac'
    } >> "$profile"
    log "Added ~/.local/bin to PATH in ${profile}"
  done
}

print_next_steps() {
  if [[ "${PLATFORM:-}" == "mac" ]]; then
    cat <<EOF

Rakkib is installed.

Next:
  rakkib web

If your shell cannot find rakkib yet, run one of:
  source ~/.zshrc   |   source ~/.zprofile   |   source ~/.profile

To install local services on macOS, run:
  rakkib auth

To uninstall:
  rakkib uninstall --yes

EOF
    return
  fi

  cat <<EOF

Rakkib is installed.

Next:
  rakkib init
  rakkib pull

If your shell cannot find rakkib yet, run one of:
  source ~/.bashrc   |   source ~/.zshrc   |   source ~/.profile

If Docker needs setup, run:
  rakkib auth

To uninstall:
  rakkib uninstall --yes

EOF
}

main() {
  parse_args "$@"
  validate_repo_url "$REPO_URL"
  validate_branch "$BRANCH"
  detect_platform
  confirm_root
  ensure_tooling
  ensure_python3_and_venv
  prepare_repo
  ensure_venv_install
  ensure_shell_path
  print_next_steps
}

if [[ "${RAKKIB_INSTALL_TEST_MODE:-0}" != "1" ]]; then
  main "$@"
fi
