#!/usr/bin/env python3
"""Generate the light-style architecture and process diagrams as editable SVG."""

from __future__ import annotations

from html import escape
from pathlib import Path

OUT = Path(__file__).resolve().parent
FONT = "'Helvetica Neue',Helvetica,Arial,'Noto Sans CJK SC','PingFang SC','Microsoft YaHei',sans-serif"


def begin(lines: list[str], *, height: int, title: str, subtitle: str) -> None:
    lines.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 {height}" '
        f'width="1200" height="{height}" role="img" aria-labelledby="title desc">'
    )
    lines.append(f'<title id="title">{escape(title)}</title>')
    lines.append(f'<desc id="desc">{escape(subtitle)}</desc>')
    lines.append(f'<style>text{{font-family:{FONT}}}</style>')
    lines.append('<defs>')
    for marker_id, color in (
        ("blue", "#2563eb"),
        ("green", "#16a34a"),
        ("purple", "#9333ea"),
        ("orange", "#ea580c"),
        ("gray", "#6b7280"),
    ):
        lines.append(
            f'<marker id="arrow-{marker_id}" markerWidth="10" markerHeight="7" '
            'refX="9" refY="3.5" orient="auto">'
        )
        lines.append(f'<polygon points="0 0,10 3.5,0 7" fill="{color}"/>')
        lines.append('</marker>')
    lines.append('</defs>')
    lines.append(f'<rect width="1200" height="{height}" fill="#ffffff"/>')
    lines.append('<text x="40" y="42" fill="#111827" font-size="24" font-weight="700">' + escape(title) + '</text>')
    lines.append('<text x="40" y="68" fill="#6b7280" font-size="13">' + escape(subtitle) + '</text>')
    lines.append('<line x1="40" y1="84" x2="1160" y2="84" stroke="#e5e7eb"/>')


def container(
    lines: list[str], x: int, y: int, w: int, h: int, label: str, color: str, fill: str
) -> None:
    lines.append(
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{fill}" '
        f'stroke="{color}" stroke-width="1.2" stroke-dasharray="7,5"/>'
    )
    lines.append(
        f'<text x="{x + 16}" y="{y + 24}" fill="{color}" font-size="12" '
        f'font-weight="700" letter-spacing="0.08em">{escape(label)}</text>'
    )


def card(
    lines: list[str],
    *,
    x: int,
    y: int,
    w: int,
    h: int,
    title: str,
    subtitle: str,
    badge: str,
    badge_fill: str,
    fill: str = "#ffffff",
    stroke: str = "#d1d5db",
    double: bool = False,
    title_size: int = 14,
) -> None:
    lines.append('<g data-graph-role="node">')
    lines.append(
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>'
    )
    if double:
        lines.append(
            f'<rect x="{x + 4}" y="{y + 4}" width="{w - 8}" height="{h - 8}" rx="7" '
            f'fill="none" stroke="{stroke}" stroke-width="0.8" opacity="0.55"/>'
        )
    badge_x = x + 30
    badge_y = y + h // 2
    lines.append(f'<circle cx="{badge_x}" cy="{badge_y}" r="18" fill="{badge_fill}"/>')
    lines.append(
        f'<text x="{badge_x}" y="{badge_y + 4}" text-anchor="middle" fill="#ffffff" '
        f'font-size="{9 if len(badge) > 4 else 10}" font-weight="700">{escape(badge)}</text>'
    )
    lines.append(
        f'<text x="{x + 58}" y="{y + h // 2 - 5}" fill="#111827" font-size="{title_size}" '
        f'font-weight="650">{escape(title)}</text>'
    )
    lines.append(
        f'<text x="{x + 58}" y="{y + h // 2 + 17}" fill="#6b7280" font-size="11.5">'
        f'{escape(subtitle)}</text>'
    )
    lines.append('</g>')


