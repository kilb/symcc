; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o - | %filecheck %s --check-prefix=REJECT

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i32 @five_leaf_tree(
    i8 %selector, i8 %iterations, i8 %choice, ptr %slot) {
entry:
  store i32 287454020, ptr %slot, align 4
  br label %loop

loop:
  %index = phi i8 [ 0, %entry ], [ %next, %latch ]
  %done = icmp uge i8 %index, %iterations
  br i1 %done, label %loop.exit, label %root

root:
  %is.0 = icmp eq i8 %choice, 0
  br i1 %is.0, label %leaf.0, label %node.1

node.1:
  %is.1 = icmp eq i8 %choice, 1
  br i1 %is.1, label %leaf.1, label %node.2

node.2:
  %is.2 = icmp eq i8 %choice, 2
  br i1 %is.2, label %leaf.2, label %node.3

node.3:
  %is.3 = icmp eq i8 %choice, 3
  br i1 %is.3, label %leaf.3, label %leaf.4

leaf.0:
  store i8 65, ptr %slot, align 1
  br label %latch

leaf.1:
  %byte.1 = getelementptr inbounds i8, ptr %slot, i64 1
  store i8 82, ptr %byte.1, align 1
  br label %latch

leaf.2:
  br label %latch

leaf.3:
  %byte.2 = getelementptr inbounds i8, ptr %slot, i64 2
  store i8 99, ptr %byte.2, align 1
  br label %latch

leaf.4:
  %byte.3 = getelementptr inbounds i8, ptr %slot, i64 3
  store i8 116, ptr %byte.3, align 1
  br label %latch

latch:
  %next = add i8 %index, 1
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
  store i32 1432778632, ptr %slot, align 4
  br label %continuation_a

deep:
  store i32 16909060, ptr %slot, align 4
  br label %continuation_b

continuation_a:
  %tag_a = phi i32 [ 1, %left ], [ 2, %middle ]
  %state = load i32, ptr %slot, align 4
  %result_a = add i32 %state, %tag_a
  ret i32 %result_a

continuation_b:
  %tag_b = phi i32 [ 3, %deep ]
  ret i32 %tag_b
}

; REJECT-LABEL: define i32 @five_leaf_tree
; REJECT: %state = load i32, ptr %slot
; REJECT-NOT: ifss.cont.memory.predicate.cycle
; REJECT-NOT: nested-predicate-cyclic-byte-lane-continuation-memory-tuple-v14
; REJECT: bounded-continuation-tuple-v1
