#!/usr/bin/env python3
"""Probe FUSE in an isolated session of the existing Instances test runtime.

No packages are installed and no existing Runtime/provider configuration is changed.
The warm-up intentionally omits user_id so this runtime rejects it before any LLM call.
"""
from __future__ import annotations

import json
import runpy
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from runtime_cmd import RuntimeSession

ROOT = Path(__file__).resolve().parents[1]
REGION = "us-west-2"
PROVIDER_ID = "capacity_provider_arm_m7g_large-1HB6aXJTVr"
RUNTIME_ID = "shared_runtime_multiuser_m7g-EZpQed4lPW"
OUT = ROOT / "results" / "instances_fuse_probe.json"

EXTRA_SCRIPT = r"""
python3 - <<'PY'
import ctypes, json, os, pathlib, shutil, subprocess, tempfile

def read(path):
    try:
        return pathlib.Path(path).read_text()[:12000]
    except OSError as exc:
        return str(exc)

def command(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=15)
        return dict(rc=p.returncode, stdout=p.stdout[:8000], stderr=p.stderr[:2000])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return dict(error=str(exc))

r = {
    'os_release': read('/etc/os-release'),
    'uid_map': read('/proc/self/uid_map'),
    'pid1_status': read('/proc/1/status'),
    'self_cgroup': read('/proc/self/cgroup'),
    'devices': read('/proc/devices'),
    'kernel_config': command(['sh', '-c', 'for p in /boot/config-$(uname -r) /proc/config.gz; do if [ -r "$p" ]; then case "$p" in *.gz) zcat "$p";; *) cat "$p";; esac | grep -E "CONFIG_(FUSE_FS|CUSE|MODULES)="; fi; done']),
    'fuse_module_info': command(['modinfo', 'fuse']),
    'fuse_module_loaded': os.path.isdir('/sys/module/fuse'),
    'fuse_libraries': command(['sh', '-c', 'ldconfig -p 2>/dev/null | grep -i fuse']),
    'tools': {x: shutil.which(x) for x in ['gcc','cc','fusermount','fusermount3','mount-s3','rclone','juicefs']},
}
# A real mount request includes an open FUSE fd; mount -t fuse without it is not sufficient.
fd = None
mount_path = tempfile.mkdtemp(prefix='instances-fuse-mount-')
mounted = False
libc = ctypes.CDLL(None, use_errno=True)
libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p]
libc.mount.restype = ctypes.c_int
libc.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
libc.umount2.restype = ctypes.c_int
try:
    fd = os.open('/dev/fuse', os.O_RDWR)
    r['fuse_open'] = {'ok': True}
    opts = f'fd={fd},rootmode=40000,user_id={os.getuid()},group_id={os.getgid()}'
    rc = libc.mount(b'instances-fuse-probe', os.fsencode(mount_path), b'fuse', 6, opts.encode())
    err = ctypes.get_errno() if rc else 0
    mounted = rc == 0
    r['real_fuse_mount'] = dict(rc=rc, errno=err, error=os.strerror(err), mounted=mounted)
    if mounted:
        r['mountinfo'] = [x for x in read('/proc/self/mountinfo').splitlines() if mount_path in x]
        # This check proves mount acceptance, not a userspace daemon's read/write behavior.
except OSError as exc:
    r['fuse_open'] = dict(ok=False, errno=exc.errno, error=str(exc))
    r['real_fuse_mount'] = dict(mounted=False, skipped='Cannot open /dev/fuse')
finally:
    if mounted:
        rc = libc.umount2(os.fsencode(mount_path), 2)
        r['unmount'] = dict(rc=rc, errno=ctypes.get_errno() if rc else 0)
    if fd is not None:
        os.close(fd)
    try:
        os.rmdir(mount_path)
    except OSError as exc:
        r['directory_cleanup_error'] = str(exc)
print('__EXTRA_JSON_BEGIN__')
print(json.dumps(r))
print('__EXTRA_JSON_END__')
PY
"""


