; REQUIRES: qsym
; RUN: rm -f %t.manifest %t.seal %t.tampered.seal
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=601 SYMCC_HYDRA_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=LOWERED
; RUN: %python %S/../util/verify_hydra_transform_manifest.py %t.manifest
; RUN: env PYTHONPATH=%S/../util %python -c "import hydra_transform as h; r=h._load_manifest(r'%t.manifest',601); assert r['record']['region_schema']=='bounded-acyclic-sese-dag-hydra-v4'"
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); assert r['left_arm_blocks']==7 and r['right_arm_blocks']==4 and r['left_internal_branches']==2 and r['right_internal_branches']==1 and r['left_local_merges']==2 and r['right_local_merges']==1 and r['left_local_phis']==2 and r['right_local_phis']==1 and r['left_leaf_edges']==1 and r['right_leaf_edges']==1 and r['output_phis']==2 and r['internal_dag'] and not r['internal_tree']"
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['left_topology'][3]['predecessors'][1]['block_ordinal']=0; open(r'%t.predecessor-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_hydra_transform_manifest.py %t.predecessor-tamper
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['left_local_phi_sites'][0]=r['right_local_phi_sites'][0]; open(r'%t.phi-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_hydra_transform_manifest.py %t.phi-tamper
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline hydra --manifest hydra=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python %S/../util/seal_transform_artifact.py verify --pipeline hydra --manifest hydra=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.seal
; RUN: %python %S/../util/replay_transform_artifact.py --pipeline hydra --manifest hydra=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.seal
; RUN: %python -c "p=open(r'%t.lowered.ll').read(); old=' = mul i32 '; assert old in p; open(r'%t.tampered.ll','w').write(p.replace(old,' = add i32 ',1))"
; RUN: %opt -passes=verify -disable-output %t.tampered.ll
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline hydra --manifest hydra=%t.manifest --input-ir %s --lowered-ir %t.tampered.ll --compiler %passlib --llvm-tool %opt --output %t.tampered.seal
; RUN: not %python %S/../util/replay_transform_artifact.py --pipeline hydra --manifest hydra=%t.manifest --input-ir %s --lowered-ir %t.tampered.ll --compiler %passlib --llvm-tool %opt --seal %t.tampered.seal
; RUN: env SYMCC_HYDRA=0 %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.original.ll
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,b: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([b]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.original.ll',b)==run(r'%t.lowered.ll',b) for b in range(256))"
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=601 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i32 @multiple_local_merges(
    i32 %x, i32 %y, i1 %outer, i1 %la, i1 %lb, i1 %ra) {
entry:
  br i1 %outer, label %left.root, label %right.root, !symcc.site_id !0

left.root:
  br i1 %la, label %left.a, label %left.b
left.a:
  %left.av = add i32 %x, 1
  br label %left.join
left.b:
  %left.bv = sub i32 %x, 2
  br label %left.join
left.join:
  %left.first = phi i32 [ %left.av, %left.a ], [ %left.bv, %left.b ]
  %left.mixed = xor i32 %left.first, 4
  br i1 %lb, label %left.c, label %left.d
left.c:
  %left.cv = add i32 %left.mixed, 8
  br label %left.join2
left.d:
  %left.dv = sub i32 %left.mixed, 16
  br label %left.join2
left.join2:
  %left.second = phi i32 [ %left.cv, %left.c ], [ %left.dv, %left.d ]
  %left.out = mul i32 %left.second, 3
  br label %merge

right.root:
  br i1 %ra, label %right.a, label %right.b
right.a:
  %right.av = add i32 %y, 1
  br label %right.join
right.b:
  %right.bv = sub i32 %y, 2
  br label %right.join
right.join:
  %right.first = phi i32 [ %right.av, %right.a ], [ %right.bv, %right.b ]
  %right.out = xor i32 %right.first, 4
  br label %merge

merge:
  %value = phi i32 [ %left.out, %left.join2 ], [ %right.out, %right.join ]
  %tag = phi i32 [ 11, %left.join2 ], [ 23, %right.join ]
  %result = add i32 %value, %tag
  ret i32 %result
}

define i32 @semantic_main() {
entry:
  %input = alloca i8, align 1
  %read = call i64 @read(i32 0, ptr %input, i64 1)
  %raw = load i8, ptr %input, align 1
  %x = zext i8 %raw to i32
  %y = add i32 %x, 19
  %b0v = and i8 %raw, 1
  %b0 = icmp ne i8 %b0v, 0
  %b1v = and i8 %raw, 2
  %b1 = icmp ne i8 %b1v, 0
  %b2v = and i8 %raw, 4
  %b2 = icmp ne i8 %b2v, 0
  %b3v = and i8 %raw, 8
  %b3 = icmp ne i8 %b3v, 0
  %result = call i32 @multiple_local_merges(
      i32 %x, i32 %y, i1 %b0, i1 %b1, i1 %b2, i1 %b3)
  ret i32 %result
}

declare i64 @read(i32, ptr, i64)

; LOWERED-LABEL: define i32 @multiple_local_merges
; LOWERED-LABEL: entry:
; LOWERED: %hydra.dag.phi
; LOWERED: %hydra.dag.phi
; LOWERED: %hydra.dag.phi
; LOWERED: br label %merge
; LOWERED-NOT: left.root:
; LOWERED-NOT: right.root:
; LOWERED-LABEL: merge:
; LOWERED-NOT: phi
; LOWERED: ret i32
; LOWERED: !"bounded-acyclic-sese-dag-hydra-v4"

; IFSS-LABEL: define i32 @multiple_local_merges
; IFSS-NOT: left.root:
; IFSS-NOT: right.root:
; IFSS: call ptr @_sym_build_ite
; IFSS: !"bounded-acyclic-sese-dag-hydra-v4"

!0 = !{i64 601}
