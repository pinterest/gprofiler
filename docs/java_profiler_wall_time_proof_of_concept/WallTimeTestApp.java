import java.io.*;
import java.net.*;
import java.util.concurrent.*;
import java.util.Random;

public class WallTimeTestApp {
    private static final Random random = new Random();
    
    public static void main(String[] args) {
        System.out.println("=== Wall Time vs CPU Time Test Application ===");
        System.out.println("This app demonstrates the difference between CPU and Wall time profiling");
        System.out.println("Starting continuous workload...");
        
        // Start HTTP server for external requests
        startHttpServer();
        
        // Run continuous workload
        while (true) {
            try {
                // Mix of CPU-intensive and I/O-blocking operations
                runWorkloadCycle();
                Thread.sleep(100); // Brief pause between cycles
            } catch (Exception e) {
                e.printStackTrace();
            }
        }
    }
    
    private static void startHttpServer() {
        new Thread(() -> {
            try {
                ServerSocket server = new ServerSocket(8080);
                System.out.println("HTTP Server started on port 8080");
                
                while (true) {
                    Socket client = server.accept();
                    new Thread(() -> handleRequest(client)).start();
                }
            } catch (IOException e) {
                e.printStackTrace();
            }
        }).start();
    }
    
    private static void handleRequest(Socket client) {
        try (BufferedReader in = new BufferedReader(new InputStreamReader(client.getInputStream()));
             PrintWriter out = new PrintWriter(client.getOutputStream(), true)) {
            
            String line = in.readLine();
            System.out.println("Handling request: " + line);
            
            // Simulate request processing with I/O delays
            processHttpRequest();
            
            out.println("HTTP/1.1 200 OK");
            out.println("Content-Type: text/plain");
            out.println();
            out.println("Wall Time Test Response - Request processed!");
            
            client.close();
        } catch (Exception e) {
            e.printStackTrace();
        }
    }
    
    private static void processHttpRequest() {
        // This method will show high wall time but low CPU time
        try {
            // Simulate database query delay
            simulateDatabaseQuery();
            
            // Simulate external API call
            simulateExternalApiCall();
            
            // Some CPU work mixed in
            doLightCpuWork();
            
        } catch (Exception e) {
            e.printStackTrace();
        }
    }
    
    private static void runWorkloadCycle() {
        int operation = random.nextInt(4);
        
        switch (operation) {
            case 0:
                // CPU-intensive operation (high CPU time, low wall time difference)
                doCpuIntensiveWork();
                break;
            case 1:
                // I/O blocking operation (low CPU time, high wall time)
                doBlockingIoWork();
                break;
            case 2:
                // Mixed operation
                doMixedWork();
                break;
            case 3:
                // Lock contention (low CPU, high wall time)
                doLockContentionWork();
                break;
        }
    }
    
    // ========== CPU-INTENSIVE OPERATIONS (High CPU time) ==========
    
    private static void doCpuIntensiveWork() {
        // This will show up prominently in CPU profiling
        long result = 0;
        for (int i = 0; i < 1000000; i++) {
            result += Math.sqrt(i) * Math.sin(i) * Math.cos(i);
        }
        
        // More CPU work - prime number calculation
        calculatePrimes(1000);
    }
    
    private static void calculatePrimes(int limit) {
        for (int num = 2; num <= limit; num++) {
            boolean isPrime = true;
            for (int i = 2; i <= Math.sqrt(num); i++) {
                if (num % i == 0) {
                    isPrime = false;
                    break;
                }
            }
        }
    }
    
    private static void doLightCpuWork() {
        // Light CPU work that won't dominate CPU profiling
        long result = 0;
        for (int i = 0; i < 10000; i++) {
            result += i * 2;
        }
    }
    
    // ========== I/O BLOCKING OPERATIONS (High Wall time, Low CPU time) ==========
    
    private static void doBlockingIoWork() {
        try {
            // This will show up prominently in WALL profiling but not CPU profiling
            
            // Simulate file I/O
            simulateFileIo();
            
            // Simulate network delay
            simulateNetworkDelay();
            
        } catch (Exception e) {
            // Ignore for demo
        }
    }
    
    private static void simulateFileIo() throws IOException {
        // Create and read a temporary file (I/O blocking)
        File tempFile = File.createTempFile("walltest", ".tmp");
        try (FileWriter writer = new FileWriter(tempFile)) {
            writer.write("Wall time test data - this causes I/O blocking\n".repeat(100));
            writer.flush(); // Force write to disk
        }
        
        // Read it back
        try (BufferedReader reader = new BufferedReader(new FileReader(tempFile))) {
            while (reader.readLine() != null) {
                // Reading causes I/O wait
            }
        }
        
        tempFile.delete();
    }
    
    private static void simulateNetworkDelay() {
        try {
            // Simulate network timeout/delay
            Socket socket = new Socket();
            socket.connect(new InetSocketAddress("10.255.255.1", 12345), 200); // Will timeout
        } catch (Exception e) {
            // Expected timeout - this creates wall time but minimal CPU time
        }
    }
    
    private static void simulateDatabaseQuery() {
        try {
            // Simulate database query delay
            Thread.sleep(50 + random.nextInt(100)); // 50-150ms delay
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
    
    private static void simulateExternalApiCall() {
        try {
            // Simulate external API call delay
            Thread.sleep(30 + random.nextInt(70)); // 30-100ms delay
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
    
    // ========== LOCK CONTENTION (High Wall time, Low CPU time) ==========
    
    private static final Object lock1 = new Object();
    private static final Object lock2 = new Object();
    
    private static void doLockContentionWork() {
        // Create lock contention scenario
        ExecutorService executor = Executors.newFixedThreadPool(3);
        
        for (int i = 0; i < 3; i++) {
            executor.submit(() -> {
                try {
                    // This creates lock contention - high wall time, low CPU
                    synchronized (lock1) {
                        Thread.sleep(20); // Hold lock for a bit
                        synchronized (lock2) {
                            Thread.sleep(10); // Nested lock
                            doLightCpuWork(); // Minimal CPU work
                        }
                    }
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                }
            });
        }
        
        executor.shutdown();
        try {
            executor.awaitTermination(200, TimeUnit.MILLISECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
    
    // ========== MIXED OPERATIONS ==========
    
    private static void doMixedWork() {
        // Mix of CPU and I/O - will show in both profiles but differently
        doLightCpuWork();
        try {
            Thread.sleep(20); // Brief I/O simulation
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        doLightCpuWork();
    }
}
