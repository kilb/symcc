; REQUIRES: qsym
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.ll
; RUN: %opt -passes=verify -disable-output %t.ll
; RUN: %filecheck %s --input-file=%t.ll --check-prefix=REJECT

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i32 @two_writers_one_latch(
    i1 %take, i1 %arm, i64 %iterations, i8 %value) {
entry:
  %slot = call ptr @malloc(i64 8)
  store i32 287454020, ptr %slot, align 4
  br label %loop

loop:
  %index = phi i64 [ 0, %entry ], [ %next.low, %latch.low ],
                   [ %next.high, %latch.high ]
  %done = icmp uge i64 %index, %iterations
  br i1 %done, label %loop.exit, label %body

body:
  br i1 %arm, label %low.write, label %high.write

low.write:
  %first.ptr = getelementptr inbounds i8, ptr %slot, i64 %index
  store i8 %value, ptr %first.ptr, align 1
  %second.index = add i64 %index, 1
  %second.ptr = getelementptr inbounds i8, ptr %slot, i64 %second.index
  store i8 85, ptr %second.ptr, align 1
  br label %latch.low

latch.low:
  %next.low = add i64 %index, 1
  br label %loop

high.write:
  %high.index = add i64 %index, 2
  %high.ptr = getelementptr inbounds i8, ptr %slot, i64 %high.index
  store i8 102, ptr %high.ptr, align 1
  br label %latch.high

latch.high:
  %next.high = add i64 %index, 1
  br label %loop

loop.exit:
  br i1 %take, label %left, label %right

left:
  br label %continuation_a

right:
  store i32 1432778632, ptr %slot, align 4
  br label %continuation_b

continuation_a:
  %tag_a = phi i32 [ 1, %left ]
  %state = load i32, ptr %slot, align 4
  %result_a = xor i32 %state, %tag_a
  br label %final

continuation_b:
  %tag_b = phi i32 [ 2, %right ]
  br label %final

final:
  %result = phi i32 [ %result_a, %continuation_a ],
                    [ %tag_b, %continuation_b ]
  ret i32 %result
}

define i32 @dynamic_extent_multi_latch(
    i1 %take, i1 %arm, i64 %iterations, i64 %extent) {
entry:
  %slot = call ptr @malloc(i64 %extent)
  store i32 287454020, ptr %slot, align 4
  br label %loop

loop:
  %index = phi i64 [ 0, %entry ], [ %next.low, %latch.low ],
                   [ %next.high, %latch.high ]
  %done = icmp uge i64 %index, %iterations
  br i1 %done, label %loop.exit, label %body

body:
  br i1 %arm, label %low.write, label %high.write

low.write:
  %low.ptr = getelementptr inbounds i8, ptr %slot, i64 %index
  store i8 51, ptr %low.ptr, align 1
  br label %latch.low

latch.low:
  %next.low = add i64 %index, 1
  br label %loop

high.write:
  %high.index = add i64 %index, 1
  %high.ptr = getelementptr inbounds i8, ptr %slot, i64 %high.index
  store i8 68, ptr %high.ptr, align 1
  br label %latch.high

latch.high:
  %next.high = add i64 %index, 1
  br label %loop

loop.exit:
  br i1 %take, label %left, label %right

left:
  br label %continuation_a

right:
  store i32 1432778632, ptr %slot, align 4
  br label %continuation_b

continuation_a:
  %tag_a = phi i32 [ 1, %left ]
  %state = load i32, ptr %slot, align 4
  %result_a = xor i32 %state, %tag_a
  br label %final

continuation_b:
  %tag_b = phi i32 [ 2, %right ]
  br label %final

final:
  %result = phi i32 [ %result_a, %continuation_a ],
                    [ %tag_b, %continuation_b ]
  ret i32 %result
}

define i64 @five_dynamic_latches(i1 %take, i8 %arm) {
entry:
  %slot = call ptr @malloc(i64 16)
  store i64 1234605616436508552, ptr %slot, align 8
  br label %loop

loop:
  %index = phi i64 [ 0, %entry ], [ 1, %latch.0 ],
                   [ 1, %latch.1 ], [ 1, %latch.2 ],
                   [ 1, %latch.3 ], [ 1, %latch.4 ]
  %first = icmp eq i64 %index, 0
  br i1 %first, label %body, label %loop.exit

body:
  switch i8 %arm, label %latch.4 [
    i8 0, label %latch.0
    i8 1, label %latch.1
    i8 2, label %latch.2
    i8 3, label %latch.3
  ]

latch.0:
  %ptr.0 = getelementptr inbounds i8, ptr %slot, i64 %index
  store i8 17, ptr %ptr.0, align 1
  br label %loop

latch.1:
  %index.1 = add i64 %index, 1
  %ptr.1 = getelementptr inbounds i8, ptr %slot, i64 %index.1
  store i8 34, ptr %ptr.1, align 1
  br label %loop

latch.2:
  %index.2 = add i64 %index, 2
  %ptr.2 = getelementptr inbounds i8, ptr %slot, i64 %index.2
  store i8 51, ptr %ptr.2, align 1
  br label %loop

latch.3:
  %index.3 = add i64 %index, 3
  %ptr.3 = getelementptr inbounds i8, ptr %slot, i64 %index.3
  store i8 68, ptr %ptr.3, align 1
  br label %loop

latch.4:
  %index.4 = add i64 %index, 4
  %ptr.4 = getelementptr inbounds i8, ptr %slot, i64 %index.4
  store i8 85, ptr %ptr.4, align 1
  br label %loop

loop.exit:
  br i1 %take, label %left, label %right

left:
  br label %continuation_a

right:
  store i64 72623859790382856, ptr %slot, align 8
  br label %continuation_b

continuation_a:
  %tag_a = phi i64 [ 1, %left ]
  %state = load i64, ptr %slot, align 8
  %result_a = xor i64 %state, %tag_a
  br label %final

continuation_b:
  %tag_b = phi i64 [ 2, %right ]
  br label %final

final:
  %result = phi i64 [ %result_a, %continuation_a ],
                    [ %tag_b, %continuation_b ]
  ret i64 %result
}

declare noalias ptr @malloc(i64)

; REJECT-NOT: symbolic-region-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v18
; REJECT-NOT: symcc.ifss_continuation_memory_multi_cycle
; REJECT-LABEL: define i32 @two_writers_one_latch
; REJECT: %state = load i32, ptr %slot
; REJECT-LABEL: define i32 @dynamic_extent_multi_latch
; REJECT: %state = load i32, ptr %slot
; REJECT-LABEL: define i64 @five_dynamic_latches
; REJECT: %state = load i64, ptr %slot
; REJECT-NOT: symbolic-region-multi-latch-cyclic-byte-lane-continuation-memory-tuple-v18
; REJECT-NOT: symcc.ifss_continuation_memory_multi_cycle
