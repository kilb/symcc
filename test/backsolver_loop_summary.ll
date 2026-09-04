; REQUIRES: qsym
; RUN: env SYMCC_IFSS_LOOP_SUMMARY=1 %opt -load-pass-plugin=%passlib -passes=ifss-loop-summary -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=SUMMARY
; RUN: env SYMCC_IFSS_LOOP_SUMMARY=0 %opt -load-pass-plugin=%passlib -passes=ifss-loop-summary -S %s -o %t.disabled.ll
; RUN: %filecheck %s --input-file=%t.disabled.ll --check-prefix=DISABLED
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,b: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([b]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.disabled.ll',b)==run(r'%t.lowered.ll',b) for b in range(32))"
; RUN: env SYMCC_IFSS_LOOP_SUMMARY=1 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_IFSS_LOOP_SUMMARY=1 %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: printf '\000' | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t-map SYMCC_TELEMETRY_OUT=%t.json %t
; RUN: %python -c "import json; d=json.load(open(r'%t.json')); assert d['solver_queries'] >= 1; assert d['solver_sat'] >= 1"
; RUN: %python -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*')]; assert models and any(m and (m[0] & 7) == 5 for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @accumulate(i8 %raw) {
entry:
  %limit = and i8 %raw, 7
  br label %loop

loop:
  %index = phi i8 [ 0, %entry ], [ %index_next, %latch ]
  %state = phi i8 [ 10, %entry ], [ %state_next, %latch ]
  %continue = icmp ult i8 %index, %limit
  br i1 %continue, label %latch, label %exit

latch:
  %state_next = add i8 %state, 3
  %index_next = add i8 %index, 1
  br label %loop

exit:
  ret i8 %state
}

define i16 @two_states(i8 %raw, i16 %seed) {
entry:
  %limit = and i8 %raw, 7
  br label %loop

loop:
  %index = phi i8 [ 0, %entry ], [ %index_next, %latch ]
  %left = phi i16 [ %seed, %entry ], [ %left_next, %latch ]
  %right = phi i16 [ 9, %entry ], [ %right_next, %latch ]
  %continue = icmp ult i8 %index, %limit
  br i1 %continue, label %latch, label %exit

latch:
  %left_next = add i16 %left, 2
  %right_next = add i16 %right, 5
  %index_next = add i8 %index, 1
  br label %loop

exit:
  %left_out = phi i16 [ %left, %loop ]
  %result = add i16 %left_out, %right
  ret i16 %result
}

define i32 @semantic_main() {
entry:
  %input = alloca i8, align 1
  %read = call i64 @read(i32 0, i8* %input, i64 1)
  %raw = load i8, i8* %input, align 1
  %result = call i8 @accumulate(i8 %raw)
  %status = zext i8 %result to i32
  ret i32 %status
}

define i32 @main() {
entry:
  %input = alloca i8, align 1
  %read = call i64 @read(i32 0, i8* %input, i64 1)
  %raw = load i8, i8* %input, align 1
  %result = call i8 @accumulate(i8 %raw)
  %target = icmp eq i8 %result, 25
  br i1 %target, label %hit, label %miss

hit:
  ret i32 0

miss:
  ret i32 0
}

declare i64 @read(i32, i8*, i64)

; SUMMARY-LABEL: define i8 @accumulate
; SUMMARY-LABEL: entry:
; SUMMARY: %limit = and i8 %raw, 7
; SUMMARY-NEXT: %ifss.loop.delta = mul i8 %limit, 3, !symcc.ifss_loop_summary ![[STATE:[0-9]+]]
; SUMMARY-NEXT: %ifss.loop.state = add i8 10, %ifss.loop.delta, !symcc.ifss_loop_summary ![[STATE]]
; SUMMARY: br label %exit{{.*}}!symcc.ifss_loop_summary ![[CORE:[0-9]+]]
; SUMMARY-NOT: loop:
; SUMMARY-NOT: latch:
; SUMMARY-LABEL: exit:
; SUMMARY: ret i8 %ifss.loop.state
; SUMMARY-LABEL: define i16 @two_states
; SUMMARY-LABEL: entry:
; SUMMARY: %ifss.loop.trip = zext i8 %limit to i16
; SUMMARY-NEXT: %ifss.loop.delta = mul i16 %ifss.loop.trip, 2
; SUMMARY-NEXT: %ifss.loop.state = add i16 %seed, %ifss.loop.delta
; SUMMARY-NEXT: %ifss.loop.trip1 = zext i8 %limit to i16
; SUMMARY-NEXT: %ifss.loop.delta2 = mul i16 %ifss.loop.trip1, 5
; SUMMARY-NEXT: %ifss.loop.state3 = add i16 9, %ifss.loop.delta2
; SUMMARY: br label %exit
; SUMMARY-LABEL: exit:
; SUMMARY: %result = add i16 %ifss.loop.state, %ifss.loop.state3
; SUMMARY-NOT: loop:
; SUMMARY: ![[STATE]] = !{!"bounded-affine-loop-summary-v1", i64 {{-?[0-9]+}}, i64 {{-?[0-9]+}}, i64 7, i32 1, i32 0, i64 {{-?[0-9]+}}, i64 {{-?[0-9]+}}}
; SUMMARY: ![[CORE]] = !{!"bounded-affine-loop-summary-v1", i64 {{-?[0-9]+}}, i64 {{-?[0-9]+}}, i64 7, i32 1, i32 1, i64 {{-?[0-9]+}}, i64 {{-?[0-9]+}}}

; DISABLED-LABEL: define i8 @accumulate
; DISABLED: loop:
; DISABLED: %state = phi i8
; DISABLED: br i1 %continue, label %latch, label %exit
; DISABLED-NOT: bounded-affine-loop-summary-v1

; IFSS-LABEL: define i8 @accumulate
; IFSS-NOT: loop:
; IFSS: call ptr @_sym_build_mul
; IFSS: call ptr @_sym_build_add
; IFSS: !"bounded-affine-loop-summary-v1"
