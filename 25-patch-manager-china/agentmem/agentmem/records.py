"""Pure functions: record text (what gets embedded), query digest, keyword quality, sanitizing, formatting.
No I/O -- unit-testable without OpenSearch or Bedrock."""
import re

from .profile import Profile

TEXT_FIELDS = ("symptom", "root_cause", "fix", "title", "use_cases", "hints", "situation", "intent", "justification",
               "reflection", "summary")
_STOPW = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "is", "not", "no", "with", "from"}


# ---------------------------------------------------------------- record text (embedding input)
def kb_text(e: dict) -> str:
    return (f"{e.get('signature', '')}: {e.get('symptom', '')}. root cause: {e.get('root_cause', '')}. "
            f"error strings: {'; '.join(e.get('keywords', []))}")


def episode_text(e: dict) -> str:
    return f"{e.get('situation', '')} {e.get('intent', '')} {e.get('justification', '')} {e.get('reflection', '')}"


def reflection_text(r: dict) -> str:  # embed the trigger conditions; hints are what the agent reads
    return f"{r.get('title', '')}. {r.get('use_cases', '')}"


# ---------------------------------------------------------------- query digest + keyword quality
def query_digest(text: str, profile: Profile, max_chars: int = 6000) -> str:
    """Tool output is mostly boilerplate; embed only the lines that carry failure signal (deduplicated)."""
    seen, keep = set(), []
    for ln in (text or "").splitlines() or [text or ""]:
        s = re.sub(r"\s+", " ", ln).strip()
        if s and profile.signal_re.search(s) and s.lower() not in seen:
            seen.add(s.lower())
            keep.append(s[:300])
    out = "\n".join(keep) or (text or "")
    return out[:max_chars]


def filter_keywords(keywords: list[str], profile: Profile, evidence: str | None = None, limit: int = 8) -> tuple[list[str], list[str]]:
    """Keep literal, distinctive error strings that occur in `evidence` (when given). Returns (kept, dropped)."""
    kept, dropped, tl = [], [], (evidence or "").lower()
    for k in keywords:
        k = re.sub(r"\s+", " ", (k or "")).strip().strip("'\"")
        kl = k.lower()
        words = [w for w in re.findall(r"[a-z0-9_./=:-]+", kl) if w not in _STOPW]
        distinctive = len(words) >= 2 or (len(kl) >= 8 and re.search(r"[/._=:]", kl))
        if (not k or len(k) < 6 or not distinctive or any(p.search(k) for p in profile.generic_res)
                or profile.injection_re.search(k) or (evidence is not None and kl not in tl) or kl in (x.lower() for x in kept)):
            dropped.append(k)
            continue
        kept.append(k)
    return kept[:limit], dropped + kept[limit:]


# ---------------------------------------------------------------- sanitizing (records are re-injected into prompts)
def strip_injection(text: str, profile: Profile) -> tuple[str, int]:
    out, hits = [], 0
    for line in (text or "").splitlines() or [""]:
        if profile.injection_re.search(line):
            hits += 1
            out.append("[removed instruction-like text]")
        else:
            out.append(line)
    return "\n".join(out), hits


def clean(doc: dict, profile: Profile) -> dict:
    hits = 0
    for k in TEXT_FIELDS:
        if isinstance(doc.get(k), str):
            doc[k], h = strip_injection(doc[k], profile)
            hits += h
    if isinstance(doc.get("keywords"), list):
        kept = [k for k in doc["keywords"] if not strip_injection(k, profile)[1]]
        hits += len(doc["keywords"]) - len(kept)
        doc["keywords"] = kept
    if hits:
        doc["sanitized_lines"] = int(doc.get("sanitized_lines", 0)) + hits
    return doc


# ---------------------------------------------------------------- formatting for prompts
def format_knowledge(items: list[dict]) -> str:
    return "\n".join(f"- [{it['signature']}] ({'verified' if it.get('verified') else 'unverified, agent-learned'}; match {it.get('score')}) "
                     f"symptom: {it.get('symptom')} | root cause: {it.get('root_cause')} | fix: {it.get('fix')}" for it in items) or "- (no match)"


def format_reflections(items: list[dict]) -> str:
    return "\n".join(f"- {r['title']} (confidence {r.get('confidence')}): {r.get('hints')}" for r in items) or "- (none)"


def format_history(items: list[dict]) -> str:
    return "\n".join(f"- {h.get('batch_id')}: {h.get('status')} [{h.get('signature')}] {h.get('reflection', '')[:240]}" for h in items) or "- (none)"
