# Profiling Control System with Heartbeat Protocol

This document describes the implementation of a centralized profiling control system where a Performance Studio backend can dynamically issue profiling commands (start/stop) to gProfiler agents via a heartbeat protocol.

## System Overview

```
┌─────────────────────┐    Heartbeat     ┌──────────────────────┐
│                     │ ◄──────────────► │                      │
│  Performance Studio │                  │   gProfiler Agent    │
│     Backend         │   Commands       │                      │
│                     │ ────────────────► │                      │
└─────────────────────┘                  └──────────────────────┘
```

### Key Components

1. **Performance Studio Backend** - Central control server that:
   - Receives profiling requests via REST API
   - Manages profiling commands for hosts/services  
   - Responds to agent heartbeats with pending commands
   - Tracks command execution status

2. **gProfiler Agent** - Profiling agent that:
   - Sends periodic heartbeats to the backend
   - Receives and executes profiling commands
   - Ensures idempotent command execution
   - Reports command completion status

## Features

### ✅ Backend Features
- **REST API** for submitting profiling requests
- **Heartbeat endpoint** for agent communication
- **Command merging** for multiple requests targeting same host
- **Process-level and host-level** stop commands
- **Idempotent command execution** using unique command IDs
- **Command completion tracking**

### ✅ Agent Features  
- **Heartbeat communication** with configurable intervals
- **Dynamic profiling** based on server commands
- **Command-driven execution** (start/stop profiling)
- **Idempotency** to prevent duplicate command execution
- **Persistent command tracking** across agent restarts
- **Graceful error handling** and retry logic

## API Endpoints

### 1. Submit Profiling Request

```http
POST /api/metrics/profile_request
```

**Request Body:**
```json
{
  "service_name": "my-service",
  "command_type": "start",  // "start" or "stop"
  "duration": 60,
  "frequency": 11,
  "profiling_mode": "cpu",
  "target_hostnames": ["host1", "host2"],
  "pids": [1234, 5678],  // Optional: specific PIDs
  "stop_level": "process",  // "process" or "host" (for stop commands)
  "additional_args": {}
}
```

**Response:**
```json
{
  "success": true,
  "message": "Start profiling request submitted successfully",
  "request_id": "req-uuid",
  "command_id": "cmd-uuid",
  "estimated_completion_time": "2025-01-08T12:00:00Z"
}
```

### 2. Agent Heartbeat

```http
POST /api/metrics/heartbeat
```

**Request Body:**
```json
{
  "ip_address": "192.168.1.100",
  "hostname": "worker-01",
  "service_name": "my-service",
  "last_command_id": "cmd-uuid",
  "status": "active",
  "timestamp": "2025-01-08T11:00:00Z"
}
```

**Response:**
```json
{
  "success": true,
  "message": "Heartbeat received. New profiling command available.",
  "profiling_command": {
    "command_type": "start",
    "combined_config": {
      "duration": 60,
      "frequency": 11,
      "profiling_mode": "cpu"
    }
  },
  "command_id": "cmd-uuid"
}
```

### 3. Report Command Completion

```http
POST /api/metrics/command_completion
```

**Request Body:**
```json
{
  "command_id": "cmd-uuid",
  "hostname": "worker-01", 
  "status": "completed",  // "completed" or "failed"
  "execution_time": 65,
  "error_message": null,
  "results_path": "s3://bucket/path/to/results"
}
```

## Usage Examples

### Backend - Submit Start Command

```bash
curl -X POST http://localhost:8000/api/metrics/profile_request \
  -H "Content-Type: application/json" \
  -d '{
    "service_name": "web-service",
    "command_type": "start",
    "duration": 120,
    "frequency": 11,
    "profiling_mode": "cpu",
    "target_hostnames": ["web-01", "web-02"]
  }'
```

### Backend - Submit Stop Command

```bash
curl -X POST http://localhost:8000/api/metrics/profile_request \
  -H "Content-Type: application/json" \
  -d '{
    "service_name": "web-service", 
    "command_type": "stop",
    "stop_level": "host",
    "target_hostnames": ["web-01"]
  }'
```

### Agent - Run in Heartbeat Mode

