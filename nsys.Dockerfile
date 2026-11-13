# nsys.Dockerfile — build context is gprofiler-agent/
FROM ubuntu:24.04

ENV GPROFILER_IN_CONTAINER=1
COPY build/x86_64/gprofiler /gprofiler
RUN chmod +x /gprofiler

RUN apt-get update && apt-get install -y --no-install-recommends wget ca-certificates \
    && wget -qO /tmp/cuda-keyring.deb \
         https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb \
    && dpkg -i /tmp/cuda-keyring.deb \
    && apt-get update \
    && apt-get install -y --no-install-recommends nsight-systems-2025.1.3 \
    && ln -sf /opt/nvidia/nsight-systems/2025.1.3/bin/nsys /usr/local/bin/nsys \
    && rm -rf /var/lib/apt/lists/* /tmp/cuda-keyring.deb

ENTRYPOINT ["/gprofiler"]