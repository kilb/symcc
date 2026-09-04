; RUN: %symcc -O0 -S -emit-llvm %s -o - | FileCheck %s

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @memory_changes(i1 %condition, i8* dereferenceable(1) %pointer) {
entry:
  br i1 %condition, label %left, label %right

left:
  %left_value = load i8, i8* %pointer, align 1
  br label %merge

right:
  store i8 42, i8* %pointer, align 1
  br label %merge

merge:
  %implicit = phi i8 [ %left_value, %left ], [ 7, %right ]
  ret i8 %implicit
}

; CHECK-NOT: %sym.region.ld
