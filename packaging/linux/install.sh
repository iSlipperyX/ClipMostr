#!/usr/bin/env bash
# ClipMonster for Linux: the host that does the work (a Google Cloud VM, or any Ubuntu/Debian server).
#
#   curl -fsSL https://github.com/iSlipperyX/ClipMostr/releases/latest/download/install-linux.sh | sudo bash
#
# It installs ClipMonster in /opt/clipmonster, running as its own user (clipmonster, data in
# /var/lib/clipmonster) as three services: a virtual screen (clipmonster-display), automation with the Mac
# app's server (clipmonster) and, when you ask for it, a remote desktop on that screen for signing in to
# TikTok and YouTube and for ClipMonster's own window (clipmonster-desktop). Then `sudo clipmonster` runs it.
#
# Running it again updates ClipMonster and keeps your settings, sign-ins, clips and linked Macs.
#
# Options:
#   --version X.Y.Z   that release (default: the latest)
#   --dev             the newest code (the main branch)
#   --source DIR      a ClipMonster folder already on this computer (a git checkout)
#   --port N          the Mac app's port (default 47320)
#   --gpu auto|vulkan|none   build the local AI for an NVIDIA GPU through Vulkan (auto: when nvidia-smi works)
#   --no-ai-build     don't build the local AI (llama.cpp) and whisper.cpp (use Claude/Gemini, faster-whisper)
#   --no-browsers     don't install Google Chrome and Firefox (posting and YouTube sign-in need them)
#   --no-swap         don't add a swap file when there's none
#   --tailscale       also install Tailscale (then run: sudo tailscale up)
#   --no-start        install, but don't start the services
#   -y, --yes         don't ask anything
set -Eeuo pipefail

REPO="iSlipperyX/ClipMostr"
PREFIX="/opt/clipmonster"
APP_USER="clipmonster"
APP_HOME="/var/lib/clipmonster"
ETC="/etc/clipmonster"
DISPLAY_NUM="7"
DEFAULT_PORT="47320"

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m ✓\033[0m %s\n' "$*"; }
warn() { printf '\033[33m ⚠\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m ✗ %s\033[0m\n' "$*" >&2; exit 1; }
trap 'die "Stopped at line $LINENO (exit $?). Run it again once the problem above is fixed: it picks up where it left off."' ERR

STAGE2=0 VERSION="" DEV=0 SOURCE="" PORT="" GPU="auto" AI_BUILD=1 BROWSERS=1 SWAP=1 TAILSCALE=0 START=1
ARGS=("$@")
while [ $# -gt 0 ]; do
  case "$1" in
    --stage2) STAGE2=1 ;;
    --version) VERSION="${2#v}"; shift ;;
    --dev) DEV=1 ;;
    --source) SOURCE="$2"; shift ;;
    --port) PORT="$2"; shift ;;
    --gpu) GPU="$2"; shift ;;
    --no-ai-build) AI_BUILD=0 ;;
    --no-browsers) BROWSERS=0 ;;
    --no-swap) SWAP=0 ;;
    --tailscale) TAILSCALE=1 ;;
    --no-start) START=0 ;;
    -y|--yes) ;;  # it never asks anything; accepted so scripts can say so
    -h|--help) sed -n '2,29p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac
  shift
done
case "$GPU" in auto|vulkan|none) ;; *) die "--gpu is auto, vulkan or none" ;; esac
if [ -n "$PORT" ] && ! { [[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1024 ] && [ "$PORT" -le 65535 ]; }; then
  die "--port is a number from 1024 to 65535"
fi

[ "$(id -u)" = 0 ] || die "Run it as root: sudo bash install-linux.sh (or curl ... | sudo bash)"
[ -r /etc/os-release ] || die "This installer is for Ubuntu and Debian."
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}:${ID_LIKE:-}" in
  ubuntu:*|debian:*|*:*debian*|*:*ubuntu*) ;;
  *) die "This installer is for Ubuntu (22.04 or newer) and Debian (12 or newer); this is ${PRETTY_NAME:-unknown}." ;;
