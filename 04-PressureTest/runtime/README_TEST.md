# Concurrent Test Client for AgentCore Runtime

This test client allows you to perform concurrent load testing on your AgentCore Runtime server.

**Supports Two Modes:**
- **HTTP Mode**: Test local HTTP endpoints (e.g., `http://localhost:8080`)
- **AgentCore Mode**: Test deployed AWS Bedrock AgentCore Runtimes

## Features

- Concurrent request execution with configurable workers
- Real-time progress monitoring
- Comprehensive statistics (avg, median, P95, P99 response times)
- **Token usage tracking** (input/output tokens per request and totals)
- **Agent latency metrics** (internal processing time)
- Server health checks before and after tests (HTTP mode)
- JSON output for result analysis
- Customizable prompts and request parameters
- Support for AWS Bedrock AgentCore Runtime invocation

## Quick Start

### 1. Navigate to the runtime directory
```bash
cd /home/ubuntu/workspace/agentcore_demo/04-PressureTest/runtime
```

### 2. Ensure your server is running
The server should be running on `http://localhost:8080`. You should see:
```
INFO:     Started server process [3879932]
INFO:     Waiting for application startup.
2025-10-27 03:29:46,195 - __main__ - INFO - Starting Strands Agent Server with 25 worker threads
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8080 (Press CTRL+C to quit)
```

### 3. Run a quick test
```bash
# Simple smoke test (5 requests, 2 concurrent)
python test_client.py -n 5 -c 2
```

### 4. Run a full stress test
```bash
# Match your MAX_WORKERS=25 for full load test
python test_client.py -n 100 -c 25 -p "What is artificial intelligence?" -o stress_test.json
```

### 6. Test AWS Bedrock AgentCore Runtime
```bash
# Get your runtime ARN from .bedrock_agentcore.yaml
python test_client.py \
  --mode agentcore \
  --runtime-arn arn:aws:bedrock-agentcore:xxx:xxx:runtime/xxx \
  --region us-west-2 \
  -n 10 -c 5 \
  -p "Explain machine learning" \
  -o agentcore_test.json
```

### 5. Monitor server status (in another terminal)
```bash
# Real-time monitoring
watch -n 1 'curl -s http://localhost:8080/ping | jq'

# Or check stats
watch -n 1 'curl -s http://localhost:8080/stats | jq'
```

### Expected Output
```
======================================================================
Starting Concurrent Load Test
======================================================================
Target URL: http://localhost:8080
Total Requests: 100
Concurrent Workers: 25
Prompt: What is artificial intelligence?
======================================================================

Server Health: HEALTHY
Active Tasks: 0

✓ Request 1: 2.34s (tokens: 9/10, latency: 379ms)
✓ Request 2: 2.45s (tokens: 9/12, latency: 401ms)
✓ Request 3: 2.12s (tokens: 9/8, latency: 298ms)
...

======================================================================
Test Results
======================================================================
Total Requests:      100
Successful:          100 (100.0%)
Failed:              0 (0.0%)
Total Duration:      45.23s
Requests/Second:     2.21

Response Times (seconds):
  Average:           2.456s
  Median:            2.301s
  Min:               1.234s
  Max:               5.678s
  P95:               3.890s
  P99:               4.567s

Token Usage:
  Total Input:       900 tokens
  Total Output:      1,000 tokens
  Total:             1,900 tokens
  Avg Input/Req:     9.0 tokens
  Avg Output/Req:    10.0 tokens

Agent Latency:
  Average:           379.5ms
======================================================================
```

## Installation

### For HTTP Mode
The test client uses standard Python libraries:
```bash
pip install aiohttp
```

### For AgentCore Mode
Additional requirement for AWS invocation:
```bash
pip install boto3
```

Make sure you have AWS credentials configured:
```bash
aws configure
# or use environment variables
export AWS_ACCESS_KEY_ID=your_key
export AWS_SECRET_ACCESS_KEY=your_secret
export AWS_DEFAULT_REGION=us-west-2
```

## Usage

### Basic Usage

