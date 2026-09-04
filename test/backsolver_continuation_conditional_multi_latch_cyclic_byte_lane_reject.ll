; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.ll
; RUN: %opt -passes=verify -disable-output %t.ll
; RUN: %filecheck %s --input-file=%t.ll --check-prefix=REJECT

; A proof-carrying conditional transfer needs an instruction-backed guard so
; that the guard has a stable site identity. An i1 argument is therefore
; deliberately outside the v12 contract.
define i16 @argument_guard(
    i8 %selector, i1 %write, ptr %slot) {
entry:
  store i16 4386, ptr %slot, align 2
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ 1, %latch.low ],
                  [ 1, %latch.high ]
  %first = icmp eq i8 %index, 0
  br i1 %first, label %body, label %loop.exit
body:
  br i1 %write, label %low.write, label %low.carry
low.write:
  store i8 51, ptr %slot, align 1
  br label %latch.low
low.carry:
  br label %latch.low
latch.low:
  br label %loop
latch.high:
  %high = getelementptr inbounds i8, ptr %slot, i64 1
  store i8 68, ptr %high, align 1
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
  store i16 17493, ptr %slot, align 2
  br label %continuation_a
deep:
  store i16 30600, ptr %slot, align 2
  br label %continuation_b
continuation_a:
  %tag_a = phi i16 [ 1, %left ], [ 2, %middle ]
  %state = load i16, ptr %slot, align 2
  %result_a = add i16 %state, %tag_a
  ret i16 %result_a
continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

; Both arms define the byte, so the transfer is not write-versus-carry and
; cannot be represented by the guarded-store/carry lane relation.
define i16 @both_arms_write(
    i8 %selector, i8 %write, ptr %slot) {
entry:
  store i16 4386, ptr %slot, align 2
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ 1, %latch.low ],
                  [ 1, %latch.high ]
  %first = icmp eq i8 %index, 0
  br i1 %first, label %body, label %loop.exit
body:
  %guard = icmp ne i8 %write, 0
  br i1 %guard, label %low.write.true, label %low.write.false
low.write.true:
  store i8 51, ptr %slot, align 1
  br label %latch.low
low.write.false:
  store i8 52, ptr %slot, align 1
  br label %latch.low
latch.low:
  br label %loop
latch.high:
  %high = getelementptr inbounds i8, ptr %slot, i64 1
  store i8 68, ptr %high, align 1
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
  store i16 17493, ptr %slot, align 2
  br label %continuation_a
deep:
  store i16 30600, ptr %slot, align 2
  br label %continuation_b
continuation_a:
  %tag_a = phi i16 [ 1, %left ], [ 2, %middle ]
  %state = load i16, ptr %slot, align 2
  %result_a = add i16 %state, %tag_a
  ret i16 %result_a
continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

; The opaque call may modify the loaded interval. MemorySSA cannot justify
; skipping it while reconstructing the conditional backedge state.
define i16 @unknown_modref(
    i8 %selector, i8 %write, ptr %slot) {
entry:
  store i16 4386, ptr %slot, align 2
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ 1, %latch.low ],
                  [ 1, %latch.high ]
  %first = icmp eq i8 %index, 0
  br i1 %first, label %body, label %loop.exit
body:
  %guard = icmp ne i8 %write, 0
  br i1 %guard, label %low.write, label %low.carry
low.write:
  call void @opaque(ptr %slot)
  store i8 51, ptr %slot, align 1
  br label %latch.low
low.carry:
  br label %latch.low
latch.low:
  br label %loop
latch.high:
  %high = getelementptr inbounds i8, ptr %slot, i64 1
  store i8 68, ptr %high, align 1
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
  store i16 17493, ptr %slot, align 2
  br label %continuation_a
deep:
  store i16 30600, ptr %slot, align 2
  br label %continuation_b
continuation_a:
  %tag_a = phi i16 [ 1, %left ], [ 2, %middle ]
  %state = load i16, ptr %slot, align 2
  %result_a = add i16 %state, %tag_a
  ret i16 %result_a
continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

declare void @opaque(ptr)

; REJECT-NOT: conditional-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v12
; REJECT-NOT: symcc.ifss_continuation_memory_multi_cycle
; REJECT-LABEL: define i16 @argument_guard
; REJECT: br i1 %is_a{{.*}}!symcc.ifss_continuation
; REJECT: %state = load i16, ptr %slot
; REJECT-NOT: conditional-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v12
; REJECT-NOT: symcc.ifss_continuation_memory_multi_cycle
; REJECT-LABEL: define i16 @both_arms_write
; REJECT: br i1 %is_a{{.*}}!symcc.ifss_continuation
; REJECT: %state = load i16, ptr %slot
; REJECT-NOT: conditional-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v12
; REJECT-NOT: symcc.ifss_continuation_memory_multi_cycle
; REJECT-LABEL: define i16 @unknown_modref
; REJECT: br i1 %is_a{{.*}}!symcc.ifss_continuation
; REJECT: %state = load i16, ptr %slot
; REJECT-NOT: conditional-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v12
; REJECT-NOT: symcc.ifss_continuation_memory_multi_cycle
