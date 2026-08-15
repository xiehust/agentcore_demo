# Implementation Plan

1. Create the standalone directory skeleton, pinned project metadata, ignore rules, ARM64 image, microVM deployment script, cleanup script, and documentation/report skeleton.
2. Port the application isolation/server behavior from demo 15, changing only microVM-appropriate workspace defaults and metadata.
3. Implement `scripts/runtime_session.py` for runtime config, SSE and command event parsing, bounded retries, shell transport, `/proc` monitor lifecycle, window statistics, atomic checkpoints, and session cleanup.
4. Implement `invoke_multiuser.py` with command-channel fingerprint verification and guaranteed stop cleanup.
5. Implement `load_test.py` for short-task concurrency levels and command-based resource monitoring.
6. Implement `load_test_longrun.py` for two-phase user tasks, resume checks, command-based deterministic artifact verification, resource monitoring, checkpoints, and cleanup.
7. Add isolation and runtime-session unit tests; make client helpers importable without live AWS calls.
8. Complete README and `results/REPORT.md` with architecture, IAM permissions, commands, baseline comparison, interpretation rules, costs, limitations, and an explicit pending-cloud-results section.
9. Run `python -m compileall`, `bash -n`, `unittest`, and focused help/smoke checks. Fix every failure.
10. Run a full-scope Trellis check and confirm demo 15 is unchanged.

## Validation commands

```bash
cd 16-shared-runtime-microvm
python3 -m compileall -q app scripts tests
python3 -m unittest discover -s tests -v
for file in scripts/*.sh; do bash -n "$file"; done
python3 scripts/load_test.py --help
python3 scripts/load_test_longrun.py --help
```

## Rollback points

- All implementation is confined to the new directory and this task's Trellis artifacts.
- No AWS mutation or billable invocation occurs during local validation.
- Remove `16-shared-runtime-microvm/` to roll back the code-only deliverable.

## Authorized AWS execution

11. Preflight account, region, execution role, ECR repository, model profile, and absence of a conflicting target Runtime.
12. Deploy `shared_runtime_microvm`, verify READY and confirm no Capacity Provider/filesystem configuration.
13. Run the three-user isolation smoke; stop and diagnose immediately if any check or cleanup fails.
14. Run short levels `2,4,8` in one fresh shared session and preserve the result JSON.
15. Run long level `1`, then `2,4`; append level `8` only if level 4 is fully verified with healthy memory and cleanup.
16. Parse all raw JSON and rewrite `results/REPORT.md` in Chinese with traceable tables and evidence limits.
17. Re-run local gates, verify every result cleanup record, delete the dedicated test Runtime, and verify control-plane deletion.

## Completion record (2026-08-14 UTC)

All 17 steps completed. The final AWS evidence is indexed in `16-shared-runtime-microvm/results/REPORT.md`: isolation 26/26, short 2/4/8 at 14/14, and long 1/2/4/8 at 15/15 deterministic end-to-end success. Local validation passed 45/45 tests plus compileall, Ruff, Pyright, shell syntax, CLI help, server import, and diff checks. All 11 JSON-recorded sessions report successful HTTP 200 cleanup. The dedicated Runtime was deleted and confirmed absent via `ResourceNotFoundException` and an empty filtered Runtime list; the ECR image was retained.

## Scale-40 completion record (2026-08-15 UTC)

Redeployed `shared_runtime_microvm-oGddGiDWRD` version 1 with `MAX_PARALLEL_AGENTS=40`. Short levels 12/16/24/32/40 passed 124/124. Long levels 12/16/24/32 passed 84/84 Agent, artifact, and verified checks; level 40 recorded 0/40 Agent, artifact, and verified successes, with every request missing its complete SSE event, monitor collection unavailable, and successful HTTP 200 session stop. The 32 level retained only 1144 MB. The report recommendation is no more than 24 active long-running Agents for operating headroom, with 32 treated as an edge and 40 as unavailable.

The second dedicated Runtime was deleted after validation. `GetAgentRuntime` returned `ResourceNotFoundException`, the filtered Runtime list was empty, all 10 recorded sessions had successful HTTP 200 stop responses, and the ECR image was retained at digest `sha256:ca626ff6df75493fbac6d1c7f47eaeca546051d55776fcfef22cc21251cb76c9`.
