"""Two tables.

patchdemo-batch   pk=batchId  sk=INST#<instanceId> | AUDIT#<instanceId>#<ts> | APPROVAL#<id> | REPORT
                  The single source of truth for progress; every instance can be replayed by instanceId.
patchdemo-memory  self-built replacement for AgentCore Memory (not available in China regions):
                  pk=SESSION#<sessionId>       short-term: Strands session/agent/messages  (TTL 30d)
                  pk=KB                        long-term semantic: symptom -> root cause -> fix
                  pk=INST#<instanceId>         long-term summary: per-instance history
                  pk=REFLECTION                episodic lessons ("next time check X first")
"""
import sys

sys.path.insert(0, __import__("os").path.dirname(__file__) + "/..")
from common.config import BATCH_TABLE, MEMORY_TABLE, TAGS, log, session  # noqa: E402


def main():
    ddb = session().client("dynamodb")
    for t in (BATCH_TABLE, MEMORY_TABLE):
        try:
            ddb.create_table(TableName=t, BillingMode="PAY_PER_REQUEST", Tags=TAGS,
                             AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
                             KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}])
            log("creating", t)
        except ddb.exceptions.ResourceInUseException:
            log("exists", t)
        ddb.get_waiter("table_exists").wait(TableName=t)
    ddb.update_time_to_live(TableName=MEMORY_TABLE, TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"}) \
        if ddb.describe_time_to_live(TableName=MEMORY_TABLE)["TimeToLiveDescription"]["TimeToLiveStatus"] == "DISABLED" else None


if __name__ == "__main__":
    main()
