; REQUIRES: qsym
; RUN: %symcc -O0 -S -emit-llvm %s -o - | FileCheck %s --check-prefix=SNAPSHOT
; RUN: %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: printf AXZ | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t-map SYMCC_TELEMETRY_OUT=%t.json %t
; RUN: python3 -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1; assert d['solver_sat'] >= 1"
; RUN: python3 -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*')]; assert models and any(len(m) >= 3 and m[0:1] != b'A' and m[2:3] == b'Z' for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i32 @main() {
entry:
  %input = alloca [3 x i8], align 1
  %bytes = bitcast [3 x i8]* %input to i8*
  %read = call i64 @read(i32 0, i8* %bytes, i64 3)
  %first_ptr = getelementptr inbounds [3 x i8], [3 x i8]* %input, i64 0, i64 0
  %second_ptr = getelementptr inbounds [3 x i8], [3 x i8]* %input, i64 0, i64 1
  %third_ptr = getelementptr inbounds [3 x i8], [3 x i8]* %input, i64 0, i64 2
  %first = load i8, i8* %first_ptr, align 1
  %condition = icmp eq i8 %first, 65
  br i1 %condition, label %left, label %right

left:
  %left_value = load i8, i8* %second_ptr, align 1
  br label %merge

right:
  %right_value = load i8, i8* %third_ptr, align 1
  br label %merge

merge:
  %implicit = phi i8 [ %left_value, %left ], [ %right_value, %right ]
  %target = icmp eq i8 %implicit, 90
  br i1 %target, label %hit, label %miss

hit:
  ret i32 0

miss:
  ret i32 0
}

declare i64 @read(i32, i8*, i64)

; SNAPSHOT: %sym.region.ld = load i8
; SNAPSHOT: call ptr @_sym_build_ite
