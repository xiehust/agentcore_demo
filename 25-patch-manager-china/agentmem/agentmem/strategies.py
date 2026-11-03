"""Long-term memory strategies, modelled on AgentCore Memory built-in strategies (which run asynchronously after
events are written). Where the managed service is not available the agent calls process_session after a session.

  semantic strategy   EXTRACTION -> CONSOLIDATION (AddMemory / UpdateMemory / SkipMemory against related records)
  episodic strategy   EPISODE EXTRACTION -> REFLECTION GENERATION (add / update across episodes, with confidence)

Prompts are adapted from the AgentCore default prompts (memory-system-prompt / memory-episodic-prompt) and filled
from the Profile. The code -- not the LLM -- enforces the hard rules after every LLM decision:
  - the KB only learns from sessions whose outcome is in profile.learn_statuses (verified by the agent's own code)
  - verified (human-curated) KB records are never rewritten: updates become proposals + suggested keywords
  - keywords must occur literally in the evidence and must not be generic boilerplate / instruction-like text
  - update ids must be ones that were shown to the model; anything else becomes an add
  - an "add" reflection that is a near-duplicate of an existing one is merged into it

The LLM is any callable llm(system_prompt, user_prompt, pydantic_schema) -> schema instance (agentmem.llm.strands_llm).
"""
import json
import re
import uuid
from typing import Callable, Literal

from pydantic import BaseModel, Field

from . import records as R
from .ltm import LongTermMemory

StructuredLLM = Callable[[str, str, type], BaseModel]


# ------------------------------------------------------------------ schemas
class KnowledgeCandidate(BaseModel):
    signature: str = Field(description="failure class: an EXACT catalog signature if it is the same failure class, "
                                       "otherwise new:<snake_case_name>")
    keywords: list[str] = Field(description="3-8 error strings copied LITERALLY from the transcript that identify THIS failure "
                                            "class and would not appear in other failures")
    symptom: str
    root_cause: str
    fix: str = Field(description="the remediation that worked (tool / document name + parameters), or the correct escalation")


class Episode(BaseModel):
    language: str = Field(description="main language of the transcript, e.g. English or Chinese")
    situation: str = Field(description="context that started the episode (which failure, what was observed first)")
    intent: str = Field(description="the goal of the episode")
    assessment: Literal["Yes", "No"] = Field(description="Yes only if the success criterion in the rules is met")
    justification: str = Field(description="key evidence for the assessment, from tool results")
    reflection: str = Field(description="patterns in tool usage and reasoning: what worked, what to avoid, what to do first next time")


class Extraction(BaseModel):
    episode: Episode
    knowledge: KnowledgeCandidate


class ConsolidationOp(BaseModel):
    operation: Literal["AddMemory", "UpdateMemory", "SkipMemory"]
    update_id: str | None = Field(default=None, description="signature of the existing record (UpdateMemory / SkipMemory)")
    memory: KnowledgeCandidate | None = Field(default=None, description="full new or merged record (AddMemory / UpdateMemory)")
    reason: str


class ReflectionEntry(BaseModel):
    operator: Literal["add", "update"]
    id: str | None = Field(default=None, description="id of the existing reflection, only for update")
    title: str
    use_cases: str = Field(description="goals, problems and trigger conditions (error text / states) where this applies")
    hints: str = Field(description="actionable guidance: tool order, what works, what to avoid, decision criteria, and why")
    confidence: float = Field(description="0.1-1.0")


class Reflections(BaseModel):
    reflections: list[ReflectionEntry]


# ------------------------------------------------------------------ prompts (adapted from AgentCore defaults)
EXTRACTION_PROMPT = """You are a long-term memory extraction agent for {domain}. You analyse the transcript of ONE
session (assistant turns, tool calls and tool results) and extract (1) an episode summary and (2) one
troubleshooting knowledge candidate.

Rules
- Only include details that are explicitly in the transcript or can be logically inferred from tool results. Do NOT
  incorporate external knowledge. Do not guess incomplete or truncated strings.
- {untrusted}
- Causal separation: only state a root cause that tool results support; temporal order is not causation.
- Episode: situation / intent / assessment / justification / reflection, 1-2 sentences each. assessment is Yes only if
  {success_criterion}. The reflection focuses on patterns in tool usage, argument choices and reasoning: effective
  patterns, patterns to avoid, actionable advice. Do not include PII, account ids, resource ids, host names or PIDs.
- Knowledge candidate:
  - signature: reuse an EXACT catalog signature when this is the same failure class, else new:<snake_case_name>.
  - keywords: literal strings that would let a future search recognise THIS failure class from tool output.
    {keyword_guidance}
  - If nothing was fixed, `fix` describes the correct escalation.

Signature catalog:
{catalog}
"""