esac
command -v systemctl >/dev/null && [ -d /run/systemd/system ] || die "ClipMonster runs as systemd services, and systemd isn't running here."
ARCH="$(dpkg --print-architecture)"
export DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------------------------------------------
# Stage 1: get ClipMonster's source (a release, the main branch, or a folder here) and run ITS installer,
# so an update always installs with the installer that came with it.
# ---------------------------------------------------------------------------------------------------------
if [ "$STAGE2" = 0 ]; then
  say "Getting ready (packages to download ClipMonster)"
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl tar rsync >/dev/null
  mkdir -p "$PREFIX/releases"
  stage="$(mktemp -d "$PREFIX/releases/.new-XXXXXX")"
  if [ -n "$SOURCE" ]; then
    [ -f "$SOURCE/ytclip/__init__.py" ] || die "$SOURCE isn't a ClipMonster folder (no ytclip/__init__.py)."
    say "Copying ClipMonster from $SOURCE"
    rsync -a --delete --exclude .git --exclude '__pycache__' --exclude '*.egg-info' --exclude build \
      --exclude dist --exclude .pytest_cache "$SOURCE/" "$stage/"
  else
    if [ "$DEV" = 1 ]; then
      ref="heads/main"; what="the newest code (main)"
    else
      if [ -z "$VERSION" ]; then
        VERSION="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
          | sed -n 's/.*"tag_name": *"v\{0,1\}\([^"]*\)".*/\1/p' | head -n1)" || true
        [ -n "$VERSION" ] || die "Couldn't ask GitHub for the latest release. Try again, or pass --version X.Y.Z."
      fi
      ref="tags/v$VERSION"; what="ClipMonster $VERSION"
    fi
    say "Downloading $what"
    curl -fsSL "https://github.com/$REPO/archive/refs/$ref.tar.gz" | tar -xz -C "$stage" --strip-components=1 \
      || die "Couldn't download $what from GitHub."
  fi
  if [ ! -f "$stage/packaging/linux/install.sh" ]; then
    rm -rf "$stage"
    die "That version of ClipMonster is older than its Linux host. Run this with --dev (the newest code), or with --version for a newer release."
  fi
  ver="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$stage/ytclip/__init__.py")"
  [ -n "$ver" ] || die "Couldn't read the version of the downloaded ClipMonster."
  dest="$PREFIX/releases/$ver"
  if [ "$DEV" = 1 ] || [ -n "$SOURCE" ]; then dest="$PREFIX/releases/$ver-$(date +%Y%m%d%H%M%S)"; fi
  rm -rf "$dest"
  mv "$stage" "$dest"
  rest=()
  skip=0
  for a in "${ARGS[@]}"; do  # pass every option on, except where the source came from
    if [ "$skip" = 1 ]; then skip=0; continue; fi
    case "$a" in --source|--version) skip=1 ;; --dev) ;; *) rest+=("$a") ;; esac
  done
  exec bash "$dest/packaging/linux/install.sh" --stage2 --source "$dest" ${rest[@]+"${rest[@]}"}
fi

# ---------------------------------------------------------------------------------------------------------
# Stage 2: install this copy (SOURCE is in $PREFIX/releases).
# ---------------------------------------------------------------------------------------------------------
SRC="$(cd "$SOURCE" && pwd)"
HERE="$SRC/packaging/linux"
VER="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/ytclip/__init__.py")"
say "Installing ClipMonster $VER for Linux on ${PRETTY_NAME:-this computer}"

pick() {  # the first of these packages this release of the system has
  local p
  for p in "$@"; do
    if apt-cache show "$p" >/dev/null 2>&1; then echo "$p"; return 0; fi
  done
  return 0
}

say "Installing what ClipMonster needs from ${NAME:-the system} (a few minutes the first time)"
PKGS=(python3 python3-venv xz-utils python3-pip python3-dev ffmpeg git build-essential cmake pkg-config curl rsync
      xvfb x11vnc novnc openbox x11-xserver-utils xdg-utils
      fonts-dejavu-core fonts-liberation fonts-noto-color-emoji fontconfig espeak-ng
      libegl1 libgl1 libglib2.0-0 libfontconfig1 libdbus-1-3 libnss3 libxkbcommon0 libxkbcommon-x11-0
      libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0
      libxcb-shape0 libxcb-xinerama0 libxcb-xkb1 libxrender1 libxi6 libxtst6 libsm6 libpulse0 libsndfile1)
PKGS+=("$(pick libasound2t64 libasound2)" "$(pick websockify python3-websockify)")
apt-get update -qq
apt-get install -y -qq --no-install-recommends "${PKGS[@]}" >/dev/null
ok "System packages"

