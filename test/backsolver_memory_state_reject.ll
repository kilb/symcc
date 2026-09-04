; RUN: %symcc -O0 -S -emit-llvm %s -o %t
; RUN: %opt -passes=verify -disable-output %t
; RUN: FileCheck %s --check-prefix=REJECT < %t

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @noalias_state(i1 %condition) {
entry:
  %left_slot = alloca i8, align 1
  %right_slot = alloca i8, align 1
  store i8 30, i8* %left_slot, align 1
  br i1 %condition, label %left, label %right

left:
  store i8 10, i8* %left_slot, align 1
  br label %merge

right:
  store i8 20, i8* %right_slot, align 1
  br label %merge

merge:
  %state = load i8, i8* %left_slot, align 1
  ret i8 %state
}

define i8 @late_value_rejection(i8 %input, i1 %condition) {
entry:
  %slot = alloca i8, align 1
  br i1 %condition, label %left, label %right

left:
  %computed = add i8 %input, 1
  store i8 %computed, i8* %slot, align 1
  br label %merge

right:
  %opaque_value = call i8 @opaque()
  store i8 %opaque_value, i8* %slot, align 1
  br label %merge

merge:
  %state = load i8, i8* %slot, align 1
  ret i8 %state
}

define i8 @poison_state(i1 %condition) {
entry:
  %slot = alloca i8, align 1
  br i1 %condition, label %left, label %right

left:
  store i8 poison, i8* %slot, align 1
  br label %merge

right:
  store i8 1, i8* %slot, align 1
  br label %merge

merge:
  %state = load i8, i8* %slot, align 1
  ret i8 %state
}

define i8 @may_alias_chain(i1 %condition, i8* %target, i8* %maybe_alias) {
entry:
  br i1 %condition, label %left, label %right

left:
  store i8 10, i8* %target, align 1
  store i8 1, i8* %maybe_alias, align 1
  br label %merge

right:
  store i8 20, i8* %target, align 1
  store i8 2, i8* %maybe_alias, align 1
  br label %merge

merge:
  %state = load i8, i8* %target, align 1
  ret i8 %state
}

define i8 @chain_budget(i1 %condition) {
entry:
  %target = alloca i8, align 1
  %noise = alloca [9 x i8], align 1
  br i1 %condition, label %left, label %right

left:
  store i8 10, i8* %target, align 1
  %n0 = getelementptr inbounds [9 x i8], [9 x i8]* %noise, i64 0, i64 0
  store i8 0, i8* %n0, align 1
  %n1 = getelementptr inbounds [9 x i8], [9 x i8]* %noise, i64 0, i64 1
  store i8 1, i8* %n1, align 1
  %n2 = getelementptr inbounds [9 x i8], [9 x i8]* %noise, i64 0, i64 2
  store i8 2, i8* %n2, align 1
  %n3 = getelementptr inbounds [9 x i8], [9 x i8]* %noise, i64 0, i64 3
  store i8 3, i8* %n3, align 1
  %n4 = getelementptr inbounds [9 x i8], [9 x i8]* %noise, i64 0, i64 4
  store i8 4, i8* %n4, align 1
  %n5 = getelementptr inbounds [9 x i8], [9 x i8]* %noise, i64 0, i64 5
  store i8 5, i8* %n5, align 1
  %n6 = getelementptr inbounds [9 x i8], [9 x i8]* %noise, i64 0, i64 6
  store i8 6, i8* %n6, align 1
  %n7 = getelementptr inbounds [9 x i8], [9 x i8]* %noise, i64 0, i64 7
  store i8 7, i8* %n7, align 1
  %n8 = getelementptr inbounds [9 x i8], [9 x i8]* %noise, i64 0, i64 8
  store i8 8, i8* %n8, align 1
  br label %merge

right:
  store i8 20, i8* %target, align 1
  br label %merge

merge:
  %state = load i8, i8* %target, align 1
  ret i8 %state
}

define i8 @volatile_chain(i1 %condition) {
entry:
  %target = alloca i8, align 1
  %noise = alloca i8, align 1
  br i1 %condition, label %left, label %right

left:
  store i8 10, i8* %target, align 1
  store volatile i8 1, i8* %noise, align 1
  br label %merge

right:
  store i8 20, i8* %target, align 1
  br label %merge

merge:
  %state = load i8, i8* %target, align 1
  ret i8 %state
}

define i8 @call_chain_rejection(i1 %condition, i8* %target) {
entry:
  br i1 %condition, label %left, label %right

left:
  store i8 10, i8* %target, align 1
  call void @touch(i8* %target)
  br label %merge

right:
  store i8 20, i8* %target, align 1
  br label %merge

merge:
  %state = load i8, i8* %target, align 1
  ret i8 %state
}

define i8 @multiarm_may_alias(
    i8 %first, i8 %second, i8* %target, i8* %maybe_alias) {
entry:
  store i8 40, i8* %target, align 1
  %root = icmp eq i8 %first, 65
  br i1 %root, label %left, label %right_test

left:
  store i8 10, i8* %target, align 1
  br label %merge

right_test:
  %nested = icmp eq i8 %second, 88
  br i1 %nested, label %middle, label %right

middle:
  store i8 20, i8* %target, align 1
  br label %merge

right:
  store i8 30, i8* %maybe_alias, align 1
  br label %merge

merge:
  %state = load i8, i8* %target, align 1
  ret i8 %state
}

declare i8 @opaque() #0
declare void @touch(i8*)
attributes #0 = { nounwind readnone }

; REJECT-LABEL: define i8 @noalias_state
; REJECT-NOT: ifss.memory
; REJECT: ret i8 %state
; REJECT-LABEL: define i8 @late_value_rejection
; REJECT-NOT: ifss.memory
; REJECT-NOT: !symcc.ifss_memory
; REJECT: ret i8 %state
; REJECT-LABEL: define i8 @poison_state
; REJECT-NOT: ifss.memory
; REJECT: ret i8 %state
; REJECT-LABEL: define i8 @may_alias_chain
; REJECT-NOT: ifss.memory
; REJECT-NOT: must-alias-memoryssa-chain-v1
; REJECT: ret i8 %state
; REJECT-LABEL: define i8 @chain_budget
; REJECT-NOT: ifss.memory
; REJECT-NOT: must-alias-memoryssa-chain-v1
; REJECT: ret i8 %state
; REJECT-LABEL: define i8 @volatile_chain
; REJECT-NOT: ifss.memory
; REJECT-NOT: must-alias-memoryssa-chain-v1
; REJECT: ret i8 %state
; REJECT-LABEL: define i8 @call_chain_rejection
; REJECT-NOT: ifss.memory
; REJECT-NOT: must-alias-memoryssa-chain-v1
; REJECT: ret i8 %state
; REJECT-LABEL: define i8 @multiarm_may_alias
; REJECT-NOT: ifss.memory
; REJECT-NOT: must-alias-memoryssa-multi-v1
; REJECT: ret i8 %state
