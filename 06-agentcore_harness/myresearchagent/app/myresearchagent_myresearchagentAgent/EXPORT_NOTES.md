# Export Notes — myresearchagent_myresearchagent → myresearchagent_myresearchagentAgent

Exported on: 2026-06-26
Strands version: strands-agents >= 1.15.0
Source harness: agentcore/app/myresearchagent_myresearchagent/harness.json
Generated agent: app/myresearchagent_myresearchagentAgent/

## Items requiring manual follow-up

### Browser tool requires Container build — excluded from CodeZip export
The browser tool requires a Container build to run. In a CodeZip (Lambda-style) runtime the Playwright node driver cannot be executed and the tool will fail at invocation time.

Re-export with `--build Container` to include browser tool support:

  agentcore export harness --name myresearchagent_myresearchagent --target-agent-name myresearchagent_myresearchagentAgent --build Container
