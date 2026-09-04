; REQUIRES: qsym
; RUN: env SYMCC_IFSS_EXIT_STATE=1 %opt -load-pass-plugin=%passlib -passes=ifss-exit-lowering -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=EXIT
; RUN: env SYMCC_IFSS_EXIT_STATE=0 %opt -load-pass-plugin=%passlib -passes=ifss-exit-lowering -S %s -o %t.disabled.ll
; RUN: %filecheck %s --input-file=%t.disabled.ll --check-prefix=DISABLED
; RUN: env SYMCC_IFSS_EXIT_STATE=1 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_IFSS_EXIT_STATE=1 %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: printf AX | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t-map SYMCC_TELEMETRY_OUT=%t.json %t
; RUN: %python -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1; assert d['backsolver_attempts'] >= 1; assert d['backsolver_sat'] >= 1; assert d['backsolver_direct_attempts'] >= 1; assert d['backsolver_direct_sat'] >= 1"
; RUN: %python -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert models and any(len(m) >= 2 and m[0:1] != b'A' and m[1:2] == b'X' for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @choose(i8 %first, i8 %second) {
entry:
  %root = icmp eq i8 %first, 65
  br i1 %root, label %left, label %right_test

left:
  ret i8 10

right_test:
  %inner = icmp eq i8 %second, 88
  br i1 %inner, label %middle, label %right

middle:
  ret i8 20

right:
  ret i8 30
}

define i32 @main() {
entry:
  %input = alloca [2 x i8], align 1
  %bytes = bitcast [2 x i8]* %input to i8*
  %read = call i64 @read(i32 0, i8* %bytes, i64 2)
  %first_ptr = getelementptr inbounds [2 x i8], [2 x i8]* %input, i64 0, i64 0
  %second_ptr = getelementptr inbounds [2 x i8], [2 x i8]* %input, i64 0, i64 1
  %first = load i8, i8* %first_ptr, align 1
  %second = load i8, i8* %second_ptr, align 1
  %choice = call i8 @choose(i8 %first, i8 %second)
  %target = icmp eq i8 %choice, 20
  br i1 %target, label %hit, label %miss

hit:
  ret i32 0

miss:
  ret i32 0
}

declare i64 @read(i32, i8*, i64)

; EXIT-LABEL: define i8 @choose
; EXIT-LABEL: left:
; EXIT: br label %ifss.exit.dispatch, !symcc.ifss_exit_arm ![[LEFT:[0-9]+]]
; EXIT-LABEL: middle:
; EXIT: br label %ifss.exit.dispatch, !symcc.ifss_exit_arm ![[MIDDLE:[0-9]+]]
; EXIT-LABEL: right:
; EXIT: br label %ifss.exit.dispatch, !symcc.ifss_exit_arm ![[RIGHT:[0-9]+]]
; EXIT-LABEL: ifss.exit.dispatch:
; EXIT: %ifss.exit.state = phi i8 [ 10, %left ], [ 20, %middle ], [ 30, %right ], !symcc.ifss_exit ![[STATE:[0-9]+]]
; EXIT: ret i8 %ifss.exit.state, !symcc.ifss_exit ![[RETURN:[0-9]+]]
; EXIT: ![[LEFT]] = !{!"bounded-return-exit-state-v1", i32 0, i64
; EXIT: ![[MIDDLE]] = !{!"bounded-return-exit-state-v1", i32 1, i64
; EXIT: ![[RIGHT]] = !{!"bounded-return-exit-state-v1", i32 2, i64
; EXIT: ![[STATE]] = !{!"bounded-return-exit-state-v1", i64 {{-?[0-9]+}}, i32 3, i32 4, i32 3
; EXIT: ![[RETURN]] = !{!"bounded-return-exit-state-v1", i64 {{-?[0-9]+}}, i32 3, i32 4, i32 3}

; IFSS-LABEL: define i8 @choose
; IFSS: %ifss.exit.state = phi i8
; IFSS-COUNT-2: call ptr @_sym_build_ite
; IFSS: call void @_sym_set_return_expression

; DISABLED-LABEL: define i8 @choose
; DISABLED-COUNT-3: ret i8
; DISABLED-NOT: bounded-return-exit-state-v1