CONSOLIDATION_PROMPT = """You are a conservative memory manager for a {kb_topic} knowledge base. You preserve existing
information while carefully integrating a new candidate record.

Operations (choose exactly one):
- AddMemory: the candidate describes a failure class not covered by any existing record. Give the full record; its
  signature must be new:<snake_case_name> and must not reuse an existing signature.
- UpdateMemory: the candidate is the SAME failure class as an existing record (same root cause, same fix family) and
  adds complementary detail (new literal error strings, a more precise root cause or fix). Give update_id and the full
  merged record. Preserve all specific details of the original; compress redundancy; avoid idle words.
- SkipMemory: the information already exists in sufficient detail, or the candidate is not reusable. If it confirms an
  existing record, set update_id to that record.
Records marked verified=true are human-curated: you may still choose UpdateMemory for them -- the update will be
queued for human review instead of being applied.
{untrusted}
"""

REFLECTION_PROMPT = """You are an expert at extracting actionable insights from agent task execution trajectories to build reusable
knowledge for future {task_noun}.

Input: a main episode, relevant earlier episodes, and existing reflections (each with an id).

Process
1. Pattern identification: review the main episode, then the relevant episodes; identify patterns across them; review
   what existing reflections already cover.
2. Knowledge synthesis, for each pattern:
   - operator: "add" for a pattern not covered by existing reflections (no id); "update" only when it shares the SAME
     core concept/title as an existing reflection (give its id). If an update would make use_cases + hints exceed 300
     words, add a new, more specific reflection instead.
   - title: concise and descriptive; keep the same title when updating.
   - use_cases: goals, problems and trigger conditions (literal error text or states) that signal when to apply it.
     When updating, combine the original and new use cases.
   - hints: tool selection and ordering that worked, what to avoid, decision criteria, and WHY. When updating, keep the
     original hints and add new ones.
   - confidence 0.1-1.0: 0.8-1.0 clear patterns that consistently led to success/failure; 0.4-0.7 useful but
     context-dependent; 0.1-0.3 tentative. When updating, adjust based on the additional evidence.
3. Focus on transferable knowledge, not subject-specific details (no ids, host names, PIDs). Include positive and
   negative patterns. If existing reflections already cover the patterns, return fewer or no entries.
Keep use_cases + hints around 100-200 words per entry.
"""


def _kb_view(e: dict) -> dict:
    return {k: e.get(k) for k in ("signature", "verified", "symptom", "root_cause", "fix", "keywords")}


