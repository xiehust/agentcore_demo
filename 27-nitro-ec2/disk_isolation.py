#!/usr/bin/env python3
"""Hardware isolation/persistence test for two independent Nitro raw disks."""
from __future__ import annotations
import argparse
import concurrent.futures
import contextlib
import datetime as dt
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import benchmark as common

NETWORKS = {"A": ("198.51.100.1", "198.51.100.2"), "B": ("203.0.113.1", "203.0.113.2")}

def slots():
    return int((common.SYSFS / "pcie_slots_in_use").read_text().strip())


def wait_slots(expected, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if slots() == expected:
            return
        time.sleep(.05)
    raise RuntimeError(f"hardware slots did not reach {expected}")


def validate_backends(images):
    identities = [file.stat() for file in images]
    if len({(stat.st_dev, stat.st_ino) for stat in identities}) != len(images):
        raise ValueError("one writable backend may not be assigned to multiple Sandboxes")
    if any(not file.is_file() or file.is_symlink() for file in images):
        raise ValueError("test backends must be regular, non-symlink files")


def create_disk(file):
    # Exclusive creation: mkfs NEVER receives an existing file or /dev device.
    file.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with file.open("xb") as image:
        os.fchmod(image.fileno(), 0o600)
        image.truncate(2 * 1024 * 1024 * 1024)
    common.command("mkfs.ext4", "-F", "-q", str(file))
    return {"path": str(file), "logical_bytes": file.stat().st_size,
            "uuid": common.command("blkid", "-p", "-s", "UUID", "-o", "value", str(file))}


@contextlib.contextmanager
def private_network(sid):
    token = uuid.uuid4().hex[:10]
    tap, table = "dsh" + sid.lower() + token, "dshb_disk_" + token
    host, guest = NETWORKS[sid]
    subnet = ipaddress.ip_network(host + "/30", strict=False)
    routes = json.loads(common.command("ip", "-j", "route", "show", "table", "all"))
    if any(ipaddress.ip_network(r["dst"], strict=False).overlaps(subnet)
           for r in routes if r.get("dst", "default") != "default"):
        raise RuntimeError("test subnet already has a route")
    tables = json.loads(common.command("nft", "-j", "list", "tables"))["nftables"]
    if any(entry.get("table", {}).get("name") == table for entry in tables):
        raise RuntimeError("test table collision")
    created = False
    rules_attempted = False
    try:
        common.command("ip", "tuntap", "add", "dev", tap, "mode", "tap")
        created = True
        rules_attempted = True
        subprocess.run(["nft", "-f", "-"], input=common.firewall_rules(table, tap, False, ""),
                       check=True, text=True, capture_output=True, timeout=15)
        common.command("ip", "addr", "add", host + "/30", "dev", tap)
        common.command("ip", "link", "set", tap, "up")
        yield {"tap": tap, "table": table, "host": host, "guest": guest}
    finally:
        # Keep protection if tearing down connectivity fails.
        if created:
            common.command("ip", "link", "delete", tap)
        if rules_attempted:
            actual = json.loads(common.command("nft", "-j", "list", "tables"))["nftables"]
            if any(entry.get("table", {}).get("name") == table for entry in actual):
                common.command("nft", "delete", "table", "inet", table)


def vm_config(args, sid, run_id, image, net):
    return {"boot-source": {"kernel_image_path": str(args.kernel.resolve()),
                             "initrd_path": str(args.initrd.resolve()),
                             "boot_args": f"panic=1 initcall_blacklist=i8042_init bench_id={run_id} sandbox_id={sid} "
                                          f"guest_ip={net['guest']} host_ip={net['host']}"},
            "machine-config": {"vcpu_count": 2, "mem_size_mib": 2048},
            "drives": [{"drive_id": "workspace", "path_on_host": str(image.resolve()),
                        "is_root_device": False, "is_read_only": False, "cache_type": "Writeback"}],
            "network-interfaces": [{"iface_id": "eth0", "host_dev_name": net["tap"]}],
            "enclave": {"cpu_overcommit": True, "direct_mode": args.direct_mode, "debug_mode": False}}


class VM:
    def __init__(self, args, sid, generation, image, net):
        self.args, self.sid, self.image, self.net = args, sid, image, net
        self.run_id = uuid.uuid4().hex
        self.directory = args.out / f"{sid}-{generation}"
        self.proc = None
        self.log = None
        self.identity = None
        self.shutdown = None

    def start(self):
        self.directory.mkdir()
        cfg = self.directory / "sandbox.json"
        cfg.write_text(json.dumps(vm_config(self.args, self.sid, self.run_id, self.image, self.net), indent=2))
        self.log = (self.directory / "firecracker.log").open("wb")
        self.proc = subprocess.Popen([self.args.firecracker, "--no-api", "--config-file", str(cfg.resolve())],
                                     stdout=self.log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     env={"PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")})
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("Firecracker exited; inspect the saved log")
            try:
                data = common.request(f"http://{self.net['guest']}:8090/health", timeout=.5)
                if data.get("run_id") != self.run_id or data.get("sandbox_id") != self.sid:
                    raise RuntimeError("wrong guest identity")
                parts = (data.get("mount") or "").split()
                if len(parts) < 4 or parts[:3] != ["/dev/vda", "/workspace", "ext4"] or "rw" not in parts[3].split(","):
                    raise RuntimeError("workspace is not the writable ext4 data disk")
                if [d for d in data.get("block_devices", []) if d.startswith("vd")] != ["vda"] or not data.get("filesystem_uuid"):
                    raise RuntimeError("unexpected guest block devices or UUID")
                self.identity = data
                (self.directory / "health.json").write_text(json.dumps(data, indent=2))
                return self
            except OSError:
                time.sleep(.05)
        raise TimeoutError("disk guest did not become healthy")

    def call(self, route, **payload):
        # Never replay a POST after a timeout. Every write has one attempt.
        data = common.request(f"http://{self.net['guest']}:8090{route}", payload, timeout=15)
        if data.get("run_id") != self.run_id or data.get("boot_id") != self.identity["boot_id"]:
            raise RuntimeError("operation returned a different guest")
        return data

    def close(self):
        if self.proc is None:
            return
        error = None
        try:
            if self.proc.poll() is None and self.identity:
                self.shutdown = self.call("/quiesce")
                if self.shutdown.get("unmounted") is not True:
                    raise RuntimeError("guest did not unmount its data disk")
        except Exception as err:
            error = err
        finally:
            common.stop(self.proc)
            self.proc = None
            if self.log:
                self.log.close()
            # The guest already fsynced and unmounted. Flush the parent backend too.
            with self.image.open("rb") as image:
                os.fsync(image.fileno())
            (self.directory / "shutdown.json").write_text(json.dumps({"response": self.shutdown,
                "error": None if error is None else type(error).__name__}, indent=2))
        if error:
            raise error