PY="$(command -v python3)"
"$PY" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || die "ClipMonster needs Python 3.10 or newer."

# --- Node.js: yt-dlp runs YouTube's player code in it (the distribution's own is often too old) -------------
node_ok() { local v; v="$("$1" --version 2>/dev/null | sed 's/^v//; s/\..*//')"; [ -n "$v" ] && [ "$v" -ge 20 ]; }
if ! { [ -x "$PREFIX/node/bin/node" ] && node_ok "$PREFIX/node/bin/node"; }; then
  case "$ARCH" in amd64) narch=x64 ;; arm64) narch=arm64 ;; *) narch="" ;; esac
  if [ -n "$narch" ]; then
    say "Installing Node.js 22 (YouTube downloads need it)"
    base="https://nodejs.org/dist/latest-v22.x"
    sums="$(curl -fsSL "$base/SHASUMS256.txt")"
    file="$(printf '%s\n' "$sums" | awk -v a="linux-$narch.tar.xz" '$2 ~ a"$" {print $2; exit}')"
    [ -n "$file" ] || die "Couldn't find Node.js 22 for linux-$narch."
    tmp="$(mktemp -d)"
    curl -fsSL -o "$tmp/$file" "$base/$file"
    (cd "$tmp" && printf '%s\n' "$sums" | grep " $file\$" | sha256sum -c --status) || die "Node.js's download didn't match its checksum."
    rm -rf "$PREFIX/node.new"; mkdir -p "$PREFIX/node.new"
    tar -xJf "$tmp/$file" -C "$PREFIX/node.new" --strip-components=1
    rm -rf "$PREFIX/node"; mv "$PREFIX/node.new" "$PREFIX/node"; rm -rf "$tmp"
  else
    warn "No Node.js build for $ARCH: YouTube downloads may fail."
  fi
fi
[ -x "$PREFIX/node/bin/node" ] && ok "Node.js $("$PREFIX/node/bin/node" --version)"

# --- the browsers: Chrome posts to TikTok, Firefox keeps ClipMonster's YouTube sign-in ---------------------
if [ "$BROWSERS" = 1 ]; then
  if ! command -v google-chrome-stable >/dev/null; then
    if [ "$ARCH" = amd64 ]; then
      say "Installing Google Chrome (posting to TikTok)"
      deb="$(mktemp --suffix=.deb)"
      curl -fsSL -o "$deb" https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
      apt-get install -y -qq "$deb" >/dev/null
      rm -f "$deb"
    else
      warn "Google Chrome is only made for x86-64 Linux, so this $ARCH computer can't post to TikTok (it can still clip and make videos)."
    fi
  fi
  command -v google-chrome-stable >/dev/null && ok "Google Chrome $(google-chrome-stable --version 2>/dev/null | sed 's/[^0-9.]//g')"
  firefox_real() {  # a Firefox a service can run (Ubuntu's own firefox package only launches the snap)
    command -v firefox-esr >/dev/null && return 0
    local f; f="$(command -v firefox || true)"
    [ -n "$f" ] || return 1
    if dpkg-query -W -f='${Version}' firefox 2>/dev/null | grep -q snap; then return 1; fi
    if [ -e /snap/bin/firefox ] && [ "$(readlink -f "$f")" = "$(readlink -f /snap/bin/firefox)" ]; then return 1; fi
    return 0
  }
  if ! firefox_real; then
    say "Installing Firefox (ClipMonster's YouTube sign-in)"
    if [ "${ID:-}" = debian ] && apt-cache show firefox-esr >/dev/null 2>&1; then
      apt-get install -y -qq --no-install-recommends firefox-esr >/dev/null
    else  # Ubuntu's own firefox is a snap, which a background service can't use: Mozilla's package instead
      install -d -m 0755 /etc/apt/keyrings
      curl -fsSL https://packages.mozilla.org/apt/repo-signing-key.gpg -o /etc/apt/keyrings/packages.mozilla.org.asc
      echo "deb [signed-by=/etc/apt/keyrings/packages.mozilla.org.asc] https://packages.mozilla.org/apt mozilla main" \
        > /etc/apt/sources.list.d/mozilla.list
      printf 'Package: *\nPin: origin packages.mozilla.org\nPin-Priority: 1000\n' > /etc/apt/preferences.d/mozilla
      apt-get update -qq
      apt-get install -y -qq --allow-downgrades firefox >/dev/null
    fi
  fi
  ok "Firefox"
