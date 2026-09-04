; REQUIRES: qsym
; RUN: %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json %t-disabled-map %t-disabled.json
; RUN: printf A | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t-map SYMCC_TELEMETRY_OUT=%t.json %t
; RUN: python3 -c "import json; d=json.load(open(r'%t.json')); assert d['symbolic_branches'] >= 2; assert d['backsolver_targets'] >= 1; assert d['backsolver_attempts'] >= 1; assert d['backsolver_sat'] >= 1; assert d['backsolver_direct_attempts'] >= 1; assert d['backsolver_direct_sat'] >= 1; assert d['backsolver_validations'] >= 1; assert d['backsolver_z3_fallbacks'] == 0"
; RUN: find %t-out -type f -name '*-backsolve' | grep -q .
; RUN: python3 -c "import glob; assert any(open(p,'rb').read() != b'A' for p in glob.glob(r'%t-out/*-backsolve'))"
; RUN: rm -rf %t-disabled && mkdir %t-disabled
; RUN: printf A | env SYMCC_OUTPUT_DIR=%t-disabled SYMCC_AFL_COVERAGE_MAP=%t-disabled-map SYMCC_TELEMETRY_OUT=%t-disabled.json SYMCC_BACKSOLVER=0 %t
; RUN: python3 -c "import json; d=json.load(open(r'%t-disabled.json')); assert d['backsolver_targets'] >= 1; assert d['backsolver_attempts'] == 0; assert d['backsolver_sat'] == 0; assert d['backsolver_direct_attempts'] == 0; assert d['backsolver_z3_fallbacks'] == 0"
; RUN: python3 -c "import glob; assert not glob.glob(r'%t-disabled/*-backsolve')"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i32 @main() {
entry:
  %input = alloca i8, align 1
  %read = call i64 @read(i32 0, i8* %input, i64 1)
  %value = load i8, i8* %input, align 1
  %is_a = icmp eq i8 %value, 65
  br i1 %is_a, label %left, label %right

left:
  br label %merge

right:
  br label %merge

merge:
  %implicit = phi i8 [ 7, %left ], [ 9, %right ]
  %target = icmp eq i8 %implicit, 9
  br i1 %target, label %hit, label %miss

hit:
  ret i32 0

miss:
  ret i32 0
}

declare i64 @read(i32, i8*, i64)
