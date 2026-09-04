import json
import unittest
import urllib.error
import urllib.request

from demo import DemoEnvironment, GatewaySimulator, verify_token


class TokenExchangeFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = DemoEnvironment().start()
        self.gateway = GatewaySimulator(self.environment)

    def tearDown(self) -> None:
        self.environment.close()

    def test_user_identity_survives_audience_exchange(self) -> None:
        user_token = self.environment.issue_user_token("alice")
        delegated_token = self.gateway.exchange(user_token)
        claims = verify_token(
            delegated_token,
            self.environment.signing_secret,
            expected_issuer=self.environment.issuer,
            expected_audience=self.environment.mcp_audience,
        )

        self.assertEqual("alice", claims["sub"])
        self.assertEqual(self.environment.client_id, claims["act"]["sub"])
        self.assertEqual("mcp-server", claims["aud"])
        self.assertIn("mcp:invoke", claims["scope"].split())

    def test_gateway_can_call_whoami_tool(self) -> None:
        user_token = self.environment.issue_user_token("river")
        response = self.gateway.invoke(
            user_token,
            "tools/call",
            {"name": "whoami", "arguments": {}},
        )

        identity = response["result"]["structuredContent"]
        self.assertEqual("river", identity["delegatedUser"])
        self.assertEqual(self.environment.client_id, identity["actor"])
        self.assertEqual("mcp-server", identity["audience"])

    def test_gateway_audience_token_is_rejected_by_mcp(self) -> None:
        user_token = self.environment.issue_user_token("alice")
        request = urllib.request.Request(
            self.environment.mcp_url,
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
            headers={
                "Authorization": f"Bearer {user_token}",
                "Content-Type": "application/json",
            },
        )

        with self.assertRaises(urllib.error.HTTPError) as context:
            urllib.request.urlopen(request, timeout=3)
        self.assertEqual(401, context.exception.code)
        error = json.loads(context.exception.read())
        self.assertEqual("unexpected JWT audience", error["error_description"])

    def test_exchange_rejects_unregistered_audience(self) -> None:
        user_token = self.environment.issue_user_token("alice")

        with self.assertRaises(urllib.error.HTTPError) as context:
            self.gateway.exchange(user_token, audience="other-tenant-api")
        self.assertEqual(400, context.exception.code)
        error = json.loads(context.exception.read())
        self.assertEqual("invalid_target", error["error"])


if __name__ == "__main__":
    unittest.main()
