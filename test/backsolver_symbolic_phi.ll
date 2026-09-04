; REQUIRES: qsym
; RUN: %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: printf AXZ | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t-map SYMCC_TELEMETRY_OUT=%t.json %t
; RUN: python3 -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1; assert d['backsolver_attempts'] >= 1; assert d['backsolver_sat'] >= 1; assert d['backsolver_direct_attempts'] >= 1; assert d['backsolver_direct_sat'] >= 1; assert d['backsolver_constraints_kept'] >= 1; assert d['backsolver_constraints_dropped'] >= 1"
; RUN: python3 -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert models and any(m[0:1] != b'A' and m[1:2] == b'X' and m[2:3] == b'Z' for m in models)"

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
  %second = load i8, i8* %second_ptr, align 1
  %third = load i8, i8* %third_ptr, align 1
  %guard = icmp eq i8 %second, 88
  br i1 %guard, label %body, label %exit

body:
  %controller = icmp eq i8 %first, 65
  br i1 %controller, label %left, label %right

left:
  br label %merge

right:
  br label %merge

merge:
  %implicit = phi i8 [ %second, %left ], [ %third, %right ]
  %target = icmp eq i8 %implicit, 90
  br i1 %target, label %hit, label %exit

hit:
  ret i32 0

exit:
  ret i32 0
}

declare i64 @read(i32, i8*, i64)
