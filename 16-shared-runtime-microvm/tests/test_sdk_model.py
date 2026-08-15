"""Offline checks for the pinned AgentCore Runtime SDK operation model."""

from __future__ import annotations

import unittest
from typing import Any, cast

import boto3
import botocore
from botocore.session import Session


class TestPinnedAgentCoreModel(unittest.TestCase):
    def setUp(self):
        self.service = cast(Any, Session().get_service_model("bedrock-agentcore"))

    def test_locked_sdk_versions_and_operations(self):
        self.assertEqual(boto3.__version__, "1.42.59")
        self.assertEqual(botocore.__version__, "1.42.97")
        self.assertTrue(
            {
                "InvokeAgentRuntime",
                "InvokeAgentRuntimeCommand",
                "StopRuntimeSession",
            }.issubset(self.service.operation_names)
        )

    def test_command_event_union_and_stop_response(self):
        command = self.service.operation_model("InvokeAgentRuntimeCommand")
        self.assertTrue(
            {"agentRuntimeArn", "runtimeSessionId", "body"}.issubset(
                command.input_shape.members
            )
        )
        self.assertTrue(
            {"statusCode", "runtimeSessionId", "stream"}.issubset(
                command.output_shape.members
            )
        )
        stream = command.output_shape.members["stream"]
        self.assertEqual(
            set(stream.members),
            {
                "chunk",
                "accessDeniedException",
                "internalServerException",
                "resourceNotFoundException",
                "serviceQuotaExceededException",
                "throttlingException",
                "validationException",
                "runtimeClientError",
            },
        )
        chunk = stream.members["chunk"]
        self.assertEqual(
            set(chunk.members), {"contentStart", "contentDelta", "contentStop"}
        )
        self.assertEqual(set(chunk.members["contentStart"].members), set())
        self.assertEqual(
            set(chunk.members["contentDelta"].members), {"stdout", "stderr"}
        )
        self.assertEqual(
            set(chunk.members["contentStop"].members), {"exitCode", "status"}
        )

        stop = self.service.operation_model("StopRuntimeSession")
        self.assertTrue(
            {"statusCode", "runtimeSessionId"}.issubset(stop.output_shape.members)
        )


if __name__ == "__main__":
    unittest.main()
