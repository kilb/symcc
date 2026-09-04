; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.ll
; RUN: %opt -passes=verify -disable-output %t.ll
; RUN: %filecheck %s --input-file=%t.ll --check-prefix=REJECT

define i16 @three_latches(
    i8 %selector, i8 %arm, ptr %slot) {
entry:
  store i16 4386, ptr %slot, align 2
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ 1, %latch.a ],
                  [ 1, %latch.b ], [ 1, %latch.c ]
  %first = icmp eq i8 %index, 0
  br i1 %first, label %body, label %loop.exit
body:
  switch i8 %arm, label %latch.c [
    i8 0, label %latch.a
    i8 1, label %latch.b
  ]
latch.a:
  store i8 51, ptr %slot, align 1
  br label %loop
latch.b:
  %high.b = getelementptr inbounds i8, ptr %slot, i64 1
  store i8 68, ptr %high.b, align 1
  br label %loop
latch.c:
  store i8 85, ptr %slot, align 1
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

define i16 @full_width_latch(
    i8 %selector, i1 %arm, ptr %slot) {
entry:
  store i16 4386, ptr %slot, align 2
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ 1, %latch.a ],
                  [ 1, %latch.b ]
  %first = icmp eq i8 %index, 0
  br i1 %first, label %body, label %loop.exit
body:
  br i1 %arm, label %latch.a, label %latch.b
latch.a:
  store i16 13124, ptr %slot, align 2
  br label %loop
latch.b:
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

define i16 @nested_conditional_latch(
    i8 %selector, i1 %outer, i1 %write, ptr %slot) {
entry:
  store i16 4386, ptr %slot, align 2
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ 1, %latch.join ],
                  [ 1, %latch.b ]
  %first = icmp eq i8 %index, 0
  br i1 %first, label %body, label %loop.exit
body:
  br i1 %outer, label %conditional, label %latch.b
conditional:
  br i1 %write, label %write.arm, label %carry.arm
write.arm:
  store i8 51, ptr %slot, align 1
  br label %latch.join
carry.arm:
  br label %latch.join
latch.join:
  br label %loop
latch.b:
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

; REJECT-NOT: multi-latch-cyclic-byte-lane-continuation-memory-tuple-v10
; REJECT-NOT: symcc.ifss_continuation_memory_multi_cycle
; REJECT-LABEL: define i16 @three_latches
; REJECT: %state = load i16, ptr %slot
; REJECT-LABEL: define i16 @full_width_latch
; REJECT: %state = load i16, ptr %slot
; REJECT-LABEL: define i16 @nested_conditional_latch
; REJECT: %state = load i16, ptr %slot
