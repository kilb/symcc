; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.ll
; RUN: %opt -passes=verify -disable-output %t.ll
; RUN: %filecheck %s --input-file=%t.ll --check-prefix=REJECT

define i16 @conditional_backedge(
    i8 %selector, i1 %write, ptr %slot) {
entry:
  store i16 4386, ptr %slot, align 2
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ 1, %latch ]
  %first = icmp eq i8 %index, 0
  br i1 %first, label %body, label %loop.exit
body:
  br i1 %write, label %write.arm, label %carry.arm
write.arm:
  store i8 51, ptr %slot, align 1
  br label %latch
carry.arm:
  br label %latch
latch:
  br label %loop
loop.exit:
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %deep
left:
  br label %continuation_a
deep:
  store i16 30600, ptr %slot, align 2
  br label %continuation_b
continuation_a:
  %tag_a = phi i16 [ 1, %left ]
  %state = load i16, ptr %slot, align 2
  ret i16 %state
continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

define i16 @conditional_two_writers(
    i8 %selector, i8 %write, ptr %slot) {
entry:
  store i16 4386, ptr %slot, align 2
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ 1, %latch ]
  %first = icmp eq i8 %index, 0
  br i1 %first, label %body, label %loop.exit
body:
  %guard = icmp ne i8 %write, 0
  br i1 %guard, label %write.arm, label %carry.arm
write.arm:
  store i8 51, ptr %slot, align 1
  br label %latch
carry.arm:
  %high = getelementptr inbounds i8, ptr %slot, i64 1
  store i8 68, ptr %high, align 1
  br label %latch
latch:
  br label %loop
loop.exit:
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %deep
left:
  br label %continuation_a
deep:
  store i16 30600, ptr %slot, align 2
  br label %continuation_b
continuation_a:
  %tag_a = phi i16 [ 1, %left ]
  %state = load i16, ptr %slot, align 2
  ret i16 %state
continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

define i16 @conditional_unknown_modref(
    i8 %selector, i8 %write, ptr %slot) {
entry:
  store i16 4386, ptr %slot, align 2
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ 1, %latch ]
  %first = icmp eq i8 %index, 0
  br i1 %first, label %body, label %loop.exit
body:
  %guard = icmp ne i8 %write, 0
  br i1 %guard, label %write.arm, label %carry.arm
write.arm:
  store i8 51, ptr %slot, align 1
  call void @unknown_write(ptr %slot)
  br label %latch
carry.arm:
  br label %latch
latch:
  br label %loop
loop.exit:
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %deep
left:
  br label %continuation_a
deep:
  store i16 30600, ptr %slot, align 2
  br label %continuation_b
continuation_a:
  %tag_a = phi i16 [ 1, %left ]
  %state = load i16, ptr %slot, align 2
  ret i16 %state
continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

define i16 @conditional_non_strict_join(
    i8 %selector, i8 %write, ptr %slot) {
entry:
  store i16 4386, ptr %slot, align 2
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ 1, %latch ]
  %first = icmp eq i8 %index, 0
  br i1 %first, label %body, label %loop.exit
body:
  %guard = icmp ne i8 %write, 0
  br i1 %guard, label %write.arm, label %carry.bridge
write.arm:
  store i8 51, ptr %slot, align 1
  br label %latch
carry.bridge:
  br label %carry.arm
carry.arm:
  br label %latch
latch:
  br label %loop
loop.exit:
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %deep
left:
  br label %continuation_a
deep:
  store i16 30600, ptr %slot, align 2
  br label %continuation_b
continuation_a:
  %tag_a = phi i16 [ 1, %left ]
  %state = load i16, ptr %slot, align 2
  ret i16 %state
continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

define i16 @dynamic_backedge(
    i8 %selector, i64 %offset, ptr %slot) {
entry:
  store i16 4386, ptr %slot, align 2
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ 1, %body ]
  %first = icmp eq i8 %index, 0
  br i1 %first, label %body, label %loop.exit