def arrow(
    lines: list[str],
    *,
    d: str,
    color: str,
    marker: str,
    label: str = "",
    lx: int = 0,
    ly: int = 0,
    dashed: bool = False,
) -> None:
    dash = ' stroke-dasharray="6,4"' if dashed else ""
    lines.append(
        f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2" '
        f'stroke-linejoin="round" stroke-linecap="round" marker-end="url(#arrow-{marker})"'
        f'{dash} data-graph-role="edge"/>'
    )
    if label:
        text_w = max(54, len(label) * 7 + 14)
        lines.append(
            f'<rect x="{lx - text_w // 2}" y="{ly - 14}" width="{text_w}" height="20" '
            'rx="5" fill="#ffffff" opacity="0.96"/>'
        )
        lines.append(
            f'<text x="{lx}" y="{ly}" text-anchor="middle" fill="{color}" '
            f'font-size="11" font-weight="600">{escape(label)}</text>'
        )


def legend(lines: list[str], *, y: int) -> None:
    items = (
        (50, "#2563eb", "blue", "Agent request"),
        (260, "#9333ea", "purple", "Telemetry / readback"),
        (520, "#16a34a", "green", "Tool and evaluation"),
    )
    for x, color, marker, label in items:
        lines.append(
            f'<line x1="{x}" y1="{y}" x2="{x + 34}" y2="{y}" stroke="{color}" '
            f'stroke-width="2" marker-end="url(#arrow-{marker})"/>'
        )
        lines.append(f'<text x="{x + 44}" y="{y + 4}" fill="#6b7280" font-size="11.5">{label}</text>')
    lines.append(
        f'<text x="1148" y="{y + 4}" text-anchor="end" fill="#9ca3af" font-size="11">'
        'Light / Flat Icon</text>'
    )


