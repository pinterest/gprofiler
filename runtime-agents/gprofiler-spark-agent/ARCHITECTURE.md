# gProfiler Spark Agent Architecture

## Overview
The `gprofiler-spark-agent` is a lightweight Java agent designed to run alongside Apache Spark applications (drivers and executors). Its primary purpose is to identify Spark processes to the local gProfiler agent and facilitate selective profiling based on a centralized "Allowed List".

## Key Responsibilities
1.  **Metadata Extraction**: Captures Spark-specific identity (`spark.app.id`, `spark.app.name`) and the process PID.
2.  **Heartbeating**: Periodically announces its presence to the local gProfiler agent.
3.  **Profiling Orchestration**: Receives commands from gProfiler (via heartbeat responses) to enable or disable deep profiling features.
4.  **Thread Tracking**: When profiling is enabled, tracks thread names and updates in real-time to ensure accurate profiling context.

## Communication Protocol
The agent communicates with the local gProfiler Python agent using HTTP.

*   **Protocol**: HTTP POST
*   **Target**: `http://127.0.0.1:12345/spark`
*   **Format**: JSON
*   **Default Interval**: 60 seconds

### Heartbeat Payload
```json
{
  "spark.app.id": "app-20230101000000-0000",
  "spark.app.name": "MySparkJob",
  "pid": 12345
}
```

### Heartbeat Response (Control)
The gProfiler agent responds with a JSON object indicating whether this process should be profiled.
```json
{
  "profile": true
}
```

### Thread Info Payload
Sent when profiling is enabled or a thread name changes.
```json
{
  "spark.app.id": "...",
  "pid": 12345,
  "type": "thread_info",
  "threads": [
    { "tid": 1, "name": "main" },
    { "tid": 15, "name": "Executor task launch worker-0" }
  ]
}
```

## Internal Architecture

### 1. Instrumentation (ASM)
To capture thread name changes in real-time without polling overhead, the agent uses **ObjectWeb ASM** to instrument the `java.lang.Thread` class.

*   **Transformer**: `ThreadNameTransformer`
*   **Target Method**: `java.lang.Thread.setName(String name)`
*   **Action**: Injects a callback to `Agent.onThreadNameChanged(this)` after the method execution.
*   **Bootstrap Injection**: Since `java.lang.Thread` is loaded by the Bootstrap ClassLoader, the agent jar is appended to the bootstrap search path (`instrumentation.appendToBootstrapClassLoaderSearch`) during `premain`.

### 2. Concurrency Model (Decoupled I/O)
Thread instrumentation happens on the application's critical path. To minimize impact:

1.  **Producer (App Thread)**: The `setName` hook simply places the `Thread` reference into a `LinkedBlockingQueue`. This is a non-blocking (or very low contention) operation.
2.  **Consumer (Background Thread)**: A `ScheduledExecutorService` task runs frequently (1ms fixed delay) to drain this queue.
3.  **Network I/O**: The consumer thread handles the JSON serialization and HTTP transmission.

This separation ensures that network latency or blocking never affects the Spark application threads.

### 3. Dependencies
The agent aims for minimal interference with the host application.
*   **Gson**: Used for JSON serialization.
*   **ASM**: Used for bytecode manipulation.
*   **Shading**: All dependencies are relocated (shaded) under `com.gprofiler.spark.shaded.*` using the `maven-shade-plugin`. This prevents `NoSuchMethodError` or class conflicts if the Spark application uses different versions of Gson or ASM.

## Build System
*   **Tool**: Maven
*   **Compatibility**: Java 8 source/target (to support Spark running on Java 8, 11, 17+).
*   **Artifact**: Produces a standalone "uber-jar" suitable for `-javaagent`.

## Usage
Add the agent to the Spark configuration:
```bash
--conf "spark.driver.extraJavaOptions=-javaagent:/path/to/gprofiler-spark-agent.jar"
--conf "spark.executor.extraJavaOptions=-javaagent:/path/to/gprofiler-spark-agent.jar"
```
