#!/bin/bash

echo "=== Starting Wall Time Test Application ==="
echo "This application demonstrates CPU vs Wall time profiling differences"
echo ""
echo "Expected behavior:"
echo "- CPU profiling: Will show doCpuIntensiveWork(), calculatePrimes() prominently"
echo "- Wall profiling: Will show doBlockingIoWork(), simulateFileIo(), simulateDatabaseQuery() prominently"
echo ""

# Run the Java application
java WallTimeTestApp
