package com.gprofiler.spark;

import java.io.OutputStream;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import org.json.JSONObject;

public class HeartbeatSender {

    // Default port, could be made configurable via agentArgs if needed
    private static final int PORT = 12345;
    private static final String HOST = "127.0.0.1";
    private static final int INTERVAL_SECONDS = 10;

    private static final ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
        Thread t = new Thread(r, "gprofiler-spark-heartbeat");
        t.setDaemon(true);
        return t;
    });

    public static void start() {
        scheduler.scheduleAtFixedRate(HeartbeatSender::sendHeartbeat, 0, INTERVAL_SECONDS, TimeUnit.SECONDS);
    }

    private static void sendHeartbeat() {
        try {
            JSONObject metadata = SparkMetadata.getMetadata();

            // Only send if we have at least an App ID, otherwise we might not be in a fully initialized Spark context yet
            // Or maybe we want to send PID anyway? The requirement says "identifies Spark metadata".
            // If it's just a random process with this agent, it might not be a Spark app yet.
            // However, usually agent is attached to the driver/executor.
            // Let's send what we have.

            String jsonString = metadata.toString();

            try (Socket socket = new Socket(HOST, PORT);
                 OutputStream out = socket.getOutputStream()) {

                // Send JSON payload
                // We might need a framing strategy (e.g., newline delimited) so the receiver knows when a message ends.
                // I'll append a newline.
                out.write((jsonString + "\n").getBytes(StandardCharsets.UTF_8));
                out.flush();
                // We don't necessarily need to read a response, this is a "heartbeat".
            }

        } catch (Exception e) {
            // Squelch errors to avoid spamming logs if gProfiler is down
            // System.err.println("gProfiler Spark Agent: Failed to send heartbeat: " + e.getMessage());
        }
    }
}
