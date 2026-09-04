; REQUIRES: qsym
; RUN: env SYMCC_IFSS_SWITCH_STATE=1 %opt -load-pass-plugin=%passlib -passes=ifss-switch-lowering -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=SWITCH
; RUN: env SYMCC_IFSS_SWITCH_STATE=0 %opt -load-pass-plugin=%passlib -passes=ifss-switch-lowering -S %s -o %t.disabled.ll
; RUN: %filecheck %s --input-file=%t.disabled.ll --check-prefix=DISABLED
; RUN: env SYMCC_IFSS_SWITCH_STATE=1 SYMCC_IFSS_EXIT_STATE=1 %opt -load-pass-plugin=%passlib -passes=ifss-switch-lowering,ifss-exit-lowering -S %s -o %t.interaction.ll
; RUN: %opt -passes=verify -disable-output %t.interaction.ll
; RUN: %filecheck %s --input-file=%t.interaction.ll --check-prefix=INTERACTION
; RUN: env SYMCC_IFSS_SWITCH_STATE=1 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: %python -c "text=open(r'%t.symbolized.ll').read(); assert text.count('!\"partition-condition-cache-v1\"') == 2"
; RUN: env SYMCC_IFSS_SWITCH_STATE=1 %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: printf A | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t-map SYMCC_TELEMETRY_OUT=%t.json %t
; RUN: %python -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1; assert d['backsolver_attempts'] >= 1; assert d['backsolver_sat'] >= 1; assert d['backsolver_direct_attempts'] >= 1; assert d['backsolver_direct_sat'] >= 1"
; RUN: %python -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert models and any(m[:1] == b'B' for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @data_switch(i8 %selector) {
entry:
  switch i8 %selector, label %default [
    i8 65, label %case_a
    i8 66, label %case_b
  ]

case_a:
  br label %merge

case_b:
  br label %merge

default:
  br label %merge

merge:
  %state = phi i8 [ 10, %case_a ], [ 20, %case_b ], [ 30, %default ]
  ret i8 %state
}

define i8 @return_switch(i8 %selector) {
entry:
  switch i8 %selector, label %default [
    i8 65, label %case_a
    i8 66, label %case_b
  ]
case_a:
  ret i8 10
case_b:
  ret i8 20
default:
  ret i8 30
}

define i32 @main() {
entry:
  %input = alloca i8, align 1
  %slot = alloca i8, align 1
  %read = call i64 @read(i32 0, i8* %input, i64 1)
  %selector = load i8, i8* %input, align 1
  switch i8 %selector, label %default [
    i8 65, label %case_a
    i8 66, label %case_b
  ]

case_a:
  store i8 10, i8* %slot, align 1
  br label %merge

case_b:
  store i8 20, i8* %slot, align 1
  br label %merge

default:
  store i8 30, i8* %slot, align 1
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

; SWITCH-LABEL: define i8 @data_switch
; SWITCH-NOT: switch i8
; SWITCH: %ifss.switch.case = icmp eq i8 %selector, 65, !symcc.ifss_switch ![[CASE0:[0-9]+]]
; SWITCH: br i1 %ifss.switch.case, label %case_a, label %ifss.switch.test, !symcc.ifss_switch ![[BRANCH0:[0-9]+]]
; SWITCH-LABEL: ifss.switch.test:
; SWITCH: %ifss.switch.case1 = icmp eq i8 %selector, 66, !symcc.ifss_switch ![[CASE1:[0-9]+]]
; SWITCH: br i1 %ifss.switch.case1, label %case_b, label %default, !symcc.ifss_switch ![[BRANCH1:[0-9]+]], !symcc.ifss_switch_default ![[DEFAULT:[0-9]+]]
; SWITCH: ![[CASE0]] = !{!"bounded-switch-chain-v1", i64 {{-?[0-9]+}}, i32 0, i32 3, !"case", i8 65}
; SWITCH: ![[CASE1]] = !{!"bounded-switch-chain-v1", i64 {{-?[0-9]+}}, i32 1, i32 3, !"case", i8 66}
; SWITCH: ![[DEFAULT]] = !{!"bounded-switch-chain-v1", i64 {{-?[0-9]+}}, i32 2, i32 3, !"default"}

; IFSS-LABEL: define i8 @data_switch
; IFSS: call ptr @_sym_build_ite
; IFSS-LABEL: define i32 @main
; IFSS: !symcc.ifss_memory
; IFSS: !"must-alias-memoryssa-multi-v1"

; DISABLED-LABEL: define i8 @data_switch
; DISABLED: switch i8 %selector
; DISABLED-NOT: bounded-switch-chain-v1

; INTERACTION-LABEL: define i8 @return_switch
; INTERACTION-NOT: switch i8
; INTERACTION: br i1
; INTERACTION-LABEL: ifss.exit.dispatch:
; INTERACTION: %ifss.exit.state = phi i8 [ 10, %case_a ], [ 20, %case_b ], [ 30, %default ], !symcc.ifss_exit
; INTERACTION: ret i8 %ifss.exit.state, !symcc.ifss_exit
