# System Statistics Documentation

The `/stats` and `/ping` endpoints now provide detailed CPU and memory usage information.

## Overview

The runtime server now tracks:
- **Process-level metrics**: CPU and memory usage of the agent runtime process
- **System-level metrics**: Overall system CPU and memory usage
- **Thread pool status**: Active threads and tasks
- **Timestamps**: When statistics were captured

## Dependencies

The system statistics feature requires `psutil`:
```bash
pip install psutil
```

This is already included in `pyproject.toml`.

## Endpoints

### GET /stats

Returns detailed statistics including CPU and memory usage.

**Example Request:**
```bash
curl http://localhost:8080/stats
```

**Example Response:**
```json
{
  "max_workers": 50,
  "active_threads": 10,
  "active_tasks": 5,
  "status": "operational",
  "timestamp": "2025-10-27T12:34:56.789Z",
  "process": {
    "cpu_percent": 45.5,
    "memory_mb": 512.75,
    "memory_percent": 3.2,
    "pid": 12345
  },
  "system": {
    "cpu_percent": 62.3,
    "memory_total_mb": 16384.0,
    "memory_used_mb": 8192.5,
    "memory_available_mb": 8191.5,
    "memory_percent": 50.0
  }
}
```

### GET /ping

Returns basic health status.

**Example Request:**
```bash
curl http://localhost:8080/ping
```

**Example Response:**
```json
{
  "status": "HEALTHY",
  "timeOfLastUpdate": "2025-10-27T12:34:56.789Z",
  "activeTasks": 5
}
```

### POST /invocations with get_stats

You can also get stats via the invocations endpoint.

**Example Request:**
```bash
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"input": {"get_stats": true}}'
```

**Example Response:**
```json
{
  "output": {
    "type": "stats",
    "data": {
      "max_workers": 50,
      "active_threads": 10,
      "active_tasks": 5,
      "status": "operational",
      "timestamp": "2025-10-27T12:34:56.789Z",
      "process": {
        "cpu_percent": 45.5,
        "memory_mb": 512.75,
        "memory_percent": 3.2,
        "pid": 12345
      },
      "system": {
        "cpu_percent": 62.3,
        "memory_total_mb": 16384.0,
        "memory_used_mb": 8192.5,
        "memory_available_mb": 8191.5,
        "memory_percent": 50.0
      }
    }
  }
}
```

## Metrics Explained

### Process Metrics

These metrics are specific to the agent runtime process:

| Metric | Description | Unit |
|--------|-------------|------|
| `cpu_percent` | CPU usage by this process | Percentage (0-100+) |
| `memory_mb` | Memory used by this process (RSS) | Megabytes |
| `memory_percent` | Process memory as % of total system memory | Percentage |
| `pid` | Process ID | Integer |

**Note**: CPU percentage can exceed 100% on multi-core systems (e.g., 200% = fully using 2 cores).

### System Metrics

These metrics are system-wide:

| Metric | Description | Unit |
|--------|-------------|------|
| `cpu_percent` | Overall system CPU usage | Percentage (0-100) |
| `memory_total_mb` | Total system memory | Megabytes |
| `memory_used_mb` | Currently used memory | Megabytes |
| `memory_available_mb` | Available memory | Megabytes |
| `memory_percent` | System memory usage | Percentage |

### Thread Pool Metrics

| Metric | Description |
|--------|-------------|
| `max_workers` | Maximum number of worker threads configured |
| `active_threads` | Number of threads currently active in the pool |
| `active_tasks` | Number of requests currently being processed |

## Monitoring Examples

### Real-time Monitoring with watch

Monitor stats every second:
```bash
watch -n 1 'curl -s http://localhost:8080/stats | jq'
```

Monitor only CPU and memory:
```bash
watch -n 1 'curl -s http://localhost:8080/stats | jq "{process: .process, system: .system}"'
```

### Using jq to Extract Specific Metrics

**Process CPU usage:**
```bash
curl -s http://localhost:8080/stats | jq '.process.cpu_percent'
```

**Process memory usage:**
```bash
curl -s http://localhost:8080/stats | jq '.process.memory_mb'
```

**System memory usage:**
```bash
curl -s http://localhost:8080/stats | jq '.system.memory_percent'
```

**Active tasks:**
```bash
curl -s http://localhost:8080/stats | jq '.active_tasks'
```

### Monitoring During Load Tests

Run a monitoring script in one terminal:
```bash
#!/bin/bash
while true; do
  STATS=$(curl -s http://localhost:8080/stats)
  CPU=$(echo $STATS | jq -r '.process.cpu_percent')
  MEM=$(echo $STATS | jq -r '.process.memory_mb')
  TASKS=$(echo $STATS | jq -r '.active_tasks')
  TIME=$(date '+%H:%M:%S')

  echo "$TIME - CPU: $CPU% | Memory: ${MEM}MB | Tasks: $TASKS"
  sleep 1
done
```

Then run your load test in another terminal:
```bash
python test_client.py -n 100 -c 25
```

### Logging Stats to File

