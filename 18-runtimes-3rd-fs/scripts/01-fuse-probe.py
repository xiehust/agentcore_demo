#!/usr/bin/env python3
"""Probe an AgentCore Runtime (microVM) session for FUSE / mount capabilities.

The probe answers the customer question "can we run a third-party FUSE file
system inside the sandbox?" with evidence instead of guesses:

* kernel + capability bounding set of the agent process (CapEff/CapBnd);
* whether ``/dev/fuse`` exists and can be opened;
* whether ``fuse`` / ``nfs`` appear in ``/proc/filesystems``;
* whether ``mount`` / ``fusermount`` binaries exist and whether a tmpfs mount
  or an unprivileged user namespace (``unshare -Urm``) is permitted;
* seccomp mode, existing mounts, and writable disk space.

Usage:
    python3 scripts/01-fuse-probe.py --runtime-arn <arn> --region us-east-2 \
        [--out results/fuse_probe.json] [--keep-session]

The runtime must already be deployed (any HTTP-protocol agent works: the probe
only needs an active session and ``InvokeAgentRuntimeCommand`` permission).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_cmd import RuntimeSession  # noqa: E402

# Static, code-owned probe script. Every check is guarded so the script always
# exits 0 and reports findings as JSON between the two marker lines.
PROBE_SCRIPT = r"""
set +e
export LC_ALL=C
python3 - <<'PY'
import json, os, subprocess, re

def sh(cmd, timeout=15):
    try:
        p = subprocess.run(["/bin/bash", "-c", cmd], capture_output=True, text=True, timeout=timeout)
        return {"rc": p.returncode, "out": p.stdout.strip()[:2000], "err": p.stderr.strip()[:500]}
    except Exception as exc:  # noqa: BLE001
        return {"rc": -1, "out": "", "err": repr(exc)}

CAP_NAMES = ["chown","dac_override","dac_read_search","fowner","fsetid","kill","setgid","setuid",
 "setpcap","linux_immutable","net_bind_service","net_broadcast","net_admin","net_raw","ipc_lock",
 "ipc_owner","sys_module","sys_rawio","sys_chroot","sys_ptrace","sys_pacct","sys_admin","sys_boot",
 "sys_nice","sys_resource","sys_time","sys_tty_config","mknod","lease","audit_write","audit_control",
 "setfcap","mac_override","mac_admin","syslog","wake_alarm","block_suspend","audit_read","perfmon",
 "bpf","checkpoint_restore"]

def decode_caps(hexmask):
    try:
        mask = int(hexmask, 16)
    except Exception:  # noqa: BLE001
        return []
    return [n for i, n in enumerate(CAP_NAMES) if mask >> i & 1]

status = {}
try:
    for line in open("/proc/self/status"):
        k, _, v = line.partition(":")
        if k in ("CapInh","CapPrm","CapEff","CapBnd","CapAmb","Seccomp","Seccomp_filters","NoNewPrivs","Uid","Gid"):
            status[k] = v.strip()
except Exception as exc:  # noqa: BLE001
    status["error"] = repr(exc)

fuse_dev = os.path.exists("/dev/fuse")
fuse_open = None
if fuse_dev:
    try:
        fd = os.open("/dev/fuse", os.O_RDWR)
        os.close(fd)
        fuse_open = {"ok": True}
    except OSError as exc:
        fuse_open = {"ok": False, "errno": exc.errno, "error": exc.strerror}

filesystems = ""
try:
    filesystems = open("/proc/filesystems").read()
except Exception as exc:  # noqa: BLE001
    filesystems = "ERROR " + repr(exc)

