#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  exec sudo bash "$0" "$@"
fi

swap_file="/swapfile"
required_kib=$((3 * 1024 * 1024))
available_kib="$(df --output=avail / | tail -n 1 | tr -d ' ')"

if swapon --show=NAME --noheadings | grep -Fxq "$swap_file"; then
  echo "$swap_file is already active."
elif [[ -e "$swap_file" ]]; then
  echo "$swap_file exists but is not active; inspect it before continuing." >&2
  exit 1
else
  if (( available_kib < required_kib )); then
    echo "At least 3 GiB of free disk space is required before creating swap." >&2
    exit 1
  fi
  fallocate -l 2G "$swap_file"
  chmod 600 "$swap_file"
  mkswap "$swap_file"
  swapon "$swap_file"
fi

if ! grep -Eq '^/swapfile[[:space:]]' /etc/fstab; then
  printf '%s\n' '/swapfile none swap sw 0 0' >> /etc/fstab
fi

printf '%s\n' 'vm.swappiness=10' > /etc/sysctl.d/99-hawkhive-memory.conf
sysctl --system >/dev/null

echo "Memory preparation complete:"
free -h
swapon --show