**Basic heartbeat mode:**
```bash
python gprofiler/main.py \
  --enable-heartbeat-server \
  --upload-results \
  --token "your-token" \
  --service-name "web-service" \
  --api-server "http://performance-studio:8000" \
  --heartbeat-interval 30 \
  --output-dir /tmp/profiles \
  --verbose
```

**Production deployment with all optimizations:**
```bash
# Set environment variables first
export GPROFILER_TOKEN="my_token"
export GPROFILER_SERVICE="your-service-name"  
export GPROFILER_SERVER="http://localhost:8080"

# Production command (can also source /opt/gprofiler/envs.sh for variables)
/opt/gprofiler/gprofiler \
  -u \
  --token=$GPROFILER_TOKEN \
  --service-name=$GPROFILER_SERVICE \
  --server-host $GPROFILER_SERVER \
  --dont-send-logs \
  --server-upload-timeout 10 \
  -c \
  --disable-metrics-collection \
  --java-safemode= \
  -d 60 \
  --java-no-version-check
```

## Implementation Details

### Backend Logic

1. **Command Generation**: Each profiling request generates a unique `command_id`
2. **Command Merging**: Multiple requests for the same host are merged into single commands
3. **Stop Handling**: 
   - Process-level stops remove specific PIDs from commands
   - Host-level stops terminate all profiling for the host
4. **Heartbeat Response**: Returns pending commands with `command_type` and configuration

### Agent Logic

1. **Heartbeat Loop**: Sends heartbeats at configured intervals
2. **Command Processing**:
   - `start`: Stop current profiler (if any) and start new one with given config
   - `stop`: Stop current profiler without starting a new one
3. **Idempotency**: Track executed command IDs to prevent duplicates
4. **Persistence**: Save executed command IDs to disk for restart resilience

### Command Flow

```
1. User submits profiling request to backend
   ↓
2. Backend creates command with unique ID
   ↓  
3. Agent sends heartbeat to backend
   ↓
4. Backend responds with pending command
   ↓
5. Agent executes command (start/stop profiling)
   ↓
6. Agent reports completion to backend
   ↓
7. Backend updates command status
```

## Configuration

### Backend Configuration
- Database connection for command storage
- API endpoints for profiling control
- Command merging and deduplication logic

### Agent Configuration
```bash
--enable-heartbeat-server     # Enable heartbeat mode
--heartbeat-interval 30       # Heartbeat frequency (seconds)
--api-server URL             # Backend server URL
--upload-results             # Required for heartbeat mode
--token TOKEN                # Authentication token
--service-name NAME          # Service identifier
```

## Testing

### Test Scripts

1. **test_heartbeat_system.py** - Test backend API and heartbeat flow
2. **run_heartbeat_agent.py** - Run agent in heartbeat mode for testing

### Test Workflow

1. Start Performance Studio backend
2. Run test agent: `python run_heartbeat_agent.py`
3. Submit test commands: `python test_heartbeat_system.py`  
4. Verify agent receives and executes commands
5. Check idempotency and error handling

## Error Handling

### Backend
- Validates profiling request parameters
- Handles database connection errors
- Returns appropriate HTTP status codes
- Logs all operations for debugging

### Agent  
- Retries failed heartbeats with backoff
- Continues heartbeat loop on command execution errors
- Persists executed command IDs across restarts
- Graceful shutdown on termination signals

## Security Considerations

- **Authentication**: Token-based authentication for agent-backend communication
- **Authorization**: Service-based access control for profiling commands
- **Command Validation**: Validate all command parameters before execution
- **Rate Limiting**: Prevent abuse of profiling requests
- **Audit Logging**: Track all profiling activities for compliance

## Future Enhancements

- **Real-time Status**: WebSocket connection for real-time agent status
- **Command Scheduling**: Schedule profiling commands for future execution  
- **Resource Monitoring**: Check system resources before starting profiling
- **Multi-tenant Support**: Isolation between different services/teams
- **Command Prioritization**: Priority queues for urgent profiling requests
- **Distributed Coordination**: Coordinate profiling across multiple agents

## Troubleshooting

### Common Issues

1. **Agent not receiving commands**
   - Check network connectivity to backend
   - Verify authentication token
   - Check service name matching

2. **Commands not executing**
   - Check agent logs for errors
   - Verify command parameters are valid
   - Check system permissions for profiling

3. **Duplicate commands**
   - Verify idempotency implementation
   - Check command ID persistence
   - Review heartbeat timing