report = {
    "uname": sh("uname -a"),
    "id": sh("id"),
    "proc_status": status,
    "cap_eff_decoded": decode_caps(status.get("CapEff", "0")),
    "cap_bnd_decoded": decode_caps(status.get("CapBnd", "0")),
    "dev_fuse_exists": fuse_dev,
    "dev_fuse_open": fuse_open,
    "dev_listing": sh("ls -la /dev | head -60"),
    "proc_filesystems_has_fuse": bool(re.search(r"\bfuse\b", filesystems)),
    "proc_filesystems_has_nfs": bool(re.search(r"\bnfs4?\b", filesystems)),
    "proc_filesystems": filesystems.strip()[:1500],
    "binaries": {b: sh(f"command -v {b}")["out"] for b in
                 ["mount","umount","fusermount","fusermount3","unshare","nsenter","s3fs","rclone","sshfs","mount.nfs","mount.nfs4","git","curl"]},
    "mount_tmpfs_attempt": sh("mkdir -p /tmp/_probe_mnt && mount -t tmpfs none /tmp/_probe_mnt 2>&1; echo rc=$?; umount /tmp/_probe_mnt 2>/dev/null"),
    # If the kernel had a FUSE driver but udev never created the node, mknod would fix it.
    # ENODEV on open proves the driver itself is absent (monolithic kernel, no modules).
    # (/tmp is mounted nodev, so the node is created under /dev, which is not.)
    "mknod_dev_fuse_attempt": sh("[ -e /dev/fuse ] || mknod /dev/_probe_fuse c 10 229 2>&1; echo mknod_rc=$?; python3 -c \"import os;os.open('/dev/_probe_fuse',os.O_RDWR)\" 2>&1 | tail -1; rm -f /dev/_probe_fuse"),
    "mount_fuse_type_attempt": sh("mkdir -p /tmp/_fuse_mnt && mount -t fuse none /tmp/_fuse_mnt 2>&1; echo rc=$?"),
    "kernel_modules_dir": sh("ls /lib/modules 2>&1 | head -5; echo rc=$?"),
    # Connection-refused proves the NFS client path is reachable; a real mount needs VPC mode.
    "mount_nfs4_loopback_attempt": sh("mkdir -p /tmp/_nfs_mnt && timeout 8 mount -t nfs4 -o soft,timeo=10,retrans=1,retry=0 127.0.0.1:/ /tmp/_nfs_mnt 2>&1; echo rc=$?", timeout=20),
    "unshare_userns_attempt": sh("unshare -Urm true 2>&1; echo rc=$?"),
    "unshare_userns_mount_attempt": sh("unshare -Urm sh -c 'mkdir -p /tmp/_ns_mnt && mount -t tmpfs none /tmp/_ns_mnt && echo NS_TMPFS_OK' 2>&1; echo rc=$?"),
    "proc_mounts": sh("cat /proc/mounts"),
    "df": sh("df -hP / /tmp /mnt 2>&1"),
    "mnt_listing": sh("ls -la /mnt 2>&1"),
    "kernel_modules": sh("cat /proc/modules 2>&1 | head -20"),
    "env_hint": sh("env | grep -iE 'AGENTCORE|AWS_REGION|AWS_CONTAINER|AWS_DEFAULT_REGION|OTEL_SERVICE' | sed -E 's/=.*/=<redacted>/'"),
}
print("__PROBE_JSON_BEGIN__")
print(json.dumps(report))
print("__PROBE_JSON_END__")
PY
exit 0
"""


def extract_report(stdout: str) -> dict:
    begin = stdout.find("__PROBE_JSON_BEGIN__")
    end = stdout.find("__PROBE_JSON_END__")
    if begin < 0 or end < 0 or end < begin:
        raise ValueError("probe markers missing in stdout")
    payload = stdout[begin + len("__PROBE_JSON_BEGIN__"):end].strip()
    return json.loads(payload)


def summarize(report: dict) -> dict:
    caps = set(report.get("cap_eff_decoded", []))
    tmpfs = report.get("mount_tmpfs_attempt", {}).get("out", "")
    userns = report.get("unshare_userns_mount_attempt", {}).get("out", "")
    mknod = report.get("mknod_dev_fuse_attempt", {}).get("out", "")
    nfs_loop = report.get("mount_nfs4_loopback_attempt", {}).get("out", "")
    binaries = report.get("binaries", {})
    return {
        "kernel": report.get("uname", {}).get("out", ""),
        "runs_as_root": report.get("id", {}).get("out", "").startswith("uid=0("),
        "dev_fuse_exists": report.get("dev_fuse_exists"),
        "dev_fuse_openable": (report.get("dev_fuse_open") or {}).get("ok"),
        "kernel_fuse_registered": report.get("proc_filesystems_has_fuse"),
        "mknod_fuse_open_error": mknod.splitlines()[-1] if mknod else "",
        "kernel_nfs_registered": report.get("proc_filesystems_has_nfs"),
        "nfs_mount_helper_present": bool(binaries.get("mount.nfs4") or binaries.get("mount.nfs")),
        "nfs_client_path_reachable": ("refused" in nfs_loop.lower() or "rc=32" in nfs_loop) and "not supported" not in nfs_loop.lower(),
        "has_cap_sys_admin": "sys_admin" in caps,
        "has_cap_mknod": "mknod" in caps,
        "seccomp_mode": report.get("proc_status", {}).get("Seccomp"),
        "tmpfs_mount_allowed": "rc=0" in tmpfs,
        "userns_tmpfs_mount_allowed": "NS_TMPFS_OK" in userns,
        "fusermount_binary": bool(binaries.get("fusermount") or binaries.get("fusermount3")),
        "loadable_modules": "No such file" not in report.get("kernel_modules", {}).get("out", "No such file"),
        "verdict": None,
    }


def verdict(summary: dict) -> str:
    mount_priv = summary["has_cap_sys_admin"] or summary["userns_tmpfs_mount_allowed"]
    if summary["dev_fuse_exists"] and summary["dev_fuse_openable"] and mount_priv:
        return "FUSE mount inside the session is likely possible (device + driver + mount privilege present)."
    if not summary["kernel_fuse_registered"] and mount_priv:
        return (
            "Mount privilege is present but the guest kernel has NO FUSE driver (not in /proc/filesystems, "
            "no loadable modules): third-party FUSE clients cannot run inside the microVM session. "
            "Use runtime filesystemConfigurations (session storage / EFS / S3 Files); the kernel NFS client "
            "is present, so an in-VPC NFS re-export is a technically possible (unsupported) bridge."
        )
    if summary["dev_fuse_exists"]:
        return "FUSE device present but no mount privilege: userspace FUSE daemons cannot attach."
    return (
        "No /dev/fuse and no mount privilege: third-party FUSE clients cannot run inside the "
        "microVM session. Use runtime filesystemConfigurations (session storage / EFS / S3 Files) "
        "or a userspace sync/API client instead."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runtime-arn", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--qualifier", default="DEFAULT")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "results" / "fuse_probe.json"))
    parser.add_argument("--keep-session", action="store_true", help="retain the billable session for debugging")
    args = parser.parse_args()

    record: dict = {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "runtime_arn": args.runtime_arn,
        "region": args.region,
    }
    with RuntimeSession(args.runtime_arn, args.region, qualifier=args.qualifier, keep_session=args.keep_session) as session:
        record["session_id"] = session.session_id
        record["warm_up"] = session.warm_up()
        result = session.run_script(PROBE_SCRIPT, timeout=180, require_success=False)
        record["command"] = {k: v for k, v in result.items() if k not in ("stdout",)}
        if not result["success"]:
            record["error"] = result["error"]
            print(json.dumps(record, indent=2))
            return 1
        report = extract_report(result["stdout"])
        summary = summarize(report)
        summary["verdict"] = verdict(summary)
        record["summary"] = summary
        record["report"] = report
    record["session_cleanup"] = session.stop_result

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"summary": summary, "session_cleanup": session.stop_result, "saved_to": str(out)}, indent=2))
    return 0 if session.stop_result and session.stop_result.get("success", args.keep_session) else 2


if __name__ == "__main__":
    sys.exit(main())
