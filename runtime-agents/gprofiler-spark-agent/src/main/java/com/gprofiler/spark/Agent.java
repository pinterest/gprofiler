package com.gprofiler.spark;

import java.lang.instrument.Instrumentation;

public class Agent {
    public static void premain(String agentArgs, Instrumentation inst) {
        System.out.println("gProfiler Spark Agent starting...");
        HeartbeatSender.start();
    }
}
