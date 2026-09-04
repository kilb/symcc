; RUN: %symcc -O0 -S -emit-llvm %s -o %t
; RUN: %opt -passes=verify -disable-output %t
; RUN: FileCheck %s --check-prefix=REJECT < %t

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @unsupported_arm(i8 %first, i8 %second) {
entry:
  %root_condition = icmp eq i8 %first, 65
  br i1 %root_condition, label %left, label %right_test

left:
  br label %merge

right_test:
  %right_condition = icmp eq i8 %second, 88
  br i1 %right_condition, label %middle, label %right

middle:
  br label %merge

right:
  %opaque_value = call i8 @opaque()
  br label %merge

merge:
  %implicit = phi i8 [ 10, %left ], [ 20, %middle ], [ %opaque_value, %right ]
  ret i8 %implicit
}

declare i8 @opaque()

; The unsupported side-effecting value is discovered after earlier path
; predicates were synthesized. Rejection must roll all speculative IR back.
; REJECT-LABEL: define i8 @unsupported_arm
; REJECT-NOT: ifss.
; REJECT-NOT: call ptr @_sym_build_ite
; REJECT-NOT: call ptr @_sym_build_bool_xor
; REJECT: ret i8 %implicit
