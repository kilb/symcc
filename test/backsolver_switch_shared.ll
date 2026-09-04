; REQUIRES: qsym
; RUN: rm -f %t.linear.manifest
; RUN: env SYMCC_IFSS_SWITCH_STATE=1 SYMCC_IFSS_SWITCH_MODE=linear SYMCC_IFSS_SWITCH_MANIFEST_OUT=%t.linear.manifest %opt -load-pass-plugin=%passlib -passes=ifss-switch-lowering -S %s -o %t.linear.ll
; RUN: %opt -passes=verify -disable-output %t.linear.ll
; RUN: %filecheck %s --input-file=%t.linear.ll --check-prefix=LINEAR
; RUN: %python -c "line=next(x for x in open(r'%t.linear.ll') if '%edge = phi' in x); assert line.count('[ 10,') == 3; assert line.count('%entry') == 1"
; RUN: %python %S/../util/verify_ifss_switch_manifest.py %t.linear.manifest
; RUN: rm -f %t.balanced.manifest
; RUN: env SYMCC_IFSS_SWITCH_STATE=1 SYMCC_IFSS_SWITCH_MODE=balanced SYMCC_IFSS_SWITCH_MANIFEST_OUT=%t.balanced.manifest %opt -load-pass-plugin=%passlib -passes=ifss-switch-lowering -S %s -o %t.balanced.ll
; RUN: %opt -passes=verify -disable-output %t.balanced.ll
; RUN: %filecheck %s --input-file=%t.balanced.ll --check-prefix=BALANCED
; RUN: %python -c "line=next(x for x in open(r'%t.balanced.ll') if '%edge = phi' in x); assert line.count('[ 10,') == 5; assert line.count('%entry') == 0"
; RUN: %python %S/../util/verify_ifss_switch_manifest.py %t.balanced.manifest
; RUN: %python -c "open(r'%t.profile','w').write('258258 65 1000\\n258258 66 1\\n258258 67 1\\n258258 default 1\\n')"
; RUN: env SYMCC_IFSS_SWITCH_STATE=1 SYMCC_IFSS_SWITCH_MODE=profile SYMCC_IFSS_SWITCH_PROFILE=%t.profile %opt -load-pass-plugin=%passlib -passes=ifss-switch-lowering -S %s -o %t.profile.ll
; RUN: %opt -passes=verify -disable-output %t.profile.ll
; RUN: %filecheck %s --input-file=%t.profile.ll --check-prefix=PROFILE
; RUN: env SYMCC_IFSS_SWITCH_STATE=1 SYMCC_IFSS_EXIT_STATE=1 %opt -load-pass-plugin=%passlib -passes=ifss-switch-lowering,ifss-exit-lowering -S %s -o %t.exit.ll
; RUN: %opt -passes=verify -disable-output %t.exit.ll
; RUN: %filecheck %s --input-file=%t.exit.ll --check-prefix=EXIT
; RUN: env SYMCC_IFSS_SWITCH_STATE=1 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_IFSS_SWITCH_STATE=1 %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: printf A | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t-map SYMCC_TELEMETRY_OUT=%t.json %t
; RUN: %python -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1; assert d['backsolver_attempts'] >= 1; assert d['backsolver_sat'] >= 1; assert d['backsolver_direct_attempts'] >= 1; assert d['backsolver_z3_fallbacks'] >= 1"
; RUN: %python -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert models and any(m[:1] == b'B' for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i8 @shared_switch(i8 %selector) {
entry:
  switch i8 %selector, label %shared [
    i8 65, label %shared
    i8 66, label %other
    i8 67, label %shared
  ], !symcc.site_id !0

shared:
  %edge = phi i8 [ 10, %entry ], [ 10, %entry ], [ 10, %entry ]
  br label %merge

other:
  br label %merge

merge:
  %state = phi i8 [ 10, %shared ], [ 20, %other ]
  ret i8 %state
}

define i8 @shared_memory(i8 %selector, i8* %slot) {
entry:
  switch i8 %selector, label %shared [
    i8 65, label %shared
    i8 66, label %other
    i8 67, label %shared
  ]

shared:
  store i8 10, i8* %slot, align 1
  br label %merge

other:
  store i8 20, i8* %slot, align 1
  br label %merge

merge:
  %state = load i8, i8* %slot, align 1
  ret i8 %state
}

define i8 @shared_return(i8 %selector) {
entry:
  switch i8 %selector, label %shared [
    i8 65, label %shared
    i8 66, label %other
    i8 67, label %shared
  ]
shared:
  ret i8 10
other:
  ret i8 20
}

define i32 @main() {
entry:
  %input = alloca i8, align 1
  %slot = alloca i8, align 1
  %read = call i64 @read(i32 0, i8* %input, i64 1)
  %selector = load i8, i8* %input, align 1
  %data = call i8 @shared_switch(i8 %selector)
  %state = call i8 @shared_memory(i8 %selector, i8* %slot)
  %target = icmp eq i8 %state, 20
  br i1 %target, label %hit, label %miss
hit:
  ret i32 0
miss:
  ret i32 0
}

declare i64 @read(i32, i8*, i64)

; LINEAR-LABEL: define i8 @shared_switch
; LINEAR-NOT: switch i8
; LINEAR: %ifss.switch.case = icmp eq i8 %selector, 65, !symcc.ifss_switch ![[CASE:[0-9]+]], !symcc.ifss_switch_shared ![[SUMMARY:[0-9]+]]
; LINEAR: %edge = phi i8 {{.*}}!symcc.ifss_switch_phi ![[PHI:[0-9]+]]
; LINEAR: ![[CASE]] = !{!"bounded-switch-chain-v1", i64 258258, i32 0, i32 4, !"case", i8 65}
; LINEAR: ![[SUMMARY]] = !{!"shared-switch-edge-split-v1", i64 258258, i32 4, i32 4, i32 2, i32 0, i32 3, i32 3, i32 1, i32 1, i32 1}
; LINEAR: ![[PHI]] = !{!"shared-switch-phi-multiplicity-v1", i64 258258, i32 0, i32 3, i32 3}

; BALANCED-LABEL: define i8 @shared_switch
; BALANCED: %ifss.switch.range = icmp ule i8 %selector, 65, !symcc.ifss_switch
; BALANCED-SAME: !symcc.ifss_switch_shared
; BALANCED: %edge = phi i8 {{.*}}!symcc.ifss_switch_phi
; BALANCED: !"bounded-switch-tree-v1"
; BALANCED: !"shared-switch-edge-split-v1", i64 258258, i32 4, i32 6, i32 2, i32 0, i32 3, i32 5, i32 1, i32 1, i32 1
; BALANCED: !"shared-switch-phi-multiplicity-v1", i64 258258, i32 0, i32 3, i32 5

; PROFILE-LABEL: define i8 @shared_switch
; PROFILE: %ifss.switch.range = icmp ule i8 %selector, 65
; PROFILE-SAME: !symcc.ifss_switch_shared
; PROFILE-SAME: !symcc.ifss_switch_profile
; PROFILE: !"bounded-switch-profile-tree-v1"
; PROFILE: !"shared-switch-edge-split-v1", i64 258258, i32 4, i32 6
; PROFILE: !"profile-weighted-switch-v1", i64 258258

; IFSS-LABEL: define i8 @shared_switch
; IFSS: call ptr @_sym_build_ite
; IFSS-LABEL: define i8 @shared_memory
; IFSS: call ptr @_sym_build_ite
; IFSS: !"must-alias-memoryssa-multi-v1"

; EXIT-LABEL: define i8 @shared_return
; EXIT-NOT: switch i8
; EXIT-LABEL: ifss.exit.dispatch:
; EXIT: %ifss.exit.state = phi i8 [ 10, %shared ], [ 20, %other ], !symcc.ifss_exit
; EXIT: ret i8 %ifss.exit.state, !symcc.ifss_exit

!0 = !{i64 258258}
