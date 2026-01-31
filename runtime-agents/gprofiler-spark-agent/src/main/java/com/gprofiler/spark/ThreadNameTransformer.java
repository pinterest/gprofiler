package com.gprofiler.spark;

import java.lang.instrument.ClassFileTransformer;
import java.lang.instrument.IllegalClassFormatException;
import java.security.ProtectionDomain;

import org.objectweb.asm.ClassReader;
import org.objectweb.asm.ClassWriter;
import org.objectweb.asm.ClassVisitor;
import org.objectweb.asm.MethodVisitor;
import org.objectweb.asm.Opcodes;

public class ThreadNameTransformer implements ClassFileTransformer {

    @Override
    public byte[] transform(ClassLoader loader, String className, Class<?> classBeingRedefined,
                            ProtectionDomain protectionDomain, byte[] classfileBuffer) throws IllegalClassFormatException {

        if ("java/lang/Thread".equals(className)) {
            try {
                ClassReader reader = new ClassReader(classfileBuffer);
                ClassWriter writer = new ClassWriter(reader, ClassWriter.COMPUTE_FRAMES | ClassWriter.COMPUTE_MAXS);
                ClassVisitor visitor = new ThreadClassVisitor(writer);
                reader.accept(visitor, 0);
                return writer.toByteArray();
            } catch (Exception e) {
                e.printStackTrace();
            }
        }
        return null;
    }

    static class ThreadClassVisitor extends ClassVisitor {
        public ThreadClassVisitor(ClassVisitor cv) {
            super(Opcodes.ASM9, cv);
        }

        @Override
        public MethodVisitor visitMethod(int access, String name, String descriptor, String signature, String[] exceptions) {
            MethodVisitor mv = super.visitMethod(access, name, descriptor, signature, exceptions);
            if ("setName".equals(name) && "(Ljava/lang/String;)V".equals(descriptor)) {
                return new SetNameMethodVisitor(mv);
            }
            return mv;
        }
    }

    static class SetNameMethodVisitor extends MethodVisitor {
        public SetNameMethodVisitor(MethodVisitor mv) {
            super(Opcodes.ASM9, mv);
        }

        @Override
        public void visitInsn(int opcode) {
            if (opcode == Opcodes.RETURN) {
                // Load 'this' (the Thread instance)
                super.visitVarInsn(Opcodes.ALOAD, 0);
                // Call Agent.onThreadNameChanged(Thread)
                super.visitMethodInsn(Opcodes.INVOKESTATIC, "com/gprofiler/spark/Agent", "onThreadNameChanged", "(Ljava/lang/Thread;)V", false);
            }
            super.visitInsn(opcode);
        }
    }
}