Capture stats every 5 seconds during a test:
```bash
# Start logging
while true; do
  curl -s http://localhost:8080/stats >> stats_log.jsonl
  echo "" >> stats_log.jsonl
  sleep 5
done &
LOG_PID=$!

# Run test
python test_client.py -n 100 -c 25

# Stop logging
kill $LOG_PID

# Analyze logs
cat stats_log.jsonl | jq -s 'map(.process.cpu_percent) | add / length'  # Average CPU
cat stats_log.jsonl | jq -s 'map(.process.memory_mb) | max'  # Peak memory
```

## Performance Analysis

### Identifying Resource Bottlenecks

**CPU Bottleneck Indicators:**
- Process CPU % consistently at or near 100% per core
- Response times increasing with higher concurrency
- Active tasks backing up

**Memory Bottleneck Indicators:**
- Process memory continuously increasing
- System memory % approaching 100%
- Possible OOM (Out of Memory) errors

### Optimal Resource Usage

**Good indicators:**
- CPU usage scales with load
- Memory usage stable or grows slowly
- Active tasks processed efficiently

**Example healthy stats under load:**
```json
{
  "active_tasks": 15,
  "process": {
    "cpu_percent": 180.5,  // Using ~2 cores efficiently
    "memory_mb": 768.5,    // Stable memory usage
    "memory_percent": 4.8
  },
  "system": {
    "cpu_percent": 45.0,   // System not saturated
    "memory_percent": 35.0  // Plenty of memory available
  }
}
```

## Integration with Test Client

The test client can fetch stats during tests:

**Python example:**
```python
import requests

# During test
stats = requests.get("http://localhost:8080/stats").json()
print(f"CPU: {stats['process']['cpu_percent']}%")
print(f"Memory: {stats['process']['memory_mb']}MB")
print(f"Active Tasks: {stats['active_tasks']}")
```

## Grafana/Prometheus Integration

For production monitoring, you can expose these metrics to Prometheus:

1. Create a metrics endpoint that formats stats in Prometheus format
2. Configure Prometheus to scrape the endpoint
3. Visualize in Grafana

**Example Prometheus metrics format:**
```
# HELP agentcore_cpu_percent Process CPU usage percentage
# TYPE agentcore_cpu_percent gauge
agentcore_cpu_percent{pid="12345"} 45.5

# HELP agentcore_memory_mb Process memory usage in megabytes
# TYPE agentcore_memory_mb gauge
agentcore_memory_mb{pid="12345"} 512.75

# HELP agentcore_active_tasks Number of active tasks
# TYPE agentcore_active_tasks gauge
agentcore_active_tasks 5
```

## Troubleshooting

### High CPU Usage

**Symptoms:**
- Process CPU % consistently above 300-400%
- Slow response times

**Solutions:**
1. Increase `MAX_WORKERS` if tasks are CPU-bound
2. Optimize agent prompts/models
3. Scale horizontally (multiple instances)

### High Memory Usage

**Symptoms:**
- Process memory continuously growing
- System running out of memory

**Solutions:**
1. Check for memory leaks
2. Reduce `MAX_WORKERS`
3. Implement request queuing
4. Increase system memory

### Stats Endpoint Slow

If `/stats` itself is slow:
- The `psutil` calls use short intervals (0.1s)
- Consider caching stats if called very frequently
- Use background task to update stats periodically

## Best Practices

1. **Monitor before, during, and after tests**: Establish baseline metrics
2. **Track trends**: Log stats over time to identify patterns
3. **Set alerts**: Monitor for CPU/memory thresholds
4. **Correlate with performance**: Compare stats with response times
5. **Test different loads**: Find optimal concurrency for your resources

## Example Monitoring Dashboard

Create a simple dashboard script:

```bash
#!/bin/bash
# dashboard.sh

clear
echo "AgentCore Runtime Dashboard"
echo "==========================="
echo ""

while true; do
  STATS=$(curl -s http://localhost:8080/stats)

  # Extract metrics
  CPU=$(echo $STATS | jq -r '.process.cpu_percent')
  MEM=$(echo $STATS | jq -r '.process.memory_mb')
  MEM_PCT=$(echo $STATS | jq -r '.process.memory_percent')
  TASKS=$(echo $STATS | jq -r '.active_tasks')
  THREADS=$(echo $STATS | jq -r '.active_threads')
  MAX_WORKERS=$(echo $STATS | jq -r '.max_workers')

  SYS_CPU=$(echo $STATS | jq -r '.system.cpu_percent')
  SYS_MEM_PCT=$(echo $STATS | jq -r '.system.memory_percent')

  # Display
  tput cup 3 0
  echo "Process Stats:"
  echo "  CPU:     ${CPU}%"
  echo "  Memory:  ${MEM}MB (${MEM_PCT}%)"
  echo ""
  echo "System Stats:"
  echo "  CPU:     ${SYS_CPU}%"
  echo "  Memory:  ${SYS_MEM_PCT}%"
  echo ""
  echo "Thread Pool:"
  echo "  Active:  ${THREADS}/${MAX_WORKERS}"
  echo "  Tasks:   ${TASKS}"
  echo ""
  echo "Last Update: $(date '+%H:%M:%S')"

  sleep 1
done
```

Run it:
```bash
chmod +x dashboard.sh
./dashboard.sh
```
