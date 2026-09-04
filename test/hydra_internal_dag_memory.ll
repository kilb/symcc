; REQUIRES: qsym
; RUN: rm -f %t.manifest
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=aggressive SYMCC_HYDRA_SITE=602 SYMCC_HYDRA_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=LOWERED
; RUN: %python %S/../util/verify_hydra_transform_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); assert r['region_schema']=='bounded-acyclic-sese-dag-hydra-v4' and r['mode']=='aggressive-memory' and r['left_local_merges']==1 and r['right_local_merges']==1 and r['left_local_phis']==1 and r['right_local_phis']==1 and r['readback_stores']==1 and r['requires_original_replay']"
; RUN: env SYMCC_HYDRA=0 %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.original.ll
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,b: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([b]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.original.ll',b)==run(r'%t.lowered.ll',b) for b in range(256))"
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=aggressive SYMCC_HYDRA_SITE=602 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define void @dag_store(
    ptr %pointer, i8 %left.value, i8 %right.value,
    i1 %outer, i1 %left.take, i1 %right.take) {
entry:
  br i1 %outer, label %left.root, label %right.root, !symcc.site_id !0

left.root:
  br i1 %left.take, label %left.a, label %left.b
left.a:
  %left.av = add i8 %left.value, 1
  br label %left.join
left.b:
  %left.bv = sub i8 %left.value, 2
  br label %left.join
left.join:
  %left.merged = phi i8 [ %left.av, %left.a ], [ %left.bv, %left.b ]
  store i8 %left.merged, ptr %pointer, align 1
  br label %merge

right.root:
  br i1 %right.take, label %right.a, label %right.b
right.a:
  %right.av = add i8 %right.value, 1
  br label %right.join
right.b:
  %right.bv = sub i8 %right.value, 2
  br label %right.join
right.join:
  %right.merged = phi i8 [ %right.av, %right.a ], [ %right.bv, %right.b ]
  store i8 %right.merged, ptr %pointer, align 1
  br label %merge

merge:
  ret void
}

define i32 @semantic_main() {
entry:
  %input = alloca i8, align 1
  %cell = alloca i8, align 1
  %read = call i64 @read(i32 0, ptr %input, i64 1)
  %raw = load i8, ptr %input, align 1
  store i8 91, ptr %cell, align 1
  %right.value = add i8 %raw, 17
  %b0v = and i8 %raw, 1
  %b0 = icmp ne i8 %b0v, 0
  %b1v = and i8 %raw, 2
  %b1 = icmp ne i8 %b1v, 0
  %b2v = and i8 %raw, 4
  %b2 = icmp ne i8 %b2v, 0
  call void @dag_store(
      ptr %cell, i8 %raw, i8 %right.value,
      i1 %b0, i1 %b1, i1 %b2)
  %result = load i8, ptr %cell, align 1
  %extended = zext i8 %result to i32
  ret i32 %extended
}

declare i64 @read(i32, ptr, i64)

; LOWERED-LABEL: define void @dag_store
; LOWERED-LABEL: entry:
; LOWERED: %hydra.dag.phi
; LOWERED: %hydra.dag.phi
; LOWERED: %hydra.tree.store.old = load i8, ptr %pointer
; LOWERED: %hydra.tree.store.right = select i1
; LOWERED: %hydra.tree.store.value = select i1
; LOWERED: store i8 %hydra.tree.store.value, ptr %pointer
; LOWERED: br label %merge
; LOWERED-NOT: left.root:
; LOWERED-NOT: right.root:
; LOWERED-LABEL: merge:
; LOWERED: ret void
; LOWERED: !"bounded-acyclic-sese-dag-hydra-v4"

; IFSS-LABEL: define void @dag_store
; IFSS-NOT: left.root:
; IFSS-NOT: right.root:
; IFSS: call ptr @_sym_build_ite
; IFSS: !"bounded-acyclic-sese-dag-hydra-v4"

!0 = !{i64 602}
