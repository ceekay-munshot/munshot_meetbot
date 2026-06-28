#!/usr/bin/env bash
#
# Relocate Docker (data-root + containerd image store) onto an ext4 image file
# living on the big NTFS disk, freeing the root disk. Reversible: originals are
# moved to .bak (a rename, no copy) and only removed by you after verification.
#
set -euo pipefail

IMG=/media/blastre/NewVolume4/docker-storage.img
MNT=/mnt/docker-storage
SIZE=200G

log(){ printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

if [[ $EUID -ne 0 ]]; then echo "Run with sudo: sudo bash $0"; exit 1; fi

log "Stopping Docker + containerd"
systemctl stop docker docker.socket containerd 2>/dev/null || true

log "Current usage (root disk)"
du -sh /var/lib/docker /var/lib/containerd 2>/dev/null || true

if [[ ! -f "$IMG" ]]; then
  log "Creating ${SIZE} sparse ext4 image at $IMG"
  truncate -s "$SIZE" "$IMG"
  mkfs.ext4 -F -m 0 -E lazy_itable_init=1,lazy_journal_init=1 "$IMG"
else
  log "Image already exists, reusing $IMG"
fi

log "Mounting image at $MNT"
mkdir -p "$MNT"
mountpoint -q "$MNT" || mount -o loop "$IMG" "$MNT"
mkdir -p "$MNT/docker" "$MNT/containerd"

log "Copying existing data into the image (preserves ownership/xattrs)"
rsync -aHAX --info=progress2 /var/lib/docker/      "$MNT/docker/"
rsync -aHAX --info=progress2 /var/lib/containerd/  "$MNT/containerd/"

log "Swapping originals for symlinks"
for d in docker containerd; do
  if [[ ! -L "/var/lib/$d" ]]; then
    rm -rf "/var/lib/$d.bak"
    mv "/var/lib/$d" "/var/lib/$d.bak"
    ln -s "$MNT/$d" "/var/lib/$d"
  fi
done

log "Persisting the loop mount in /etc/fstab"
if ! grep -q "$IMG" /etc/fstab; then
  echo "$IMG $MNT ext4 loop,nofail,x-systemd.requires=/media/blastre/NewVolume4 0 2" >> /etc/fstab
fi

log "Ensuring docker/containerd start after the mount"
for svc in docker containerd; do
  mkdir -p "/etc/systemd/system/$svc.service.d"
  cat > "/etc/systemd/system/$svc.service.d/storage.conf" <<EOF
[Unit]
RequiresMountsFor=$MNT
EOF
done
systemctl daemon-reload

log "Starting containerd + Docker"
systemctl start containerd docker

log "Done. Verifying"
docker info --format 'Docker root: {{.DockerRootDir}}'
findmnt "$MNT" -o SOURCE,TARGET,FSTYPE,SIZE || true
echo
echo "If 'docker images' below lists your vexaai/* images, reclaim root space with:"
echo "    sudo rm -rf /var/lib/docker.bak /var/lib/containerd.bak"
docker images | grep -E 'vexaai|REPOSITORY' || true