def write(vm, scope, file, text):
    result = vm.call("/write", scope=scope, file=file, text=text)
    if result.get("text") != text or result.get("fsynced") is not True:
        raise RuntimeError("write acknowledgement does not match")
    return result


def read(vm, scope, file):
    return vm.call("/read", scope=scope, file=file)


def exercise(args, document, persist):
    token = uuid.uuid4().hex
    root = args.disk_root.resolve() / token
    root.mkdir(parents=True, mode=0o700)
    images = {sid: root / (sid + ".ext4") for sid in NETWORKS}
    document["disks"] = {sid: create_disk(image) for sid, image in images.items()}
    validate_backends(list(images.values()))
    try:
        validate_backends([images["A"], images["A"]])
    except ValueError:
        document["duplicate_backend_refused"] = True
    else:
        raise AssertionError("duplicate writable backend was accepted")
    leases = []
    for image in images.values():
        lease = image.with_suffix(".lease").open("a")
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        leases.append(lease)
    vms = []
    cleanup_errors = []

    def check(name, condition, **evidence):
        row = {"name": name, "passed": bool(condition), "slots_in_use": slots(), **evidence}
        document["checks"].append(row)
        persist()
        if not condition:
            raise AssertionError(name)

    def start(sid, generation, net):
        vm = VM(args, sid, generation, images[sid], net)
        vms.append(vm)  # Register BEFORE launch so partial starts are cleaned up.
        stack.callback(vm.close)  # VM shutdown precedes TAP removal even on failure.
        vm.start()
        document["boots"].append({"generation": generation, **vm.identity})
        return vm

    try:
        with contextlib.ExitStack() as stack:
            nets = {sid: stack.enter_context(private_network(sid)) for sid in NETWORKS}
            document["networks"] = nets
            a, b = start("A", "initial", nets["A"]), start("B", "initial", nets["B"])
            wait_slots(2)
            check("two_hardware_sandboxes_live", slots() == 2 and a.identity["boot_id"] != b.identity["boot_id"],
                  A=a.identity, B=b.identity)
            check("independent_filesystem_UUIDs", a.identity["filesystem_uuid"] != b.identity["filesystem_uuid"]
                  and all(vm.identity["filesystem_uuid"] == document["disks"][vm.sid]["uuid"] for vm in (a, b)))
            for vm in (a, b):
                result = vm.call("/dsh-help")
                check("stock_DSH_help_" + vm.sid, result.get("stock_help") is True and result.get("dsh_home") == "/workspace/.dsh")
            values = {sid: sid + "-initial-" + token for sid in NETWORKS}
            write(a, "workspace", "shared.txt", values["A"])
            check("B_does_not_see_A_new_file", read(b, "workspace", "shared.txt")["exists"] is False)
            write(b, "workspace", "shared.txt", values["B"])
            check("same_path_different_contents", read(a, "workspace", "shared.txt")["text"] == values["A"]
                  and read(b, "workspace", "shared.txt")["text"] == values["B"], values=values.copy())
            for vm in (a, b):
                write(vm, "workspace", "only-" + vm.sid + ".txt", values[vm.sid])
                write(vm, "home", "private.marker", values[vm.sid])
                write(vm, "volatile", "ram.marker", values[vm.sid])
            check("A_only_file_absent_in_B", read(b, "workspace", "only-A.txt")["exists"] is False)
            check("B_only_file_absent_in_A", read(a, "workspace", "only-B.txt")["exists"] is False)
            check("DSH_HOME_private", all(read(vm, "home", "private.marker")["text"] == values[vm.sid] for vm in (a, b)))
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                for i in range(args.concurrent_rounds):
                    values = {sid: f"{sid}-concurrent-{i}-{token}" for sid in NETWORKS}
                    futures = [pool.submit(write, vm, "workspace", "shared.txt", values[vm.sid]) for vm in (a, b)]
                    for future in futures:
                        future.result()  # A failed/ambiguous write is NOT retried.
                    check("concurrent_write_round_" + str(i), slots() == 2 and all(
                          read(vm, "workspace", "shared.txt")["text"] == values[vm.sid] for vm in (a, b)),
                          expected=values.copy())
            old_a, old_b = a.identity.copy(), b.identity.copy()
            a.close()
            wait_slots(1)
            check("B_unaffected_while_A_stopped", read(b, "workspace", "shared.txt")["text"] == values["B"])
            a = start("A", "rebuilt", nets["A"])
            wait_slots(2)
            check("A_new_boot_same_disk_data", a.identity["boot_id"] != old_a["boot_id"]
                  and a.identity["filesystem_uuid"] == old_a["filesystem_uuid"]
                  and read(a, "workspace", "shared.txt")["text"] == values["A"])
            check("A_DSH_HOME_persisted", read(a, "home", "private.marker")["text"] == "A-initial-" + token)
            check("A_RAM_not_persisted", read(a, "volatile", "ram.marker")["exists"] is False)
            check("B_same_live_boot_after_A_rebuild", b.identity["boot_id"] == old_b["boot_id"]
                  and read(b, "volatile", "ram.marker")["text"] == "B-initial-" + token)
            b.close()
            wait_slots(1)
            b = start("B", "rebuilt", nets["B"])
            wait_slots(2)
            check("B_new_boot_same_disk_data", b.identity["boot_id"] != old_b["boot_id"]
                  and b.identity["filesystem_uuid"] == old_b["filesystem_uuid"]
                  and read(b, "workspace", "shared.txt")["text"] == values["B"])
            check("B_DSH_HOME_persisted", read(b, "home", "private.marker")["text"] == "B-initial-" + token)
            check("B_RAM_not_persisted", read(b, "volatile", "ram.marker")["exists"] is False)
            write(a, "workspace", "shared.txt", "A-after-rebuild-" + token)
            check("isolation_after_both_rebuilt", read(b, "workspace", "shared.txt")["text"] == values["B"])
            # Stop and unmount all guests BEFORE the network contexts or offline fsck.
            for vm in reversed(vms):
                vm.close()
            wait_slots(0)
    finally:
        for vm in reversed(vms):
            try:
                vm.close()
            except Exception as err:
                cleanup_errors.append(type(err).__name__)
        for lease in leases:
            lease.close()
        document["cleanup_errors"] = cleanup_errors
        document["slots_after"] = slots()
        persist()
    if cleanup_errors:
        raise RuntimeError("guest cleanup failed")
    for sid, image in images.items():
        result = subprocess.run(["e2fsck", "-f", "-n", str(image)], capture_output=True, text=True, timeout=60)
        (args.out / (sid + "-e2fsck.txt")).write_text(result.stdout + result.stderr)
        check("offline_ext4_clean_" + sid, result.returncode == 0, exit_code=result.returncode)
        # The parent administrator can read its backends; isolation is not confidentiality.
        text = common.command("debugfs", "-R", "cat /shared.txt", str(image))
        expected = "A-after-rebuild-" + token if sid == "A" else values["B"]
        check("parent_can_read_backend_" + sid, text == expected, observed=text)
        document["disks"][sid]["allocated_bytes"] = image.stat().st_blocks * 512


