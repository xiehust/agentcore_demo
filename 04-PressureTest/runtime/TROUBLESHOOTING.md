# Troubleshooting Guide - 422 Unprocessable Content Error

## Overview

HTTP 422 "Unprocessable Content" error occurs when the server understands the request but cannot process it due to invalid request data that doesn't match the expected schema.

## Common Causes

### 1. Missing `input` Key ❌
```json
// WRONG
{"prompt": "Hello"}

// CORRECT
{"input": {"prompt": "Hello"}}
```

**Error**: The Pydantic model `InvocationRequest` expects an `input` field at the root level.

### 2. `input` is Not a Dictionary/Object ❌
```json
// WRONG - input is a string
{"input": "Hello"}

// WRONG - input is null
{"input": null}

// CORRECT
{"input": {"prompt": "Hello"}}
```

**Error**: The `input` field must be a dictionary/object type (`Dict[str, Any]`).

### 3. Wrong Key Name ❌
```json
// WRONG - typo in key name
{"inputs": {"prompt": "Hello"}}

// CORRECT
{"input": {"prompt": "Hello"}}
```

**Error**: FastAPI/Pydantic expects exact field name `input`, not `inputs` or other variations.

### 4. Array Instead of Object ❌
```json
// WRONG - root level is an array
[{"input": {"prompt": "Hello"}}]

// CORRECT - root level is an object
{"input": {"prompt": "Hello"}}
```

**Error**: The endpoint expects a single object, not an array.

### 5. Missing or Wrong Content-Type Header ❌
```bash
# WRONG - no Content-Type or wrong type
curl -X POST http://localhost:8080/invocations \
  -d '{"input": {"prompt": "Hello"}}'

# CORRECT - application/json
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"input": {"prompt": "Hello"}}'
```

**Error**: Server expects `Content-Type: application/json` header.

### 6. Malformed JSON Syntax ❌
```bash
# WRONG - missing quotes, trailing commas, etc.
{"input": {prompt: "Hello",}}

# CORRECT
{"input": {"prompt": "Hello"}}
```

**Error**: Invalid JSON syntax will cause parsing errors.

### 7. Missing Required Fields ❌

For agent processing requests:
```json
// WRONG - input is empty (no prompt, no get_stats)
{"input": {}}

// This will trigger 400 Bad Request with:
// "No prompt found in input. Please provide a 'prompt' key in the input."
```

## Valid Request Formats

### 1. Standard Agent Request
```json
{
  "input": {
    "prompt": "Your message here"
  }
}
```

### 2. Get Statistics Request
```json
{
  "input": {
    "get_stats": true
  }
}
```

### 3. Request with Extra Metadata (Valid)
```json
{
  "input": {
    "prompt": "Your message",
    "metadata": {
      "user_id": "123",
      "session": "abc"
    }
  }
}
```

## Using the Debug Script

Run the debug script to test various payload formats:

```bash
cd /home/ubuntu/workspace/agentcore_demo/04-PressureTest/runtime
python debug_422.py
```

This script tests 13 different payload formats and shows which ones succeed and which fail with 422.

## Manual Testing with curl

### Test 1: Valid Request
```bash
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"input": {"prompt": "Hello"}}'
```

**Expected**: HTTP 200 with agent response

### Test 2: Missing `input` Key (Should Fail)
```bash
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Hello"}'
```

**Expected**: HTTP 422 with validation error

### Test 3: Get Stats
```bash
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"input": {"get_stats": true}}'
```

**Expected**: HTTP 200 with statistics data

### Test 4: View Detailed Error
```bash
curl -v -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"wrong": "format"}'
```

**Expected**: HTTP 422 with detailed Pydantic validation error

## Understanding the Error Response

When you get a 422 error, FastAPI returns detailed validation errors:

```json
{
  "detail": [
    {
      "type": "missing",
      "loc": ["body", "input"],
      "msg": "Field required",
      "input": {"wrong": "format"}
    }
  ]
}
```

- `type`: Type of validation error
- `loc`: Location of the error (e.g., `["body", "input"]` means missing `input` field in request body)
- `msg`: Human-readable error message
- `input`: The actual data that was sent

## Common Scenarios

### Scenario 1: Python requests Library
```python
import requests

# WRONG
response = requests.post(
    "http://localhost:8080/invocations",
    data={"input": {"prompt": "Hello"}}  # data= sends form data
)

# CORRECT
response = requests.post(
    "http://localhost:8080/invocations",
    json={"input": {"prompt": "Hello"}}  # json= sends JSON
)
```

### Scenario 2: JavaScript fetch
```javascript
// WRONG
fetch('http://localhost:8080/invocations', {
  method: 'POST',
  body: {input: {prompt: 'Hello'}}  // Missing JSON.stringify
});

// CORRECT
fetch('http://localhost:8080/invocations', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({input: {prompt: 'Hello'}})
});
```

### Scenario 3: test_client.py
The test client in `test_client.py` uses the correct format:

```python
payload = {"input": {"prompt": prompt}}
async with session.post(url, json=payload) as response:
    ...
```

If your test client is getting 422 errors, check:
1. The `json=` parameter is used (not `data=`)
2. The payload structure matches the expected format
3. Content-Type header is automatically set by aiohttp

## Debugging Steps

1. **Check Server Logs**: Look for validation errors in the server output
   ```
   INFO:     127.0.0.1:47960 - "POST /invocations HTTP/1.1" 422 Unprocessable Content
   ```

2. **Use Verbose curl**: See full request/response details
   ```bash
   curl -v -X POST http://localhost:8080/invocations \
     -H "Content-Type: application/json" \
     -d '{"input": {"prompt": "test"}}'
   ```

3. **Run Debug Script**: Test multiple formats automatically
   ```bash
   python debug_422.py
   ```

4. **Check FastAPI Docs**: Visit auto-generated API documentation
   ```
   http://localhost:8080/docs
   ```
   This interactive UI shows the exact schema expected.

5. **Enable Debug Logging**: Modify `agent_entry.py` to see request bodies:
   ```python
   logging.basicConfig(level=logging.DEBUG)
   ```

## Quick Reference

| Issue | Solution |
|-------|----------|
| Missing `input` key | Wrap your data in `{"input": {...}}` |
| Wrong Content-Type | Add `-H "Content-Type: application/json"` |
| Malformed JSON | Validate JSON syntax with `jq` or JSON validator |
| Empty input | Must provide either `prompt` or `get_stats: true` |
| Wrong data type | Ensure `input` is object/dict, not string/array |

## Still Getting 422?

1. Run the debug script to identify the exact issue:
   ```bash
   python debug_422.py
   ```

2. Check the FastAPI auto-docs for the exact schema:
   ```
   http://localhost:8080/docs
   ```

3. Compare your request format with the working examples in this guide

4. Enable verbose logging to see what the server is receiving
