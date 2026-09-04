; REQUIRES: qsym
; RUN: %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: printf AX | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t-map SYMCC_TELEMETRY_OUT=%t.json %t
; RUN: python3 -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_attempts'] >= 1; assert d['backsolver_sat'] >= 1; assert d['backsolver_direct_attempts'] >= 1; assert d['backsolver_direct_sat'] >= 1; assert d['backsolver_validations'] >= 1; assert d['backsolver_z3_fallbacks'] == 0; assert d['backsolver_constraints_kept'] >= 1; assert d['backsolver_constraints_dropped'] >= 1"
; RUN: python3 -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert models and any(m[0:1] != b'A' and m[1:2] == b'X' for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i32 @main() {
entry:
  %input = alloca [2 x i8], align 1
  %bytes = bitcast [2 x i8]* %input to i8*
  %read = call i64 @read(i32 0, i8* %bytes, i64 2)
  %first_ptr = getelementptr inbounds [2 x i8], [2 x i8]* %input, i64 0, i64 0
  %second_ptr = getelementptr inbounds [2 x i8], [2 x i8]* %input, i64 0, i64 1
  %first = load i8, i8* %first_ptr, align 1
  %second = load i8, i8* %second_ptr, align 1
  %guard = icmp eq i8 %second, 88
  br i1 %guard, label %body, label %exit

body:
  %controller = icmp eq i8 %first, 65
  %implicit = select i1 %controller, i8 %second, i8 9
  %target = icmp eq i8 %implicit, 9
  br i1 %target, label %hit, label %exit

hit:
  ret i32 0

exit:
  ret i32 0
}

declare i64 @read(i32, i8*, i64)
