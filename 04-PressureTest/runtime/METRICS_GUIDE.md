# Metrics Tracking Guide

The test client now automatically tracks and reports detailed metrics from your AgentCore Runtime responses.

## What Metrics Are Tracked

### 1. Token Usage
- **Input Tokens**: Tokens in the user's prompt
- **Output Tokens**: Tokens in the agent's response
- **Total Tokens**: Sum of input and output tokens

### 2. Agent Latency
- **Latency (ms)**: Internal processing time by the agent (excludes network overhead)

## Metrics in Real-Time Output

During test execution, you'll see metrics for each successful request:

```
✓ Request 1: 2.34s (tokens: 9/10, latency: 379ms)
                      ↑        ↑    ↑         ↑
                      │        │    │         └─ Agent processing time
                      │        │    └─────────── Output tokens
                      │        └──────────────── Input tokens
                      └───────────────────────── Total request time (including network)
```

## Metrics in Summary Statistics

After test completion, you'll see aggregated metrics:

```
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
  Total Input:       900 tokens        ← Sum of all input tokens
  Total Output:      1,000 tokens      ← Sum of all output tokens
  Total:             1,900 tokens      ← Total tokens consumed
  Avg Input/Req:     9.0 tokens        ← Average input per request
  Avg Output/Req:    10.0 tokens       ← Average output per request

Agent Latency:
  Average:           379.5ms           ← Average agent processing time
======================================================================
```

## Understanding the Metrics

### Response Time vs Agent Latency

- **Response Time**: Total time from client perspective (includes network, serialization, etc.)
- **Agent Latency**: Time spent in agent processing only (from metrics.accumulated_metrics.latencyMs)

Example:
```
Request 1: 2.34s (tokens: 9/10, latency: 379ms)
           ↑                            ↑
           Total response time          Agent processing time only
           (includes 2.34s - 0.379s = 1.96s of network/overhead)
```

### Token Usage Analysis

**Total Tokens** is important for:
- Cost estimation (tokens × price per token)
- Usage monitoring
- Performance optimization

**Average Tokens per Request** helps:
- Understand typical prompt/response sizes
- Identify outliers
- Optimize prompt design

## Metrics in JSON Output

When saving results with `-o output.json`, metrics are included in detail:

```json
{
  "timestamp": "2025-10-27T12:34:56.789Z",
  "statistics": {
    "total_requests": 100,
    "successful_requests": 100,
    "avg_response_time": 2.456,
    "total_input_tokens": 900,
    "total_output_tokens": 1000,
    "total_tokens": 1900,
    "avg_input_tokens": 9.0,
    "avg_output_tokens": 10.0,
    "avg_latency_ms": 379.5,
    ...
  },
  "results": [
    {
      "request_id": 1,
      "success": true,
      "duration": 2.34,
      "status_code": 200,
      "metrics": {
        "latency_ms": 379,
        "input_tokens": 9,
        "output_tokens": 10,
        "total_tokens": 19
      },
      "response_data": {...}
    },
    ...
  ]
}
```

## Analyzing Metrics

### View Total Token Usage
```bash
cat results.json | jq '.statistics | {
  total_input: .total_input_tokens,
  total_output: .total_output_tokens,
  total: .total_tokens
}'
```

### View Average Metrics
```bash
cat results.json | jq '.statistics | {
  avg_latency_ms: .avg_latency_ms,
  avg_input_tokens: .avg_input_tokens,
  avg_output_tokens: .avg_output_tokens
}'
```

### View Per-Request Metrics
```bash
cat results.json | jq '.results[] | select(.metrics != null) | {
  request_id: .request_id,
  duration: .duration,
  latency_ms: .metrics.latency_ms,
  tokens: .metrics.total_tokens
}'
```

### Find High Token Usage Requests
```bash
cat results.json | jq '.results[] | select(.metrics.total_tokens > 50) | {
  request_id: .request_id,
  tokens: .metrics.total_tokens
}'
```

## Cost Estimation

If you know your token pricing, calculate costs:

```bash
# Example: $0.003 per 1K input tokens, $0.015 per 1K output tokens
cat results.json | jq '
  .statistics |
  (.total_input_tokens / 1000 * 0.003) +
  (.total_output_tokens / 1000 * 0.015)
' | awk '{printf "Total Cost: $%.4f\n", $1}'
```

## Example: Comparing Different Prompts

Test with different prompt lengths to see token usage:

```bash
# Short prompt
python test_client.py -n 50 -c 10 -p "Hello" -o short_prompt.json

# Medium prompt
python test_client.py -n 50 -c 10 -p "Explain AI in detail" -o medium_prompt.json

# Long prompt
python test_client.py -n 50 -c 10 -p "Write a comprehensive essay about AI" -o long_prompt.json

# Compare token usage
echo "Short prompt:"
cat short_prompt.json | jq '.statistics | {avg_input: .avg_input_tokens, avg_output: .avg_output_tokens}'

echo "Medium prompt:"
cat medium_prompt.json | jq '.statistics | {avg_input: .avg_input_tokens, avg_output: .avg_output_tokens}'

echo "Long prompt:"
cat long_prompt.json | jq '.statistics | {avg_input: .avg_input_tokens, avg_output: .avg_output_tokens}'
```

## Metrics Data Structure

The response from your runtime includes:

```json
{
  "output": {
    "message": {...},
    "timestamp": "2025-10-27T06:29:54.252094+00:00",
    "request_id": "1761546593.668231",
    "metrics": {
      "accumulated_metrics": {
        "latencyMs": 379
      },
      "accumulated_usage": {
        "inputTokens": 9,
        "outputTokens": 10,
        "totalTokens": 19
      }
    },
    "status": "success"
  }
}
```

The test client automatically extracts these metrics and includes them in:
1. Real-time console output
2. Summary statistics
3. JSON output files

## Best Practices

1. **Save results**: Always use `-o` to save detailed metrics
2. **Compare tests**: Save different test scenarios to compare token usage
3. **Monitor trends**: Track token usage over time to identify changes
4. **Optimize prompts**: Use metrics to optimize prompt efficiency
5. **Budget planning**: Use token metrics for cost estimation

## Troubleshooting

### No Metrics Displayed

If you don't see metrics in the output:

1. **Check runtime version**: Ensure your runtime returns metrics in responses
2. **Verify response format**: Check that responses include the `metrics` field
3. **Review logs**: Failed requests won't have metrics

### Metrics Show Zero

If all metrics are zero:

- The runtime may not be returning metrics data
- Check the response format matches expected structure
- Verify the agent is properly configured to track metrics