class MemoryStrategies:
    def __init__(self, ltm: LongTermMemory, llm: StructuredLLM):
        self.ltm, self.llm = ltm, llm
        self.p, self.cfg = ltm.profile, ltm.cfg

    # -------------------------------------------------------------- semantic strategy: consolidation
    def consolidate_knowledge(self, cand: KnowledgeCandidate, evidence: str | None, source: str) -> dict:
        m = self.ltm
        kws, dropped = m.filter_keywords(cand.keywords, evidence)
        cand = cand.model_copy(update={"keywords": kws})
        proposed = cand.signature.split(":", 1)[-1]
        rel = m.related("KB", R.kb_text(cand.model_dump()), k=3)
        existing = {e["signature"]: e for _, e in rel}
        picked = m.get_kb(cand.signature)  # an exact catalog pick is always shown to the consolidator
        if picked and cand.signature not in existing:
            existing[cand.signature] = picked
        trace = {"related": [(e["signature"], c) for c, e in rel], "kept_keywords": kws, "dropped_keywords": dropped}
        if not existing:
            op = ConsolidationOp(operation="AddMemory", memory=cand, reason="no related record")
        else:
            op = self.llm(CONSOLIDATION_PROMPT.format(kb_topic=self.p.kb_topic, untrusted=self.p.untrusted_rule),
                          "<existing_records>\n" + json.dumps([_kb_view(e) for e in existing.values()], ensure_ascii=False, indent=1)
                          + "\n</existing_records>\n<candidate>\n" + cand.model_dump_json(indent=1) + "\n</candidate>", ConsolidationOp)
        trace.update(operation=op.operation, update_id=op.update_id, reason=op.reason)

        # --- hard rules after the LLM decision
        if op.operation in ("UpdateMemory", "SkipMemory") and op.update_id not in existing:
            op = ConsolidationOp(operation="AddMemory", memory=op.memory or cand, reason=f"unknown update_id {op.update_id}")
            trace["corrected"] = "unknown update_id -> AddMemory"
        if op.operation == "SkipMemory":
            if op.update_id:
                tgt = existing[op.update_id]
                tgt["confirmations"] = int(tgt.get("confirmations", 0)) + 1
                m.put_kb(tgt)
                trace["outcome"] = f"confirmed {op.update_id}"
            else:
                trace["outcome"] = "skipped"
            return trace
        new = (op.memory or cand).model_dump()
        new["keywords"] = m.filter_keywords(new.get("keywords", []), evidence)[0]
        if op.operation == "UpdateMemory":
            tgt = existing[op.update_id]
            old_kws = tgt.get("keywords", [])
            merged_kws = old_kws + [k for k in new["keywords"] if k.lower() not in {x.lower() for x in old_kws}]
            tgt["confirmations"] = int(tgt.get("confirmations", 0)) + 1
            if proposed != tgt["signature"]:
                tgt["aliases"] = sorted(set(tgt.get("aliases", [])) | {proposed})
            if tgt.get("verified"):  # human gate
                tgt["suggested_keywords"] = sorted(set(tgt.get("suggested_keywords", [])) | set(merged_kws[len(old_kws):]))[:20]
                tgt["update_proposals"] = (tgt.get("update_proposals", []) + [{"from": source, **{k: new[k] for k in ("symptom", "root_cause", "fix")}}])[-5:]
                trace["outcome"] = f"proposal queued for verified {tgt['signature']}"
            else:
                tgt.update({k: new[k] for k in ("symptom", "root_cause", "fix") if new.get(k)}, keywords=merged_kws[:10])
                trace["outcome"] = f"updated {tgt['signature']}"
            m.put_kb(tgt)
            return trace
        # AddMemory: controlled vocabulary -- never silently overwrite an existing signature
        sig = re.sub(r"[^a-z0-9_]", "_", new["signature"].split(":", 1)[-1].lower()).strip("_")[:60] or "unclassified"
        while m.get_kb(sig):
            sig += "_v2"
        m.put_kb({**{k: new[k] for k in ("symptom", "root_cause", "fix", "keywords")}, "signature": sig, "verified": False,
                  "source": "agent", "learned_from": source, "confirmations": 0, "aliases": []})
        trace["outcome"] = f"added {sig}"
        return trace

    # -------------------------------------------------------------- episodic strategy: reflection generation
    def generate_reflections(self, main: dict, session_id: str) -> dict:
        m = self.ltm
        text = R.episode_text(main)
        episodes = [e for _, e in m.related("EPISODE", text, k=4) if e.get("session_id") != session_id][:3]
        refls = {r["id"]: r for _, r in m.related("REFLECTION", text, k=5, gate=self.cfg.refl_related_gate)}
        view = lambda r: {k: r.get(k) for k in ("id", "title", "use_cases", "hints", "confidence")}  # noqa: E731
        ep_view = lambda e: {k: e.get(k) for k in ("status", "signature", "situation", "intent", "assessment", "justification", "reflection")}  # noqa: E731
        out = self.llm(REFLECTION_PROMPT.format(task_noun=self.p.task_noun),
                       f"<main_episode>\n{json.dumps(ep_view(main), ensure_ascii=False, indent=1)}\n</main_episode>\n"
                       f"<relevant_episodes>\n{json.dumps([ep_view(e) for e in episodes], ensure_ascii=False, indent=1)}\n</relevant_episodes>\n"
                       f"<existing_reflections>\n{json.dumps([view(r) for r in refls.values()], ensure_ascii=False, indent=1)}\n</existing_reflections>",
                       Reflections)
        trace = {"relevant_episodes": len(episodes), "existing": list(refls), "ops": []}
        for r in out.reflections:
            conf = min(1.0, max(0.1, round(float(r.confidence), 1)))
            words = len((r.use_cases + " " + r.hints).split())
            if r.operator == "update" and r.id in refls and words <= 320:
                src = sorted(set(refls[r.id].get("source_episodes", [])) | {session_id})
                m.put_reflection(r.id, {"title": r.title, "use_cases": r.use_cases, "hints": r.hints, "confidence": conf,
                                        "source_episodes": src})
                trace["ops"].append(f"update {r.id} '{r.title[:50]}' conf={conf}")
                continue
            # code-level near-duplicate guard: an "add" that says the same thing as an existing reflection becomes an update
            dup = m.related("REFLECTION", R.reflection_text({"title": r.title, "use_cases": r.use_cases}), k=1, gate=self.cfg.refl_dup_gate)
            if dup:
                old = dup[0][1]
                m.put_reflection(old["id"], {"title": old["title"], "use_cases": r.use_cases, "hints": r.hints,
                                             "confidence": max(conf, float(old.get("confidence", 0))),
                                             "source_episodes": sorted(set(old.get("source_episodes", [])) | {session_id})})
                trace["ops"].append(f"merge-into {old['id']} (cos {dup[0][0]}) '{old['title'][:50]}'")
            else:
                rid = uuid.uuid4().hex[:10]
                m.put_reflection(rid, {"title": r.title, "use_cases": r.use_cases, "hints": r.hints, "confidence": conf,
                                       "source_episodes": [session_id]})
                trace["ops"].append(f"add {rid} '{r.title[:50]}' conf={conf}" + (" (update id invalid/too long)" if r.operator == "update" else ""))
        return trace

    # -------------------------------------------------------------- entry point after a session
    def process_session(self, *, session_id: str, subject: str, batch_id: str, result: dict, transcript: str,
                        evidence: str | None = None) -> dict:
        """transcript -> LLM extraction; evidence (grounding tool results) -> literal keyword check."""
        p = self.p
        catalog = "\n".join(f"- {e['signature']}: {e.get('symptom', '')}" for e in self.ltm.kb_catalog()) or "- (empty)"
        ex = self.llm(EXTRACTION_PROMPT.format(domain=p.domain, untrusted=p.untrusted_rule, success_criterion=p.success_criterion,
                                               keyword_guidance=p.keyword_guidance, catalog=catalog),
                      f"<transcript>\n{transcript}\n</transcript>\n<final_report>\n{json.dumps(result, ensure_ascii=False)}\n</final_report>",
                      Extraction)
        episode = {**ex.episode.model_dump(), "instance_id": subject, "batch_id": batch_id, "status": result["status"],
                   "signature": ex.knowledge.signature if not ex.knowledge.signature.startswith("new:") else result.get("signature", "")}
        self.ltm.put_episode(session_id, episode)
        out = {"episode": "stored", "proposed_signature": ex.knowledge.signature,
               "reflection": self.generate_reflections(episode, session_id)}
        if result["status"] in p.learn_statuses:  # the KB only takes outcomes the agent's code verified
            out["kb"] = self.consolidate_knowledge(ex.knowledge, evidence if evidence is not None else transcript, session_id)
        return out


