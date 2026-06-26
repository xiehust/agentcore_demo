# State: AgentCore harness → evaluation → optimization closed-loop demo

**Status:** COMPLETE
**Current phase:** 6 (all done) — AUDIT_COMPLETE (round 1, no gaps, 88.7% coverage)
**Started:** 2026-06-26
**Last update:** 2026-06-26
**Run root:** .supergoal/agentcore-harness-eval-optimization-clos-us4IuK
**Baseline ref:** e83cdd0c2cef203a1193f223c960e8d37f683904

## Phase progress

| # | Phase | Status | Started | Completed | Notes |
|---|-------|--------|---------|-----------|-------|
| 1 | Preflight & scaffold | completed | 2026-06-26 | 2026-06-26 | 6/6 pass; full AgentCore API surface present in boto3 1.43.36; model=haiku-4-5; TxnSearch ACTIVE |
| 2 | Build support agent + local smoke | completed | 2026-06-26 | 2026-06-26 | 5/5 pass; 9 unit tests; smoke vs real Bedrock w/ correct multi-tool use |
| 3 | Deploy to AgentCore | completed | 2026-06-26 | 2026-06-26 | 5/5 pass; Runtime acmesupport-fOfv652Bjq live (obs on) + managed Harness AcmeSupportHarness-9dw7DNYPDv invoked for real |
| 4 | Generate sessions + baseline evaluation | completed | 2026-06-26 | 2026-06-26 | 5/5 pass; eval COMPLETED 10/10; baseline GoalSuccessRate 0.9 / Faithfulness 1.0 / Helpfulness 0.88. Required ADOT instrumentation fix + redeploy. |
| 5 | Optimization closed loop | completed | 2026-06-26 | 2026-06-26 | 6/6 pass; recommendation→apply→redeploy→re-eval→compare; loop_closed=True (GoalSuccessRate held 0.9; honest analysis of near-ceiling baseline) |
| 6 | Docs, diagram, teardown & Polish/Harden | completed | 2026-06-26 | 2026-06-26 | 8/8 pass; README runbook + CONCEPTS + diagram (svg+png) + teardown; secrets/cleanliness clean; 36MB build cache gitignored |

## Engineering check status

Updated by each phase as it runs. Cleared at the start of the next phase, so this always reflects the **most recent** engineering check.

- Build/compile: PASS (phase 5 — compile all)
- Lint (ruff): PASS (phase 5)
- Tests (pytest): PASS (phase 5 — 9 passed; prompts.py change safe)
- Scripts smoke: PASS (phase 5 — optimization + compare + ab_test; improved eval COMPLETED 10/10)

## Notable events

Append-only log of anything noteworthy during execution.

- 2026-06-26 — Plan locked, 6 phases. Mode: Full real AgentCore · customer-support FAQ agent · thorough.
- 2026-06-26 — Pre-flight green: 7/7 foundational tools (uv, python3, node v26, npm, aws CLI, valid creds acct 434444145045, region us-west-2). Project-level build/test/lint deferred to phase 1 (greenfield). git baseline e83cdd0 captured.
- 2026-06-26 — Status READY_TO_DISPATCH, current phase 1.
- 2026-06-26 — Phase 1 DONE (6/6). uv project + py3.12 venv; agentcore CLI present (starter-toolkit 0.3.9); preflight PASS; config.json written; full AgentCore API surface (harness/eval/optimization/abtest) present in boto3 1.43.36 — no degradation needed. MEMORY_SAVED: agentcore-api-surface.
- 2026-06-26 — Phase 2 DONE (5/5). Strands support agent (5 tools, weak baseline) in agent/; 9 unit tests pass; smoke vs real Bedrock shows correct single + multi-tool use. Added pythonpath=["."] to pytest config. MEMORY_SAVED: none.
- 2026-06-26 — Phase 3 DONE (5/5). Deployed to AgentCore Runtime (direct_code_deploy, observability on): arn .../runtime/acmesupport-fOfv652Bjq. invoke_deployed returns correct order details. Managed Harness created + invoked for real: .../harness/AcmeSupportHarness-9dw7DNYPDv (memory disabled to avoid ListEvents perm gap). deployment.json captured. NO degradation. MEMORY_SAVED: agentcore-api-surface (updated).
- 2026-06-26 — Phase 4 DONE (5/5). Real AgentCore batch Evaluations COMPLETED (10/10). Baseline: GoalSuccessRate 0.9 / Faithfulness 1.0 / Helpfulness 0.88. KEY FIX: deployed agent emitted only the runtime wrapper span (eval failed all sessions w/ AgentSpanMappingException) → added aws-opentelemetry-distro to requirements + StrandsTelemetry OTLP setup + redeployed → GenAI trajectory spans now flow. Also fixed eval request shape (serviceNames=[<agent_name>.DEFAULT], regex job name). MEMORY_SAVED: agentcore-api-surface (updated, highest-value lessons).
- 2026-06-26 — Phase 5 DONE (6/6). Closed loop executed on real AgentCore: StartRecommendation (COMPLETED) → optimized prompt applied to prompts.py → redeploy → improved eval COMPLETED (GoalSuccessRate 0.9 / Faithfulness 1.0 / Helpfulness 0.83) → compare (loop_closed=True). Primary metric held; honest analysis of near-ceiling baseline + n=10 noise. A/B documented (needs gateway+online-eval+traffic). Fixed re.sub \\n repr corruption (lambda repl). MEMORY_SAVED: agentcore-api-surface (updated).
- 2026-06-26 — Phase 6 DONE (8/8). README runbook + docs/CONCEPTS.md + closed-loop diagram (svg+png via qlmanage) + scripts/teardown.py + capture_deployment.py. Diff review caught + gitignored a 36MB build cache. 0 secrets, 0 stray debug. MEMORY_SAVED: project-agentcore-eval-opti-demo.
- 2026-06-26 — FINAL AUDIT round 1: no gaps. 6/6 phases done; deterministic commands re-run clean; 18/18 deliverables present; artifacts carry real COMPLETED cloud results. Coverage 88.7% (trust-prior 11.3%). Status COMPLETE.
- 2026-06-26 — POST-COMPLETION CHANGE (user): re-architected to make the agent created+deployed as a managed AgentCore HARNESS (5 inline-function tools + client-side tool loop): agent/harness_tools.py, scripts/harness_agent.py, scripts/harness_create.py (+ test_harness_tools). Discovered managed-harness telemetry is NOT scoreable by AgentCore Evaluations (AgentSpanMappingException — double-nested content). Per user choice ("Harness deploy + Runtime eval"), eval+optimization run on the Runtime mirror of the same agent; optimization applies to both. Honest closed-loop outcome this run: recommendation regressed GoalSuccessRate 1.0→0.6, eval CAUGHT it, compare promote=false, rolled back to baseline. Docs (README/CONCEPTS/diagram) updated. 14 tests pass, ruff clean, 0 secrets. harness_demo.py removed (superseded). MEMORY updated (agentcore-api-surface + project).

## Failure log

If a phase hits FAILURE_PROBE, record it here:

- (none yet)
