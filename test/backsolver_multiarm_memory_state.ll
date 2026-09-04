; REQUIRES: qsym
; RUN: %symcc -O0 -S -emit-llvm %s -o - | FileCheck %s --check-prefix=MULTI
; RUN: %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: printf AX | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t-map SYMCC_TELEMETRY_OUT=%t.json %t
; RUN: python3 -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1; assert d['backsolver_sat'] >= 1; assert d['backsolver_direct_sat'] >= 1"
; RUN: python3 -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert models and any(len(m) >= 2 and m[0:1] != b'A' and m[1:2] == b'X' for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i32 @main() {
entry:
  %input = alloca [2 x i8], align 1
  %slot = alloca i8, align 1
  %left_noise = alloca i8, align 1
  %middle_noise = alloca i8, align 1
  %right_noise = alloca i8, align 1
  %bytes = bitcast [2 x i8]* %input to i8*
  %read = call i64 @read(i32 0, i8* %bytes, i64 2)
  %first_ptr = getelementptr inbounds [2 x i8], [2 x i8]* %input, i64 0, i64 0
  %second_ptr = getelementptr inbounds [2 x i8], [2 x i8]* %input, i64 0, i64 1
  %first = load i8, i8* %first_ptr, align 1
  %second = load i8, i8* %second_ptr, align 1
  %root_condition = icmp eq i8 %first, 65
  br i1 %root_condition, label %left, label %right_test

left:
  store i8 10, i8* %slot, align 1
  store i8 1, i8* %left_noise, align 1
  br label %merge

right_test:
  %right_condition = icmp eq i8 %second, 88
  br i1 %right_condition, label %middle, label %right

middle:
  store i8 20, i8* %slot, align 1
  store i8 2, i8* %middle_noise, align 1
  br label %merge

right:
  store i8 30, i8* %slot, align 1
  store i8 3, i8* %right_noise, align 1
  br label %merge

merge:
  %state = load i8, i8* %slot, align 1
  %target = icmp eq i8 %state, 20
  br i1 %target, label %hit, label %miss

hit:
  ret i32 0

miss:
  ret i32 0
}

declare i64 @read(i32, i8*, i64)

; MULTI: %ifss.memory.not
; MULTI: %ifss.memory.and
; MULTI: call ptr @_sym_build_ite
; MULTI: %ifss.memory.state = call ptr @_sym_build_ite
; MULTI-SAME: !symcc.ifss_memory
; MULTI: ![[PROOF:[0-9]+]] = !{!"must-alias-memoryssa-multi-v1",
; MULTI-SAME: i32 3
