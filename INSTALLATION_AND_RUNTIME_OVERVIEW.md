# gProfiler: From Source to Self-Contained Executable

This document explains the complete journey of how gProfiler Python source code becomes a self-contained executable and runs in two different deployment models:

1. **Docker Container** - Running inside a container environment
2. **Debian Package** - Installing directly on the host system via `apt install`

## 🎯 Deployment Models Overview

```
                    Python Source Code
                           ↓
                Multi-Stage Docker Build
                           ↓
                PyInstaller Bundle + StaticX
                           ↓
                    ┌──────────────────┐
                    │   Build Output   │
                    └─────────┬────────┘
                              │
               ┌──────────────┴──────────────┐
               ↓                             ↓
    📦 Docker Container Path          🗂️ Debian Package Path
    ├─ Docker Image                   ├─ .deb Package Creation
    ├─ Container Registry             ├─ Package Repository
    ├─ docker run / K8s               ├─ apt install
    └─ Runs IN container              └─ Runs ON host system
```

## 🏗️ Build Process Overview

### Universal Build Pipeline (Same for Both Paths)

```
Python Source Code → Multi-Stage Docker Build → PyInstaller Bundle → StaticX → Final Executable
                                                                              ↓
                                                               ┌─────────────────────────┐
                                                               │    Distribution Split   │
                                                               └─────┬──────────────┬────┘
                                                                     ↓              ↓
                                                            Docker Image      .deb Package
```

## 📋 Detailed Build Pipeline (Universal - Same for Both Deployment Models)

### Phase 1: Multi-Stage Docker Build (Native Components)

**🤔 Why Multi-Stage?**  
Different profilers are written in different programming languages. Each language needs its own build environment and tools. Multi-stage builds let us use the right tool for each job without bloating the final image.

**What Happens:** The build process uses **multiple Docker stages** to compile native profiling tools from different languages:

```dockerfile
# Rust Components (py-spy, rbspy)
FROM rust:1.86.0-alpine3.21 AS pyspy-builder
FROM rust:1.59-alpine3.15 AS rbspy-builder

# C/C++ Components (perf, async-profiler)
FROM ubuntu:18.04 AS perf-builder          # Old glibc for compatibility
FROM centos:7 AS async-profiler-builder    # Old glibc for compatibility

# Go Components (burn profiler)
FROM golang:1.16.3 AS burn-builder

# .NET Components
FROM mcr.microsoft.com/dotnet/sdk:6.0 AS dotnet-builder

# PHP Components
FROM ubuntu:20.04 AS phpspy-builder

# Node.js Components
FROM alpine AS node-package-builder-musl
FROM centos/devtoolset-7 AS node-package-builder-glibc
```

**🔨 What Each Stage Produces:**
- **py-spy**: Python profiler written in Rust → compiled to static Linux binary
- **rbspy**: Ruby profiler written in Rust → compiled to static Linux binary  
- **perf**: Linux system profiler written in C → compiled to Linux binary
- **async-profiler**: Java profiler written in C++ → compiled to `.so` shared library
- **phpspy**: PHP profiler written in C → compiled to Linux binary
- **dotnet-trace**: .NET profiler written in C# → compiled to Linux binary
- **burn**: Go profiler written in Go → compiled to static Linux binary

**🎯 Why This Approach:**
- **Language-specific**: Each profiler uses the best language for its target runtime
- **Performance**: Native code is faster than Python for CPU-intensive profiling
- **System access**: Native code can access low-level system APIs that Python cannot
- **Minimal overhead**: Profiling tools should not slow down target applications

### Phase 2: Python Environment Setup (CentOS 7)

**🤔 Why CentOS 7?**  
CentOS 7 uses glibc version 2.17 (from 2012). Linux programs link against glibc, and newer glibc versions cannot run on systems with older glibc. By building on CentOS 7, the executable can run on almost any Linux system from the last 10+ years.

```dockerfile
FROM centos:7 AS build-prepare

# Why CentOS 7?
# - Old glibc (2.17) ensures compatibility with older Linux systems
# - Built executable can run on CentOS 6+ and Ubuntu 14.04+

# Install Python 3.10 from source
COPY ./scripts/python310_build.sh .
RUN ./python310_build.sh

# Install build dependencies
RUN yum install -y gcc curl glibc-static libicu
```

**🔧 What This Does:**
1. **Installs old glibc**: Ensures maximum compatibility with target systems
2. **Builds Python 3.10 from source**: CentOS 7 only has Python 2.7, so we build modern Python
3. **Installs build tools**: gcc (C compiler), curl (downloads), glibc-static (for static linking)

**🎯 Backwards Compatibility Strategy:**
```
CentOS 7 (glibc 2.17) → Can run on:
├─ CentOS 6+ (2011+)
├─ Ubuntu 14.04+ (2014+)  
├─ RHEL 7+ (2014+)
├─ Amazon Linux 1+ (2010+)
└─ Most production Linux systems
```

### Phase 3: Python Dependencies Installation

**🤔 What Are Dependencies?**  
Python programs use external libraries (packages) for functionality. gProfiler needs libraries for gRPC communication, HTTP requests, system monitoring, etc.

```dockerfile
FROM build-prepare AS build-stage

# Install Python runtime dependencies
COPY requirements.txt requirements.txt
RUN python3 -m pip install --no-cache-dir -r requirements.txt

# Install executable-specific dependencies (PyInstaller, etc.)
COPY exe-requirements.txt exe-requirements.txt
RUN python3 -m pip install --no-cache-dir -r exe-requirements.txt
```

**📦 What Gets Installed:**

**Runtime Dependencies (requirements.txt):**
- **grpcio**: Library for gRPC communication with Performance Studio backend (source of memory leak issues)
- **requests**: HTTP client library for REST API calls
- **psutil**: System and process monitoring library
- **protobuf**: Protocol buffer serialization for efficient data transfer
- **click**: Command-line argument parsing library

**Build Dependencies (exe-requirements.txt):**
- **PyInstaller**: Tool to bundle Python applications into executables
- **StaticX**: Tool for creating truly static executables (no external library dependencies)
- **wheel**: Python packaging format
- **setuptools**: Python package building tools

**🎯 Why Two Requirements Files:**
- **requirements.txt**: Libraries needed at runtime (bundled into executable)
- **exe-requirements.txt**: Tools only needed during build process (not bundled)

### Phase 4: Resource Bundling

**🤔 What Are Resources?**  
The native profiler binaries built in Phase 1 need to be packaged with the Python code so the final executable has everything it needs.

```dockerfile
# Copy all native profilers into Python package
COPY --from=pyspy-builder /tmp/py-spy/py-spy gprofiler/resources/python/py-spy
COPY --from=rbspy-builder /tmp/rbspy/rbspy gprofiler/resources/ruby/rbspy
COPY --from=perf-builder /perf gprofiler/resources/perf
COPY --from=async-profiler-builder-glibc /tmp/async-profiler/build/lib/libasyncProfiler.so gprofiler/resources/java/glibc/libasyncProfiler.so
COPY --from=async-profiler-builder-musl /tmp/async-profiler/build/lib/libasyncProfiler.so gprofiler/resources/java/musl/libasyncProfiler.so
COPY --from=phpspy-builder /tmp/phpspy/phpspy gprofiler/resources/php/phpspy
COPY --from=dotnet-builder /usr/share/dotnet/host gprofiler/resources/dotnet/host
COPY --from=burn-builder /tmp/burn/burn gprofiler/resources/burn

# Copy gProfiler Python source code
COPY gprofiler gprofiler
```

