; REQUIRES: qsym
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=LOWERED
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=0 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering -S %s -o %t.disabled.ll
; RUN: %filecheck %s --input-file=%t.disabled.ll --check-prefix=DISABLED
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: printf C | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t-map SYMCC_TELEMETRY_OUT=%t.json %t
; RUN: %python -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1; assert d['backsolver_attempts'] >= 1; assert d['backsolver_sat'] >= 1"
; RUN: %python -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert models and any(m[:1] == b'B' for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @choose(i8 %value) {
entry:
  %is_a = icmp eq i8 %value, 65
  br i1 %is_a, label %left, label %rest

left:
  %left_value = add i8 %value, 10
  br label %continuation_a

rest:
  %is_b = icmp eq i8 %value, 66
  br i1 %is_b, label %middle, label %deep

middle:
  %middle_value = add i8 %value, 20
  br label %continuation_b

deep:
  %deep_value = add i8 %value, 30
  br label %continuation_a

continuation_a:
  %a = phi i8 [ %left_value, %left ], [ %deep_value, %deep ]
  %a_result = and i8 %a, 1
  br label %final

continuation_b:
  %b = phi i8 [ %middle_value, %middle ]
  %b_result = add i8 %b, 2
  br label %final

final:
  %result = phi i8 [ %a_result, %continuation_a ],
                   [ %b_result, %continuation_b ]
  ret i8 %result
}

define i32 @main() {
entry:
  %input = alloca i8, align 1
  %read = call i64 @read(i32 0, i8* %input, i64 1)
  %value = load i8, i8* %input, align 1
  %choice = call i8 @choose(i8 %value)
  %target = icmp eq i8 %choice, 88
  br i1 %target, label %hit, label %miss

hit:
  ret i32 0

miss:
  ret i32 0
}

declare i64 @read(i32, i8*, i64)

; LOWERED-LABEL: define i8 @choose
; LOWERED-LABEL: left:
; LOWERED: br label %ifss.cont.capture
; LOWERED-LABEL: middle:
; LOWERED: br label %ifss.cont.capture{{[0-9]*}}
; LOWERED-LABEL: deep:
; LOWERED: br label %ifss.cont.capture{{[0-9]*}}
; LOWERED-LABEL: ifss.cont.capture:
; LOWERED: br label %ifss.cont.dispatch, !symcc.ifss_continuation_exit ![[EXIT0:[0-9]+]]
; LOWERED-LABEL: ifss.cont.capture{{[0-9]+}}:
; LOWERED: br label %ifss.cont.dispatch, !symcc.ifss_continuation_exit ![[EXIT1:[0-9]+]]
; LOWERED-LABEL: ifss.cont.capture{{[0-9]+}}:
; LOWERED: br label %ifss.cont.dispatch, !symcc.ifss_continuation_exit ![[EXIT2:[0-9]+]]
; LOWERED-LABEL: ifss.cont.dispatch:
; LOWERED: %ifss.cont.exit_id = phi i8 [ 0, %ifss.cont.capture ], [ 1, %ifss.cont.capture{{[0-9]*}} ], [ 2, %ifss.cont.capture{{[0-9]*}} ], !symcc.ifss_continuation ![[SUMMARY:[0-9]+]]
; LOWERED: %ifss.cont.liveout = phi i8 [ %left_value, %ifss.cont.capture ], [ 0, %ifss.cont.capture{{[0-9]*}} ], [ %deep_value, %ifss.cont.capture{{[0-9]*}} ], !symcc.ifss_continuation_liveout ![[SLOT0:[0-9]+]]
; LOWERED: %ifss.cont.liveout{{[0-9]+}} = phi i8 [ 0, %ifss.cont.capture ], [ %middle_value, %ifss.cont.capture{{[0-9]*}} ], [ 0, %ifss.cont.capture{{[0-9]*}} ], !symcc.ifss_continuation_liveout ![[SLOT1:[0-9]+]]
; LOWERED: %ifss.cont.matches = icmp eq i8 %ifss.cont.exit_id, 0, !symcc.ifss_continuation ![[SUMMARY]], !symcc.ifss_force_partition ![[SUMMARY]]
; LOWERED: br i1 %ifss.cont.matches, label %ifss.cont.resume, label %ifss.cont.test, !symcc.ifss_continuation ![[SUMMARY]], !symcc.ifss_force_partition ![[SUMMARY]]
; LOWERED-LABEL: ifss.cont.test:
; LOWERED: %ifss.cont.matches{{[0-9]+}} = icmp eq i8 %ifss.cont.exit_id, 1
; LOWERED: br i1 %ifss.cont.matches{{[0-9]+}}, label %ifss.cont.resume{{[0-9]*}}, label %ifss.cont.resume{{[0-9]*}}
; LOWERED-LABEL: continuation_a:
; LOWERED: %a = phi i8 [ %ifss.cont.liveout, %ifss.cont.resume ], [ %ifss.cont.liveout, %ifss.cont.resume{{[0-9]*}} ]{{.*}}!symcc.ifss_continuation_liveout ![[SLOT0]]
; LOWERED-LABEL: continuation_b:
; LOWERED: %b = phi i8 [ %ifss.cont.liveout{{[0-9]+}}, %ifss.cont.resume{{[0-9]*}} ]{{.*}}!symcc.ifss_continuation_liveout ![[SLOT1]]
; LOWERED: ![[SUMMARY]] = !{!"bounded-continuation-tuple-v1", i64 {{-?[0-9]+}}, i32 3, i32 2, i32 2, i32 4, i32 3}
; LOWERED: ![[EXIT0]] = !{!"bounded-continuation-tuple-v1", i64 {{-?[0-9]+}}, i32 0, i64 {{-?[0-9]+}}, i32 0, i32 0}
; LOWERED: ![[EXIT1]] = !{!"bounded-continuation-tuple-v1", i64 {{-?[0-9]+}}, i32 1, i64 {{-?[0-9]+}}, i32 0, i32 1}
; LOWERED: ![[EXIT2]] = !{!"bounded-continuation-tuple-v1", i64 {{-?[0-9]+}}, i32 2, i64 {{-?[0-9]+}}, i32 0, i32 0}
; LOWERED: ![[SLOT0]] = !{!"bounded-continuation-tuple-v1", i64 {{-?[0-9]+}}, i32 0, i32 0, i64 {{-?[0-9]+}}}
; LOWERED: ![[SLOT1]] = !{!"bounded-continuation-tuple-v1", i64 {{-?[0-9]+}}, i32 1, i32 1, i64 {{-?[0-9]+}}}

; IFSS-LABEL: define i8 @choose
; IFSS: %ifss.cont.exit_id = phi i8
; IFSS: %ifss.cont.liveout = phi i8
; IFSS-COUNT-6: call ptr @_sym_build_ite
; IFSS: %ifss.cont.matches = icmp eq i8 %ifss.cont.exit_id, 0
; IFSS: !"bounded-continuation-tuple-v1"
; IFSS: !"partition-condition-cache-v1"

; DISABLED-LABEL: define i8 @choose
; DISABLED-NOT: ifss.cont.
; DISABLED-NOT: bounded-continuation-tuple-v1
