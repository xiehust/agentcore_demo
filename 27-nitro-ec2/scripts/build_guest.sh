#!/bin/bash
# Build on the AL2023 parent, after installing Node 24 and stock DSH there.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)

[[ $EUID == 0 ]] || { echo 'Run as root on the prepared AL2023 parent' >&2; exit 1; }
DSH_DIR=${DSH_DIR:?Set DSH_DIR to a directory containing node_modules/@deepseek-ai/dsh}
NODE_BIN=${NODE_BIN:?Set NODE_BIN to the absolute Node 24 executable path}
OUTPUT_DIR=${OUTPUT_DIR:-"$ROOT/artifacts"}
for tool in dracut ldd find sha256sum python3; do command -v "$tool" >/dev/null; done
[[ -x "$NODE_BIN" && "$NODE_BIN" = /* ]] || { echo 'NODE_BIN must be absolute and executable' >&2; exit 1; }
[[ $("$NODE_BIN" -p 'process.versions.node.split(".")[0]') == 24 ]] || { echo 'Use Node 24' >&2; exit 1; }
[[ "$DSH_DIR" = /* && -x "$DSH_DIR/node_modules/.bin/dsh" ]] || { echo 'DSH is not installed in DSH_DIR' >&2; exit 1; }
KERNEL=$(uname -r)
python3 -c 'import re, sys; m=re.fullmatch(r"(\d+)\.(\d+)\.(\d+)-(\d+)\.(\d+)\.amzn2023\.(aarch64|x86_64)", sys.argv[1]); sys.exit(0 if m and tuple(map(int,m.groups()[:5])) >= (6,18,51,120,162) else 1)' "$KERNEL" \
    || { echo 'Requires AL2023 kernel >= 6.18.51-120.162.amzn2023' >&2; exit 1; }
[[ ! -e "$OUTPUT_DIR" ]] || { echo 'OUTPUT_DIR already exists; select a new directory' >&2; exit 1; }
mkdir -m 700 -p "$OUTPUT_DIR"
STAGE=$(mktemp -d)
trap 'rm -rf -- "$STAGE"' EXIT
mkdir -p "$STAGE/app" "$STAGE/home" "$STAGE/bin"
# Only package code is copied, never the installing user's home/config/credentials.
cp -a "$DSH_DIR/node_modules" "$STAGE/app/"
cp "$ROOT/guest/server.mjs" "$STAGE/app/server.mjs"
cp "$NODE_BIN" "$STAGE/bin/node"
cp "$ROOT/guest/sandbox-init" "$STAGE/init"
chmod 755 "$STAGE/init" "$STAGE/bin/node"
# Materialize the stock headless profile at build time, as in 26-runtime-dsh.
env -i PATH="$STAGE/bin:/usr/bin:/bin" HOME="$STAGE/home" DSH_HOME="$STAGE/home/.dsh" \
    AWS_EC2_METADATA_DISABLED=true "$STAGE/app/node_modules/.bin/dsh" headless --help >/dev/null

# dracut --install resolves Node's and the CLI tools' shared libraries. Native
# DSH addons may link more libraries; include their dependencies explicitly too.
INSTALL="mount modprobe ip env mkdir"
add_libraries() {
    local deps lib
    deps=$(ldd "$1")
    if grep -q 'not found' <<<"$deps"; then
        echo "Missing shared library: $1" >&2; exit 1
    fi
    while IFS= read -r lib; do INSTALL="$INSTALL $lib"; done < <(
        awk '/=> \// {print $3} /^[[:space:]]*\// {print $1}' <<<"$deps"
    )
}
# Include Node only once at /usr/local/bin/node, but keep its ELF dependencies.
add_libraries "$NODE_BIN"
# npm packages can bundle glibc/musl, Windows/macOS, and both CPU architectures.
# Keep all package files, but only resolve deps of addons this AL2023 can load.
python3 "$ROOT/scripts/native_addons.py" "$STAGE/app/node_modules" --arch "$(uname -m)" > "$STAGE/addons.list"
while IFS= read -r -d '' addon; do
    add_libraries "$addon"
done < "$STAGE/addons.list"
cp "/boot/vmlinuz-$KERNEL" "$OUTPUT_DIR/vmlinuz"
dracut --force --no-hostonly --reproducible --no-early-microcode \
    --modules bash \
    --add-drivers "virtio_pci virtio_net vmw_vsock_virtio_transport nsm" \
    --install "$INSTALL" \
    --include "$STAGE/init" /init \
    --include "$STAGE/app" /opt/dsh \
    --include "$STAGE/home" /home/dsh \
    --include "$STAGE/bin/node" /usr/local/bin/node \
    --kver "$KERNEL" "$OUTPUT_DIR/initrd.img"
sha256sum "$OUTPUT_DIR/vmlinuz" "$OUTPUT_DIR/initrd.img" > "$OUTPUT_DIR/SHA256SUMS"
"$NODE_BIN" -e '
const fs = require("node:fs");
const [release, dsh, out] = process.argv.slice(1);
const hashes = fs.readFileSync(out + "/SHA256SUMS", "utf8").trim().split("\n").map(x => x.split(/\s+/)[0]);
console.log(JSON.stringify({kernel_release: release, arch: process.arch,
  dsh_version: require(dsh + "/node_modules/@deepseek-ai/dsh/package.json").version,
  node_version: process.version, kernel_sha256: hashes[0], initrd_sha256: hashes[1]}, null, 2));
' "$KERNEL" "$DSH_DIR" "$OUTPUT_DIR" > "$OUTPUT_DIR/manifest.json"
printf 'Built %s; Node=%s; kernel=%s\n' "$OUTPUT_DIR" "$("$NODE_BIN" --version)" "$KERNEL"