**🗂️ Final Resource Structure:**
```
gprofiler/                    # Main Python package
├── __init__.py              # Python package marker
├── __main__.py              # Entry point (what runs when you execute)
├── profilers/               # Python code for different profilers
│   ├── java.py             # Java profiling logic
│   ├── python.py           # Python profiling logic
│   └── ruby.py             # Ruby profiling logic
├── resources/               # Native profiler binaries
│   ├── python/
│   │   ├── py-spy          # Rust binary (15MB)
│   │   └── pyperf/PyPerf   # BPF-based Python profiler
│   ├── ruby/rbspy          # Rust binary (8MB)
│   ├── java/
│   │   ├── glibc/libasyncProfiler.so    # For standard Linux systems
│   │   └── musl/libasyncProfiler.so     # For Alpine/musl systems
│   ├── php/phpspy          # C binary (2MB)
│   ├── dotnet/             # .NET profiling tools (50MB)
│   ├── node/               # Node.js profiling modules (5MB)
│   ├── perf                # Linux perf tool (3MB)
│   └── burn                # Go profiler (10MB)
└── utils/                   # Helper Python modules
```

**🎯 Why This Structure:**
- **Modular**: Each language profiler is separate
- **Platform-specific**: Different binaries for glibc vs musl systems
- **Self-contained**: Everything needed is included in one place

### Phase 5: PyInstaller Bundling

**🤔 What Is PyInstaller?**  
PyInstaller is a tool that takes a Python application and creates a standalone executable. It bundles the Python interpreter, all Python libraries, and your code into a single directory or file.

**🚫 Important Misconception:** PyInstaller does NOT compile Python to machine code. It just packages everything together.

```dockerfile
# Create PyInstaller bundle
COPY pyi_build.py pyinstaller.spec scripts/check_pyinstaller.sh ./

RUN pyinstaller pyinstaller.spec \
    && test -f build/pyinstaller/warn-pyinstaller.txt \
    && ./check_pyinstaller.sh
```

**🔧 What PyInstaller Does (Step by Step):**

1. **Dependency Analysis:**
   ```python
   # PyInstaller scans your code for imports
   import grpc           # Found: needs grpcio package
   import requests       # Found: needs requests package  
   import subprocess     # Found: built into Python
   from gprofiler import profilers  # Found: your code
   ```

2. **Python Interpreter Bundling:**
   - Copies the entire CPython interpreter (python3.10 binary + standard library)
   - This is ~50MB of Python runtime

3. **Library Collection:**
   - Copies ALL installed pip packages (grpcio, requests, psutil, etc.)
   - Copies ALL your Python source code (gprofiler package)
   - Copies ALL resources (native profiler binaries)

4. **Bootstrap Creation:**
   - Creates an executable that knows how to start the bundled Python interpreter
   - Sets up Python paths to find bundled libraries

5. **Directory Structure Output:**
   ```
   dist/gprofiler                    # Main executable (bootstrap)
   dist/gprofiler/_internal/         # All bundled content
   ├── Python/                      # Python interpreter + stdlib
   ├── grpcio/                      # gRPC Python package
   ├── requests/                    # HTTP client package
   ├── gprofiler/                   # Your Python code
   │   └── resources/               # Native profilers
   └── ... (hundreds of other files)
   ```