def architecture() -> str:
    lines: list[str] = []
    begin(
        lines,
        height=720,
        title="Claude Agent SDK - Langfuse - AgentCore Evaluator",
        subtitle="Local agent tracing and readback; sessionSpans go directly to AgentCore without CloudWatch",
    )
    container(lines, 30, 105, 600, 510, "LOCAL EXECUTION", "#2563eb", "#eff6ff")
    container(lines, 660, 105, 240, 510, "LANGFUSE", "#6366f1", "#faf5ff")
    container(lines, 930, 105, 240, 510, "AWS / US-WEST-2", "#ea580c", "#fff7ed")

    card(
        lines,
        x=60,
        y=165,
        w=150,
        h=88,
        title="CLI",
        subtitle="uv run",
        badge="CLI",
        badge_fill="#2563eb",
        fill="#ffffff",
        stroke="#bfdbfe",
    )
    card(
        lines,
        x=250,
        y=140,
        w=340,
        h=142,
        title="Claude Agent SDK",
        subtitle="model: claude-sonnet-5",
        badge="Claude",
        badge_fill="#D97757",
        fill="#fff7ed",
        stroke="#fdba74",
        double=True,
        title_size=16,
    )
    lines.append('<text x="308" y="244" fill="#9a3412" font-size="11">Agent loop / system prompt / tool selection</text>')
    card(
        lines,
        x=250,
        y=350,
        w=170,
        h=82,
        title="MCP Tool",
        subtitle="lookup price",
        badge="MCP",
        badge_fill="#16a34a",
        fill="#f0fdf4",
        stroke="#86efac",
    )
    card(
        lines,
        x=450,
        y=350,
        w=150,
        h=82,
        title="OpenInference",
        subtitle="OTel spans",
        badge="OTel",
        badge_fill="#9333ea",
        fill="#faf5ff",
        stroke="#d8b4fe",
        title_size=13,
    )
    card(
        lines,
        x=60,
        y=485,
        w=180,
        h=88,
        title="Langfuse Bridge",
        subtitle="fetch + convert",
        badge="PY",
        badge_fill="#2563eb",
        fill="#ffffff",
        stroke="#bfdbfe",
        title_size=13,
    )

    card(
        lines,
        x=690,
        y=145,
        w=180,
        h=82,
        title="OTLP Ingest",
        subtitle="trace export",
        badge="LF",
        badge_fill="#6366f1",
        fill="#faf5ff",
        stroke="#c4b5fd",
    )
    card(
        lines,
        x=690,
        y=295,
        w=180,
        h=92,
        title="Trace Store",
        subtitle="session + observations",
        badge="DB",
        badge_fill="#6366f1",
        fill="#ffffff",
        stroke="#c4b5fd",
    )
    card(
        lines,
        x=690,
        y=475,
        w=180,
        h=78,
        title="Public API",
        subtitle="Sessions + Trace",
        badge="API",
        badge_fill="#6366f1",
        fill="#faf5ff",
        stroke="#c4b5fd",
    )

    card(
        lines,
        x=960,
        y=175,
        w=180,
        h=86,
        title="AgentCore Evaluate",
        subtitle="on-demand API",
        badge="ACE",
        badge_fill="#ea580c",
        fill="#fff7ed",
        stroke="#fdba74",
        title_size=13,
    )
    card(
        lines,
        x=960,
        y=330,
        w=180,
        h=82,
        title="Bedrock Judge",
        subtitle="built-in evaluator",
        badge="BR",
        badge_fill="#ea580c",
        fill="#ffffff",
        stroke="#fdba74",
        title_size=13,
    )
    card(
        lines,
        x=960,
        y=475,
        w=180,
        h=78,
        title="Evaluation Result",
        subtitle="score + explanation",
        badge="0.83",
        badge_fill="#16a34a",
        fill="#f0fdf4",
        stroke="#86efac",
        title_size=13,
    )
    lines.append('<g opacity="0.9">')
    lines.append('<rect x="970" y="575" width="160" height="28" rx="7" fill="#f9fafb" stroke="#9ca3af" stroke-dasharray="4,3"/>')
    lines.append('<text x="1050" y="594" text-anchor="middle" fill="#6b7280" font-size="11">CloudWatch - not on path</text>')
    lines.append('<line x1="978" y1="579" x2="1122" y2="599" stroke="#dc2626" stroke-width="1.7"/>')
    lines.append('</g>')

    arrow(lines, d="M210 209 H250", color="#2563eb", marker="blue", label="prompt", lx=230, ly=198)
    arrow(lines, d="M332 282 V350", color="#16a34a", marker="green", label="tool call", lx=306, ly=322)
    arrow(lines, d="M390 350 V318 H492 V282", color="#16a34a", marker="green", label="result", lx=444, ly=309)
    arrow(lines, d="M520 282 V350", color="#9333ea", marker="purple", label="spans", lx=548, ly=322)
    arrow(lines, d="M600 391 H640 V186 H690", color="#9333ea", marker="purple", label="OTLP", lx=650, ly=176)
    arrow(lines, d="M780 227 V295", color="#9333ea", marker="purple", label="persist", lx=816, ly=266)
    arrow(lines, d="M780 387 V475", color="#9333ea", marker="purple", label="query", lx=814, ly=436)
    arrow(lines, d="M690 514 H240", color="#9333ea", marker="purple", label="session + trace", lx=470, ly=503)
    arrow(lines, d="M150 573 V630 H940 V218 H960", color="#16a34a", marker="green", label="sessionSpans", lx=600, ly=621)
    arrow(lines, d="M1050 261 V330", color="#16a34a", marker="green", label="evaluate", lx=1088, ly=300)
    arrow(lines, d="M1050 412 V475", color="#16a34a", marker="green", label="response", lx=1090, ly=452)

    legend(lines, y=680)
    lines.append('</svg>')
    return "\n".join(lines)