def save(record):
    OUT.write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str) + '\n')


def clean_response(response):
    return {k: v for k, v in response.items() if k != 'ResponseMetadata'}

def main():
    control = boto3.client('bedrock-agentcore-control', region_name=REGION)
    provider = clean_response(control.get_capacity_provider(capacityProviderId=PROVIDER_ID))
    runtime = control.get_agent_runtime(agentRuntimeId=RUNTIME_ID)
    assert provider['status'] == runtime['status'] == 'READY'
    assert runtime['capacityProviderConfiguration']['capacityProviderArn'] == provider['capacityProviderArn']
    record = {
        'probed_at': datetime.now(timezone.utc).isoformat(),
        'region': REGION,
        'provider': provider,
        'runtime': {k: runtime[k] for k in ('agentRuntimeArn', 'agentRuntimeVersion', 'agentRuntimeArtifact', 'capacityProviderConfiguration')},
        'scope': 'Existing container runtime, new isolated session; no package installation or configuration changes',
    }
    probe = runpy.run_path(str(ROOT / 'scripts' / '01-fuse-probe.py'))
    session = RuntimeSession(runtime['agentRuntimeArn'], REGION)
    record['session_id'] = session.session_id
    save(record)
    try:
        with session:
            print('Activating isolated session', session.session_id, flush=True)
            try:
                record['warm_up'] = session.warm_up({'prompt': 'ping'})
            except ClientError as exc:
                # This app requires user_id; a 400 from the app starts the session without calling a model.
                record['warm_up_error'] = str(exc)
                if '400' not in str(exc):
                    raise
            save(record)
            print('Running capability probe', flush=True)
            result = session.run_script(probe['PROBE_SCRIPT'], timeout=180)
            record['command'] = {k: v for k, v in result.items() if k != 'stdout'}
            record['report'] = probe['extract_report'](result['stdout'])
            # Do not reuse the old microVM-specific verdict for Instances.
            record['summary'] = probe['summarize'](record['report'])
            record['summary'].pop('verdict', None)
            # rc=32 alone also means permission denied; it does not prove NFS connectivity.
            nfs_output = record['report']['mount_nfs4_loopback_attempt']['out']
            record['summary']['nfs_client_path_reachable'] = 'connection refused' in nfs_output.lower()
            save(record)
            print('Checking real FUSE mount and container context', flush=True)
            result = session.run_script(EXTRA_SCRIPT, timeout=90)
            record['extra_command'] = {k: v for k, v in result.items() if k != 'stdout'}
            raw = result['stdout'].split('__EXTRA_JSON_BEGIN__', 1)[1].split('__EXTRA_JSON_END__', 1)[0]
            record['extra'] = json.loads(raw)
            save(record)
    except Exception as exc:
        record['error'] = f'{type(exc).__name__}: {exc}'
        if hasattr(exc, 'result'):
            record['failed_command'] = exc.result
    finally:
        record['session_cleanup'] = session.stop_result
        save(record)
        if (session.stop_result or {}).get('success'):
            try:
                response = session.client.delete_capacity_provider_session(
                    capacityProviderId=PROVIDER_ID, sessionId=session.session_id,
                )
                record['capacity_session_cleanup'] = clean_response(response)
            except ClientError as exc:
                record['capacity_session_cleanup'] = {'error': str(exc)}
            save(record)
    print(json.dumps({k: v for k, v in record.items() if k in ('summary', 'extra', 'error', 'session_cleanup', 'capacity_session_cleanup')}, indent=2), flush=True)
    print('Saved', OUT, flush=True)
    cleanup = record.get('capacity_session_cleanup', {})
    accepted = cleanup.get('status') in ('Deprovisioning', 'Deleting', 'Deleted')
    return 1 if record.get('error') or not (session.stop_result or {}).get('success') or not accepted else 0


if __name__ == '__main__':
    raise SystemExit(main())