### Debugging

- Enable verbose logging: `--verbose`
- Check heartbeat logs: `/tmp/gprofiler-heartbeat.log`
- Monitor backend API logs
- Use test scripts to isolate issues

## Building and Running gProfiler Locally

### Prerequisites
- Linux system (x86_64 or Aarch64)
- Python 3.10+ for source builds
- Docker for containerized builds
- 16GB+ RAM for full builds
- Root access for profiling operations

### Build Options

#### 1. Build Executable (Recommended)

```bash
cd gprofiler

# Full build (takes 20-30 minutes, builds all profilers from source)
./scripts/build_x86_64_executable.sh

# Fast build (for development, skips some optimizations)
./scripts/build_x86_64_executable.sh --fast
```

The executable will be created at `build/x86_64/gprofiler`.

#### 2. Build Docker Image

```bash
./scripts/build_x86_64_container.sh -t gprofiler
```

#### 3. Run from Source (Development)

```bash
# Install dependencies
pip3 install -r requirements.txt

# Copy required resources
./scripts/copy_resources_from_image.sh

# Run directly from source (requires root)
sudo python3 -m gprofiler [options]
```

### Running Locally

#### Basic Local Profiling

```bash
# Make executable and run basic profiling
chmod +x build/x86_64/gprofiler
sudo ./build/x86_64/gprofiler -o /tmp/gprofiler-output -d 30
```

#### Production-Style Local Run

```bash
# Set environment variables
export GPROFILER_TOKEN="my_token"
export GPROFILER_SERVICE="your-service-name"
export GPROFILER_SERVER="http://localhost:8080"

# Run with production flags
sudo ./build/x86_64/gprofiler \
  -u \
  --token=$GPROFILER_TOKEN \
  --service-name=$GPROFILER_SERVICE \
  --server-host $GPROFILER_SERVER \
  --dont-send-logs \
  --server-upload-timeout 10 \
  -c \
  --disable-metrics-collection \
  --java-safemode= \
  -d 60 \
  --java-no-version-check
```

#### Local Heartbeat Mode Testing

```bash
# Run agent in heartbeat mode for testing
sudo ./build/x86_64/gprofiler \
  --enable-heartbeat-server \
  --upload-results \
  --token=$GPROFILER_TOKEN \
  --service-name=$GPROFILER_SERVICE \
  --api-server=$GPROFILER_SERVER \
  --server-host=$GPROFILER_SERVER \
  --heartbeat-interval 30 \
  --output-dir /tmp/profiles \
  --dont-send-logs \
  --server-upload-timeout 10 \
  --disable-metrics-collection \
  --java-safemode= \
  --java-no-version-check \
  --verbose
```

### Command Line Options Explained

```bash
-u, --upload-results              # Upload results to Performance Studio
--token=$GPROFILER_TOKEN          # Authentication token
--service-name=$GPROFILER_SERVICE # Service identifier  
--server-host $GPROFILER_SERVER   # Performance Studio backend URL
--dont-send-logs                  # Disable log transmission
--server-upload-timeout 10        # Upload timeout (seconds)
-c, --continuous                  # Continuous profiling mode
--disable-metrics-collection      # Disable system metrics collection
--java-safemode=                  # Disable Java safe mode (empty value)
-d 60                            # Profiling duration (seconds)
--java-no-version-check          # Skip Java version check
--enable-heartbeat-server         # Enable heartbeat communication
--heartbeat-interval 30           # Heartbeat frequency (seconds)
--api-server URL                  # Heartbeat API server URL
-o, --output-dir PATH            # Local output directory
--verbose                        # Enable verbose logging
```

### Development Workflow

1. **Build**: `./scripts/build_x86_64_executable.sh --fast`
2. **Test locally**: `sudo ./build/x86_64/gprofiler -o /tmp/results -d 30`
3. **View results**: Open `/tmp/results/last_flamegraph.html` in browser
4. **Test heartbeat**: Run with `--enable-heartbeat-server` flag

### Troubleshooting Local Builds

- **Build fails**: Ensure 16GB+ RAM available
- **Permission errors**: Run profiling commands with `sudo`
- **Docker issues**: Ensure Docker daemon is running
- **Missing dependencies**: Install build requirements with package manager
