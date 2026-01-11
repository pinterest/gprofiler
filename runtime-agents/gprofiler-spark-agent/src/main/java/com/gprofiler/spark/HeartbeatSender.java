package com.gprofiler.spark;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;

public class HeartbeatSender {

    private static final int PORT = 12345;
    private static final String HOST = "127.0.0.1";
    private static final int INTERVAL_SECONDS = 60;

    private static final AtomicBoolean profilingEnabled = new AtomicBoolean(false);
    private static final ScheduledExecutorService scheduler = Executors.newScheduledThreadPool(2, r -> {
        Thread t = new Thread(r, "gprofiler-spark-sender");
        t.setDaemon(true);
        return t;
    });

    private static final Gson gson = new Gson();

    public static void start() {
        scheduler.scheduleAtFixedRate(HeartbeatSender::sendHeartbeat, 0, INTERVAL_SECONDS, TimeUnit.SECONDS);
    }

    private static void sendHeartbeat() {
        try {
            JsonObject metadata = SparkMetadata.getMetadata();
            String jsonString = gson.toJson(metadata);

            try (Socket socket = new Socket(HOST, PORT);
                 OutputStream out = socket.getOutputStream();
                 BufferedReader in = new BufferedReader(new InputStreamReader(socket.getInputStream(), StandardCharsets.UTF_8))) {

                out.write((jsonString + "\n").getBytes(StandardCharsets.UTF_8));
                out.flush();

                // Read response
                String responseLine = in.readLine();
                if (responseLine != null) {
                    try {
                        JsonObject response = gson.fromJson(responseLine, JsonObject.class);
                        boolean shouldProfile = false;
                        if (response.has("profile")) {
                            shouldProfile = response.get("profile").getAsBoolean();
                        }

                        if (shouldProfile && !profilingEnabled.get()) {
                            profilingEnabled.set(true);
                            // Initial thread dump
                            sendAllThreads();
                        } else if (!shouldProfile && profilingEnabled.get()) {
                            profilingEnabled.set(false);
                        }
                    } catch (Exception e) {
                        // Ignore parse errors
                    }
                }
            }

        } catch (Exception e) {
            // Squelch errors
        }
    }

    public static void sendAllThreads() {
        if (!profilingEnabled.get()) return;

        scheduler.execute(() -> {
            try {
                JsonObject payload = SparkMetadata.getMetadata(); // Includes PID/AppID
                payload.addProperty("type", "thread_info");

                JsonArray threadsArray = new JsonArray();
                Map<Thread, StackTraceElement[]> stacks = Thread.getAllStackTraces();
                for (Thread t : stacks.keySet()) {
                    JsonObject threadInfo = new JsonObject();
                    threadInfo.addProperty("tid", t.getId());
                    threadInfo.addProperty("name", t.getName());
                    threadsArray.add(threadInfo);
                }
                payload.add("threads", threadsArray);

                sendPayload(payload);
            } catch (Exception e) {
                e.printStackTrace();
            }
        });
    }

    public static void sendThreadInfo(Thread t) {
        if (!profilingEnabled.get()) return;

        scheduler.execute(() -> {
            try {
                JsonObject payload = SparkMetadata.getMetadata();
                payload.addProperty("type", "thread_info");

                JsonArray threadsArray = new JsonArray();
                JsonObject threadInfo = new JsonObject();
                threadInfo.addProperty("tid", t.getId());
                threadInfo.addProperty("name", t.getName());
                threadsArray.add(threadInfo);

                payload.add("threads", threadsArray);

                sendPayload(payload);
            } catch (Exception e) {
                // Ignore
            }
        });
    }

    private static void sendPayload(JsonObject payload) {
        try (Socket socket = new Socket(HOST, PORT);
             OutputStream out = socket.getOutputStream()) {
            out.write((gson.toJson(payload) + "\n").getBytes(StandardCharsets.UTF_8));
            out.flush();
        } catch (Exception e) {
            // Ignore
        }
    }
}