def step_card(
    lines: list[str],
    *,
    x: int,
    y: int,
    number: int,
    title: str,
    subtitle: str,
    tag: str,
    color: str,
    fill: str,
) -> None:
    lines.append('<g data-graph-role="node">')
    lines.append(f'<rect x="{x}" y="{y}" width="220" height="82" rx="10" fill="{fill}" stroke="{color}" stroke-width="1.5"/>')
    lines.append(f'<circle cx="{x + 26}" cy="{y + 26}" r="15" fill="{color}"/>')
    lines.append(f'<text x="{x + 26}" y="{y + 31}" text-anchor="middle" fill="#ffffff" font-size="12" font-weight="700">{number}</text>')
    lines.append(f'<text x="{x + 50}" y="{y + 31}" fill="#111827" font-size="14" font-weight="650">{escape(title)}</text>')
    lines.append(f'<text x="{x + 20}" y="{y + 58}" fill="#6b7280" font-size="11.5">{escape(subtitle)}</text>')
    lines.append(f'<text x="{x + 202}" y="{y + 17}" text-anchor="end" fill="{color}" font-size="9.5" font-weight="700">{escape(tag)}</text>')
    lines.append('</g>')


def process_flow() -> str:
    lines: list[str] = []
    begin(
        lines,
        height=850,
        title="End-to-end Evaluation Flow",
        subtitle="Run the agent, read back from Langfuse, then call AgentCore Evaluate directly",
    )

    # Light platform bands make ownership visible without constraining the routing grid.
    lines.append('<rect data-graph-role="decoration" x="35" y="108" width="1130" height="128" rx="12" fill="#eff6ff" stroke="#bfdbfe"/>')
    lines.append('<text x="52" y="130" fill="#2563eb" font-size="10" font-weight="700">AGENT EXECUTION</text>')
    lines.append('<rect data-graph-role="decoration" x="35" y="278" width="1130" height="128" rx="12" fill="#faf5ff" stroke="#ddd6fe"/>')
    lines.append('<text x="52" y="300" fill="#7c3aed" font-size="10" font-weight="700">TRACE DELIVERY &amp; READBACK</text>')
    lines.append('<rect data-graph-role="decoration" x="35" y="448" width="1130" height="270" rx="12" fill="#f0fdf4" stroke="#bbf7d0"/>')
    lines.append('<text x="52" y="470" fill="#16a34a" font-size="10" font-weight="700">CONVERSION &amp; EVALUATION</text>')

    step_card(lines, x=70, y=142, number=1, title="Validate config", subtitle="keys / endpoint / AWS region", tag="LOCAL", color="#2563eb", fill="#ffffff")
    step_card(lines, x=330, y=142, number=2, title="Start tracing", subtitle="Langfuse + OpenInference", tag="LOCAL", color="#2563eb", fill="#ffffff")
    step_card(lines, x=590, y=142, number=3, title="Run agent", subtitle="Claude Agent SDK / Sonnet 5", tag="CLAUDE", color="#D97757", fill="#fff7ed")
    step_card(lines, x=850, y=142, number=4, title="Execute MCP tool", subtitle="lookup_product_price", tag="LOCAL", color="#16a34a", fill="#ffffff")

    step_card(lines, x=850, y=312, number=5, title="Flush spans", subtitle="OpenTelemetry export", tag="OTEL", color="#9333ea", fill="#ffffff")
    step_card(lines, x=590, y=312, number=6, title="Store trace", subtitle="session / trace / observations", tag="LANGFUSE", color="#6366f1", fill="#ffffff")
    step_card(lines, x=330, y=312, number=7, title="Read session / trace", subtitle="Sessions API + Trace API", tag="LANGFUSE", color="#6366f1", fill="#ffffff")

    lines.append('<g data-graph-role="node">')
    lines.append('<polygon points="180,302 275,353 180,404 85,353" fill="#fff7ed" stroke="#ea580c" stroke-width="1.5"/>')
    lines.append('<circle cx="180" cy="332" r="14" fill="#ea580c"/>')
    lines.append('<text x="180" y="336" text-anchor="middle" fill="#ffffff" font-size="11" font-weight="700">8</text>')
    lines.append('<text x="180" y="361" text-anchor="middle" fill="#111827" font-size="13" font-weight="650">Trace ready?</text>')
    lines.append('<text x="180" y="380" text-anchor="middle" fill="#9a3412" font-size="10.5">contains AGENT span</text>')
    lines.append('</g>')

    step_card(lines, x=300, y=510, number=9, title="Convert spans", subtitle="AGENT / TOOL unified spans", tag="LOCAL", color="#16a34a", fill="#ffffff")
    step_card(lines, x=650, y=510, number=10, title="Call Evaluate", subtitle="evaluationInput.sessionSpans", tag="AWS", color="#ea580c", fill="#fff7ed")
    card(lines, x=920, y=510, w=220, h=82, title="Save result", subtitle="score + explanation", badge="JSON", badge_fill="#16a34a", fill="#ffffff", stroke="#86efac")

    lines.append('<rect x="70" y="625" width="220" height="62" rx="9" fill="#fff7ed" stroke="#ea580c" stroke-width="1.5" stroke-dasharray="6,4"/>')
    lines.append('<text x="180" y="650" text-anchor="middle" fill="#9a3412" font-size="13" font-weight="650">Wait 2 seconds</text>')
    lines.append('<text x="180" y="670" text-anchor="middle" fill="#6b7280" font-size="11">Poll Langfuse API again</text>')

    arrow(lines, d="M290 183 H330", color="#2563eb", marker="blue")
    arrow(lines, d="M550 183 H590", color="#2563eb", marker="blue")
    arrow(lines, d="M810 183 H850", color="#16a34a", marker="green", label="tool call", lx=830, ly=171)
    arrow(lines, d="M960 224 V270 H960 V312", color="#9333ea", marker="purple", label="trace", lx=1000, ly=274)
    arrow(lines, d="M850 353 H810", color="#9333ea", marker="purple")
    arrow(lines, d="M590 353 H550", color="#9333ea", marker="purple")
    arrow(lines, d="M330 353 H275", color="#9333ea", marker="purple")
    arrow(lines, d="M180 404 V445 H410 V510", color="#16a34a", marker="green", label="Yes", lx=210, ly=438)
    arrow(lines, d="M520 551 H650", color="#16a34a", marker="green", label="sessionSpans", lx=585, ly=538)
    arrow(lines, d="M870 551 H920", color="#16a34a", marker="green", label="result", lx=895, ly=538)
    arrow(lines, d="M85 353 H55 V656 H70", color="#ea580c", marker="orange", label="No", lx=55, ly=500, dashed=True)
    arrow(lines, d="M290 656 H1165 V430 H440 V394", color="#ea580c", marker="orange", label="retry", lx=820, ly=422, dashed=True)

    lines.append('<g transform="translate(55,758)">')
    lines.append('<rect x="0" y="0" width="1090" height="54" rx="10" fill="#f9fafb" stroke="#e5e7eb"/>')
    lines.append('<circle cx="28" cy="27" r="12" fill="#ffffff" stroke="#9ca3af" stroke-dasharray="3,2"/>')
    lines.append('<line x1="20" y1="19" x2="36" y2="35" stroke="#dc2626" stroke-width="2"/>')
    lines.append('<text x="52" y="24" fill="#111827" font-size="12.5" font-weight="650">CloudWatch is not used</text>')
    lines.append('<text x="52" y="41" fill="#6b7280" font-size="11">No Logs query or Transaction Search; Langfuse is the only trace source</text>')
    lines.append('<text x="1068" y="32" text-anchor="end" fill="#9ca3af" font-size="11">Light / Flat Icon</text>')
    lines.append('</g>')

    lines.append('</svg>')
    return "\n".join(lines)


def main() -> None:
    outputs = {
        "architecture.light.svg": architecture(),
        "evaluation-flow.light.svg": process_flow(),
    }
    for name, content in outputs.items():
        path = OUT / name
        path.write_text(content + "\n", encoding="utf-8")
        print(path)


if __name__ == "__main__":
    main()
