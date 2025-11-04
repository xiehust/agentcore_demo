#!/usr/bin/env python3
"""
Test script for MCP protocol version 2025-06-18
Tests the Quip MCP server implementation
"""

import json
import requests
from typing import Any

# Server configuration
SERVER_URL = "http://localhost:8000/mcp"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream"
}

class MCPTester:
    """MCP protocol tester for version 2025-06-18"""

    def __init__(self, server_url: str):
        self.server_url = server_url
        self.session_id = None
        self.request_id = 0

    def _next_id(self) -> int:
        """Get next request ID"""
        self.request_id += 1
        return self.request_id

    def _make_request(self, method: str, params: dict[str, Any] = None) -> dict:
        """Make a JSON-RPC request"""
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method
        }
        if params:
            payload["params"] = params

        headers = HEADERS.copy()
        if self.session_id:
            headers["mcp-session-id"] = self.session_id

        response = requests.post(self.server_url, json=payload, headers=headers)

        # Extract session ID from headers if present
        if "mcp-session-id" in response.headers:
            self.session_id = response.headers["mcp-session-id"]

        # Parse SSE response if needed
        text = response.text.strip()

        # Debug output
        if not text:
            print(f"  DEBUG: Empty response from server")
            print(f"  DEBUG: Status code: {response.status_code}")
            print(f"  DEBUG: Headers: {dict(response.headers)}")
            return {"error": {"code": -1, "message": "Empty response from server"}}

        if text.startswith("event: message"):
            lines = text.split('\n')
            for line in lines:
                if line.startswith("data: "):
                    text = line[6:].strip()  # Remove "data: " prefix
                    break

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            print(f"  DEBUG: Failed to parse JSON: {e}")
            print(f"  DEBUG: Response text: {text[:200]}")
            return {"error": {"code": -1, "message": f"JSON decode error: {e}"}}

    def test_initialize(self) -> bool:
        """Test initialize method with protocol version 2025-06-18"""
        print("=" * 70)
        print("TEST: Initialize with MCP Protocol 2025-06-18")
        print("=" * 70)

        result = self._make_request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {
                "name": "mcp-tester",
                "version": "1.0.0"
            }
        })

        if "result" in result:
            print("✓ Initialize successful")
            print(f"  Protocol Version: {result['result']['protocolVersion']}")
            print(f"  Server Name: {result['result']['serverInfo']['name']}")
            print(f"  Server Version: {result['result']['serverInfo']['version']}")
            print(f"  Capabilities: {json.dumps(result['result']['capabilities'], indent=4)}")
            return True
        else:
            print(f"✗ Initialize failed: {result.get('error', 'Unknown error')}")
            return False

    def test_list_tools(self) -> bool:
        """Test tools/list method"""
        print("\n" + "=" * 70)
        print("TEST: List Tools")
        print("=" * 70)

        result = self._make_request("tools/list", {})

        if "result" in result:
            tools = result['result'].get('tools', [])
            print(f"✓ Found {len(tools)} tools:")
            for tool in tools:
                print(f"\n  Tool: {tool['name']}")
                print(f"    Description: {tool.get('description', 'N/A')}")
                if 'inputSchema' in tool:
                    props = tool['inputSchema'].get('properties', {})
                    print(f"    Parameters: {', '.join(props.keys())}")
            return True
        else:
            print(f"✗ List tools failed: {result.get('error', 'Unknown error')}")
            return False

    def test_list_resources(self) -> bool:
        """Test resources/list method"""
        print("\n" + "=" * 70)
        print("TEST: List Resources")
        print("=" * 70)

        result = self._make_request("resources/list", {})

        if "result" in result:
            resources = result['result'].get('resources', [])
            print(f"✓ Found {len(resources)} resources:")
            for resource in resources:
                print(f"\n  Resource: {resource['uri']}")
                print(f"    Name: {resource.get('name', 'N/A')}")
                print(f"    Description: {resource.get('description', 'N/A')}")
            return True
        else:
            print(f"✗ List resources failed: {result.get('error', 'Unknown error')}")
            return False

    def test_read_resource(self, uri: str) -> bool:
        """Test resources/read method"""
        print("\n" + "=" * 70)
        print(f"TEST: Read Resource - {uri}")
        print("=" * 70)

        result = self._make_request("resources/read", {"uri": uri})

        if "result" in result:
            contents = result['result'].get('contents', [])
            print(f"✓ Read successful ({len(contents)} content items)")
            for content in contents[:1]:  # Show first content
                text = content.get('text', content.get('blob', 'N/A'))
                if len(text) > 200:
                    text = text[:200] + "..."
                print(f"  Content: {text}")
            return True
        else:
            print(f"✗ Read resource failed: {result.get('error', 'Unknown error')}")
            return False

    def test_call_tool(self, tool_name: str, arguments: dict) -> bool:
        """Test tools/call method"""
        print("\n" + "=" * 70)
        print(f"TEST: Call Tool - {tool_name}")
        print("=" * 70)
        print(f"Arguments: {json.dumps(arguments, indent=2)}")

        result = self._make_request("tools/call", {
            "name": tool_name,
            "arguments": arguments
        })

        if "result" in result:
            contents = result['result'].get('content', [])
            print(f"✓ Tool call successful ({len(contents)} content items)")
            for content in contents:
                text = content.get('text', 'N/A')
                if len(text) > 300:
                    text = text[:300] + "..."
                print(f"  Result: {text}")
            return True
        else:
            print(f"✗ Tool call failed: {result.get('error', 'Unknown error')}")
            return False

    def run_all_tests(self):
        """Run all tests"""
        print("\n" + "=" * 70)
        print("MCP PROTOCOL VERSION 2025-06-18 TEST SUITE")
        print("=" * 70)
        print(f"Server URL: {self.server_url}")
        print("=" * 70)

        results = []

        # Test 1: Initialize
        results.append(("Initialize", self.test_initialize()))

        # Test 2: List tools
        results.append(("List Tools", self.test_list_tools()))

        # Test 3: List resources
        results.append(("List Resources", self.test_list_resources()))

        # Test 4: Read resource
        results.append(("Read Resource", self.test_read_resource("config://server-info")))

        # Test 5: Call tool (will fail without real Quip token, but tests the protocol)
        results.append(("Call Tool", self.test_call_tool(
            "get_thread_metadata",
            {"thread_id": "test123"}
        )))

        # Summary
        print("\n" + "=" * 70)
        print("TEST SUMMARY")
        print("=" * 70)
        passed = sum(1 for _, result in results if result)
        total = len(results)

        for test_name, result in results:
            status = "✓ PASS" if result else "✗ FAIL"
            print(f"{status}: {test_name}")

        print("=" * 70)
        print(f"Results: {passed}/{total} tests passed")
        print("=" * 70)

        return passed == total


def main():
    """Main test function"""
    tester = MCPTester(SERVER_URL)
    success = tester.run_all_tests()

    if success:
        print("\n✓ All tests passed!")
        return 0
    else:
        print("\n✗ Some tests failed")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
