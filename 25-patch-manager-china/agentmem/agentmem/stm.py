"""Short-term memory: Strands SessionRepository on DynamoDB (pk=SESSION#{id}, sk=SESSION | AGENT#.. | MSG#..#000001).
The conversation survives the microVM, so a human can resume the exact session later. Requires strands-agents."""
import json
import time

import boto3
from boto3.dynamodb.conditions import Key
from strands.session.session_repository import SessionRepository
from strands.types.session import Session, SessionAgent, SessionMessage


class DynamoDBSessionRepository(SessionRepository):
    def __init__(self, table_name: str, region: str, ttl_days: int = 30, table=None):
        self.table = table or boto3.resource("dynamodb", region_name=region).Table(table_name)
        self.ttl_days = ttl_days

    def _put(self, session_id, sk, data: dict):
        self.table.put_item(Item={"pk": f"SESSION#{session_id}", "sk": sk, "data": json.dumps(data, ensure_ascii=False),
                                  "ttl": int(time.time()) + self.ttl_days * 86400})

    def _get(self, session_id, sk):
        it = self.table.get_item(Key={"pk": f"SESSION#{session_id}", "sk": sk}).get("Item")
        return json.loads(it["data"]) if it else None

    def create_session(self, session: Session, **kw) -> Session:
        self._put(session.session_id, "SESSION", session.to_dict())
        return session

    def read_session(self, session_id: str, **kw) -> Session | None:
        d = self._get(session_id, "SESSION")
        return Session.from_dict(d) if d else None

    def create_agent(self, session_id: str, session_agent: SessionAgent, **kw) -> None:
        self._put(session_id, f"AGENT#{session_agent.agent_id}", session_agent.to_dict())

    def read_agent(self, session_id: str, agent_id: str, **kw) -> SessionAgent | None:
        d = self._get(session_id, f"AGENT#{agent_id}")
        return SessionAgent.from_dict(d) if d else None

    def update_agent(self, session_id: str, session_agent: SessionAgent, **kw) -> None:
        self.create_agent(session_id, session_agent)

    def create_message(self, session_id: str, agent_id: str, session_message: SessionMessage, **kw) -> None:
        self._put(session_id, f"MSG#{agent_id}#{session_message.message_id:06d}", session_message.to_dict())

    def read_message(self, session_id: str, agent_id: str, message_id: int, **kw) -> SessionMessage | None:
        d = self._get(session_id, f"MSG#{agent_id}#{message_id:06d}")
        return SessionMessage.from_dict(d) if d else None

    def update_message(self, session_id: str, agent_id: str, session_message: SessionMessage, **kw) -> None:
        self.create_message(session_id, agent_id, session_message)

    def list_messages(self, session_id: str, agent_id: str, limit: int | None = None, offset: int = 0, **kw) -> list[SessionMessage]:
        items, q = [], dict(KeyConditionExpression=Key("pk").eq(f"SESSION#{session_id}") & Key("sk").begins_with(f"MSG#{agent_id}#"))
        while True:
            r = self.table.query(**q)
            items += r["Items"]
            if "LastEvaluatedKey" not in r:
                break
            q["ExclusiveStartKey"] = r["LastEvaluatedKey"]
        msgs = [SessionMessage.from_dict(json.loads(i["data"])) for i in items][offset:]
        return msgs[:limit] if limit is not None else msgs

    # multi-agent not used
    def create_multi_agent(self, session_id, multi_agent, **kw):
        raise NotImplementedError

    def read_multi_agent(self, session_id, multi_agent_id, **kw):
        return None

    def update_multi_agent(self, session_id, multi_agent, **kw):
        raise NotImplementedError


class ShortTermMemory:
    """STM + the two views long-term extraction needs (transcript for the LLM, evidence for keyword grounding)."""

    def __init__(self, repo: DynamoDBSessionRepository, agent_id: str, evidence_tools: tuple = ()):
        self.repo, self.agent_id, self.evidence_tools = repo, agent_id, tuple(evidence_tools)

    def _contents(self, session_id: str):
        for m in self.repo.list_messages(session_id, self.agent_id):
            msg = m.to_message()
            for c in msg.get("content", []):
                yield msg["role"], c

    def transcript(self, session_id: str, max_chars: int = 30000) -> str:
        """Condensed text of a session (tool calls + results) for extraction and for humans."""
        out = []
        for role, c in self._contents(session_id):
            if "text" in c:
                out.append(f"[{role}] {c['text'][:1500]}")
            elif "toolUse" in c:
                out.append(f"[tool_call] {c['toolUse']['name']} {json.dumps(c['toolUse'].get('input'), ensure_ascii=False)[:300]}")
            elif "toolResult" in c:
                txt = " ".join(x.get("text", "") for x in c["toolResult"].get("content", []) if isinstance(x, dict))
                out.append(f"[tool_result:{c['toolResult'].get('status')}] {txt[:1200]}")
        return "\n".join(out)[-max_chars:]

    def evidence(self, session_id: str) -> str | None:
        """Text of the results of `evidence_tools`. Learned keywords must occur here: recall queries are built from that
        output, so strings that only exist in other tool output or in the model's own prose are useless.
        None (= use the whole transcript) when no evidence tools are configured."""
        if not self.evidence_tools:
            return None
        names, out = {}, []
        for _role, c in self._contents(session_id):
            if "toolUse" in c:
                names[c["toolUse"]["toolUseId"]] = c["toolUse"]["name"]
            elif "toolResult" in c and names.get(c["toolResult"].get("toolUseId")) in self.evidence_tools:
                out.append(" ".join(x.get("text", "") for x in c["toolResult"].get("content", []) if isinstance(x, dict)))
        return "\n".join(out)
