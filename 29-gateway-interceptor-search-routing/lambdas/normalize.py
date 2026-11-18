"""Normalize AgentCore Web Search and Exa results into one schema.

Unified tool result text (JSON):
{
  "provider": "agentcore-web-search" | "exa" | "anysearch",
  "results": [
    {"title": str, "url": str, "publishedDate": "YYYY-MM-DD" | null,
     "author": str | null, "text": str}
  ]
}
"""

import json
import re
from datetime import datetime

MONTH_DATE = re.compile(r"([A-Z][a-z]+) (\d{1,2}),? (\d{4})")  # "June 24 2026" / "Aug 14, 2026"
ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")
EXA_FIELD = re.compile(r"^(Title|URL|Published|Author):\s*(.*)$")
AS_DATE = re.compile(r"\s*date:\s*([^\n]+)$")  # AnySearch appends " date: ..." to the snippet


def _date(raw):
    if not raw or raw in ("unknown", "N/A"):
        return None
    m = ISO_DATE.match(raw)
    if m:
        return m.group(1)
    m = MONTH_DATE.search(raw)
    if m:
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(" ".join(m.groups()), fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
    return None  # relative dates such as "2 days ago" are not resolved


def from_websearch(data):
    return [{
        "title": r.get("title") or "",
        "url": r.get("url") or "",
        "publishedDate": _date(r.get("publishedDate")),
        "author": None,
        "text": r.get("text") or "",
    } for r in data.get("results") or []]


def from_exa(text):
    results = []
    for block in re.split(r"\n-{3,}\n", text):
        fields, rest, in_highlights = {}, [], False
        for line in block.strip().splitlines():
            if in_highlights:
                rest.append(line)
                continue
            if line.startswith("Highlights:"):
                in_highlights = True
                tail = line[len("Highlights:"):].strip()
                if tail:
                    rest.append(tail)
                continue
            m = EXA_FIELD.match(line)
            if m:
                fields[m.group(1)] = m.group(2).strip()
        if "URL" not in fields:
            continue
        author = fields.get("Author")
        results.append({
            "title": fields.get("Title", ""),
            "url": fields["URL"],
            "publishedDate": _date(fields.get("Published")),
            "author": None if author in (None, "", "N/A") else author,
            "text": "\n".join(rest).strip(),
        })
    return results


def from_anysearch(text):
    """Parse AnySearch's markdown: '### N. Title' / '- **URL**: ...' / '- snippet ... date: Aug 14, 2026'."""
    results = []
    body = text.split("```yaml")[0]  # drop the trailing request_id block
    for block in re.split(r"\n(?=### \d+\. )", body):
        m = re.match(r"### \d+\. (.*)", block.strip())
        if not m:
            continue
        url, snippet = "", []
        for line in block.strip().splitlines()[1:]:
            u = re.match(r"- \*\*URL\*\*:\s*(\S+)", line)
            if u:
                url = u.group(1)
            elif line.startswith("- "):
                snippet.append(line[2:])
            elif line.strip():
                snippet.append(line)
        text_, date = "\n".join(snippet).strip(), None
        d = AS_DATE.search(text_)
        if d:
            text_, date = text_[:d.start()].rstrip(), _date(d.group(1))
        if url:
            results.append({"title": m.group(1).strip(), "url": url, "publishedDate": date,
                            "author": None, "text": text_})
    return results


def normalize_text(text):
    """Return (provider, unified_json_text) or (None, text) if the format is unknown."""
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "results" in data:
            unified = {"provider": "agentcore-web-search", "results": from_websearch(data)}
            return unified["provider"], json.dumps(unified, ensure_ascii=False)
    except ValueError:
        pass
    if text.lstrip().startswith("Title:"):
        unified = {"provider": "exa", "results": from_exa(text)}
        return unified["provider"], json.dumps(unified, ensure_ascii=False)
    if text.lstrip().startswith("## Search Results"):
        unified = {"provider": "anysearch", "results": from_anysearch(text)}
        return unified["provider"], json.dumps(unified, ensure_ascii=False)
    return None, text
