; REQUIRES: qsym
; RUN: rm -f %t.manifest %t.nophi.manifest %t.seal %t.tampered.seal
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=606 SYMCC_HYDRA_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=LOWERED
; RUN: %python %S/../util/verify_hydra_transform_manifest.py %t.manifest
; RUN: env PYTHONPATH=%S/../util %python -c "import hydra_transform as h; r=h._load_manifest(r'%t.manifest',606); assert r['record']['region_schema']=='bounded-shared-predicate-sese-dag-hydra-v5'"
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); assert r['left_arm_blocks']==13 and r['right_arm_blocks']==1 and r['left_internal_branches']==4 and r['left_local_merges']==4 and r['left_local_phis']==4 and r['canonical_guard_edges']==18 and r['unique_predicates']==3 and r['reused_predicate_occurrences']==1 and r['reused_guard_edges']>=4 and r['reused_predicate_negations']>=1 and r['shared_predicate_dag']"
; RUN: %python -c "p=open(r'%t.lowered.ll').read(); assert p.count(' = xor i1 ')==4"
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['canonical_guard_edges']+=1; open(r'%t.edge-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_hydra_transform_manifest.py %t.edge-tamper
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['left_predicate_sites'][2]=r['left_predicate_sites'][1]; open(r'%t.predicate-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_hydra_transform_manifest.py %t.predicate-tamper
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=607 SYMCC_HYDRA_MANIFEST_OUT=%t.nophi.manifest %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.nophi.ll
; RUN: %opt -passes=verify -disable-output %t.nophi.ll
; RUN: %python %S/../util/verify_hydra_transform_manifest.py %t.nophi.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.nophi.manifest').read()); assert r['region_schema']=='bounded-shared-predicate-sese-dag-hydra-v5' and r['left_local_merges']==4 and r['left_local_phis']==0 and r['reused_guard_edges']==0 and r['reused_predicate_occurrences']==1"
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline hydra --manifest hydra=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python %S/../util/replay_transform_artifact.py --pipeline hydra --manifest hydra=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.seal
; RUN: %python -c "p=open(r'%t.lowered.ll').read(); old=' = mul i32 '; assert old in p; open(r'%t.tampered.ll','w').write(p.replace(old,' = add i32 ',1))"
; RUN: %opt -passes=verify -disable-output %t.tampered.ll
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline hydra --manifest hydra=%t.manifest --input-ir %s --lowered-ir %t.tampered.ll --compiler %passlib --llvm-tool %opt --output %t.tampered.seal
; RUN: not %python %S/../util/replay_transform_artifact.py --pipeline hydra --manifest hydra=%t.manifest --input-ir %s --lowered-ir %t.tampered.ll --compiler %passlib --llvm-tool %opt --seal %t.tampered.seal
; RUN: env SYMCC_HYDRA=0 %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.original.ll
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,b: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([b]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.original.ll',b)==run(r'%t.lowered.ll',b) for b in range(256))"
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=606 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i32 @shared_predicate_dag(
    i32 %x, i1 %outer, i1 %shared, i1 %middle, i1 %last) {
entry:
  br i1 %outer, label %left.0, label %right, !symcc.site_id !0

left.0:
  br i1 %shared, label %left.0a, label %left.0b
left.0a:
  %v0a = add i32 %x, 1
  br label %left.1
left.0b:
  %v0b = sub i32 %x, 2
  br label %left.1
left.1:
  %p1 = phi i32 [ %v0a, %left.0a ], [ %v0b, %left.0b ]
  br i1 %middle, label %left.1a, label %left.1b
left.1a:
  %v1a = xor i32 %p1, 4
  br label %left.2
left.1b:
  %v1b = add i32 %p1, 8
  br label %left.2
left.2:
  %p2 = phi i32 [ %v1a, %left.1a ], [ %v1b, %left.1b ]
  br i1 %shared, label %left.2a, label %left.2b
left.2a:
  %v2a = mul i32 %p2, 3
  br label %left.3
left.2b:
  %v2b = sub i32 %p2, 16
  br label %left.3
left.3:
  %p3 = phi i32 [ %v2a, %left.2a ], [ %v2b, %left.2b ]
  br i1 %last, label %left.3a, label %left.3b
left.3a:
  %v3a = add i32 %p3, 32
  br label %left.out
left.3b:
  %v3b = xor i32 %p3, 64
  br label %left.out
left.out:
  %p4 = phi i32 [ %v3a, %left.3a ], [ %v3b, %left.3b ]
  br label %merge

right:
  %right.value = add i32 %x, 17
  br label %merge

merge:
  %value = phi i32 [ %p4, %left.out ], [ %right.value, %right ]
  ret i32 %value
}

define i32 @semantic_main() {
entry:
  %input = alloca i8, align 1
  %read = call i64 @read(i32 0, ptr %input, i64 1)
  %raw = load i8, ptr %input, align 1
  %x = zext i8 %raw to i32
  %b0v = and i8 %raw, 1
  %b0 = icmp ne i8 %b0v, 0
  %b1v = and i8 %raw, 2
  %b1 = icmp ne i8 %b1v, 0
  %b2v = and i8 %raw, 4
  %b2 = icmp ne i8 %b2v, 0
  %b3v = and i8 %raw, 8
  %b3 = icmp ne i8 %b3v, 0
  %result = call i32 @shared_predicate_dag(
      i32 %x, i1 %b0, i1 %b1, i1 %b2, i1 %b3)
  ret i32 %result
}

declare i64 @read(i32, ptr, i64)

define i32 @shared_predicate_dag_without_local_phi(
    i32 %x, i1 %outer, i1 %shared, i1 %middle, i1 %last) {
entry:
  br i1 %outer, label %left.n0, label %right.n, !symcc.site_id !1
left.n0:
  br i1 %shared, label %left.n0a, label %left.n0b
left.n0a:
  br label %left.n1
left.n0b:
  br label %left.n1
left.n1:
  br i1 %middle, label %left.n1a, label %left.n1b
left.n1a:
  br label %left.n2
left.n1b:
  br label %left.n2
left.n2:
  br i1 %shared, label %left.n2a, label %left.n2b
left.n2a:
  br label %left.n3
left.n2b:
  br label %left.n3
left.n3:
  br i1 %last, label %left.n3a, label %left.n3b
left.n3a:
  br label %left.nout
left.n3b:
  br label %left.nout
left.nout:
  %left.nvalue = add i32 %x, 33
  br label %merge.n
right.n:
  %right.nvalue = sub i32 %x, 9
  br label %merge.n
merge.n:
  %value.n = phi i32 [ %left.nvalue, %left.nout ],
                       [ %right.nvalue, %right.n ]
  ret i32 %value.n
}

; LOWERED-LABEL: define i32 @shared_predicate_dag
; LOWERED-LABEL: entry:
; LOWERED: %hydra.dag.phi
; LOWERED: %hydra.dag.phi
; LOWERED: %hydra.dag.phi
; LOWERED: %hydra.dag.phi
; LOWERED: br label %merge
; LOWERED-NOT: left.0:
; LOWERED-LABEL: merge:
; LOWERED-NOT: phi
; LOWERED: ret i32
; LOWERED: !"bounded-shared-predicate-sese-dag-hydra-v5"

; IFSS-LABEL: define i32 @shared_predicate_dag
; IFSS-NOT: left.0:
; IFSS: call ptr @_sym_build_ite
; IFSS: !"bounded-shared-predicate-sese-dag-hydra-v5"

!0 = !{i64 606}
!1 = !{i64 607}
