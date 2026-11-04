r"""Allowlisted SSM documents, deployed into every business account.

The agent never gets AWS-RunShellScript / AWS-RunPowerShellScript. It can only run:
  PatchDiagnose-Collect        read-only, parameter is an enum            [Linux step + Windows step]
  PatchRemediate-FreeDisk      Linux: dnf cache / journal / >500MB files under /var/tmp,/tmp
                               Windows: >500MB files under C:\Windows\Temp,C:\Temp / old temp files /
                               SoftwareDistribution\Download                      [Linux step + Windows step]
  PatchRemediate-DisableRepo   disable ONE non-Amazon repo (RepoId regex-validated, core repos refused)   [Linux]
  PatchRemediate-ReleaseRpmLock kill non-package-manager holders of the rpm lock and stale waiters        [Linux]
  PatchRemediate-StartUpdateServices  Disabled -> Manual and start wuauserv / bits / cryptsvc / TrustedInstaller [Windows]

Multi-platform documents carry one step per platform; SSM runs only the step whose platformType precondition
matches the instance, so the same document name (and IAM allowlist entry) works on both.
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


# ------------------------------------------------------------------ Windows (PowerShell 5.1 on Windows Server)
WIN_DIAG = r'''
$ErrorActionPreference = 'Continue'
$c = '{{ Check }}'
function Sec($n) { ''; "===== $n =====" }
function On($n) { ($c -eq $n) -or ($c -eq 'all') }
# PS 5.1: Get-ChildItem -Recurse on a MISSING path searches the whole drive for that name (~1 min) -> only existing dirs
function TempFiles { foreach ($p in 'C:\Windows\Temp', 'C:\Temp') { if (Test-Path $p) { Get-ChildItem $p -File -Recurse -Force -ErrorAction SilentlyContinue } } }
function Short($m) { $m = ($m -replace '\s+', ' ').Trim(); if ($m.Length -gt 220) { $m.Substring(0, 220) } else { $m } }
if (On 'disk') {
  Sec disk
  $d = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"
  "C: size={0:N1}GB free={1:N1}GB" -f ($d.Size / 1GB), ($d.FreeSpace / 1GB)
  # machine-readable line for the orchestrator's pre-patch headroom check (same format as the Linux step)
  "ROOT_FREE_PCT={0} ROOT_FREE_MB={1}" -f [int][math]::Floor(100 * $d.FreeSpace / $d.Size), [int][math]::Floor($d.FreeSpace / 1MB)
  '-- files >200MB in C:\Windows\Temp C:\Temp:'
  TempFiles | Where-Object Length -gt 200MB | ForEach-Object { '{0} {1}' -f $_.Length, $_.FullName }
  $sd = 'C:\Windows\SoftwareDistribution\Download'
  if (Test-Path $sd) {
    $mb = (Get-ChildItem $sd -Recurse -Force -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum / 1MB
    '-- SoftwareDistribution\Download: {0:N0} MB' -f $mb
  }
}
if (On 'update_services') {
  Sec update_services
  foreach ($s in 'wuauserv', 'bits', 'cryptsvc', 'TrustedInstaller', 'msiserver') {
    $svc = Get-CimInstance Win32_Service -Filter "Name='$s'"
    if ($svc) { 'SERVICE {0} state={1} start={2}' -f $s, $svc.State, $svc.StartMode } else { "SERVICE $s not found" }
  }
  '-- installer processes:'
  Get-Process -Name msiexec, TiWorker, wusa, dism -ErrorAction SilentlyContinue |
    ForEach-Object { '{0} pid={1} started={2:s}' -f $_.Name, $_.Id, $_.StartTime }
}
if (On 'pending_reboot') {
  Sec pending_reboot
  'CBS RebootPending: {0}' -f (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending')
  'WU RebootRequired: {0}' -f (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired')
  $p = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -Name PendingFileRenameOperations -ErrorAction SilentlyContinue).PendingFileRenameOperations
  'PendingFileRenameOperations: {0} (informational: common on healthy hosts, not a reboot requirement by itself)' -f [bool]$p
  'LastBoot: {0:s}' -f (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
}
if (On 'update_source') {
  Sec update_source
  $wu = Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate' -ErrorAction SilentlyContinue
  $au = Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' -ErrorAction SilentlyContinue
  'WUServer={0} UseWUServer={1} DisableWindowsUpdateAccess={2}' -f $wu.WUServer, $au.UseWUServer, $wu.DisableWindowsUpdateAccess
  $targets = @(@{h = 'download.windowsupdate.com'; p = 443 }, @{h = 'fe2.update.microsoft.com'; p = 443 })
  if ($wu.WUServer) { $u = [uri]$wu.WUServer; $targets = @(@{h = $u.Host; p = $u.Port }) + $targets }
  foreach ($t in $targets) {
    $ok = $false
    try { $cl = New-Object Net.Sockets.TcpClient; $ok = $cl.ConnectAsync($t.h, $t.p).Wait(5000); $cl.Close() } catch { }
    'CONNECT {0}:{1} ok={2}' -f $t.h, $t.p, $ok
  }
}
if (On 'patch_log') {
  Sec patch_log
  $l = Get-ChildItem 'C:\ProgramData\Amazon\PatchBaselineOperations\Logs' -File -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if ($l) {  # error lines only, de-duplicated (stack traces would overflow the 2500-char output limit)
    "## $($l.Name)"
    Get-Content $l.FullName -Tail 400 | Where-Object { $_ -match '0x8[0-9A-Fa-f]{7}|\[ERROR\]|Error Message' -and $_ -notmatch 'Stack Trace|^\S+ \[\w+\] -\s+at ' } |
      ForEach-Object { ($_ -replace '^\S+ ', '').Trim() } | Select-Object -Unique | Select-Object -Last 8
  }
  '-- WindowsUpdateClient errors/warnings (System log, last 8):'
  Get-WinEvent -FilterHashtable @{ LogName = 'System'; ProviderName = 'Microsoft-Windows-WindowsUpdateClient'; Level = 2, 3 } -MaxEvents 8 -ErrorAction SilentlyContinue |
    ForEach-Object { '{0:s} id={1} {2}' -f $_.TimeCreated, $_.Id, (Short $_.Message) }
}
if (On 'ssm_agent') {
  Sec ssm_agent
  Get-Service AmazonSSMAgent | ForEach-Object { 'SERVICE AmazonSSMAgent state={0} start={1}' -f $_.Status, $_.StartType }
  # registration retries of the hybrid-activation path are benign on EC2; keep the output under the 2500-char API limit
  Get-Content 'C:\ProgramData\Amazon\SSM\Logs\errors.log' -Tail 200 -ErrorAction SilentlyContinue |
    Where-Object { $_ -match 'ERROR' -and $_ -notmatch 'RegisterManagedInstance|Registrar' } | Select-Object -Last 6
}
exit 0
'''

WIN_FREE_DISK = r'''
$ErrorActionPreference = 'Continue'
# PS 5.1: Get-ChildItem -Recurse on a MISSING path searches the whole drive for that name (~1 min) -> only existing dirs
function TempFiles { foreach ($p in 'C:\Windows\Temp', 'C:\Temp') { if (Test-Path $p) { Get-ChildItem $p -File -Recurse -Force -ErrorAction SilentlyContinue } } }
function Free { $x = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='C:'"; 'C: free={0:N0} MB ({1:N0}%)' -f ($x.FreeSpace / 1MB), (100 * $x.FreeSpace / $x.Size) }
'before:'; Free
'removing files >500MB under C:\Windows\Temp,C:\Temp:'
TempFiles | Where-Object Length -gt 500MB | ForEach-Object { $_.FullName; Remove-Item $_.FullName -Force -ErrorAction Continue }
$old = Get-ChildItem 'C:\Windows\Temp' -File -Recurse -Force -ErrorAction SilentlyContinue | Where-Object LastWriteTime -lt (Get-Date).AddDays(-7)
'removing {0} temp files older than 7 days' -f @($old).Count
$old | Remove-Item -Force -ErrorAction SilentlyContinue
'clearing SoftwareDistribution\Download (wuauserv stopped while clearing):'
$was = (Get-Service wuauserv).Status
Stop-Service wuauserv -Force -ErrorAction SilentlyContinue
Remove-Item 'C:\Windows\SoftwareDistribution\Download\*' -Recurse -Force -ErrorAction SilentlyContinue
if ($was -eq 'Running') { Start-Service wuauserv -ErrorAction SilentlyContinue }
'after:'; Free
'''

WIN_START_UPDATE_SERVICES = r'''
$ErrorActionPreference = 'Continue'
foreach ($s in 'wuauserv', 'bits', 'cryptsvc', 'TrustedInstaller') {
  $svc = Get-CimInstance Win32_Service -Filter "Name='$s'"
  if (-not $svc) { "SERVICE $s not found"; continue }
  if ($svc.StartMode -eq 'Disabled') { "SERVICE $s start=Disabled -> Manual"; Set-Service $s -StartupType Manual }
  if ($svc.State -ne 'Running') { "SERVICE $s starting"; Start-Service $s -ErrorAction Continue }
}
Start-Sleep 2
Get-Service wuauserv, bits, cryptsvc, TrustedInstaller | ForEach-Object { 'SERVICE {0} state={1} start={2}' -f $_.Name, $_.Status, $_.StartType }
'''


LINUX, WINDOWS = "Linux", "Windows"


def step(platform, script):
    action, name = ("aws:runShellScript", "linux") if platform == LINUX else ("aws:runPowerShellScript", "windows")
    return {"action": action, "name": name, "precondition": {"StringEquals": ["platformType", platform]},
            "inputs": {"timeoutSeconds": "300", "runCommand": script.strip("\n").split("\n")}}


def ssm_doc(desc, steps, params=None):
    return json.dumps({"schemaVersion": "2.2", "description": desc, "parameters": params or {}, "mainSteps": steps})


LINUX_CHECKS = ["disk", "repos", "package_locks", "dnf_log", "ssm_agent", "all"]
WINDOWS_CHECKS = ["disk", "update_services", "pending_reboot", "update_source", "patch_log", "ssm_agent", "all"]

DOCS = {
    DIAG_DOC: ssm_doc("Read-only patch diagnostics", [step(LINUX, DIAG), step(WINDOWS, WIN_DIAG)], {"Check": {
        "type": "String", "default": "all", "allowedValues": list(dict.fromkeys(LINUX_CHECKS + WINDOWS_CHECKS))}}),
    "PatchRemediate-FreeDisk": ssm_doc("Free disk space for patching", [step(LINUX, FREE_DISK), step(WINDOWS, WIN_FREE_DISK)]),
    "PatchRemediate-DisableRepo": ssm_doc("Disable one broken non-Amazon repo", [step(LINUX, DISABLE_REPO)], {"RepoId": {
        "type": "String", "allowedPattern": "^[A-Za-z0-9._-]{1,64}$"}}),
    "PatchRemediate-ReleaseRpmLock": ssm_doc("Release rpm lock held by non package-manager processes", [step(LINUX, RELEASE_LOCK)]),
    "PatchRemediate-StartUpdateServices": ssm_doc("Re-enable (Disabled -> Manual) and start Windows Update / Modules Installer services",
                                                  [step(WINDOWS, WIN_START_UPDATE_SERVICES)]),
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
