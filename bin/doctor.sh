#!/usr/bin/env bash
# bin/doctor.sh — host preflight for the Atlas demo (make doctor / make preflight).
#
# Checks the tools, Docker resources, host ports, disk, DNS and kernel limits
# `make up` depends on. Every problem prints ONE actionable line.
#
#   ✓ ok    ! warning (demo may be degraded, continues)    ✗ hard failure
#
# Exit: 0 = no hard failures (warnings allowed); 1 = at least one hard failure.
# Portable: macOS bash 3.2, GNU/Linux, WSL2.

set -uo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-atlas-demo}"
INOTIFY_MIN="${INOTIFY_MIN:-512}"
MIN_MEM_GIB="${MIN_MEM_GIB:-20}"
MIN_CPUS="${MIN_CPUS:-6}"
MIN_DISK_GB="${MIN_DISK_GB:-30}"
K3D_MIN="5.6"
PORTS="8080 8443 5001"

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

PASS=0; WARN=0; FAIL=0
if [ -t 1 ]; then G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'; else G=; Y=; R=; N=; fi
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$1"; PASS=$((PASS+1)); }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$1"; WARN=$((WARN+1)); }
bad()  { printf '  %s✗%s %s\n' "$R" "$N" "$1"; FAIL=$((FAIL+1)); }
have() { command -v "$1" >/dev/null 2>&1; }

# version_ge A B -> true if dotted version A >= B (numeric, major.minor.patch)
version_ge() {
  awk -v a="$1" -v b="$2" 'BEGIN{
    na=split(a,x,"."); nb=split(b,y,".");
    for(i=1;i<=3;i++){ xi=(i<=na)?x[i]+0:0; yi=(i<=nb)?y[i]+0:0;
      if(xi>yi) exit 0; if(xi<yi) exit 1 }
    exit 0 }'
}

OS=$(uname -s)
case "$OS" in
  Darwin) PKG_HINT="brew install" ;;
  *)      PKG_HINT="install via your package manager:" ;;
esac

echo "==> Atlas demo doctor ($OS $(uname -m))"

# --- 1. Required CLIs ------------------------------------------------------
echo "--- tools"
for t in docker kubectl helm python3; do
  if have "$t"; then ok "$t found"
  else bad "$t not found — $PKG_HINT $t"; fi
done

if have k3d; then
  kv=$(k3d version 2>/dev/null | awk '/^k3d version/{sub(/^v/,"",$3); print $3; exit}')
  if [ -z "$kv" ]; then
    warn "k3d found but version unreadable — need ≥ $K3D_MIN"
  elif version_ge "$kv" "$K3D_MIN"; then
    ok "k3d $kv (≥ $K3D_MIN)"
  else
    bad "k3d $kv is too old — upgrade to ≥ $K3D_MIN (https://k3d.io/#installation)"
  fi
else
  bad "k3d not found — $PKG_HINT k3d (need ≥ $K3D_MIN; https://k3d.io/#installation)"
fi

if have jq; then ok "jq found"
else warn "jq not found — $PKG_HINT jq (used by troubleshooting one-liners in the docs)"; fi

# --- 2. Docker daemon + buildx + resources --------------------------------
echo "--- docker"
DOCKER_UP=0
if have docker; then
  if docker info >/dev/null 2>&1; then
    DOCKER_UP=1
    ok "docker daemon reachable (context: $(docker context show 2>/dev/null || echo default))"
  else
    bad "docker daemon not reachable — start Docker Desktop / OrbStack / dockerd, then re-run"
  fi
  if docker buildx version >/dev/null 2>&1; then ok "docker buildx available"
  else bad "docker buildx missing — install the buildx plugin (https://docs.docker.com/build/)"; fi
fi

if [ "$DOCKER_UP" -eq 1 ]; then
  mem=$(docker info --format '{{.MemTotal}}' 2>/dev/null)
  cpus=$(docker info --format '{{.NCPU}}' 2>/dev/null)
  case "$mem" in ''|*[!0-9]*) mem=0 ;; esac
  case "$cpus" in ''|*[!0-9]*) cpus=0 ;; esac
  mem_gib=$(awk -v m="$mem" 'BEGIN{printf "%.1f", m/1073741824}')
  if awk -v m="$mem" -v min="$MIN_MEM_GIB" 'BEGIN{exit !(m >= min*1073741824*0.97)}'; then
    ok "docker memory ${mem_gib} GiB (≥ ${MIN_MEM_GIB})"
  else
    warn "docker memory ${mem_gib} GiB < ${MIN_MEM_GIB} GiB — raise it in Docker Desktop (Settings → Resources) or OrbStack (Settings → System → Memory limit); pods will OOM/evict"
  fi
  if [ "$cpus" -ge "$MIN_CPUS" ]; then
    ok "docker CPUs $cpus (≥ $MIN_CPUS)"
  else
    warn "docker CPUs $cpus < $MIN_CPUS — raise CPUs in Docker Desktop/OrbStack settings; convergence will be slow"
  fi