**🎯 PyInstaller Output Analysis:**
- **dist/gprofiler**: ~5MB executable that bootstraps Python
- **dist/gprofiler/_internal/**: ~200MB of Python interpreter, libraries, and resources
- **Total size**: ~205MB for complete Python application

**❗ Critical Point:** The Python code is still interpreted! PyInstaller just makes it portable.

### Phase 6: StaticX - True Static Linking

**🤔 What Is the Problem PyInstaller Doesn't Solve?**  
The PyInstaller executable still depends on system libraries (libc, libssl, etc.). If you move it to a different Linux system, it might fail if those libraries are missing or incompatible.

**🛡️ What Is StaticX?**  
StaticX solves this by bundling ALL system libraries with the executable, making it truly self-contained.

```dockerfile
# Install StaticX and create truly static executable
RUN yum install -y patchelf upx && yum clean all

COPY ./scripts/list_needed_libs.sh ./scripts/list_needed_libs.sh

RUN set -e; \
    if [ "$STATICX" = "true" ]; then \
        LIBS=$(./scripts/list_needed_libs.sh) && \
        staticx $LIBS dist/gprofiler dist/gprofiler.output ; \
    else \
        mv dist/gprofiler dist/gprofiler.output ; \
    fi
```

**🔧 What StaticX Does (Step by Step):**

1. **Dependency Analysis:**
   ```bash
   ldd dist/gprofiler    # Lists dynamic library dependencies
   # Output:
   # linux-vdso.so.1
   # libc.so.6 => /lib/x86_64-linux-gnu/libc.so.6       # Core C library
   # libdl.so.2 => /lib/x86_64-linux-gnu/libdl.so.2     # Dynamic loading
   # libpthread.so.0 => /lib/x86_64-linux-gnu/libpthread.so.0  # Threading
   # libssl.so.1.1 => /lib/x86_64-linux-gnu/libssl.so.1.1      # HTTPS/TLS
   # libcrypto.so.1.1 => /lib/x86_64-linux-gnu/libcrypto.so.1.1 # Cryptography
   ```

2. **Additional Dependencies from Native Profilers:**
   ```bash
   # list_needed_libs.sh scans all resource binaries
   ldd gprofiler/resources/python/py-spy
   ldd gprofiler/resources/java/glibc/libasyncProfiler.so
   ldd gprofiler/resources/php/phpspy
   # Finds even more library dependencies
   ```

3. **Library Collection:**
   - Copies ALL system libraries the executable needs
   - Creates a self-extracting archive with libraries + executable

4. **Wrapper Creation:**
   - Creates a new executable that contains everything
   - This wrapper handles library extraction and loading at runtime

**🎯 StaticX Output:**
- **dist/gprofiler.output**: ~300MB truly self-contained executable
- **Contains**: PyInstaller bundle + ALL system libraries it needs
- **Dependencies**: Zero! Can run on any Linux system

### Phase 7: Final Packaging

```dockerfile
FROM scratch AS export-stage
COPY --from=build-stage /app/dist/gprofiler.output /gprofiler
```

**📦 Build Output:** Single self-contained executable file `gprofiler` (~300MB)

**🎯 What This Executable Contains:**
```
gprofiler (300MB executable)
├─ StaticX wrapper (self-extractor)
├─ System libraries (libc, libssl, etc.)
└─ PyInstaller bundle
    ├─ CPython interpreter (Python runtime)
    ├─ Python standard library
    ├─ Python packages (grpcio, requests, etc.)
    ├─ gProfiler Python source code
    └─ Native profiler resources (py-spy, async-profiler, etc.)
```

---

# � DEPLOYMENT PATH 1: Docker Container

## 📦 Container Image Creation

After the universal build process, the executable can be packaged into a Docker image:

```dockerfile
# Container runtime version
FROM ubuntu:20.04

# Install minimal runtime dependencies (if any)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy the self-contained executable
COPY --from=build-stage /app/dist/gprofiler.output /usr/local/bin/gprofiler

# Set up runtime environment
ENV GPROFILER_TOKEN=""
ENV GPROFILER_SERVICE=""
ENV GPROFILER_SERVER=""

# Set entrypoint
ENTRYPOINT ["/usr/local/bin/gprofiler"]
CMD ["--help"]
```

**📝 What This Creates:**
- **Docker Image**: ~400MB (Ubuntu 20.04 base + gProfiler executable)
- **Self-contained**: All profiling tools bundled inside the image
- **Portable**: Can run on any system with Docker/Podman/Kubernetes

## 🐋 Container Deployment Options

### Option 1: Direct Docker Run

```bash
# Pull the image from registry
docker pull granulate/gprofiler:latest

# Run as container
docker run -d \
  --name gprofiler \
  --pid=host \                    # Access host processes
  --privileged \                  # Required for profiling
  -v /proc:/host/proc:ro \        # Mount host /proc
  -v /sys:/host/sys:ro \          # Mount host /sys
  -e GPROFILER_TOKEN="your-token" \
  -e GPROFILER_SERVICE="your-service" \
  -e GPROFILER_SERVER="http://performance-studio:8000" \
  granulate/gprofiler:latest \
  --upload-results \
  --continuous \
  --duration 60
```

**🔧 Container Runtime Architecture:**
```
┌─────────────────────────────────────────────┐
│              Host Linux System              │
├─────────────────────────────────────────────┤
│  Docker Engine                              │
│  └─ gprofiler container                     │
│     ├─ Ubuntu 20.04 base                   │
│     ├─ /usr/local/bin/gprofiler (StaticX)  │
│     │  ├─ Extracts to /tmp/.staticx-123/   │
│     │  └─ Runs PyInstaller bundle          │
│     │     ├─ CPython interpreter           │
│     │     ├─ gProfiler Python code         │
│     │     └─ Native profilers              │
│     │                                      │
│     └─ Volume mounts:                      │
│        ├─ /host/proc → Host /proc          │
│        └─ /host/sys → Host /sys            │
└─────────────────────────────────────────────┘
```

### Option 2: Kubernetes DaemonSet

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: gprofiler
  namespace: monitoring
spec:
  selector:
    matchLabels:
      app: gprofiler
  template:
    metadata:
      labels:
        app: gprofiler
    spec:
      serviceAccount: gprofiler
      hostPID: true                    # Access host process namespace
      hostNetwork: true                # Access host network
      containers:
      - name: gprofiler
        image: granulate/gprofiler:latest
        securityContext:
          privileged: true             # Required for system profiling
          runAsUser: 0                 # Run as root
        env:
        - name: GPROFILER_TOKEN
          valueFrom:
            secretKeyRef:
              name: gprofiler-secret
              key: token
        - name: GPROFILER_SERVICE
          valueFrom:
            fieldRef:
              fieldPath: spec.nodeName  # Use node name as service name
        - name: GPROFILER_SERVER
          value: "http://performance-studio:8000"
        resources:
          requests:
            memory: "256Mi"
            cpu: "100m"
          limits:
            memory: "2Gi"              # Same as systemd limit
            cpu: "500m"                # 50% of 1 core (similar to systemd 5%)
        volumeMounts:
        - name: proc
          mountPath: /host/proc
          readOnly: true
        - name: sys
          mountPath: /host/sys
          readOnly: true
        - name: debug
          mountPath: /host/debug
          readOnly: true
      volumes:
      - name: proc
        hostPath:
          path: /proc
      - name: sys
        hostPath:
          path: /sys
      - name: debug
        hostPath:
          path: /sys/kernel/debug
```

**🏗️ Kubernetes Deployment Architecture:**
```
┌─────────────────────────────────────────────┐
│           Kubernetes Cluster                │
├─────────────────────────────────────────────┤
│  Node 1:                                    │
│  ├─ kubelet                                 │
│  ├─ gprofiler pod (DaemonSet)               │
│  │  └─ gprofiler container                  │
│  │     └─ profiles Node 1 processes        │
│  └─ Application pods                        │
│                                             │
│  Node 2:                                    │
│  ├─ kubelet                                 │
│  ├─ gprofiler pod (DaemonSet)               │
│  │  └─ gprofiler container                  │
│  │     └─ profiles Node 2 processes        │
│  └─ Application pods                        │
└─────────────────────────────────────────────┘
```

## � Container Runtime Behavior

### When Container Starts:

1. **Kubernetes/Docker** starts the container:
   ```bash
   docker run granulate/gprofiler:latest --upload-results --continuous
   ```

2. **Container entrypoint** executes:
   ```bash
   /usr/local/bin/gprofiler --upload-results --continuous
   ```

3. **StaticX wrapper** (same as .deb deployment):
   - Extracts to `/tmp/.staticx-ABC123/` inside container
   - Sets `LD_LIBRARY_PATH=/tmp/.staticx-ABC123/lib`
   - Executes PyInstaller bundle

4. **gProfiler Python code** runs:
   - Discovers processes in `/host/proc/` (mounted from host)
   - Profiles applications running on the host system
   - Uploads results to Performance Studio backend

## 📊 Container Benefits vs Limitations

### ✅ **Container Benefits:**

**Isolation:**
- ✅ **No conflicts** with host Python/libraries
- ✅ **Consistent environment** across different host systems
- ✅ **Easy rollbacks** - just change image tag

**Operations:**
- ✅ **Standard deployment** - same as any other containerized application
- ✅ **Resource limits** - Kubernetes native CPU/memory limits
- ✅ **Monitoring integration** - Works with Prometheus, logging, etc.
- ✅ **Multi-cluster deployment** - Deploy to hundreds of nodes easily

**Development:**
- ✅ **Version management** - Tagged images for different releases
- ✅ **CI/CD integration** - Automated builds and deployments
- ✅ **Testing isolation** - Test different versions without conflicts

### ❌ **Container Limitations:**

**Performance:**
- ❌ **Extra overhead** - Container runtime adds ~50-100MB memory
- ❌ **Network isolation** - May need hostNetwork=true for full profiling
- ❌ **File system layers** - Slightly slower file access through overlay

**Security:**
- ❌ **Privileged required** - Container needs privileged access for profiling
- ❌ **Host access needed** - Must mount /proc, /sys from host
- ❌ **Security scanning** - Container images need vulnerability scanning

**Complexity:**
- ❌ **Container knowledge** - Teams need Docker/Kubernetes expertise
- ❌ **Registry dependency** - Needs container registry for image distribution
- ❌ **Orchestration setup** - Requires Kubernetes/Docker Swarm setup

---

# 📦 DEPLOYMENT PATH 2: Debian Package (.deb)

## 🛠️ .deb Package Creation

After the universal build process, the executable is packaged into a Debian package:

```bash
#!/bin/bash
# Script: create_deb_package.sh

# Create package directory structure
mkdir -p debian-package/opt/gprofiler
mkdir -p debian-package/etc/systemd/system
mkdir -p debian-package/usr/share/doc/gprofiler
mkdir -p debian-package/DEBIAN

# Copy the self-contained executable
cp build/x86_64/gprofiler debian-package/opt/gprofiler/

# Create environment configuration template
cat > debian-package/opt/gprofiler/envs.sh.template << EOF
#!/bin/bash
# gProfiler Configuration
# Copy this file to envs.sh and fill in your values

export GPROFILER_TOKEN="your-token-here"
export GPROFILER_SERVICE="your-service-name" 
export GPROFILER_SERVER="http://performance-studio:8000"
EOF

# Copy systemd service file
cat > debian-package/etc/systemd/system/gprofiler.service << 'EOF'
[Unit]
Description=Intel Granulate gProfiler Agent
Documentation=https://profiler.granulate.io/
After=network.target

[Service]
PIDFile=/run/gprofiler.pid
User=root
WorkingDirectory=/opt/gprofiler/
ExecStart=/bin/bash -c "source /opt/gprofiler/envs.sh; /opt/gprofiler/gprofiler -u --token=$GPROFILER_TOKEN --service-name=$GPROFILER_SERVICE --server-host $GPROFILER_SERVER --dont-send-logs --server-upload-timeout 10 -c --disable-metrics-collection --java-safemode= -d 60 --java-no-version-check --nodejs-mode=attach-maps"
TimeoutStopSec=10

# Resource limits to prevent noisy neighbor issues
CPUAccounting=yes
MemoryAccounting=yes
CPUQuota=5%
MemoryLimit=2147483648
LimitCORE=0

# Reliability settings
KillMode=control-group
Restart=always
RuntimeMaxSec=1d
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Create package control file
cat > debian-package/DEBIAN/control << EOF
Package: gprofiler
Version: 1.53.2
Architecture: amd64
Maintainer: Intel Corporation <support@granulate.io>
Description: Intel Granulate gProfiler Agent
 Continuous profiling agent for production environments.
 Profiles Java, Python, Ruby, Go, .NET, PHP, and Node.js applications
 with minimal overhead and uploads results to Performance Studio.
 .
 This package includes:
  - Self-contained gProfiler executable (~300MB)
  - Systemd service configuration
  - Resource limits (5% CPU, 2GB memory)
Section: admin
Priority: optional
Depends: systemd (>= 220)
Homepage: https://profiler.granulate.io/
EOF

# Create postinstall script
cat > debian-package/DEBIAN/postinst << 'EOF'
#!/bin/bash
set -e

echo "Setting up gProfiler service..."

# Reload systemd to recognize new service
systemctl daemon-reload

# Enable service for auto-start on boot
systemctl enable gprofiler

# Create configuration if it doesn't exist
if [ ! -f /opt/gprofiler/envs.sh ]; then
    echo "Creating configuration template at /opt/gprofiler/envs.sh"
    echo "Please edit this file with your configuration before starting the service."
    cp /opt/gprofiler/envs.sh.template /opt/gprofiler/envs.sh
    chmod +x /opt/gprofiler/envs.sh
fi

echo ""
echo "gProfiler installation complete!"
echo ""
echo "Next steps:"
echo "1. Edit /opt/gprofiler/envs.sh with your configuration"
echo "2. Start the service: sudo systemctl start gprofiler"
echo "3. Check status: sudo systemctl status gprofiler"
echo ""
EOF

# Create preremove script
cat > debian-package/DEBIAN/prerm << 'EOF'
#!/bin/bash
set -e

# Stop service before removal
if systemctl is-active --quiet gprofiler; then
    echo "Stopping gProfiler service..."
    systemctl stop gprofiler
fi

# Disable service
if systemctl is-enabled --quiet gprofiler; then
    echo "Disabling gProfiler service..."
    systemctl disable gprofiler
fi
EOF

# Make scripts executable
chmod +x debian-package/DEBIAN/postinst
chmod +x debian-package/DEBIAN/prerm

# Create documentation
cat > debian-package/usr/share/doc/gprofiler/README.md << 'EOF'
# gProfiler

Intel Granulate gProfiler Agent for continuous profiling.

## Configuration

Edit `/opt/gprofiler/envs.sh` with your settings:

```bash
export GPROFILER_TOKEN="your-api-token"
export GPROFILER_SERVICE="your-service-name"
export GPROFILER_SERVER="http://performance-studio:8000"
```

## Service Management

```bash
# Start service
sudo systemctl start gprofiler

# Check status
sudo systemctl status gprofiler

# View logs
sudo journalctl -u gprofiler -f

# Stop service
sudo systemctl stop gprofiler
```

For more information, visit https://profiler.granulate.io/
EOF

# Build the .deb package
dpkg-deb --build debian-package gprofiler_1.53.2_amd64.deb

echo "Created gprofiler_1.53.2_amd64.deb"
ls -lh gprofiler_1.53.2_amd64.deb
```

**📦 Package Contents:**
```
gprofiler_1.53.2_amd64.deb                    # ~300MB package
├── opt/gprofiler/
│   ├── gprofiler                             # ~300MB self-contained executable
│   └── envs.sh.template                      # Configuration template
├── etc/systemd/system/
│   └── gprofiler.service                     # Systemd service file
├── usr/share/doc/gprofiler/
│   └── README.md                             # Documentation
└── DEBIAN/
    ├── control                               # Package metadata
    ├── postinst                              # Post-install script
    └── prerm                                 # Pre-removal script
```

## 📥 Package Distribution

### Method 1: Package Repository (Recommended)

```bash
# Add Intel repository
curl -fsSL https://packages.granulate.io/gpg | sudo gpg --dearmor -o /usr/share/keyrings/granulate-archive-keyring.gpg

echo "deb [signed-by=/usr/share/keyrings/granulate-archive-keyring.gpg] https://packages.granulate.io/deb stable main" | sudo tee /etc/apt/sources.list.d/granulate.list

# Update package list
sudo apt update

# Install gProfiler
sudo apt install gprofiler
```

### Method 2: Direct Package Install

```bash
# Download package
wget https://releases.granulate.io/gprofiler/gprofiler_1.53.2_amd64.deb

# Install package
sudo dpkg -i gprofiler_1.53.2_amd64.deb

# Install any missing dependencies
sudo apt-get install -f
```

## 🔧 Installation Process (.deb Package)

### What Happens During `apt install gprofiler`:

**Step 1: Package Download & Verification**
```bash
# APT downloads the package and verifies GPG signature
Downloading: gprofiler_1.53.2_amd64.deb (300MB)
Verifying GPG signature...
```

**Step 2: Dependency Check**
```bash
# APT ensures systemd is installed (required dependency)
Reading package lists... Done
Building dependency tree... Done
The following packages will be installed:
  gprofiler
```

**Step 3: Package Extraction**
```bash
# dpkg extracts files to their target locations
Unpacking gprofiler (1.53.2) ...
```

**Files Created:**
```
/opt/gprofiler/gprofiler               # ~300MB executable
/opt/gprofiler/envs.sh.template        # Configuration template  
/etc/systemd/system/gprofiler.service  # Systemd service
/usr/share/doc/gprofiler/README.md     # Documentation
```

**Step 4: Post-Install Script Execution**
```bash
# DEBIAN/postinst script runs automatically
Setting up gprofiler (1.53.2) ...
Setting up gProfiler service...

# Script performs these actions:
systemctl daemon-reload                # Reload systemd configs
systemctl enable gprofiler             # Enable auto-start on boot
cp envs.sh.template envs.sh            # Create config file
chmod +x /opt/gprofiler/envs.sh        # Make config executable

# User sees this output:
gProfiler installation complete!

Next steps:
1. Edit /opt/gprofiler/envs.sh with your configuration  
2. Start the service: sudo systemctl start gprofiler
3. Check status: sudo systemctl status gprofiler
```

## ⚙️ Manual Configuration & Startup

### Step 1: Configure Environment

```bash
# Edit configuration file
sudo nano /opt/gprofiler/envs.sh

# Add your settings:
export GPROFILER_TOKEN="my_token"
export GPROFILER_SERVICE="web-production"  
export GPROFILER_SERVER="http://localhost:8080"
```

### Step 2: Start Service

```bash
# Start the service
sudo systemctl start gprofiler

# Check if it started successfully
sudo systemctl status gprofiler
# ● gprofiler.service - Intel Granulate gProfiler Agent
#    Loaded: loaded (/etc/systemd/system/gprofiler.service; enabled)
#    Active: active (running) since Sat 2025-07-19 10:30:15 UTC; 5s ago
#  Main PID: 12345 (gprofiler)
#     Tasks: 3 (limit: 4915)
#    Memory: 145.2M (limit: 2.0G)
#       CPU: 1.2s (5% quota)
#    CGroup: /system.slice/gprofiler.service
#            └─12345 /opt/gprofiler/gprofiler -u --token=... --service-name=web-production
```

### Step 3: Verify Operation

```bash
# Check logs
sudo journalctl -u gprofiler -f

# Sample log output:
# Jul 19 10:30:16 host gprofiler[12345]: [2025-07-19 10:30:16,123] INFO: gprofiler: Starting continuous profiling
# Jul 19 10:30:17 host gprofiler[12345]: [2025-07-19 10:30:17,456] INFO: gprofiler: Found 3 Java processes to profile
# Jul 19 10:30:18 host gprofiler[12345]: [2025-07-19 10:30:18,789] INFO: gprofiler: Successfully uploaded profiling data

# Check resource usage
systemctl show gprofiler | grep -E "CPUQuota|MemoryCurrent|MemoryLimit"
# CPUQuotaPerSecUSec=50ms     (5% CPU limit)
# MemoryCurrent=152428544     (~145MB current usage)
# MemoryLimit=2147483648      (2GB limit)
```

## 🖥️ Host System Runtime Architecture

### System Integration After Installation:

```
┌─────────────────────────────────────────────────────────────┐
│                    Linux Host System                       │
│                   (Ubuntu/CentOS/RHEL)                     │
├─────────────────────────────────────────────────────────────┤
│  systemd (Process Manager)                                  │
│  ├─ gprofiler.service                                       │
│  │   ├─ WorkingDirectory=/opt/gprofiler/                    │
│  │   ├─ ExecStart=bash -c "source envs.sh; ./gprofiler"    │
│  │   ├─ User=root (required for system profiling)          │
│  │   ├─ CPUQuota=5% (max 50ms per second)                  │
│  │   ├─ MemoryLimit=2GB                                     │
│  │   ├─ Restart=always (auto-restart on crash)             │
│  │   └─ RuntimeMaxSec=1d (restart daily)                   │
│  │                                                          │
│  └─ Process Tree:                                           │
│      └─ bash (PID 12345)                                    │
│          └─ /opt/gprofiler/gprofiler (StaticX Wrapper)      │
│              ├─ Extracts to /tmp/.staticx-XYZ123/          │
│              │   ├─ libc.so, libssl.so, etc.              │
│              │   └─ gprofiler (PyInstaller Bundle)         │
│              │       ├─ python3.10 (CPython Interpreter)   │
│              │       ├─ grpcio, requests (Python packages) │
│              │       ├─ gprofiler/*.py (Python source)     │
│              │       └─ resources/ (Native profilers)      │
│              │           ├─ python/py-spy                   │
│              │           ├─ java/libasyncProfiler.so        │
│              │           ├─ ruby/rbspy                      │
│              │           └─ etc.                            │
│              │                                              │
│              └─ Child Processes (launched as needed):      │
│                  ├─ py-spy -p 1001 (profiles Python)       │
│                  ├─ asprof -d 60 -f profile.jfr 1002       │
│                  └─ perf record -F 11 -p 1003              │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│  Target Application Processes (being profiled)             │
│  ├─ java -jar myapp.jar (PID 1002)                         │
│  ├─ python3 manage.py runserver (PID 1001)                 │
│  ├─ ruby rails server (PID 1003)                           │
│  └─ node server.js (PID 1004)                              │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│  File System:                                              │
│  ├─ /opt/gprofiler/gprofiler (300MB executable)            │
│  ├─ /opt/gprofiler/envs.sh (configuration)                 │
│  ├─ /etc/systemd/system/gprofiler.service                  │
│  ├─ /tmp/.staticx-XYZ123/ (temporary extraction ~500MB)    │
│  └─ /var/log/journal/ (service logs)                       │
└─────────────────────────────────────────────────────────────┘
```

## 🔄 .deb Runtime Behavior (Detailed)

### When System Boots:

1. **systemd** reads service files:
   ```bash
   systemctl daemon-reload    # Reads /etc/systemd/system/gprofiler.service
   ```

2. **Service auto-start** (because it's enabled):
   ```bash
   systemctl start gprofiler  # Runs automatically on boot
   ```

### When Service Starts:

1. **systemd** executes the service:
   ```bash
   # Changes to working directory
   cd /opt/gprofiler/
   
   # Sources environment variables
   source /opt/gprofiler/envs.sh
   
   # Executes the main command
   /opt/gprofiler/gprofiler -u --token=$GPROFILER_TOKEN --service-name=$GPROFILER_SERVICE --server-host $GPROFILER_SERVER --dont-send-logs --server-upload-timeout 10 -c --disable-metrics-collection --java-safemode= -d 60 --java-no-version-check --nodejs-mode=attach-maps
   ```

2. **StaticX wrapper** (same behavior as container):
   ```bash
   # Creates unique temporary directory
   mkdir /tmp/.staticx-ABC123/
   
   # Extracts all bundled libraries
   tar -xf /embedded/libraries.tar.gz -C /tmp/.staticx-ABC123/
   
   # Sets library path
   export LD_LIBRARY_PATH=/tmp/.staticx-ABC123/lib
   
   # Executes original PyInstaller executable
   exec /tmp/.staticx-ABC123/gprofiler "$@"
   ```

3. **PyInstaller bootstrap**:
   ```python
   # Initializes Python interpreter
   Py_Initialize()
   
   # Sets Python path to bundled modules
   sys.path = ['/tmp/.staticx-ABC123/_internal', ...]
   
   # Imports and runs main module  
   import gprofiler.__main__
   gprofiler.__main__.main()
   ```

4. **gProfiler Python application**:
   ```python
   # Loads configuration from environment
   token = os.environ['GPROFILER_TOKEN']
   service = os.environ['GPROFILER_SERVICE']  
   server = os.environ['GPROFILER_SERVER']
   
   # Discovers running processes
   processes = discover_processes()
   
   # Launches appropriate profilers
   for process in processes:
       if process.is_java():
           launch_async_profiler(process.pid)
       elif process.is_python():
           launch_py_spy(process.pid)
       # etc.
   
   # Collects and uploads results
   while True:
       results = collect_profiling_data()
       upload_to_server(server, results)
       time.sleep(60)
   ```

### Process Lifecycle:

```
Boot → systemd → gprofiler.service → StaticX → PyInstaller → CPython → gProfiler Python → Native Profilers → Target Apps
  ↓       ↓            ↓               ↓           ↓           ↓             ↓                  ↓            ↓
 Auto   Reads      Starts with      Extracts    Starts      Interprets    Executes       Launch        Profile
start  service    resource        libraries   bundled     Python        profiling      profilers     running
       config     limits          to /tmp     Python      source        logic          (py-spy,      processes
                                              runtime     code                         async-prof)
```

## 📊 .deb Benefits vs Limitations

### ✅ **Package Benefits:**

**Native Integration:**
- ✅ **Native systemd** - Full integration with system service management
- ✅ **No container overhead** - Runs directly on host with minimal overhead
- ✅ **System-level access** - Direct access to /proc, /sys, kernel features
- ✅ **Boot integration** - Starts automatically with system boot

**Operations:**
- ✅ **Familiar tools** - Use systemctl, journalctl, standard Linux tools
- ✅ **Resource limits** - Native systemd CPU/memory limits
- ✅ **Easy debugging** - Direct process access, standard debugging tools
- ✅ **Log integration** - Works with rsyslog, systemd journal, logrotate

**Distribution:**
- ✅ **Package management** - Standard apt/yum package management
- ✅ **Dependency handling** - Automatic dependency resolution
- ✅ **Version control** - Standard package versioning and updates
- ✅ **Signing/verification** - GPG signed packages for security

### ❌ **Package Limitations:**

**System Dependencies:**
- ❌ **Host compatibility** - Must be compatible with host Linux distribution
- ❌ **Package conflicts** - Potential conflicts with other installed packages
- ❌ **Permission requirements** - Needs root access for installation

**Deployment Complexity:**
- ❌ **Manual installation** - Requires manual setup on each system
- ❌ **Configuration management** - Need tools like Ansible/Puppet for scale
- ❌ **Update management** - Manual or scripted updates required

**Portability:**
- ❌ **Distribution specific** - Different packages for Ubuntu/CentOS/etc.
- ❌ **Architecture specific** - Separate packages for x86_64/arm64
- ❌ **Host modifications** - Installs files directly on host system

---

# 🤝 Comparison: Docker vs .deb Deployment

## 📊 Side-by-Side Comparison

| Aspect | 🐋 Docker Container | 📦 .deb Package |
|--------|-------------------|-----------------|
| **Installation** | `docker pull granulate/gprofiler` | `apt install gprofiler` |
| **Runtime** | Inside container namespace | Direct on host system |
| **Resource Usage** | +50-100MB container overhead | Native host process |
| **System Access** | Requires privileged mode + volume mounts | Native system access |
| **Updates** | `docker pull` new image | `apt update && apt upgrade` |
| **Rollback** | Change image tag | Package downgrade |
| **Configuration** | Environment variables | Config files + env vars |
| **Logs** | `docker logs` or `kubectl logs` | `journalctl -u gprofiler` |
| **Monitoring** | Kubernetes metrics | systemd + native monitoring |
| **Scale Deployment** | Excellent (K8s DaemonSet) | Manual/scripted (Ansible/Puppet) |
| **Development** | Excellent (CI/CD pipelines) | Traditional (build servers) |
| **Debugging** | Container context required | Native debugging tools |

## 🎯 When to Choose What

### Choose **Docker Container** When:
- ✅ **Kubernetes environment** - Already using container orchestration
- ✅ **CI/CD pipelines** - Automated deployments and rollbacks needed  
- ✅ **Multi-cluster** - Deploying across many different environments
- ✅ **Immutable infrastructure** - Treat servers as cattle, not pets
- ✅ **Development teams** - Teams familiar with containerization

### Choose **.deb Package** When:
- ✅ **Traditional infrastructure** - VM-based deployments
- ✅ **System integration** - Need tight integration with host OS
- ✅ **Performance critical** - Minimal overhead requirements
- ✅ **Operations teams** - Teams familiar with traditional system administration
- ✅ **Compliance requirements** - Strict control over what's installed

---

# 🏃 Runtime Behavior (Universal)

Regardless of deployment method, the actual runtime behavior is identical because both approaches run the same StaticX executable.

## 🔄 Universal Runtime Flow

### When gProfiler Process Starts:

```
1. Process Execution
   ├─ Docker: docker run granulate/gprofiler
   └─ .deb: systemctl start gprofiler
                    ↓
2. StaticX Wrapper Execution
   ├─ Creates: /tmp/.staticx-ABC123/
   ├─ Extracts: System libraries (libc, libssl, etc.)  
   ├─ Sets: LD_LIBRARY_PATH=/tmp/.staticx-ABC123/lib
   └─ Executes: /tmp/.staticx-ABC123/gprofiler
                    ↓
3. PyInstaller Bootstrap  
   ├─ Initializes: Bundled CPython interpreter
   ├─ Sets: Python paths to bundled libraries
   ├─ Imports: gprofiler package  
   └─ Calls: gprofiler.__main__.main()
                    ↓
4. gProfiler Python Application
   ├─ Loads: Configuration (env vars/config files)
   ├─ Connects: Performance Studio backend (gRPC/HTTP)
   ├─ Discovers: Running processes (/proc scanning)
   └─ Launches: Appropriate profilers for each language
                    ↓
5. Native Profiler Execution (Python orchestrates but doesn't profile directly)
   ├─ Java: async-profiler → libasyncProfiler.so (Python calls via JNI/subprocess)
   ├─ Python: py-spy → Rust binary (Python subprocess.run(['/path/to/py-spy', ...]))
   ├─ Ruby: rbspy → Rust binary (Python subprocess.run(['/path/to/rbspy', ...]))
   ├─ Go: burn → Go binary (Python subprocess.run(['/path/to/burn', ...]))
   ├─ .NET: dotnet-trace → .NET binary (Python subprocess.run(['/path/to/dotnet-trace', ...]))
   ├─ PHP: phpspy → C binary (Python subprocess.run(['/path/to/phpspy', ...]))
   └─ System: perf → C binary (Python subprocess.run(['perf', 'record', ...]))

   🎯 Key Point: The Python code COORDINATES but doesn't do the actual profiling.
   Each profiler is a separate executable that runs as a child process.
            What Python Does:
                Scans proc to find running processes
                Identifies language by command line: java -jar, python app.py, etc.
                Launches appropriate profiler: subprocess.run(['/path/to/py-spy', 'record', '--pid', '1234'])
                Waits for profiler to finish and write results to file
                Reads the result files and uploads to backend
                Repeats every 60 seconds

            What Python Does NOT Do:
                ❌ Parse bytecode directly
                ❌ Attach to process memory
                ❌ Read stack traces itself
                ❌ Perform CPU sampling
                ❌ Any of the actual profiling work
                    ↓
6. Data Collection & Upload
   ├─ Collects: Stack traces from each profiler
   ├─ Processes: Aggregates and analyzes data
   ├─ Formats: Converts to flame graphs/call trees
   └─ Uploads: Sends to Performance Studio backend
                    ↓
7. Continuous Loop
   └─ Repeats every 60 seconds (configurable)
```

## 🎛️ Runtime Configuration

### Universal Environment Variables:

```bash
# Authentication & Endpoint
GPROFILER_TOKEN="my_token"
GPROFILER_SERVICE="web-production"  
GPROFILER_SERVER="http://localhost:8080"

# Profiling Behavior  
GPROFILER_DURATION="60"                    # Profiling duration in seconds
GPROFILER_FREQUENCY="11"                   # Profiling frequency (Hz)
GPROFILER_PROFILING_MODE="cpu"             # cpu, alloc, lock, etc.
GPROFILER_OUTPUT_DIR="/tmp/profiles"       # Local output directory (optional)

# Feature Flags
GPROFILER_DISABLE_JAVA_PROFILING="false"  # Disable Java profiling
GPROFILER_DISABLE_PYTHON_PROFILING="false" # Disable Python profiling  
GPROFILER_JAVA_SAFEMODE="true"            # Use safe mode for Java
GPROFILER_NODEJS_MODE="attach-maps"       # Node.js profiling mode
```

### Command Line Arguments (Same for Both):

```bash
# Common runtime arguments
/opt/gprofiler/gprofiler \
  -u \                                     # Upload results  
  --token=$GPROFILER_TOKEN \               # API token
  --service-name=$GPROFILER_SERVICE \      # Service identifier
  --server-host $GPROFILER_SERVER \        # Backend URL
  --dont-send-logs \                       # Don't send logs to server  
  --server-upload-timeout 10 \             # Upload timeout
  -c \                                     # Continuous mode
  --disable-metrics-collection \           # Disable system metrics
  --java-safemode= \                       # Java safe mode (empty=disabled)
  -d 60 \                                  # Duration per profiling session
  --java-no-version-check \               # Skip Java version validation
  --nodejs-mode=attach-maps                # Node.js profiling strategy
```

## 📈 Resource Usage Analysis

### Memory Usage Breakdown:

```
Total Memory Usage: ~500-1400MB
├─ StaticX Libraries: ~100MB 
│  ├─ libc, libssl, libcrypto: ~50MB
│  ├─ Python shared libraries: ~30MB  
│  └─ Native profiler dependencies: ~20MB
├─ PyInstaller Bundle: ~200MB
│  ├─ CPython interpreter: ~50MB
│  ├─ Python standard library: ~80MB
│  ├─ Python packages (grpcio, etc.): ~50MB
│  └─ gProfiler resources: ~20MB  
└─ Runtime Memory: ~200-1100MB (varies with grpcio version)
   ├─ Python process heap: ~100-200MB
   ├─ gRPC connections (leak potential): ~100-900MB  
   ├─ Profiling data buffers: ~50MB
   └─ Native profiler processes: ~50MB
```

**🎯 Memory Leak Context:**
- **grpcio 1.43.0**: ~600MB stable memory usage
- **grpcio 1.71.0**: ~1400MB due to Cython memory leaks  
- **grpcio 1.71.2+**: ~800MB with Cython 3.1.1 fixes

### CPU Usage Pattern:

```
Typical CPU Usage: 1-3% of one core
├─ Python interpreter: ~0.5-1%
├─ Native profilers: ~0.5-1% 
├─ Data processing: ~0.2-0.5%
└─ Network I/O: ~0.1-0.3%

Profiling Spike Pattern (every 60 seconds):
├─ Profiling collection: ~5-10% for 5-10 seconds
├─ Data processing: ~2-5% for 10-15 seconds  
├─ Network upload: ~1-2% for 5-10 seconds
└─ Idle period: ~0.5-1% for remaining time
```

**🛡️ Resource Limits (Both Deployments):**
- **systemd (.deb)**: CPUQuota=5%, MemoryLimit=2GB
- **Kubernetes (container)**: cpu: 500m, memory: 2Gi
- **Protection**: Prevents "noisy neighbor" issues

## 🔍 Profiling Process Discovery

### How gProfiler Finds Target Processes:

```python
# Simplified process discovery logic
def discover_processes():
    processes = []
    
    # Scan /proc for all running processes
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
            
        try:
            # Read process command line
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                cmdline = f.read().decode().replace('\x00', ' ')
            
            # Identify process type by command line patterns
            if 'java' in cmdline and '-jar' in cmdline:
                processes.append(JavaProcess(pid, cmdline))
            elif 'python' in cmdline:
                processes.append(PythonProcess(pid, cmdline))  
            elif 'ruby' in cmdline:
                processes.append(RubyProcess(pid, cmdline))
            elif 'node' in cmdline:
                processes.append(NodeProcess(pid, cmdline))
            elif 'dotnet' in cmdline:
                processes.append(DotNetProcess(pid, cmdline))
                
        except (OSError, IOError):
            # Process disappeared or no permission
            continue
            
    return processes

# Language-specific profiler execution - THIS IS THE KEY PART!
def profile_java_process(pid):
    # Path to bundled async-profiler executable
    asprof_path = get_resource_path('java/glibc/asprof')
    libap_path = get_resource_path('java/glibc/libasyncProfiler.so')
    
    # Python LAUNCHES async-profiler as separate process
    result = subprocess.run([
        asprof_path,              # Execute the native binary
        '-d', '60',               # Duration: 60 seconds
        '-f', f'/tmp/profile_{pid}.jfr',  # Output file
        '-i', '11ms',             # Sample interval
        '-e', 'cpu',              # Event type (CPU sampling)
        '--fdtransfer',           # Use file descriptor transfer
        str(pid)                  # Target Java process PID
    ], capture_output=True, text=True)
    
    # Python processes the results but doesn't do the profiling
    if result.returncode == 0:
        return parse_jfr_file(f'/tmp/profile_{pid}.jfr')
    else:
        log.error(f"async-profiler failed: {result.stderr}")

def profile_python_process(pid):
    # Path to bundled py-spy executable (Rust binary)
    pyspy_path = get_resource_path('python/py-spy')
    
    # Python LAUNCHES py-spy as separate process
    result = subprocess.run([
        pyspy_path,
        'record',                 # py-spy subcommand
        '--pid', str(pid),        # Target Python process PID  
        '--duration', '60',       # Profile for 60 seconds
        '--rate', '11',           # Sample rate (11 Hz)
        '--format', 'speedscope', # Output format
        '--output', f'/tmp/python_profile_{pid}.json'
    ], capture_output=True, text=True)
    
    # Python processes the results
    if result.returncode == 0:
        return parse_speedscope_file(f'/tmp/python_profile_{pid}.json')

def profile_ruby_process(pid):
    # Path to bundled rbspy executable (Rust binary)
    rbspy_path = get_resource_path('ruby/rbspy')
    
    # Python LAUNCHES rbspy as separate process
    result = subprocess.run([
        rbspy_path,
        'record',                 # rbspy subcommand
        '--pid', str(pid),        # Target Ruby process PID
        '--duration', '60',       # Profile for 60 seconds  
        '--rate', '11',           # Sample rate
        '--format', 'speedscope',
        '--file', f'/tmp/ruby_profile_{pid}.json'
    ], capture_output=True, text=True)
    
    return parse_speedscope_file(f'/tmp/ruby_profile_{pid}.json')

# The main orchestration loop
def main_profiling_loop():
    while True:
        # 1. Discover what processes are running
        processes = discover_processes()
        
        # 2. Launch appropriate profilers (as separate processes!)
        profiling_tasks = []
        for process in processes:
            if isinstance(process, JavaProcess):
                task = threading.Thread(target=profile_java_process, args=(process.pid,))
            elif isinstance(process, PythonProcess):
                task = threading.Thread(target=profile_python_process, args=(process.pid,))
            elif isinstance(process, RubyProcess):
                task = threading.Thread(target=profile_ruby_process, args=(process.pid,))
            # ... etc for other languages
            
            profiling_tasks.append(task)
            task.start()
        
        # 3. Wait for all profiling to complete
        for task in profiling_tasks:
            task.join()
        
        # 4. Collect results and upload to Performance Studio
        results = collect_all_profiling_results()
        upload_to_backend(results)
        
        # 5. Wait before next profiling cycle
        time.sleep(60)
```

**🎯 Key Architecture Points:**

1. **Python is the Coordinator**: The gProfiler Python code doesn't do any actual profiling itself
2. **Native Profilers Do the Work**: Each language has a specialized native profiler that's much faster than Python could be
3. **Process Spawning**: Python uses `subprocess.run()` to launch these native binaries
4. **Resource Bundling**: All these native binaries are bundled into the PyInstaller package 
5. **Result Processing**: Python collects the output files and sends them to the backend

**🔧 Why This Architecture?**

- **Performance**: Native profilers (C/Rust/Go) are much faster than Python for CPU-intensive profiling
- **Language Expertise**: Each profiler is written by experts in that specific language/runtime
- **Safety**: External profilers are safer - they can crash without taking down the coordinator
- **Modularity**: Easy to update individual profilers without changing the orchestration code
```

### Multi-Process Coordination:

```
gProfiler Main Process (Python)
├─ Process Discovery Thread
│  ├─ Scans /proc every 30 seconds
│  ├─ Detects new/terminated processes  
│  └─ Updates profiling targets
├─ Java Profiling Thread
│  ├─ async-profiler (PID 1001) → Java App (PID 5001)
│  └─ async-profiler (PID 1002) → Java App (PID 5002)  
├─ Python Profiling Thread  
│  ├─ py-spy (PID 1003) → Python App (PID 5003)
│  └─ py-spy (PID 1004) → Python App (PID 5004)
├─ Ruby Profiling Thread
│  └─ rbspy (PID 1005) → Ruby App (PID 5005)
├─ Data Collection Thread
│  ├─ Collects outputs from all profilers
│  ├─ Aggregates into unified format
│  └─ Uploads to Performance Studio  
└─ Heartbeat Thread (if enabled)
   └─ Sends status updates to backend
```

## 🎯 Key Takeaways

### Universal Architecture Principles:

1. **Language Agnostic**: gProfiler coordinates multiple language-specific profilers
2. **Non-Intrusive**: Profilers attach externally, don't modify target applications
3. **Resource Bounded**: Hard limits prevent interference with production workloads
4. **Self-Contained**: Zero dependencies on target system (except Linux kernel)
5. **Production Safe**: Designed for always-on profiling in production environments

### Technical Summary:

```
User Installs → Deployment Method → StaticX Wrapper → PyInstaller Bundle → CPython Interpreter → gProfiler Python Code → Native Profilers → Target Applications

Docker:   K8s/Docker → Container → StaticX → PyInstaller → Python → gProfiler → Profilers → Apps
.deb:     apt install → systemd → StaticX → PyInstaller → Python → gProfiler → Profilers → Apps
                                     ↑
                            Same from this point onwards
```

**Critical Understanding**: The Python code is **NEVER compiled** to machine code. It remains interpreted Python code running on a bundled CPython interpreter with all necessary system libraries included for portability and self-containment.

### grpcio Memory Leak Resolution:

The memory leak issues occur in the **Python runtime layer**:
- **grpcio** (Python package) has memory leaks in versions 1.43 < grpcio < 1.71.2
- **Cython** version inconsistencies in grpcio wheel builds cause AsyncIO memory leaks  
- **Solution**: Pin to grpcio==1.43.0 or upgrade to grpcio>=1.71.2 with Cython 3.1.1
- **Impact**: Affects both Docker and .deb deployments equally since they run identical code

The bundling process (PyInstaller + StaticX) doesn't affect these memory leaks - they're runtime issues in the gRPC library itself that need to be addressed at the dependency level.

---

# 🚀 Quick Local Development Setup

For modifying and testing the Python orchestrator code in gProfiler:

## ⚡ Two Development Approaches

### Option 1: Build Executable (Recommended - What Actually Works)

```bash
# Clone the repo
git clone https://github.com/Granulate/gprofiler.git
cd gprofiler

# Build executable (includes all dependencies)
./scripts/build_x86_64_executable.sh --fast

# Test your changes
sudo ./build/x86_64/gprofiler -o /tmp/results -d 30
```

### Option 2: Pure Python Development (If Dependencies Work)

```bash
# Clone and setup virtual environment
git clone https://github.com/Granulate/gprofiler.git
cd gprofiler
python3 -m venv venv
source venv/bin/activate

# Try installing dependencies (may fail on some systems)
pip install -r requirements.txt

# If successful, run directly
python -m gprofiler \
  --no-upload \
  --dry-run \
  --log-level=DEBUG \
  --output-dir=./test_profiles
```

## 🔧 Development Workflow (Build-Based)

```bash
# 1. Make your Python code changes
vim gprofiler/profilers/java.py
vim gprofiler/utils/processes.py

# 2. Fast rebuild (reuses containers, much faster)
./scripts/build_x86_64_executable.sh --fast

# 3. Test locally
sudo ./build/x86_64/gprofiler \
  --no-upload \
  -o /tmp/test_profiles \
  -d 30 \
  --log-level=DEBUG

# 4. Check results
ls -la /tmp/test_profiles/
```

## 🎯 Fast Iteration Tips

**Quick testing without full profiling:**
```bash
# Just test process discovery
sudo ./build/x86_64/gprofiler \
  --no-upload \
  --dry-run \
  -o /tmp/test \
  --log-level=DEBUG

# Test specific profiler only
sudo ./build/x86_64/gprofiler \
  --no-upload \
  -o /tmp/test \
  -d 10 \
  --disable-java-profiling \
  --disable-ruby-profiling  # Enable only Python profiling
```

**Add debug prints to your code:**
```python
# In any profiler file, add debug logging
import logging
logger = logging.getLogger(__name__)

def your_function():
    logger.debug(f"Debug: Found process {pid} with cmdline {cmdline}")
    # Your logic here
```

## 🔧 Key Files to Modify

```bash
gprofiler/
├── __main__.py              # CLI entry point
├── profilers/
│   ├── registry.py         # Profiler discovery & coordination
│   ├── java.py             # Java orchestration logic
│   ├── python.py           # Python orchestration logic
│   └── base.py             # Base profiler class
└── utils/
    └── processes.py         # Process discovery logic
```

## 🧪 Testing Your Changes

**The build approach is more reliable because:**
- ✅ Handles all Python dependencies automatically
- ✅ Includes native profilers (py-spy, async-profiler, etc.)
- ✅ Uses Docker to resolve complex dependency issues
- ✅ Matches production environment exactly

**Key development files to modify:**
```bash
gprofiler/
├── __main__.py              # CLI entry point
├── profilers/
│   ├── registry.py         # Main orchestration logic
│   ├── java.py             # Java profiling coordination
│   ├── python.py           # Python profiling coordination
│   └── base.py             # Base profiler class
└── utils/
    └── processes.py         # Process discovery logic
```

**Common modification workflow:**
```bash
# 1. Edit Python orchestrator code
vim gprofiler/profilers/registry.py

# 2. Rebuild (fast with --fast flag)
./scripts/build_x86_64_executable.sh --fast

# 3. Test specific functionality
sudo ./build/x86_64/gprofiler \
  --no-upload \
  -o /tmp/debug \
  --single-process=1234 \     # Test specific PID
  -d 15 \                     # Short duration
  --log-level=DEBUG

# 4. Check logs and output
cat /tmp/debug/*.json        # Profile results
journalctl | tail -50        # System logs
```

That's the practical approach that actually works! The build system handles all the complexity for you.