def main():
    p = common.parser()
    p.description = __doc__
    p.add_argument("--disk-root", type=Path, default=Path("private-disks"))
    p.add_argument("--concurrent-rounds", type=common.positive_int, default=10)
    args = p.parse_args()
    if args.concurrent_rounds > 20:
        p.error("concurrent rounds must be <= 20")
    if os.geteuid() != 0:
        p.error("run on the prepared parent as root")
    for tool in ("mkfs.ext4", "blkid", "e2fsck", "debugfs"):
        if not shutil.which(tool):
            p.error("missing filesystem tool: " + tool)
    with open("/run/dsh-nitro-benchmark.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        host = common.preflight(args)
        args.out = args.out.resolve()
        args.out.mkdir(parents=True, mode=0o700, exist_ok=False)
        document = {"schema_version": 1, "tested_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "mode": "direct" if args.direct_mode else "confidential", "host": host,
                    "memory_mib_per_VM": 2048, "vcpus_per_VM": 2, "concurrent_rounds_requested": args.concurrent_rounds,
                    "checks": [], "boots": [], "status": "running"}
        def persist():
            (args.out / "results.json").write_text(json.dumps(document, indent=2) + "\n")
        persist()
        try:
            exercise(args, document, persist)
            document["status"] = "passed"
        except Exception as err:
            document["status"] = "failed"
            document["error"] = str(err)
            raise
        finally:
            document["checks_passed"] = sum(row["passed"] for row in document["checks"])
            document["checks_total"] = len(document["checks"])
            document["slots_after"] = slots()
            persist()
            print(json.dumps({k: document[k] for k in ("mode", "status", "checks_passed", "checks_total", "slots_after")}), flush=True)
            print("Evidence: " + str(args.out / "results.json"), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