# ------------------------------------------------------------------ maintenance (no LLM)
def refilter_keywords(ltm: LongTermMemory) -> list[str]:
    """Re-apply the current keyword rules to agent-learned (unverified) KB records."""
    ops = []
    for e in ltm.list_kind("KB"):
        if e.get("verified"):
            continue
        kept, dropped = ltm.filter_keywords(e.get("keywords", []))
        if dropped:
            e["keywords"] = kept
            ltm.put_kb(e)
            ops.append(f"{e['signature']}: dropped {dropped}")
    return ops


def dedupe_reflections(ltm: LongTermMemory, gate: float | None = None) -> list[str]:
    """Merge reflections that are near-duplicates (keeps the better-supported one)."""
    gate = ltm.cfg.refl_dup_gate if gate is None else gate
    done, ops = set(), []
    for r in sorted(ltm.list_kind("REFLECTION"), key=lambda r: (-len(r.get("source_episodes", [])), -float(r.get("confidence", 0)))):
        if r["id"] in done:
            continue
        for c, o in ltm.related("REFLECTION", R.reflection_text(r), k=5, gate=gate):
            if o["id"] == r["id"] or o["id"] in done:
                continue
            r["source_episodes"] = sorted(set(r.get("source_episodes", [])) | set(o.get("source_episodes", [])))
            r["confidence"] = max(float(r.get("confidence", 0)), float(o.get("confidence", 0)))
            ltm.delete(f"REFLECTION#{o['id']}")
            done.add(o["id"])
            ops.append(f"merged {o['id']} -> {r['id']} (cos {c}) '{r['title'][:50]}'")
        ltm.put_reflection(r["id"], {k: r[k] for k in ("title", "use_cases", "hints", "confidence", "source_episodes")})
        done.add(r["id"])
    return ops
