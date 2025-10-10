# Error Metrics Integration Example

## Usage

To enable error metrics collection and sending to your company's websocket-based metrics service:

```bash
# Basic usage with error metrics enabled
./gprofiler --enable-error-metrics \
            --metrics-server-url "wss://metrics.company.com/ws" \
            --metrics-auth-token "your-auth-token" \
            --service-name "gprofiler-production" \
            --continuous

# With additional options
./gprofiler --enable-error-metrics \
            --metrics-server-url "wss://metrics.company.com/ws" \
            --metrics-auth-token "your-auth-token" \
            --service-name "gprofiler-production" \
            --upload-results \
            --token "your-api-token" \
            --continuous
```

## Metric Format

The error metrics handler sends metrics in the following format:

```json
{
  "type": "metrics",
  "batch": [
    {
      "timestamp": 1696781234567,
      "service": "gprofiler-production",
      "metric_name": "gprofiler_error_count",
      "metric_type": "counter",
      "value": 1,
      "tags": {
        "error_level": "error",
        "error_category": "profiler_error",
        "logger_name": "gprofiler.profilers.py_spy",
        "gprofiler_version": "1.153.0",
        "run_id": "abc123-def456",
        "cycle_id": "cycle_789",
        "exception_type": "ProcessLookupError"
      }
    }
  ],
  "auth_token": "your-auth-token"
}
```

## Error Categories

The handler automatically categorizes errors based on logger names and message content:

- `profiler_error`: Issues with individual profilers (py-spy, perf, etc.)
- `upload_error`: Problems uploading data to servers
- `memory_error`: Memory management and cleanup issues
- `system_error`: System-level metrics and monitoring problems
- `container_error`: Container-related issues
- `general_error`: Other uncategorized errors

## Configuration

The metrics handler is designed to be resilient:

- **Non-blocking**: Uses a queue to avoid blocking the main application
- **Batching**: Sends metrics in batches for efficiency
- **Automatic reconnection**: Handles websocket disconnections gracefully
- **Error isolation**: Handler failures don't affect the main application
- **Memory bounded**: Drops metrics if queue becomes full

## Customization

To customize the metric format or add additional fields, modify the `_create_metric_from_record()` method in `ErrorMetricsHandler`.

To change error categorization logic, modify the `_categorize_error()` method.

## Monitoring the Metrics Handler

The handler logs its own status at DEBUG level. To see metrics handler logs:

```bash
./gprofiler --enable-error-metrics \
            --metrics-server-url "wss://metrics.company.com/ws" \
            --verbose \
            ...
```