fi

# --- its own user ---------------------------------------------------------------------------------------
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_HOME" --create-home --shell /usr/sbin/nologin --comment "ClipMonster" "$APP_USER"
  ok "User $APP_USER (its data: $APP_HOME)"
fi
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$APP_HOME"

# --- Python and ClipMonster -----------------------------------------------------------------------------
say "Installing ClipMonster $VER and its Python packages (several minutes the first time)"
if [ ! -x "$PREFIX/venv/bin/python" ] || ! "$PREFIX/venv/bin/python" -c 'import sys' 2>/dev/null; then
  rm -rf "$PREFIX/venv"
  "$PY" -m venv "$PREFIX/venv"
fi
"$PREFIX/venv/bin/python" -m pip install -q --upgrade pip wheel
"$PREFIX/venv/bin/python" -m pip install -q --upgrade "${SRC}[whisper,creator]"
"$PREFIX/venv/bin/python" -c 'import ytclip, ytclip.remote.server, ytclip.gui.app' \
  || die "ClipMonster was installed but doesn't start; see above."
ok "ClipMonster $("$PREFIX/venv/bin/python" -c 'import ytclip; print(ytclip.__version__)')"

# --- the local AI and whisper.cpp, built for this computer ------------------------------------------------
build_tool() {  # name script out_dir
  local name="$1" script="$2" out="$3" tag vulkan=() want have
  tag="$("$PREFIX/venv/bin/python" -c "import runpy; print(runpy.run_path('$script')['TAG'])")"
  if [ "$GPU" = vulkan ] || { [ "$GPU" = auto ] && command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1; }; then
    apt-get install -y -qq --no-install-recommends libvulkan-dev glslc >/dev/null 2>&1 \
      || apt-get install -y -qq --no-install-recommends libvulkan-dev glslang-tools >/dev/null 2>&1 || true
    vulkan=(--vulkan)
  fi
  want="$name $tag${vulkan:+ (Vulkan)}"
  have="$(cat "$out/VERSION.txt" 2>/dev/null || true)"
  if [ "$have" = "$want" ]; then ok "$want (already built)"; return 0; fi
  say "Building $name $tag for this computer${vulkan:+ and its graphics card} (5-15 minutes)"
  if ! "$PREFIX/venv/bin/python" "$script" "$out.new" ${vulkan[@]+"${vulkan[@]}"} >"$PREFIX/build-$name.log" 2>&1; then
    if [ ${#vulkan[@]} -gt 0 ]; then
      warn "The graphics-card build of $name failed (log: $PREFIX/build-$name.log); building it for the CPU."
      "$PREFIX/venv/bin/python" "$script" "$out.new" >"$PREFIX/build-$name.log" 2>&1 \
        || { warn "Couldn't build $name (log: $PREFIX/build-$name.log)."; rm -rf "$out.new"; return 1; }
    else
      warn "Couldn't build $name (log: $PREFIX/build-$name.log)."; rm -rf "$out.new"; return 1
    fi
  fi
  rm -rf "$out"; mv "$out.new" "$out"
  ok "$(cat "$out/VERSION.txt")"
}
LLAMA="" WHISPER=""
if [ "$AI_BUILD" = 1 ]; then
  build_tool llama.cpp "$SRC/packaging/build_llama_cpp.py" "$PREFIX/llama-cpp" && LLAMA="$PREFIX/llama-cpp/llama-server" || true
  build_tool whisper.cpp "$SRC/packaging/build_whisper_cpp.py" "$PREFIX/whisper-cpp" && WHISPER="$PREFIX/whisper-cpp/whisper-cli" || true
else
  [ -x "$PREFIX/llama-cpp/llama-server" ] && LLAMA="$PREFIX/llama-cpp/llama-server"
  [ -x "$PREFIX/whisper-cpp/whisper-cli" ] && WHISPER="$PREFIX/whisper-cpp/whisper-cli"
fi

# --- a swap file: the local AI and Chrome together can need more memory than a small VM has --------------
if [ "$SWAP" = 1 ] && [ -z "$(swapon --noheadings 2>/dev/null)" ] && [ ! -e /swapfile-clipmonster ]; then
  free_gb=$(( $(df --output=avail -k / | tail -n1) / 1024 / 1024 ))
  mem_gb=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1024 / 1024 ))
  size=$(( mem_gb < 16 ? 8 : 4 ))
  if [ "$free_gb" -gt $(( size + 20 )) ]; then
    if fallocate -l "${size}G" /swapfile-clipmonster 2>/dev/null && chmod 600 /swapfile-clipmonster \
       && mkswap /swapfile-clipmonster >/dev/null && swapon /swapfile-clipmonster; then
      grep -q swapfile-clipmonster /etc/fstab || echo "/swapfile-clipmonster none swap sw 0 0" >> /etc/fstab
      ok "A ${size} GB swap file (there was none)"
    else
      rm -f /swapfile-clipmonster; warn "Couldn't add a swap file; carrying on without one."
    fi
  fi
