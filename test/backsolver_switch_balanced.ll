; REQUIRES: qsym
; RUN: env SYMCC_IFSS_SWITCH_STATE=1 SYMCC_IFSS_SWITCH_MODE=linear %opt -load-pass-plugin=%passlib -passes=ifss-switch-lowering -S %s -o %t.linear.ll
; RUN: %opt -passes=verify -disable-output %t.linear.ll
; RUN: %filecheck %s --input-file=%t.linear.ll --check-prefix=LINEAR
; RUN: %python -c "line=next(x for x in open(r'%t.linear.ll') if 'default.edge = phi' in x); assert line.count('[ 99,') == 1"
; RUN: env SYMCC_IFSS_SWITCH_STATE=1 SYMCC_IFSS_SWITCH_MODE=balanced %opt -load-pass-plugin=%passlib -passes=ifss-switch-lowering -S %s -o %t.tree.ll
; RUN: %opt -passes=verify -disable-output %t.tree.ll
; RUN: %filecheck %s --input-file=%t.tree.ll --check-prefix=TREE
; RUN: %python -c "text=open(r'%t.tree.ll').read(); assert text.count('icmp ule i8') == 6; assert text.count('icmp eq i8') == 8"
; RUN: %python -c "line=next(x for x in open(r'%t.tree.ll') if 'default.edge = phi' in x); assert line.count('[ 99,') == 7"
; RUN: env SYMCC_IFSS_SWITCH_STATE=1 SYMCC_IFSS_SWITCH_MODE=balanced %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: %python -c "text=open(r'%t.symbolized.ll').read(); assert text.count('!\"partition-condition-cache-v1\"') == 12"
; RUN: env SYMCC_IFSS_SWITCH_STATE=1 SYMCC_IFSS_SWITCH_MODE=balanced %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: printf '\000' | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t-map SYMCC_TELEMETRY_OUT=%t.json %t
; RUN: %python -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1; assert d['backsolver_attempts'] >= 1; assert d['backsolver_sat'] >= 1; assert d['backsolver_direct_attempts'] >= 1; assert d['backsolver_direct_sat'] >= 1"
; RUN: %python -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert models and any(m[:1] == b'\\x06' for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i32 @main() {
entry:
  %input = alloca i8, align 1
  %read = call i64 @read(i32 0, i8* %input, i64 1)
  %selector = load i8, i8* %input, align 1
  switch i8 %selector, label %default [
    i8 0, label %case0
    i8 1, label %case1
    i8 2, label %case2
    i8 3, label %case3
    i8 4, label %case4
    i8 5, label %case5
    i8 6, label %case6
  ]

case0:
  br label %merge
case1:
  br label %merge
case2:
  br label %merge
case3:
  br label %merge
case4:
  br label %merge
case5:
  br label %merge
case6:
  br label %merge
default:
  %default.edge = phi i8 [ 99, %entry ]
  br label %merge

merge:
  %state = phi i8 [ 10, %case0 ], [ 20, %case1 ], [ 30, %case2 ],
                  [ 40, %case3 ], [ 50, %case4 ], [ 60, %case5 ],
                  [ 70, %case6 ], [ 80, %default ]
  %target = icmp eq i8 %state, 70
  br i1 %target, label %hit, label %miss
hit:
  ret i32 0
miss:
  ret i32 0
}

declare i64 @read(i32, i8*, i64)

; LINEAR-LABEL: define i32 @main
; LINEAR-NOT: icmp ule i8
; LINEAR-COUNT-7: icmp eq i8 %selector
; LINEAR: !"bounded-switch-chain-v1"

; TREE-LABEL: define i32 @main
; TREE: %ifss.switch.range = icmp ule i8 %selector, 2
; TREE: !"bounded-switch-tree-v1"
; TREE-NOT: !"bounded-switch-chain-v1"

; IFSS-LABEL: define i32 @main
; IFSS-COUNT-7: call ptr @_sym_build_ite
