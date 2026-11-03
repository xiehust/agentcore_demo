"""Allowlisted SSM documents, deployed into every business account.

The agent never gets AWS-RunShellScript. It can only run:
  PatchDiagnose-Collect        read-only, parameter is an enum
  PatchRemediate-FreeDisk      dnf cache / journal / >500MB files under /var/tmp,/tmp
  PatchRemediate-DisableRepo   disable ONE non-Amazon repo (RepoId regex-validated, core repos refused)
  PatchRemediate-ReleaseRpmLock kill non-package-manager holders of the rpm lock and stale waiters
"""
import json
import sys

sys.path.insert(0, __import__("os").path.dirname(__file__) + "/..")
from common.config import DIAG_DOC, SPOKE_PROFILES, TAGS, log, session  # noqa: E402

# Shared helper: PIDs holding / waiting on the rpm transaction lock (fcntl locks are only visible in /proc/locks).
RPM_LOCK_PIDS = r'''
rpm_lock_pids() {  # $1 = holders|waiters
  f=/var/lib/rpm/.rpm.lock; [ -e "$f" ] || return 0
  ino=$(stat -c %i "$f")
  # /proc/locks: "1: POSIX ADVISORY WRITE <pid> <maj:min:ino> 0 EOF"; waiters have "->" as field 2.
  awk -v i="$ino" -v mode="$1" '{
     blocked=($2=="->"); split(blocked?$7:$6, a, ":"); if (a[3]!=i) next;
     pid=blocked?$6:$5;
     if ((mode=="waiters") == blocked) print pid }' /proc/locks | sort -u
}
'''

DIAG = RPM_LOCK_PIDS + r'''
c="{{ Check }}"
sec() { echo; echo "===== $1 ====="; }
if [ "$c" = disk ] || [ "$c" = all ]; then
  sec disk; df -h / /var 2>/dev/null; df -i / | tail -1
  # machine-readable line for the orchestrator's pre-patch headroom check
  df -Pm / | awk 'NR==2{gsub("%","",$5); printf "ROOT_FREE_PCT=%d ROOT_FREE_MB=%d\n", 100-$5, $4}'
  echo "-- largest under /var:"; du -xsh /var/* 2>/dev/null | sort -h | tail -5
  echo "-- files >200MB in /var/tmp /tmp:"; find /var/tmp /tmp -xdev -type f -size +200M -printf '%s %p\n' 2>/dev/null
fi
if [ "$c" = repos ] || [ "$c" = all ]; then
  sec repos; dnf -q repolist --all 2>&1 | head -30
  echo "-- non-amazon repo files:"; for f in /etc/yum.repos.d/*.repo; do grep -qi amazonlinux "$f" || { echo "## $f"; head -c 1500 "$f"; }; done
  echo "-- metadata check (30s):"; timeout 30 dnf -q makecache 2>&1 | tail -15; echo "makecache rc=$?"
fi
if [ "$c" = package_locks ] || [ "$c" = all ]; then
  sec package_locks
  for p in $(rpm_lock_pids holders); do echo "HOLDER pid=$p $(ps -o etimes=,comm=,args= -p $p 2>/dev/null)"; done
  for p in $(rpm_lock_pids waiters); do echo "WAITER pid=$p $(ps -o etimes=,comm=,args= -p $p 2>/dev/null)"; done
  echo "-- package manager processes:"; ps -eo pid,etimes,comm,args | grep -E 'dnf|rpm|yum' | grep -v grep | head -10
fi
if [ "$c" = dnf_log ] || [ "$c" = all ]; then
  sec dnf_log; tail -n 30 /var/log/dnf.log 2>/dev/null; tail -n 10 /var/log/dnf.rpm.log 2>/dev/null
fi
if [ "$c" = ssm_agent ] || [ "$c" = all ]; then
  sec ssm_agent; systemctl is-active amazon-ssm-agent; tail -n 15 /var/log/amazon/ssm/errors.log 2>/dev/null
fi
exit 0
'''

FREE_DISK = r'''
set -u
echo "before:"; df -h / | tail -1
dnf clean all -q 2>&1 | tail -2
journalctl --vacuum-size=50M 2>&1 | tail -1
echo "removing files >500MB under /var/tmp,/tmp:"
find /var/tmp /tmp -xdev -type f -size +500M -print -delete 2>/dev/null
echo "after:"; df -h / | tail -1
'''