```bash
# Simple test with defaults (10 requests, 5 concurrent workers)
python test_client.py

# Test with custom server URL
python test_client.py --url http://localhost:8080
```

### Advanced Usage

```bash
# High concurrency test
python test_client.py -n 100 -c 25

# Custom prompt
python test_client.py -n 50 -c 10 -p "Explain quantum computing"

# Gradual load (delay between requests)
python test_client.py -n 100 -c 10 -d 0.1

# Save results to file
python test_client.py -n 100 -c 20 -o results.json

# Extended timeout for long-running requests
python test_client.py -n 50 -c 10 -t 600
```

## Command Line Options

### Mode Selection
| Option | Description | Default |
|--------|-------------|---------|
| `--mode` | Invocation mode: `http` or `agentcore` | `http` |

### HTTP Mode Options
| Option | Description | Default |
|--------|-------------|---------|
| `--url` | Base URL of the server | `http://localhost:8080` |

### AgentCore Mode Options
| Option | Description | Default |
|--------|-------------|---------|
| `--runtime-arn` | AgentCore Runtime ARN (required) | None |
| `--region` | AWS region | `us-west-2` |
| `--fixed-session` | Use fixed session ID (resuse same runtime session) | False |

### Common Options
| Option | Description | Default |
|--------|-------------|---------|
| `-n, --num-requests` | Total number of requests | 10 |
| `-c, --concurrency` | Number of concurrent workers | 5 |
| `-p, --prompt` | Prompt to send in each request | "Hello, how are you?" |
| `-d, --delay` | Delay between launching requests (seconds) | 0 |
| `-t, --timeout` | Request timeout (seconds) | 300 |
| `-o, --output` | Output file for JSON results | None |

## Example Test Scenarios

### AgentCore Mode Scenarios

#### 1. Basic AgentCore Test
```bash
python test_client.py \
  --mode agentcore \
  --runtime-arn arn:aws:bedrock-agentcore:us-west-2:434444145045:runtime/agent_entry-5V7YeT6HWx \
  -n 10 -c 3 \
  -p "What is machine learning?"
```

#### 2. AgentCore Stress Test
```bash
python test_client.py \
  --mode agentcore \
  --runtime-arn arn:aws:bedrock-agentcore:us-west-2:xx:runtime/agent_entry-xx \
  --region us-west-2 \
  -n 100 -c 20 \
  -p "Explain artificial intelligence" \
  -o agentcore_stress.json
```

#### 3. AgentCore Gradual Load Test
```bash
python test_client.py \
  --mode agentcore \
  --runtime-arn arn:aws:bedrock-agentcore:us-west-2:xx:runtime/agent_entry-xx \
  -n 50 -c 10 -d 0.2 \
  -p "What is cloud computing?" \
  -o agentcore_gradual.json
```

#### 4. Test with Session Persistence (Fixed Session)
```bash
# Use --fixed-session to reuse the same agentcore runtime
python test_client.py \
  --mode agentcore \
  --runtime-arn arn:aws:bedrock-agentcore:us-west-2:xx:runtime/agent_entry-xx \
  --fixed-session \
  -n 20 -c 5 \
  -p "Explain machine learning in detail, more than 500 words" \
  -o agentcore_test.json
```

#### 5. Find Your Runtime ARN
```bash
# Your Runtime ARN is in .bedrock_agentcore.yaml
cat src/.bedrock_agentcore.yaml | grep agent_arn
```

## Understanding the Output

### During Test
```
✓ Request 1: 2.34s      # Successful request with duration
✗ Request 5: HTTP 500   # Failed request
✗ Request 8: Timeout    # Request timeout
```

### Test Results
```
======================================================================
Test Results
======================================================================
Total Requests:      100
Successful:          98 (98.0%)
Failed:              2 (2.0%)
Total Duration:      45.23s
Requests/Second:     2.21

Response Times (seconds):
  Average:           2.456s
  Median:            2.301s
  Min:               1.234s
  Max:               5.678s
  P95:               3.890s    # 95% of requests completed within this time
  P99:               4.567s    # 99% of requests completed within this time
======================================================================
```

