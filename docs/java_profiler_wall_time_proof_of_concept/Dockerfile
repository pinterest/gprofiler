FROM eclipse-temurin:11-jdk

# Install curl for health checks
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# Copy Java application
COPY WallTimeTestApp.java .
COPY compile-and-run.sh .

# Make script executable
RUN chmod +x compile-and-run.sh

# Compile Java application
RUN javac WallTimeTestApp.java

# Expose port for HTTP endpoint
EXPOSE 8080

# Run the application
CMD ["./compile-and-run.sh"]
