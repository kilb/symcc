; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.ll
; RUN: %opt -passes=verify -disable-output %t.ll
; RUN: %filecheck %s --input-file=%t.ll --check-prefix=REJECT

define i64 @five_latches(i8 %selector, i8 %arm, ptr %slot) {
entry:
  store i64 1234605616436508552, ptr %slot, align 8
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ 1, %latch.0 ],
                  [ 1, %latch.1 ], [ 1, %latch.2 ],
                  [ 1, %latch.3 ], [ 1, %latch.4 ]
  %first = icmp eq i8 %index, 0
  br i1 %first, label %body, label %loop.exit
body:
  switch i8 %arm, label %latch.4 [
    i8 0, label %latch.0
    i8 1, label %latch.1
    i8 2, label %latch.2
    i8 3, label %latch.3
  ]
latch.0:
  store i8 17, ptr %slot, align 1
  br label %loop
latch.1:
  %byte.1 = getelementptr inbounds i8, ptr %slot, i64 1
  store i8 34, ptr %byte.1, align 1
  br label %loop
latch.2:
  %byte.2 = getelementptr inbounds i8, ptr %slot, i64 2
  store i8 51, ptr %byte.2, align 1
  br label %loop
latch.3:
  %byte.3 = getelementptr inbounds i8, ptr %slot, i64 3
  store i8 68, ptr %byte.3, align 1
  br label %loop
latch.4:
  %byte.4 = getelementptr inbounds i8, ptr %slot, i64 4
  store i8 85, ptr %byte.4, align 1
  br label %loop
loop.exit:
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %rest
left:
  br label %continuation_a
rest:
  %is_b = icmp eq i8 %selector, 66
  br i1 %is_b, label %middle, label %deep
middle:
  store i64 17, ptr %slot, align 8
  br label %continuation_a
deep:
  store i64 34, ptr %slot, align 8
  br label %continuation_b
continuation_a:
  %tag_a = phi i64 [ 1, %left ], [ 2, %middle ]
  %state = load i64, ptr %slot, align 8
  %result_a = add i64 %state, %tag_a
  ret i64 %result_a
continuation_b:
  %tag_b = phi i64 [ 3, %deep ]
  ret i64 %tag_b
}

; REJECT-NOT: bounded-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v13
; REJECT-NOT: symcc.ifss_continuation_memory_multi_cycle
; REJECT-LABEL: define i64 @five_latches
; REJECT: br i1 %is_a{{.*}}!symcc.ifss_continuation
; REJECT: %state = load i64, ptr %slot
; REJECT-NOT: bounded-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v13
; REJECT-NOT: symcc.ifss_continuation_memory_multi_cycle