fi

# --- Tailscale, if asked ----------------------------------------------------------------------------------
if [ "$TAILSCALE" = 1 ] && ! command -v tailscale >/dev/null; then
  say "Installing Tailscale"
  curl -fsSL https://tailscale.com/install.sh | sh >/dev/null
  ok "Tailscale (finish with: sudo tailscale up)"
fi

# --- the services and the clipmonster command -----------------------------------------------------------
say "Setting up the services"
install -d -m 0755 "$PREFIX/bin" "$ETC"
install -m 0755 "$HERE/clipmonster" "$HERE/clipmonster-display" "$HERE/clipmonster-desktop" "$PREFIX/bin/"
install -m 0755 "$HERE/install.sh" "$PREFIX/install.sh"
ln -sfn "$PREFIX/bin/clipmonster" /usr/local/bin/clipmonster
{
  echo "# Written by ClipMonster's installer: it's rewritten on every update."
  echo "DISPLAY=:$DISPLAY_NUM"
  echo "HOME=$APP_HOME"
  echo "PATH=$PREFIX/venv/bin:$PREFIX/bin:$PREFIX/node/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
  echo "PYTHONUNBUFFERED=1"
  echo "CLIPMONSTER_LINUX_HOST=1"
  [ -n "$LLAMA" ] && echo "CLIPMONSTER_LLAMA_SERVER=$LLAMA"
  [ -n "$WHISPER" ] && echo "CLIPMONSTER_WHISPER_CPP=$WHISPER"
} > "$ETC/env"
chmod 0644 "$ETC/env"
for unit in clipmonster-display.service clipmonster.service clipmonster-desktop.service; do
  install -m 0644 "$HERE/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
ln -sfn "$SRC" "$PREFIX/current"
# keep this release and the one before it
ls -1dt "$PREFIX"/releases/*/ 2>/dev/null | tail -n +3 | while read -r old; do
  [ "$(cd "$old" && pwd)" = "$SRC" ] || rm -rf "$old"
done

# --- settings for a host (the first time; only the port after that, when given) -------------------------
runuser -u "$APP_USER" -- env HOME="$APP_HOME" "$PREFIX/venv/bin/ytclip" host-setup ${PORT:+--port "$PORT"}

# a firewall on this computer (most cloud VMs have none: Google Cloud's is set in its console)
PORT_NOW="$(runuser -u "$APP_USER" -- env HOME="$APP_HOME" "$PREFIX/venv/bin/python" -c \
  'from ytclip.settings import Settings; print(Settings.load().remote_port)' 2>/dev/null || echo "$DEFAULT_PORT")"
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw allow "$PORT_NOW/tcp" comment "ClipMonster Mac app" >/dev/null && ok "Opened port $PORT_NOW in ufw"
fi

if [ "$START" = 1 ]; then
  systemctl enable clipmonster-display.service clipmonster.service >/dev/null 2>&1
  systemctl restart clipmonster-display.service
  systemctl restart clipmonster.service
  if systemctl is-active --quiet clipmonster-desktop.service; then systemctl restart clipmonster-desktop.service; fi
  say "Starting automation"
  for _ in $(seq 60); do
    if runuser -u "$APP_USER" -- env HOME="$APP_HOME" "$PREFIX/venv/bin/python" -c \
       'from ytclip.remote import state; raise SystemExit(0 if state.read_status().get("running") else 1)' 2>/dev/null; then
      break
    fi
    sleep 1
  done
  if systemctl is-active --quiet clipmonster.service; then ok "Automation is running"; else
    warn "Automation didn't start: see sudo clipmonster logs"; fi
fi

echo
say "ClipMonster $VER is installed."
"$PREFIX/bin/clipmonster" next-steps || true
