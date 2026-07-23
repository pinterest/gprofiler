FROM alpine as gprofiler

ARG ARCH
ARG EXE_PATH=build/${ARCH}/gprofiler
# lets gProfiler know it is running in a container
ENV GPROFILER_IN_CONTAINER=1
# Install sudo and bash for PerfSpect to run with full functionality
RUN apk add --no-cache sudo bash
COPY ${EXE_PATH} /gprofiler
RUN chmod +x /gprofiler

ENTRYPOINT ["/gprofiler"]
