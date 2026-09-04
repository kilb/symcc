; REQUIRES: qsym
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o - | %filecheck %s --check-prefix=REJECT

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i16 @five_dynamic_writers(
    i1 %take, i64 %iterations, i8 %value) {
entry:
  %slot = call ptr @malloc(i64 8)
  store i16 4386, ptr %slot, align 2
  br label %loop
loop:
  %index = phi i64 [ 0, %entry ], [ %next, %body ]
  %done = icmp uge i64 %index, %iterations
  br i1 %done, label %loop.exit, label %body
body:
  %p0 = getelementptr inbounds i8, ptr %slot, i64 %index
  store i8 %value, ptr %p0, align 1
  %i1 = add i64 %index, 1
  %p1 = getelementptr inbounds i8, ptr %slot, i64 %i1
  store i8 17, ptr %p1, align 1
  %i2 = add i64 %index, 2
  %p2 = getelementptr inbounds i8, ptr %slot, i64 %i2
  store i8 34, ptr %p2, align 1
  %i3 = add i64 %index, 3
  %p3 = getelementptr inbounds i8, ptr %slot, i64 %i3
  store i8 51, ptr %p3, align 1
  %i4 = add i64 %index, 4
  %p4 = getelementptr inbounds i8, ptr %slot, i64 %i4
  store i8 68, ptr %p4, align 1
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

define i16 @interposed_unknown_mod(
    i1 %take, i64 %iterations, i8 %value) {
entry:
  %slot = call ptr @malloc(i64 8)
  store i16 4386, ptr %slot, align 2
  br label %loop
loop:
  %index = phi i64 [ 0, %entry ], [ %next, %body ]
  %done = icmp uge i64 %index, %iterations
  br i1 %done, label %loop.exit, label %body
body:
  %old = getelementptr inbounds i8, ptr %slot, i64 %index
  store i8 %value, ptr %old, align 1
  call void @opaque(ptr %slot)
  %new.index = add i64 %index, 1
  %new = getelementptr inbounds i8, ptr %slot, i64 %new.index
  store i8 85, ptr %new, align 1
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
declare void @opaque(ptr)

; REJECT-NOT: ordered-symbolic-region-cyclic-byte-lane-continuation-memory-tuple-v19
; REJECT-LABEL: define i16 @five_dynamic_writers
; REJECT: %state = load i16, ptr %slot
; REJECT-LABEL: define i16 @interposed_unknown_mod
; REJECT: %state = load i16, ptr %slot
