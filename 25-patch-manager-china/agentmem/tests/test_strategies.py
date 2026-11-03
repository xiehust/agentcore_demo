from agentmem.strategies import (ConsolidationOp, Episode, Extraction, KnowledgeCandidate, ReflectionEntry, Reflections)
from conftest import LOCK, REPO, ScriptedLLM

EV = "Cannot download repomd.xml: repomd.xml download failed; curl error 6 resolving mirror.corp.invalid"


def cand(sig="new:repo_down", kws=("curl error 6 resolving mirror.corp.invalid",)):
    return KnowledgeCandidate(signature=sig, keywords=list(kws), symptom="dnf cannot download repomd.xml from mirror",
                              root_cause="mirror unreachable", fix="PatchRemediate-DisableRepo")


def test_unknown_update_id_becomes_add(mem):
    mem.ltm.seed_knowledge(REPO)
    llm = ScriptedLLM(ConsolidationOp=[ConsolidationOp(operation="UpdateMemory", update_id="made_up", memory=cand(), reason="x")])
    tr = mem.strategies(llm).consolidate_knowledge(cand(), EV, "s1")
    assert tr["corrected"] and tr["outcome"] == "added repo_down" and not mem.ltm.get_kb("repo_down")["verified"]


def test_verified_record_is_never_overwritten(mem):
    mem.ltm.seed_knowledge(REPO)
    llm = ScriptedLLM(ConsolidationOp=[ConsolidationOp(operation="UpdateMemory", update_id="repo_unreachable",
                                                       memory=cand("repo_unreachable"), reason="same")])
    tr = mem.strategies(llm).consolidate_knowledge(cand(), EV, "s1")
    kb = mem.ltm.get_kb("repo_unreachable")
    assert tr["outcome"].startswith("proposal queued")
    assert kb["root_cause"] == REPO["root_cause"] and kb["keywords"] == REPO["keywords"]
    assert kb["suggested_keywords"] == ["curl error 6 resolving mirror.corp.invalid"] and len(kb["update_proposals"]) == 1


def test_add_never_overwrites_existing_signature(mem):
    mem.ltm.seed_knowledge(LOCK)
    mem.ltm.put_kb({**REPO, "signature": "repo_down", "verified": False})
    llm = ScriptedLLM(ConsolidationOp=[ConsolidationOp(operation="AddMemory", memory=cand(), reason="new")])
    tr = mem.strategies(llm).consolidate_knowledge(cand(), EV, "s1")
    assert tr["outcome"] == "added repo_down_v2"


def test_keywords_must_be_grounded_in_evidence(mem):
    tr = mem.strategies(ScriptedLLM()).consolidate_knowledge(cand(kws=("curl error 6 resolving mirror.corp.invalid", "invented error string")), EV, "s1")
    assert tr["kept_keywords"] == ["curl error 6 resolving mirror.corp.invalid"] and "invented error string" in tr["dropped_keywords"]


def _extraction(status_ok=True):
    return Extraction(episode=Episode(language="English", situation="repo mirror unreachable", intent="fix patch",
                                      assessment="Yes" if status_ok else "No", justification="rerun ok", reflection="disable repo first"),
                      knowledge=cand())


def test_process_session_learns_kb_only_for_learn_statuses(mem):
    refl = Reflections(reflections=[ReflectionEntry(operator="add", title="Unreachable mirror", use_cases="repomd.xml errors",
                                                    hints="disable the repo", confidence=0.77)])
    llm = ScriptedLLM(Extraction=[_extraction(False)], Reflections=[refl])
    out = mem.strategies(llm).process_session(session_id="s1", subject="i-1", batch_id="b1",
                                              result={"status": "NEEDS_HUMAN", "signature": "x"}, transcript="t", evidence=EV)
    assert "kb" not in out and mem.ltm.stats() == {"EPISODE": 1, "REFLECTION": 1}
    assert mem.ltm.list_kind("REFLECTION")[0]["confidence"] == 0.8  # clamped + rounded
    llm = ScriptedLLM(Extraction=[_extraction()], Reflections=[Reflections(reflections=[])])
    out = mem.strategies(llm).process_session(session_id="s2", subject="i-1", batch_id="b2",
                                              result={"status": "FIXED", "signature": "x"}, transcript="t", evidence=EV)
    assert out["kb"]["outcome"] == "added repo_down"


def test_near_duplicate_reflection_add_is_merged(mem):
    mem.ltm.put_reflection("r1", {"title": "Unreachable mirror", "use_cases": "repomd.xml errors", "hints": "h", "confidence": 0.5,
                                  "source_episodes": ["s0"]})
    refl = Reflections(reflections=[ReflectionEntry(operator="add", title="Unreachable mirror", use_cases="repomd.xml errors",
                                                    hints="disable", confidence=0.7)])
    tr = mem.strategies(ScriptedLLM(Reflections=[refl])).generate_reflections({"situation": "x"}, "s1")
    assert tr["ops"][0].startswith("merge-into r1")
    r = mem.ltm.get_reflection("r1")
    assert r["source_episodes"] == ["s0", "s1"] and r["confidence"] == 0.7 and mem.ltm.stats() == {"REFLECTION": 1}
