import ast
import pathlib

PKG = pathlib.Path(__file__).resolve().parent.parent / "agentmem"


def test_reset_needs_opt_in_and_confirmation(mem):
    mem.ltm.seed_knowledge({"signature": "s", "symptom": "x", "root_cause": "y", "fix": "z", "keywords": []})
    assert mem.admin({"op": "reset", "confirm": "reset-t-ltm"}) == {"error": "reset disabled"}
    assert mem.admin({"op": "reset"}, allow_reset=True) == {"error": "reset disabled"}
    assert mem.admin({"op": "reset", "confirm": "reset-t-ltm"}, allow_reset=True) == {"reset": True}
    assert mem.ltm.stats() == {}
    assert mem.admin({"op": "unknown"}) is None


def test_library_has_no_agent_imports():
    """Decoupling contract: the library never imports the agent, and only stm/llm touch strands."""
    for f in PKG.glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
            for n in names:
                root = n.split(".")[0]
                assert root not in {"guardrails", "state", "tools", "app", "memory_profile", "bedrock_agentcore"}, (f.name, n)
                if root == "strands":
                    assert f.name in {"stm.py", "llm.py"}, (f.name, n)
