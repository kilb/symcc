; REQUIRES: qsym
; RUN: %symcc -O0 -S -emit-llvm %s -o - | FileCheck %s --check-prefix=MEMORY
; RUN: %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t-map %t.json
; RUN: printf A | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t-map SYMCC_TELEMETRY_OUT=%t.json %t
; RUN: python3 -c "import json; d=json.load(open(r'%t.json')); assert d['backsolver_targets'] >= 1; assert d['backsolver_attempts'] >= 1; assert d['backsolver_sat'] >= 1; assert d['backsolver_direct_sat'] >= 1"
; RUN: python3 -c "import glob; models=[open(p,'rb').read() for p in glob.glob(r'%t-out/*-backsolve')]; assert models and any(m and m[0:1] != b'A' for m in models)"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i32 @main() {
entry:
  %input = alloca i8, align 1
  %slot = alloca i8, align 1
  %read = call i64 @read(i32 0, i8* %input, i64 1)
  %byte = load i8, i8* %input, align 1
  %condition = icmp eq i8 %byte, 65
  br i1 %condition, label %left, label %right

left:
  store i8 10, i8* %slot, align 1
  br label %merge

right:
  store i8 20, i8* %slot, align 1
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

; MEMORY: %ifss.memory.state = call ptr @_sym_build_ite
; MEMORY-SAME: !symcc.ifss_memory
; MEMORY: ![[PROOF:[0-9]+]] = !{!"must-alias-memoryssa-v1",