body:
  %dynamic = getelementptr inbounds i8, ptr %slot, i64 %offset
  store i8 51, ptr %dynamic, align 1
  br label %loop
loop.exit:
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %deep
left:
  br label %continuation_a
deep:
  store i16 30600, ptr %slot, align 2
  br label %continuation_b
continuation_a:
  %tag_a = phi i16 [ 1, %left ]
  %state = load i16, ptr %slot, align 2
  ret i16 %state
continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

define i16 @volatile_backedge(i8 %selector, ptr %slot) {
entry:
  store i16 4386, ptr %slot, align 2
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ 1, %body ]
  %first = icmp eq i8 %index, 0
  br i1 %first, label %body, label %loop.exit
body:
  store volatile i8 51, ptr %slot, align 1
  br label %loop
loop.exit:
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %deep
left:
  br label %continuation_a
deep:
  store i16 30600, ptr %slot, align 2
  br label %continuation_b
continuation_a:
  %tag_a = phi i16 [ 1, %left ]
  %state = load i16, ptr %slot, align 2
  ret i16 %state
continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

define i16 @full_width_backedge(i8 %selector, ptr %slot) {
entry:
  store i16 4386, ptr %slot, align 2
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ 1, %body ]
  %first = icmp eq i8 %index, 0
  br i1 %first, label %body, label %loop.exit
body:
  store i16 13124, ptr %slot, align 2
  br label %loop
loop.exit:
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %deep
left:
  br label %continuation_a
deep:
  store i16 30600, ptr %slot, align 2
  br label %continuation_b
continuation_a:
  %tag_a = phi i16 [ 1, %left ]
  %state = load i16, ptr %slot, align 2
  ret i16 %state
continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

define i16 @multiple_backedges(
    i8 %selector, i1 %arm, ptr %slot) {
entry:
  store i16 4386, ptr %slot, align 2
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ 1, %body.a ], [ 1, %body.b ]
  %first = icmp eq i8 %index, 0
  br i1 %first, label %choose, label %loop.exit
choose:
  br i1 %arm, label %body.a, label %body.b
body.a:
  store i8 51, ptr %slot, align 1
  br label %loop
body.b:
  %high = getelementptr inbounds i8, ptr %slot, i64 1
  store i8 68, ptr %high, align 1
  br label %loop
loop.exit:
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %deep
left:
  br label %continuation_a
deep:
  store i16 30600, ptr %slot, align 2
  br label %continuation_b
continuation_a:
  %tag_a = phi i16 [ 1, %left ]
  %state = load i16, ptr %slot, align 2
  ret i16 %state
continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

declare void @unknown_write(ptr)

; REJECT-NOT: cyclic-byte-lane-continuation-memory-tuple-v6
; REJECT-NOT: conditional-cyclic-byte-lane-continuation-memory-tuple-v9
; REJECT-NOT: symcc.ifss_continuation_memory_cycle
; REJECT-LABEL: define i16 @conditional_backedge
; REJECT: %state = load i16, ptr %slot
; REJECT-LABEL: define i16 @conditional_two_writers
; REJECT: %state = load i16, ptr %slot
; REJECT-LABEL: define i16 @conditional_unknown_modref
; REJECT: %state = load i16, ptr %slot
; REJECT-LABEL: define i16 @conditional_non_strict_join
; REJECT: %state = load i16, ptr %slot
; REJECT-LABEL: define i16 @dynamic_backedge
; REJECT: %state = load i16, ptr %slot
; REJECT-LABEL: define i16 @volatile_backedge
; REJECT: %state = load i16, ptr %slot
; REJECT-LABEL: define i16 @full_width_backedge
; REJECT: %state = load i16, ptr %slot
; REJECT-LABEL: define i16 @multiple_backedges
; REJECT: %state = load i16, ptr %slot
