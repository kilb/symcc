; REQUIRES: qsym
; RUN: %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: printf AXQ | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t-map SYMCC_TELEMETRY_OUT=%t.json %t
; RUN: python3 -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1; assert d['backsolver_attempts'] >= 1; assert d['backsolver_sat'] >= 1; assert d['backsolver_direct_attempts'] >= 1; assert d['backsolver_direct_sat'] >= 1"
; RUN: python3 -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert models and any(len(m) >= 2 and m[0:1] == b'A' and m[1:2] != b'X' for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i32 @main() {
entry:
  %input = alloca [3 x i8], align 1
  %bytes = bitcast [3 x i8]* %input to i8*
  %read = call i64 @read(i32 0, i8* %bytes, i64 3)
  %first_ptr = getelementptr inbounds [3 x i8], [3 x i8]* %input, i64 0, i64 0
  %second_ptr = getelementptr inbounds [3 x i8], [3 x i8]* %input, i64 0, i64 1
  %first = load i8, i8* %first_ptr, align 1
  %second = load i8, i8* %second_ptr, align 1
  %outer_condition = icmp eq i8 %first, 65
  br i1 %outer_condition, label %outer_true, label %outer_false

outer_true:
  %inner_condition = icmp eq i8 %second, 88
  br i1 %inner_condition, label %inner_true, label %inner_false

inner_true:
  br label %inner_merge

inner_false:
  br label %inner_merge

inner_merge:
  %inner_value = phi i8 [ 10, %inner_true ], [ 20, %inner_false ]
  br label %merge

outer_false:
  br label %merge

merge:
  %implicit = phi i8 [ %inner_value, %inner_merge ], [ 30, %outer_false ]
  %target = icmp eq i8 %implicit, 20
  br i1 %target, label %hit, label %miss

hit:
  ret i32 0

miss:
  ret i32 0
}

declare i64 @read(i32, i8*, i64)
