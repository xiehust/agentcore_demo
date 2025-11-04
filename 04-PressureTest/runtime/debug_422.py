#!/usr/bin/env python3
"""
Debug script to identify causes of 422 Unprocessable Content errors
Tests various request payload formats
"""

import requests
import json

BASE_URL = "http://localhost:8080"

def test_request(description: str, payload: dict, expect_success: bool = True):
    """Test a single request and print results"""
    print(f"\n{'='*70}")
    print(f"Test: {description}")
    print(f"{'='*70}")
    print(f"Payload: {json.dumps(payload, indent=2)}")

    try:
        response = requests.post(
            f"{BASE_URL}/invocations",
            json=payload,
            timeout=10
        )

        print(f"Status Code: {response.status_code}")

        if response.status_code == 200:
            print("✓ SUCCESS")
            print(f"Response: {json.dumps(response.json(), indent=2)[:200]}...")
        elif response.status_code == 422:
            print("✗ FAILED - 422 Unprocessable Content")
            print(f"Response: {response.text}")
        else:
            print(f"✗ FAILED - HTTP {response.status_code}")
            print(f"Response: {response.text}")

    except Exception as e:
        print(f"✗ EXCEPTION: {str(e)}")

def main():
    print("\n" + "="*70)
    print("422 Error Investigation - Testing Different Payload Formats")
    print("="*70)

    # Test 1: Correct format
    test_request(
        "Correct format with prompt",
        {"input": {"prompt": "Hello"}}
    )

    # Test 2: get_stats request
    test_request(
        "Correct format with get_stats",
        {"input": {"get_stats": True}}
    )

    # Test 3: Missing 'input' key (should fail with 422)
    test_request(
        "Missing 'input' key",
        {"prompt": "Hello"},
        expect_success=False
    )

    # Test 4: 'input' is not a dict (should fail with 422)
    test_request(
        "'input' is a string instead of dict",
        {"input": "Hello"},
        expect_success=False
    )

    # Test 5: Empty payload (should fail with 422)
    test_request(
        "Empty payload",
        {},
        expect_success=False
    )

    # Test 6: 'input' is null (should fail with 422)
    test_request(
        "'input' is null",
        {"input": None},
        expect_success=False
    )

    # Test 7: 'input' is empty dict (should fail - no prompt)
    test_request(
        "'input' is empty dict",
        {"input": {}},
        expect_success=False
    )

    # Test 8: Wrong key name (should fail with 422)
    test_request(
        "Wrong key name 'inputs' instead of 'input'",
        {"inputs": {"prompt": "Hello"}},
        expect_success=False
    )

    # Test 9: Extra fields in input (should work)
    test_request(
        "Extra fields in input",
        {"input": {"prompt": "Hello", "extra_field": "test"}}
    )

    # Test 10: Nested input structure
    test_request(
        "Nested structure in input",
        {"input": {"prompt": "Hello", "metadata": {"user": "test"}}}
    )

    # Test 11: Array instead of object
    test_request(
        "Array instead of object",
        [{"input": {"prompt": "Hello"}}],
        expect_success=False
    )

    # Test 12: get_stats with wrong boolean type
    test_request(
        "get_stats as string 'true' instead of boolean",
        {"input": {"get_stats": "true"}},
        expect_success=False  # Will try to process as agent request
    )

    # Test 13: Both prompt and get_stats (get_stats should take priority)
    test_request(
        "Both prompt and get_stats",
        {"input": {"prompt": "Hello", "get_stats": True}}
    )

    print("\n" + "="*70)
    print("Investigation Complete")
    print("="*70)
    print("\nCommon causes of 422 errors:")
    print("1. Missing 'input' key in JSON payload")
    print("2. 'input' value is not a dict/object (e.g., string, null, array)")
    print("3. Sending array instead of object at root level")
    print("4. Wrong Content-Type header (not application/json)")
    print("5. Malformed JSON syntax")
    print("\nCorrect format:")
    print('  {"input": {"prompt": "your message here"}}')
    print('  or')
    print('  {"input": {"get_stats": true}}')
    print()

if __name__ == "__main__":
    main()
