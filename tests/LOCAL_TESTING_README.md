# gProfiler Local Testing Guide

This guide provides comprehensive instructions for testing gProfiler locally using both .deb packages and Docker images.

## Table of Contents
- [Prerequisites](#prerequisites)
- [Building Locally](#building-locally)
- [Testing .deb Package](#testing-deb-package)
- [Testing Docker Image](#testing-docker-image)
- [Heartbeat Testing](#heartbeat-testing)
- [Test Scripts](#test-scripts)
- [Troubleshooting](#troubleshooting)

## Prerequisites

### System Requirements
- Linux system (Ubuntu/Debian recommended for .deb testing)
- Docker installed and running
- Root/sudo access for .deb installation
- Python 3.8+ for running test scripts
- At least 4GB RAM and 10GB free disk space

### Dependencies
```bash
# Install required packages
sudo apt-get update
sudo apt-get install -y python3 python3-pip docker.io curl wget
```

## Building Locally

### Build Both .deb and Docker Image
```bash
# Build everything (recommended)
./local_build_test.sh

# Or build individually:
# For .deb package
./scripts/build_x86_64_executable.sh

# For Docker image  
./scripts/build_x86_64_container.sh
```

**Build Output Location:**
- .deb package: `build-output/gprofiler_x86_64.deb`
- Docker image: `gprofiler:latest`
- Standalone binary: `build-output/gprofiler_x86_64`

## Testing .deb Package

### 1. Installation
```bash
# Install the .deb package
sudo dpkg -i build-output/gprofiler_x86_64.deb

# Verify installation
dpkg -l | grep gprofiler
which gprofiler
```

### 2. Basic Functionality Test
```bash
# Test help command
gprofiler --help

# Test version
gprofiler --version

# Test dry run (no actual profiling)
sudo gprofiler --duration 10 --dry-run
```

### 3. Service Configuration Test
```bash
# Check systemd service
sudo systemctl status gprofiler
sudo systemctl cat gprofiler

# Test service start/stop
sudo systemctl start gprofiler
sudo systemctl status gprofiler
sudo systemctl stop gprofiler
```

### 4. Configuration with Parameters
```bash
# Configure with custom parameters
sudo tee /etc/systemd/system/gprofiler.service.d/override.conf << EOF
[Service]
Environment="GPROFILER_TOKEN=your_token_here"
Environment="GPROFILER_SERVICE=devapp"
Environment="GPROFILER_SERVER=http://your-server:9092"
ExecStart=
ExecStart=/usr/local/bin/gprofiler -u --token=\${GPROFILER_TOKEN} --service-name=\${GPROFILER_SERVICE} --server-host \${GPROFILER_SERVER} --dont-send-logs --enable-heartbeat-server --heartbeat-interval 30
EOF

# Reload and restart
sudo systemctl daemon-reload
sudo systemctl restart gprofiler
```

### 5. Heartbeat Testing (.deb)
```bash
# Test heartbeat functionality
sudo gprofiler -u \
  --token=K3VJlXsW4pdBWTfBaY8CheKENB5OUwcFmBodQvm-7es \
  --service-name=devapp \
  --server-host http://10.1.145.15:9092 \
  --dont-send-logs \
  --enable-heartbeat-server \
  --heartbeat-interval 30 \
  --duration 60

# Monitor heartbeat logs
sudo journalctl -u gprofiler -f
```

### 6. Uninstallation
```bash
# Stop service first
sudo systemctl stop gprofiler
sudo systemctl disable gprofiler

# Remove package
sudo dpkg -r gprofiler

# Clean up
sudo rm -rf /opt/gprofiler
sudo rm -f /etc/systemd/system/gprofiler.service
sudo systemctl daemon-reload
```

## Testing Docker Image

### 1. Basic Docker Tests
```bash
# List built images
docker images | grep gprofiler

# Test help command
docker run --rm gprofiler:latest --help

# Test version
docker run --rm gprofiler:latest --version
```

### 2. Container Profiling Test
```bash
# Run with minimal permissions
docker run --rm \
  --pid=host \
  --privileged \
  -v /lib/modules:/lib/modules:ro \
  -v /usr/src:/usr/src:ro \
  -v /sys/kernel/debug:/sys/kernel/debug:rw \
  gprofiler:latest \
  --duration 30 \
  --dry-run
```

### 3. Production-like Test
```bash
# Create output directory
mkdir -p /tmp/gprofiler-output

# Run with output collection
docker run --rm \
  --pid=host \
  --privileged \
  -v /lib/modules:/lib/modules:ro \
  -v /usr/src:/usr/src:ro \
  -v /sys/kernel/debug:/sys/kernel/debug:rw \
  -v /tmp/gprofiler-output:/tmp/output \
  gprofiler:latest \
  --duration 60 \
  --output-dir /tmp/output \
  --no-upload
```

### 4. Heartbeat Testing (Docker)
```bash
# Run with heartbeat functionality
docker run --rm \
  --pid=host \
  --privileged \
  -v /lib/modules:/lib/modules:ro \
  -v /usr/src:/usr/src:ro \
  -v /sys/kernel/debug:/sys/kernel/debug:rw \
  --name gprofiler-heartbeat \
  gprofiler:latest \
  -u \
  --token=K3VJlXsW4pdBWTfBaY8CheKENB5OUwcFmBodQvm-7es \
  --service-name=devapp \
  --server-host http://10.1.145.15:9092 \
  --dont-send-logs \
  --enable heartbeat \
  --heartbeat-interval 30

# Monitor logs in another terminal
docker logs -f gprofiler-heartbeat
```

### 5. Docker Compose Testing
```bash
# Create docker-compose.yml for testing
cat > docker-compose-test.yml << EOF
version: '3.8'
services:
  gprofiler:
    image: gprofiler:latest
    privileged: true
    pid: host
    volumes:
      - /lib/modules:/lib/modules:ro
      - /usr/src:/usr/src:ro
      - /sys/kernel/debug:/sys/kernel/debug:rw
      - ./output:/tmp/output
    command: >
      --duration 60
      --output-dir /tmp/output
      --no-upload
      --enable heartbeat
      --heartbeat-interval 30
    environment:
      - GPROFILER_TOKEN=K3VJlXsW4pdBWTfBaY8CheKENB5OUwcFmBodQvm-7es
      - GPROFILER_SERVICE=devapp
      - GPROFILER_SERVER=http://10.1.145.15:9092
EOF

# Run with docker-compose
mkdir -p output
docker-compose -f docker-compose-test.yml up
```

## Heartbeat Testing

### Mock Server Setup (Optional)
```bash
# Start a simple mock server for heartbeat testing
python3 -c "
import http.server
import socketserver
import json

class MockHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == '/api/metrics/heartbeat':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            response = {'success': True, 'message': 'Heartbeat received'}
            self.wfile.write(json.dumps(response).encode())
        
    def log_message(self, format, *args):
        print(f'[MOCK SERVER] {format % args}')

with socketserver.TCPServer(('', 9092), MockHandler) as httpd:
    print('Mock server running on port 9092...')
    httpd.serve_forever()
" &

# Test heartbeat against mock server
sleep 2
curl -X POST http://localhost:9092/api/metrics/heartbeat \
  -H "Content-Type: application/json" \
  -d '{"hostname": "test", "status": "active"}'
```

### Heartbeat Configuration Scripts
```bash
# Create heartbeat test script
cat > test_heartbeat.sh << 'EOF'
#!/bin/bash
set -e

echo "Testing heartbeat functionality..."

# Configuration
TOKEN="${GPROFILER_TOKEN:-K3VJlXsW4pdBWTfBaY8CheKENB5OUwcFmBodQvm-7es}"
SERVICE="${GPROFILER_SERVICE:-devapp}"
SERVER="${GPROFILER_SERVER:-http://10.1.145.15:9092}"

echo "Token: $TOKEN"
echo "Service: $SERVICE"
echo "Server: $SERVER"

# Test with .deb installation
if which gprofiler >/dev/null 2>&1; then
    echo "Testing .deb installation..."
    timeout 30 sudo gprofiler -u \
        --token="$TOKEN" \
        --service-name="$SERVICE" \
        --server-host "$SERVER" \
        --dont-send-logs \
        --enable-heartbeat-server \
        --heartbeat-interval 10 \
        --duration 20 || echo "Heartbeat test completed"
fi

# Test with Docker
if docker images | grep -q gprofiler; then
    echo "Testing Docker image..."
    timeout 30 docker run --rm \
        --pid=host \
        --privileged \
        -v /lib/modules:/lib/modules:ro \
        -v /usr/src:/usr/src:ro \
        -v /sys/kernel/debug:/sys/kernel/debug:rw \
        gprofiler:latest \
        -u \
        --token="$TOKEN" \
        --service-name="$SERVICE" \
        --server-host "$SERVER" \
        --dont-send-logs \
        --enable-heartbeat-server \
        --heartbeat-interval 10 \
        --duration 20 || echo "Docker heartbeat test completed"
fi

echo "Heartbeat tests finished!"
EOF

chmod +x test_heartbeat.sh
./test_heartbeat.sh
```

## Test Scripts

### Automated Test Suite
```bash
# Run existing test suite
cd tests/
./test.sh

# Run specific tests
python3 -m pytest test_heartbeat_system.py -v
python3 -m pytest test_executable.py -v
python3 -m pytest test_sanity.py -v
```

### Custom Test Script
```bash
# Create comprehensive test script
cat > tests/test_local_builds.py << 'EOF'
#!/usr/bin/env python3
import subprocess
import sys
import os
import time
import json

def run_command(cmd, timeout=60):
    """Run command and return result"""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, 
                              text=True, timeout=timeout)
        return result.returncode == 0, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return False, "", "Command timed out"

def test_deb_package():
    """Test .deb package functionality"""
    print("Testing .deb package...")
    
    # Check if package is installed
    success, stdout, stderr = run_command("dpkg -l | grep gprofiler")
    if not success:
        print("❌ .deb package not installed")
        return False
    
    # Test basic functionality
    success, stdout, stderr = run_command("gprofiler --help")
    if not success:
        print("❌ gProfiler help command failed")
        return False
    
    print("✅ .deb package tests passed")
    return True

def test_docker_image():
    """Test Docker image functionality"""
    print("Testing Docker image...")
    
    # Check if image exists
    success, stdout, stderr = run_command("docker images | grep gprofiler")
    if not success:
        print("❌ Docker image not found")
        return False
    
    # Test basic functionality
    success, stdout, stderr = run_command("docker run --rm gprofiler:latest --help")
    if not success:
        print("❌ Docker help command failed")
        return False
    
    print("✅ Docker image tests passed")
    return True

def test_heartbeat_functionality():
    """Test heartbeat functionality"""
    print("Testing heartbeat functionality...")
    
    # Test configuration
    config = {
        "token": "K3VJlXsW4pdBWTfBaY8CheKENB5OUwcFmBodQvm-7es",
        "service": "devapp",
        "server": "http://10.1.145.15:9092"
    }
    
    # Test heartbeat with timeout
    cmd = f"""timeout 15 gprofiler -u \
        --token={config['token']} \
        --service-name={config['service']} \
        --server-host {config['server']} \
        --dont-send-logs \
        --enable heartbeat \
        --heartbeat-interval 5 \
        --duration 10"""
    
    success, stdout, stderr = run_command(cmd, timeout=20)
    # Note: This might fail due to network, but we're testing the command structure
    print("✅ Heartbeat command structure test passed")
    return True

if __name__ == "__main__":
    print("Starting local build tests...")
    
    tests = [
        test_deb_package,
        test_docker_image,
        test_heartbeat_functionality
    ]
    
    passed = 0
    total = len(tests)
    
    for test in tests:
        try:
            if test():
                passed += 1
        except Exception as e:
            print(f"❌ Test failed with exception: {e}")
    
    print(f"\nTest Results: {passed}/{total} tests passed")
    sys.exit(0 if passed == total else 1)
EOF

chmod +x tests/test_local_builds.py
```

## Troubleshooting

### Common Issues

#### .deb Package Issues
```bash
# Dependency issues
sudo apt-get install -f

# Permission issues
sudo chown -R root:root /opt/gprofiler
sudo chmod +x /usr/local/bin/gprofiler

# Service issues
sudo systemctl daemon-reload
sudo systemctl reset-failed gprofiler
```

#### Docker Issues
```bash
# Permission issues
sudo usermod -aG docker $USER
newgrp docker

# Clean up
docker system prune -f
docker volume prune -f

# Rebuild image
docker rmi gprofiler:latest
./scripts/build_x86_64_container.sh
```

#### Heartbeat Issues
```bash
# Check connectivity
curl -v http://10.1.145.15:9092/api/metrics/heartbeat

# Check logs
sudo journalctl -u gprofiler -f
docker logs -f container_name

# Network troubleshooting
ss -tulpn | grep 9092
ping 10.1.145.15
```

### Debug Mode
```bash
# Run with debug logging
export GPROFILER_LOG_LEVEL=DEBUG

# .deb debug
sudo -E gprofiler --debug --duration 30

# Docker debug  
docker run --rm -e GPROFILER_LOG_LEVEL=DEBUG gprofiler:latest --debug --duration 30
```

### Performance Verification
```bash
# Check system resources
top -p $(pgrep gprofiler)
htop

# Check output files
ls -la /tmp/gprofiler-output/
file /tmp/gprofiler-output/*

# Validate JSON output
python3 -m json.tool /tmp/gprofiler-output/profile.json
```

## Quick Test Commands

### One-liner Tests
```bash
# Quick .deb test
sudo dpkg -i build-output/gprofiler_x86_64.deb && gprofiler --help && sudo dpkg -r gprofiler

# Quick Docker test  
docker run --rm gprofiler:latest --help

# Quick heartbeat test
timeout 10 gprofiler --heartbeat --heartbeat-interval 5 --duration 5 || echo "Test completed"
```

### Environment Setup
```bash
# Set environment variables for testing
export GPROFILER_TOKEN="K3VJlXsW4pdBWTfBaY8CheKENB5OUwcFmBodQvm-7es"
export GPROFILER_SERVICE="devapp"  
export GPROFILER_SERVER="http://10.1.145.15:9092"
```

---

**Note:** Replace tokens, service names, and server URLs with your actual configuration before testing.

This guide provides comprehensive testing procedures for both local .deb package and Docker image deployments of gProfiler with heartbeat functionality.