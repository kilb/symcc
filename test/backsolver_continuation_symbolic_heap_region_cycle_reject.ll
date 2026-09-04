; REQUIRES: qsym
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o - | %filecheck %s --check-prefix=REJECT

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i16 @argument_region(i1 %take, i64 %iterations, ptr %slot) {
entry:
  store i16 4386, ptr %slot, align 2
  br label %loop
loop:
  %index = phi i64 [ 0, %entry ], [ %next, %body ]
  %done = icmp uge i64 %index, %iterations
  br i1 %done, label %loop.exit, label %body
body:
  %element = getelementptr inbounds i8, ptr %slot, i64 %index
  store i8 51, ptr %element, align 1
  %next = add i64 %index, 1
  br label %loop
loop.exit:
  br i1 %take, label %left, label %right
left:
  br label %continuation_a
right:
  store i16 17493, ptr %slot, align 2
  br label %continuation_b
continuation_a:
  %tag_a = phi i16 [ 1, %left ]
  %state = load i16, ptr %slot, align 2
  %result_a = xor i16 %state, %tag_a
  br label %final
continuation_b:
  %tag_b = phi i16 [ 2, %right ]
  br label %final
final:
  %result = phi i16 [ %result_a, %continuation_a ],
                    [ %tag_b, %continuation_b ]
  ret i16 %result
}

define i16 @dynamic_extent(i1 %take, i64 %iterations, i64 %extent) {
entry:
  %slot = call ptr @malloc(i64 %extent)
  store i16 4386, ptr %slot, align 2
  br label %loop
loop:
  %index = phi i64 [ 0, %entry ], [ %next, %body ]
  %done = icmp uge i64 %index, %iterations
  br i1 %done, label %loop.exit, label %body
body:
  %element = getelementptr inbounds i8, ptr %slot, i64 %index
  store i8 51, ptr %element, align 1
  %next = add i64 %index, 1
  br label %loop
loop.exit:
  br i1 %take, label %left, label %right
left:
  br label %continuation_a
right:
  store i16 17493, ptr %slot, align 2
  br label %continuation_b
continuation_a:
  %tag_a = phi i16 [ 1, %left ]
  %state = load i16, ptr %slot, align 2
  %result_a = xor i16 %state, %tag_a
  br label %final
continuation_b:
  %tag_b = phi i16 [ 2, %right ]
  br label %final
final:
  %result = phi i16 [ %result_a, %continuation_a ],
                    [ %tag_b, %continuation_b ]
  ret i16 %result
}

define i16 @two_dynamic_writers(i1 %take, i64 %iterations) {
entry:
  %slot = call ptr @malloc(i64 8)
  store i16 4386, ptr %slot, align 2
  br label %loop
loop:
  %index = phi i64 [ 0, %entry ], [ %next, %body ]
  %done = icmp uge i64 %index, %iterations
  br i1 %done, label %loop.exit, label %body
body:
  %element = getelementptr inbounds i8, ptr %slot, i64 %index
  store i8 51, ptr %element, align 1
  %second.index = add i64 %index, 1
  %second = getelementptr inbounds i8, ptr %slot, i64 %second.index
  store i8 68, ptr %second, align 1
  %next = add i64 %index, 1
  br label %loop
loop.exit:
  br i1 %take, label %left, label %right
left:
  br label %continuation_a
right:
  store i16 17493, ptr %slot, align 2
  br label %continuation_b
continuation_a:
  %tag_a = phi i16 [ 1, %left ]
  %state = load i16, ptr %slot, align 2
  %result_a = xor i16 %state, %tag_a
  br label %final
continuation_b:
  %tag_b = phi i16 [ 2, %right ]
  br label %final
final:
  %result = phi i16 [ %result_a, %continuation_a ],
                    [ %tag_b, %continuation_b ]
  ret i16 %result
}

define i16 @mixed_fixed_dynamic(i1 %take, i64 %iterations) {
entry:
  %slot = call ptr @malloc(i64 8)
  store i16 4386, ptr %slot, align 2
  br label %loop
loop:
  %index = phi i64 [ 0, %entry ], [ %next, %body ]
  %done = icmp uge i64 %index, %iterations
  br i1 %done, label %loop.exit, label %body
body:
  %element = getelementptr inbounds i8, ptr %slot, i64 %index
  store i8 51, ptr %element, align 1
  %high = getelementptr inbounds i8, ptr %slot, i64 1
  store i8 68, ptr %high, align 1
  %next = add i64 %index, 1
  br label %loop
loop.exit:
  br i1 %take, label %left, label %right
left:
  br label %continuation_a
right:
  store i16 17493, ptr %slot, align 2
  br label %continuation_b
continuation_a:
  %tag_a = phi i16 [ 1, %left ]
  %state = load i16, ptr %slot, align 2
  %result_a = xor i16 %state, %tag_a
  br label %final
continuation_b:
  %tag_b = phi i16 [ 2, %right ]
  br label %final
final:
  %result = phi i16 [ %result_a, %continuation_a ],
                    [ %tag_b, %continuation_b ]
  ret i16 %result
}

declare noalias ptr @malloc(i64)

; REJECT-NOT: symbolic-region-cyclic-byte-lane-continuation-memory-tuple-v17
; REJECT-LABEL: define i16 @argument_region
; REJECT: %state = load i16, ptr %slot
; REJECT-LABEL: define i16 @dynamic_extent
; REJECT: %state = load i16, ptr %slot
; REJECT-LABEL: define i16 @two_dynamic_writers
; REJECT: %ifss.cont.memory.cycle = phi i16
; REJECT-LABEL: define i16 @mixed_fixed_dynamic
; REJECT: %state = load i16, ptr %slot
