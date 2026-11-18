#!/bin/bash
# Build a separate data-drive image. Never overwrite original cold-start artifacts.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUTPUT_DIR=${OUTPUT_DIR:?Set a NEW OUTPUT_DIR}
DSH_DIR=${DSH_DIR:?Set the existing DSH installation path}
NODE_BIN=${NODE_BIN:?Set the existing Node 24 path}

[[ $EUID == 0 && ! -e "$OUTPUT_DIR" ]] || { echo 'Needs root and a new output directory' >&2; exit 1; }
[[ -x "$NODE_BIN" && -x "$DSH_DIR/node_modules/.bin/dsh" ]]
KERNEL=$(uname -r)
mkdir -m 700 -p "$OUTPUT_DIR"
STAGE=$(mktemp -d)
trap 'rm -rf -- "$STAGE"' EXIT
mkdir -p "$STAGE/app" "$STAGE/home"
cp -a "$DSH_DIR/node_modules" "$STAGE/app/"
cp "$ROOT/guest/disk-probe.mjs" "$STAGE/app/"
cp "$ROOT/guest/disk-init" "$STAGE/init"
chmod 755 "$STAGE/init"
INSTALL="mount modprobe ip env mkdir blkid sync umount"
add_libraries() {
  local deps lib
  deps=$(ldd "$1")
  if grep -q 'not found' <<<"$deps"; then echo "Missing library: $1" >&2; exit 1; fi
  while IFS= read -r lib; do INSTALL="$INSTALL $lib"; done < <(
    awk '/=> \// {print $3} /^[[:space:]]*\// {print $1}' <<<"$deps"
  )
}
add_libraries "$NODE_BIN"
python3 "$ROOT/scripts/native_addons.py" "$STAGE/app/node_modules" --arch "$(uname -m)" > "$STAGE/addons.list"
while IFS= read -r -d '' addon; do add_libraries "$addon"; done < "$STAGE/addons.list"
cp "/boot/vmlinuz-$KERNEL" "$OUTPUT_DIR/vmlinuz"
dracut --force --no-hostonly --reproducible --no-early-microcode \
  --modules bash --add-drivers "virtio_pci virtio_net virtio_blk ext4 vmw_vsock_virtio_transport nsm" \
  --install "$INSTALL" --include "$STAGE/init" /init --include "$STAGE/app" /opt/dsh \
  --include "$STAGE/home" /home/dsh --include "$NODE_BIN" /usr/local/bin/node \
  --kver "$KERNEL" "$OUTPUT_DIR/initrd.img"
sha256sum "$OUTPUT_DIR/vmlinuz" "$OUTPUT_DIR/initrd.img" > "$OUTPUT_DIR/SHA256SUMS"
"$NODE_BIN" -e '
const fs=require("node:fs"); const [release,dsh,out]=process.argv.slice(1);
const hashes=fs.readFileSync(out+"/SHA256SUMS","utf8").trim().split("\n").map(x=>x.split(/\s+/)[0]);
console.log(JSON.stringify({kernel_release:release,arch:process.arch,node_version:process.version,
 dsh_version:require(dsh+"/node_modules/@deepseek-ai/dsh/package.json").version,
 kernel_sha256:hashes[0],initrd_sha256:hashes[1],variant:"disk-isolation"},null,2));
' "$KERNEL" "$DSH_DIR" "$OUTPUT_DIR" > "$OUTPUT_DIR/manifest.json"
cat "$OUTPUT_DIR/manifest.json"
