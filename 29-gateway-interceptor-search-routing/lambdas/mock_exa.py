"""Stand-in for Exa/Brave: returns a marker proving the call landed here."""


def handler(event, context):
    tool = None
    if context.client_context and context.client_context.custom:
        tool = context.client_context.custom.get("bedrockAgentCoreToolName")
    return {"routed_to": "mockexa", "tool_name_seen_by_target": tool, "arguments": event}
