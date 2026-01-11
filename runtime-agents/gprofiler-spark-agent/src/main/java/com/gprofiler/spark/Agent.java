package com.gprofiler.spark;

import java.lang.instrument.Instrumentation;
import java.util.jar.JarFile;
import java.io.File;

public class Agent {

    private static Instrumentation instrumentation;

    public static void premain(String agentArgs, Instrumentation inst) {
        System.out.println("gProfiler Spark Agent starting...");
        instrumentation = inst;

        // Add ourselves to the bootstrap class path so java.lang.Thread can call our hooks
        // We need to find the path to this jar.
        // In a real deployment, the agent jar path is known or passed.
        // Here we attempt to find it or expect it to be handled.
        // But for instrumentation of Thread, the callback MUST be visible to Thread (BootLoader).
        // Since we cannot easily predict the jar path at runtime without some hacks,
        // we might rely on the fact that if we are attached as -javaagent, we are in the system classloader usually?
        // No, -javaagent is AppClassLoader unless specified.
        // java.lang.Thread is Boot.

        // Let's try to inject the jar to bootclasspath if we can find it.
        try {
            // This is a common trick to find the agent jar
             java.security.CodeSource codeSource = Agent.class.getProtectionDomain().getCodeSource();
             if (codeSource != null) {
                 File agentJarFile = new File(codeSource.getLocation().toURI().getPath());
                 inst.appendToBootstrapClassLoaderSearch(new JarFile(agentJarFile));
             }
        } catch (Exception e) {
            System.err.println("Failed to append agent to boot classpath: " + e.getMessage());
        }

        HeartbeatSender.start();

        // Register transformer
        inst.addTransformer(new ThreadNameTransformer(), true);

        // Retransform Thread class
        try {
            inst.retransformClasses(java.lang.Thread.class);
        } catch (Exception e) {
            System.err.println("Failed to retransform java.lang.Thread: " + e.getMessage());
        }
    }

    // Callback from instrumented Thread.setName
    public static void onThreadNameChanged(Thread t) {
        // Send update to gProfiler
        HeartbeatSender.sendThreadInfo(t);
    }
}
