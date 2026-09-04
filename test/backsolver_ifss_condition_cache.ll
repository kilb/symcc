; REQUIRES: qsym
; RUN: %symcc -O0 -S -emit-llvm %s -o %t.ir
; RUN: %opt -passes=verify -disable-output %t.ir
; RUN: FileCheck %s --check-prefix=CACHE < %t.ir
; RUN: FileCheck %s --check-prefix=STRUCT < %t.ir
; RUN: %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: printf AXY | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t-map SYMCC_TELEMETRY_OUT=%t.json %t
; RUN: python3 -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1; assert d['backsolver_sat'] >= 1; assert d['backsolver_direct_sat'] >= 1"
; RUN: python3 -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert models and any(len(m) >= 2 and m[0:1] == b'A' and m[1:2] != b'X' for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @data_cache(i8 %first, i8 %second, i8 %third) {
entry:
  %root = icmp eq i8 %first, 65
  br i1 %root, label %left_test, label %right_test

left_test:
  %left_condition = icmp eq i8 %second, 88
  br i1 %left_condition, label %leaf0, label %leaf1

right_test:
  %right_condition = icmp eq i8 %third, 89
  br i1 %right_condition, label %leaf2, label %leaf3

leaf0:
  br label %merge

leaf1:
  br label %merge

leaf2:
  br label %merge

leaf3:
  br label %merge

merge:
  %state = phi i8 [ 10, %leaf0 ], [ 20, %leaf1 ], [ 30, %leaf2 ], [ 40, %leaf3 ]
  ret i8 %state
}

define i32 @main() {
entry:
  %input = alloca [3 x i8], align 1
  %slot = alloca i8, align 1
  %bytes = bitcast [3 x i8]* %input to i8*
  %read = call i64 @read(i32 0, i8* %bytes, i64 3)
  %first_ptr = getelementptr inbounds [3 x i8], [3 x i8]* %input, i64 0, i64 0
  %second_ptr = getelementptr inbounds [3 x i8], [3 x i8]* %input, i64 0, i64 1
  %third_ptr = getelementptr inbounds [3 x i8], [3 x i8]* %input, i64 0, i64 2
  %first = load i8, i8* %first_ptr, align 1
  %second = load i8, i8* %second_ptr, align 1
  %third = load i8, i8* %third_ptr, align 1
  %root = icmp eq i8 %first, 65
  br i1 %root, label %left_test, label %right_test

left_test:
  %left_condition = icmp eq i8 %second, 88
  br i1 %left_condition, label %leaf0, label %leaf1

right_test:
  %right_condition = icmp eq i8 %third, 89
  br i1 %right_condition, label %leaf2, label %leaf3

leaf0:
  store i8 10, i8* %slot, align 1
  br label %merge

leaf1:
  store i8 20, i8* %slot, align 1
  br label %merge

leaf2:
  store i8 30, i8* %slot, align 1
  br label %merge

leaf3:
  store i8 40, i8* %slot, align 1
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

; Each four-arm partition has two non-dominating internal conditions. The
; second leaf reuses the left condition instead of synthesizing it twice.
; CACHE-COUNT-4: !symcc.ifss_condition
; CACHE-COUNT-4: !{!"partition-condition-cache-v1"
; STRUCT: %ifss.memory.state = call ptr @_sym_build_ite
; STRUCT-SAME: !symcc.ifss_memory