fi

# --- 3. Host ports ---------------------------------------------------------
echo "--- ports"
CLUSTER_EXISTS=0
if have k3d && [ "$DOCKER_UP" -eq 1 ] && k3d cluster list "$CLUSTER_NAME" >/dev/null 2>&1; then
  CLUSTER_EXISTS=1
fi
if [ "$CLUSTER_EXISTS" -eq 1 ]; then
  ok "cluster '$CLUSTER_NAME' already exists — ports $PORTS are its own"
elif ! have python3; then
  warn "skipping port check (python3 missing)"
else
  for p in $PORTS; do
    if python3 - "$p" <<'PY' 2>/dev/null
import socket, sys
p = int(sys.argv[1])
for host in ("0.0.0.0", "127.0.0.1"):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, p))
    except OSError:
        sys.exit(1)
    finally:
        s.close()
PY
    then
      ok "port $p free"
    else
      owner=""
      if [ "$DOCKER_UP" -eq 1 ]; then
        owner=$(docker ps --filter "publish=$p" --format '{{.Names}}' 2>/dev/null | head -1)
      fi
      if [ -n "$owner" ]; then
        bad "port $p in use by container '$owner' — stop it (e.g. 'k3d cluster stop <other>' or 'docker stop $owner')"
      else
        bad "port $p in use — free it (find the process: lsof -nP -iTCP:$p -sTCP:LISTEN)"
      fi
    fi
  done
fi

# --- 4. Disk ---------------------------------------------------------------
echo "--- disk"
free_kb=$(df -Pk "$REPO_ROOT" 2>/dev/null | awk 'NR==2{print $4}')
case "$free_kb" in ''|*[!0-9]*) free_kb=0 ;; esac
free_gb=$((free_kb / 1000000))
if [ "$free_gb" -ge "$MIN_DISK_GB" ]; then
  ok "free disk ${free_gb} GB (≥ ${MIN_DISK_GB})"
else
  warn "free disk ${free_gb} GB < ${MIN_DISK_GB} GB — images + lab need ~${MIN_DISK_GB} GB; prune with 'docker system prune' or free space"
fi
if [ "$OS" = "Darwin" ]; then
  echo "    (Docker Desktop/OrbStack also cap their own VM disk — check its size limit in settings)"
fi

# --- 5. Wildcard DNS (*.127-0-0-1.nip.io) --------------------------------
echo "--- dns"
if have python3 && python3 - <<'PY' 2>/dev/null
import socket, sys
try:
    sys.exit(0 if socket.gethostbyname("argocd.127-0-0-1.nip.io") == "127.0.0.1" else 1)
except Exception:
    sys.exit(1)
PY
then
  ok "*.127-0-0-1.nip.io resolves to 127.0.0.1"
else
  warn "*.127-0-0-1.nip.io does not resolve (offline or DNS-rebind protection) — add to /etc/hosts: 127.0.0.1 argocd.127-0-0-1.nip.io netbox.127-0-0-1.nip.io grafana.127-0-0-1.nip.io workflows.127-0-0-1.nip.io clabernetes.127-0-0-1.nip.io console.127-0-0-1.nip.io"
fi

# --- 6. inotify (argo-events data plane) ---------------------------------
echo "--- kernel"
if [ ! -r /proc/sys/fs/inotify/max_user_instances ]; then
  ok "inotify: no /proc/sys/fs/inotify on $OS — limit lives in the Docker VM (default is ample)"
else
  inst=$(cat /proc/sys/fs/inotify/max_user_instances 2>/dev/null || echo 0)
  if [ "$inst" -ge "$INOTIFY_MIN" ]; then
    ok "fs.inotify.max_user_instances=$inst (≥ $INOTIFY_MIN)"
  else
    warn "fs.inotify.max_user_instances=$inst < $INOTIFY_MIN — eventing will crashloop; run: sudo sysctl fs.inotify.max_user_instances=1024 && echo 'fs.inotify.max_user_instances=1024' | sudo tee /etc/sysctl.d/99-inotify.conf"
  fi
fi

echo
printf '%s✓ %d ok%s   %s! %d warning(s)%s   %s✗ %d failure(s)%s\n' \
  "$G" "$PASS" "$N" "$Y" "$WARN" "$N" "$R" "$FAIL" "$N"
if [ "$FAIL" -gt 0 ]; then
  echo "doctor: fix the ✗ items above before 'make up'."
  exit 1
fi
exit 0