DISABLE_REPO = r'''
set -u
repo="{{ RepoId }}"
case "$repo" in amazonlinux*|kernel-livepatch*) echo "REFUSED: $repo is a core Amazon Linux repo"; exit 3;; esac
f=$(grep -l "^\[$repo\]" /etc/yum.repos.d/*.repo 2>/dev/null | head -1)
[ -n "$f" ] || { echo "repo $repo not found"; exit 4; }
mkdir -p /var/backups/patchdemo && cp -p "$f" /var/backups/patchdemo/$(basename "$f").$(date +%s)
python3 - "$f" "$repo" <<'PY'
import configparser, sys
f, repo = sys.argv[1], sys.argv[2]
cp = configparser.RawConfigParser(); cp.optionxform = str; cp.read(f)
cp.set(repo, "enabled", "0")
with open(f, "w") as fh: cp.write(fh)
PY
echo "disabled $repo in $f (backup in /var/backups/patchdemo)"
timeout 60 dnf -q makecache 2>&1 | tail -5; echo "makecache rc=$?"
'''

RELEASE_LOCK = RPM_LOCK_PIDS + r'''
killed=0
for p in $(rpm_lock_pids holders); do
  comm=$(ps -o comm= -p "$p" 2>/dev/null); args=$(ps -o args= -p "$p" 2>/dev/null)
  case "$args" in *dnf*|*rpm*|*yum*) echo "SKIP holder pid=$p ($args): package manager in transaction";;
    *) echo "KILL holder pid=$p comm=$comm args=$args"; kill "$p"; sleep 2; kill -9 "$p" 2>/dev/null; killed=$((killed+1));; esac
done
# Waiters have not started a transaction yet; ones older than 5 min are orphans of a cancelled patch run.
for p in $(rpm_lock_pids waiters); do
  et=$(ps -o etimes= -p "$p" 2>/dev/null | tr -d ' '); [ -n "$et" ] && [ "$et" -gt 300 ] && {
    echo "KILL stale waiter pid=$p age=${et}s $(ps -o args= -p $p)"; kill -9 "$p"; killed=$((killed+1)); }
done
sleep 1; echo "killed=$killed"; echo "remaining holders: $(rpm_lock_pids holders | tr '\n' ' ')"
'''


def shell_doc(desc, script, params=None):
    return json.dumps({
        "schemaVersion": "2.2", "description": desc, "parameters": params or {},
        "mainSteps": [{"action": "aws:runShellScript", "name": "run", "precondition": {"StringEquals": ["platformType", "Linux"]},
                       "inputs": {"timeoutSeconds": "300", "runCommand": script.strip("\n").split("\n")}}]})


DOCS = {
    DIAG_DOC: shell_doc("Read-only patch diagnostics", DIAG, {"Check": {
        "type": "String", "default": "all", "allowedValues": ["disk", "repos", "package_locks", "dnf_log", "ssm_agent", "all"]}}),
    "PatchRemediate-FreeDisk": shell_doc("Free disk space for patching", FREE_DISK),
    "PatchRemediate-DisableRepo": shell_doc("Disable one broken non-Amazon repo", DISABLE_REPO, {"RepoId": {
        "type": "String", "allowedPattern": "^[A-Za-z0-9._-]{1,64}$"}}),
    "PatchRemediate-ReleaseRpmLock": shell_doc("Release rpm lock held by non package-manager processes", RELEASE_LOCK),
}


def main():
    for acct, profile in SPOKE_PROFILES.items():
        ssm = session(profile).client("ssm")
        for name, content in DOCS.items():
            try:
                ssm.create_document(Name=name, Content=content, DocumentType="Command", DocumentFormat="JSON", Tags=TAGS)
                log(acct, "created", name)
            except ssm.exceptions.DocumentAlreadyExists:
                try:
                    v = ssm.update_document(Name=name, Content=content, DocumentVersion="$LATEST")["DocumentDescription"]["DocumentVersion"]
                    ssm.update_document_default_version(Name=name, DocumentVersion=v)
                    log(acct, "updated", name, v)
                except ssm.exceptions.DuplicateDocumentContent:
                    log(acct, "unchanged", name)


if __name__ == "__main__":
    main()