## Interpreting Server Status

The test client checks server health before and after tests:

```json
{
  "status": "HEALTHY",        // No active tasks
  "status": "HEALTHY_BUSY",   // Tasks are being processed
  "timeOfLastUpdate": "...",
  "activeTasks": 5
}
```

## JSON Output Format

When using `-o results.json`, the output includes:

```json
{
  "timestamp": "2025-10-27T12:34:56.789Z",
  "statistics": {
    "total_requests": 100,
    "successful_requests": 98,
    "avg_response_time": 2.456,
    "p95_response_time": 3.890,
    ...
  },
  "results": [
    {
      "request_id": 1,
      "success": true,
      "duration": 2.34,
      "status_code": 200,
      "response_data": {...}
    },
    ...
  ]
}
```

## Performance Tips

1. **Match Concurrency to Server Capacity**: Your server has `MAX_WORKERS=25`, so `-c 25` matches the server's thread pool size.

2. **Monitor Server Resources**: Watch CPU, memory, and network during tests:
   ```bash
   # In another terminal
   watch -n 1 'curl -s http://localhost:8080/stats | jq'
   ```

3. **Gradual Ramp-Up**: Use `-d` flag to gradually increase load:
   ```bash
   python test_client.py -n 100 -c 10 -d 0.1
   ```

4. **Test Different Prompt Lengths**: Longer prompts may take more time to process.

## Troubleshooting

### Connection Refused
```
✗ Request 1: Connection refused
```
**Solution**: Ensure the server is running on the specified URL.

### Too Many Timeouts
```
✗ Request 1: Timeout after 300.00s
```
**Solution**:
- Increase timeout: `-t 600`
- Reduce concurrency: `-c 10`
- Check server logs for errors

### All Requests Fail
**Solution**: Check server health manually:
```bash
curl http://localhost:8080/ping
curl http://localhost:8080/stats
```

## Best Practices

1. **Start Small**: Begin with low concurrency and gradually increase
2. **Monitor Server**: Keep an eye on server logs during tests
3. **Save Results**: Always use `-o` for important tests
4. **Test Realistic Scenarios**: Use prompts similar to production usage
5. **Check Health**: Verify server returns to HEALTHY after test completes

## Example Workflows

### HTTP Mode Workflow
```bash
# 1. Start with a small test
python test_client.py -n 10 -c 2

# 2. Increase load gradually
python test_client.py -n 50 -c 10 -o test_50.json

# 3. Full stress test
python test_client.py -n 200 -c 25 -o stress_test.json

# 4. Analyze results
cat stress_test.json | jq '.statistics'

# 5. Check if server recovered
curl http://localhost:8080/ping
```

### AgentCore Mode Workflow
```bash
# 1. Get your Runtime ARN
export RUNTIME_ARN=$(cat src/.bedrock_agentcore.yaml | grep agent_arn | awk '{print $2}')

# 2. Start with a small test
python test_client.py --mode agentcore --runtime-arn $RUNTIME_ARN -n 5 -c 2

# 3. Increase load gradually
python test_client.py --mode agentcore --runtime-arn $RUNTIME_ARN \
  -n 50 -c 10 -o agentcore_test_50.json

# 4. Full stress test
python test_client.py --mode agentcore --runtime-arn $RUNTIME_ARN \
  -n 100 -c 20 -o agentcore_stress.json

# 5. Analyze results
cat agentcore_stress.json | jq '.statistics'
```

### Comparing HTTP vs AgentCore Performance
```bash
# Test HTTP endpoint
python test_client.py --mode http --url http://localhost:8080 \
  -n 100 -c 20 -p "What is AI?" -o http_results.json

# Test AgentCore Runtime
python test_client.py --mode agentcore --runtime-arn $RUNTIME_ARN \
  -n 100 -c 20 -p "What is AI?" -o agentcore_results.json

# Compare results
echo "HTTP Results:"
cat http_results.json | jq '.statistics'
echo "\nAgentCore Results:"
cat agentcore_results.json | jq '.statistics'
```
