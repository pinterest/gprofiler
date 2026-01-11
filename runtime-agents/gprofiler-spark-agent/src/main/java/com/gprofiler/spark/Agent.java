package com.gprofiler.spark;

import java.lang.instrument.Instrumentation;
import java.util.jar.JarFile;
import java.io.File;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.LinkedBlockingQueue;

public class Agent {

    private static Instrumentation instrumentation;

    // Queue to decouple Thread.setName from network operations
    public static final BlockingQueue<Thread> threadUpdateQueue = new LinkedBlockingQueue<Thread>();

    public static void premain(String agentArgs, Instrumentation inst) {
        System.out.println("gProfiler Spark Agent starting...");
        instrumentation = inst;

        try {
             java.security.CodeSource codeSource = Agent.class.getProtectionDomain().getCodeSource();
             if (codeSource != null) {
                 File agentJarFile = new File(codeSource.getLocation().toURI().getPath());
                 inst.appendToBootstrapClassLoaderSearch(new JarFile(agentJarFile));
             }
        } catch (Exception e) {
            System.err.println("Failed to append agent to boot classpath: " + e.getMessage());
        }

        HeartbeatSender.start();

        inst.addTransformer(new ThreadNameTransformer(), true);

        try {
            inst.retransformClasses(java.lang.Thread.class);
        } catch (Exception e) {
            System.err.println("Failed to retransform java.lang.Thread: " + e.getMessage());
        }
    }

    // Callback from instrumented Thread.setName
    public static void onThreadNameChanged(Thread t) {
        // Just enqueue the thread, return immediately
        threadUpdateQueue.offer(t);
    }
}
