; REQUIRES: qsym
; RUN: rm -f %t.manifest
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=201 SYMCC_HYDRA_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=LOWERED
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); assert r['site']==201 and r['region_schema']=='bounded-multiblock-linear-hydra-v1' and r['arm_blocks']==2 and r['multi_block'] and r['aligned_pairs']==2 and r['requires_original_coverage_replay'] and not r['requires_original_replay']"
; RUN: %python %S/../util/verify_hydra_transform_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['left_block_sites'][0]=r['right_block_sites'][0]; open(r'%t.block-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_hydra_transform_manifest.py %t.block-tamper
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['alignment'][0]['right_opcode']+=1; open(r'%t.alignment-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_hydra_transform_manifest.py %t.alignment-tamper
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['structure_fingerprint']='1'; open(r'%t.hash-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_hydra_transform_manifest.py %t.hash-tamper
; RUN: env SYMCC_HYDRA=0 %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.original.ll
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,b: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([b]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.original.ll',b)==run(r'%t.lowered.ll',b) for b in range(256))"
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=201 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: rm -f %t.unequal.manifest
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=202 SYMCC_HYDRA_MANIFEST_OUT=%t.unequal.manifest %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.unequal.ll
; RUN: %opt -passes=verify -disable-output %t.unequal.ll
; RUN: %filecheck %s --input-file=%t.unequal.ll --check-prefix=UNEQUAL
; RUN: %python %S/../util/verify_hydra_transform_manifest.py %t.unequal.manifest
; RUN: env PYTHONPATH=%S/../util %python -c "import hydra_transform as h; r=h._load_manifest(r'%t.unequal.manifest',202); assert r['record']['region_schema']=='bounded-unequal-linear-hydra-v2'"
; RUN: %python -c "import json; r=json.loads(open(r'%t.unequal.manifest').read()); assert r['region_schema']=='bounded-unequal-linear-hydra-v2' and r['left_arm_blocks']==2 and r['right_arm_blocks']==1 and r['left_instruction_count']==2 and r['right_instruction_count']==1 and r['edit_distance']==1 and r['aligned_pairs']==1 and r['unequal_arm_blocks']"
; RUN: %python -c "import json; r=json.loads(open(r'%t.unequal.manifest').read()); r['alignment'][1]['left_block_ordinal']=-1; open(r'%t.ordinal-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_hydra_transform_manifest.py %t.ordinal-tamper
; RUN: not env PYTHONPATH=%S/../util %python -c "import hydra_transform as h; h._load_manifest(r'%t.ordinal-tamper',202)"
; RUN: %python -c "import json; r=json.loads(open(r'%t.unequal.manifest').read()); r['edit_distance']=0; open(r'%t.edit-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_hydra_transform_manifest.py %t.edit-tamper
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,b: subprocess.run([lli,'--entry-function=semantic_unequal_main',p],input=bytes([b]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.original.ll',b)==run(r'%t.unequal.ll',b) for b in range(256))"
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=202 %symcc -O0 -S -emit-llvm %s -o %t.unequal.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.unequal.symbolized.ll
; RUN: %filecheck %s --input-file=%t.unequal.symbolized.ll --check-prefix=UNEQUAL-IFSS
; RUN: rm -f %t.tree.manifest
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=203 SYMCC_HYDRA_MANIFEST_OUT=%t.tree.manifest %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.tree.ll
; RUN: %opt -passes=verify -disable-output %t.tree.ll
; RUN: %filecheck %s --input-file=%t.tree.ll --check-prefix=TREE
; RUN: %python %S/../util/verify_hydra_transform_manifest.py %t.tree.manifest
; RUN: env PYTHONPATH=%S/../util %python -c "import hydra_transform as h; r=h._load_manifest(r'%t.tree.manifest',203); assert r['record']['region_schema']=='bounded-internal-tree-hydra-v3'"
; RUN: %python -c "import json; r=json.loads(open(r'%t.tree.manifest').read()); assert r['left_arm_blocks']==3 and r['right_arm_blocks']==1 and r['left_internal_branches']==1 and r['right_internal_branches']==0 and r['left_leaf_edges']==2 and r['right_leaf_edges']==1 and [x['parent_ordinal'] for x in r['left_topology']]==[-1,0,0]"
; RUN: %python -c "import json; r=json.loads(open(r'%t.tree.manifest').read()); r['left_topology'][2]['parent_ordinal']=1; open(r'%t.tree-parent-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_hydra_transform_manifest.py %t.tree-parent-tamper
; RUN: not env PYTHONPATH=%S/../util %python -c "import hydra_transform as h; h._load_manifest(r'%t.tree-parent-tamper',203)"
; RUN: %python -c "import json; r=json.loads(open(r'%t.tree.manifest').read()); r['left_leaves'][0]['successor_index']=1; open(r'%t.tree-leaf-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_hydra_transform_manifest.py %t.tree-leaf-tamper
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,b: subprocess.run([lli,'--entry-function=semantic_tree_main',p],input=bytes([b]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.original.ll',b)==run(r'%t.tree.ll',b) for b in range(256))"
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=203 %symcc -O0 -S -emit-llvm %s -o %t.tree.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.tree.symbolized.ll
; RUN: %filecheck %s --input-file=%t.tree.symbolized.ll --check-prefix=TREE-IFSS
; RUN: rm -f %t.dag.manifest
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=204 SYMCC_HYDRA_MANIFEST_OUT=%t.dag.manifest %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.dag.ll
; RUN: %opt -passes=verify -disable-output %t.dag.ll
; RUN: %filecheck %s --input-file=%t.dag.ll --check-prefix=DAG
; RUN: %python %S/../util/verify_hydra_transform_manifest.py %t.dag.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.dag.manifest').read()); assert r['region_schema']=='bounded-acyclic-sese-dag-hydra-v4' and r['left_arm_blocks']==4 and r['right_arm_blocks']==1 and r['left_local_merges']==1 and r['right_local_merges']==0 and r['left_local_phis']==1"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i32 @two_block_arms(i32 %x, i32 %y, i1 %condition) {
entry:
  br i1 %condition, label %left.first, label %right.first, !symcc.site_id !0

left.first:
  %left.add = add i32 %x, 7
  br label %left.second

left.second:
  %left.mul = mul i32 %left.add, 3
  br label %merge

right.first:
  %right.add = add i32 %y, 9
  br label %right.second

right.second:
  %right.mul = mul i32 %right.add, 5
  br label %merge

merge:
  %value = phi i32 [ %left.mul, %left.second ],
                   [ %right.mul, %right.second ]
  ret i32 %value
}

define i32 @unequal_arms(i32 %x, i32 %y, i1 %condition) {
entry:
  br i1 %condition, label %left.first, label %right, !symcc.site_id !1

left.first:
  %left.add = add i32 %x, 1
  br label %left.second

left.second:
  %left.mul = mul i32 %left.add, 2
  br label %merge

right:
  %right.add = add i32 %y, 1
  br label %merge

merge:
  %value = phi i32 [ %left.mul, %left.second ], [ %right.add, %right ]
  ret i32 %value
}

define i32 @internal_tree(
    i32 %x, i32 %y, i1 %condition, i1 %inner) {
entry:
  br i1 %condition, label %left.first, label %right, !symcc.site_id !2

left.first:
  br i1 %inner, label %left.a, label %left.b

left.a:
  %left.add = add i32 %x, 3
  br label %merge

left.b:
  %left.sub = sub i32 %x, 3
  br label %merge

right:
  %right.add = add i32 %y, 3
  br label %merge

merge:
  %value = phi i32 [ %left.add, %left.a ],
                   [ %left.sub, %left.b ],
                   [ %right.add, %right ]
  ret i32 %value
}

define i32 @internal_reconvergence(
    i32 %x, i32 %y, i1 %condition, i1 %inner) {
entry:
  br i1 %condition, label %left.first, label %right, !symcc.site_id !3

left.first:
  br i1 %inner, label %left.a, label %left.b

left.a:
  %left.add = add i32 %x, 5
  br label %left.join

left.b:
  %left.sub = sub i32 %x, 5
  br label %left.join

left.join:
  %left.value = phi i32 [ %left.add, %left.a ],
                         [ %left.sub, %left.b ]
  br label %merge

right:
  %right.add = add i32 %y, 5
  br label %merge

merge:
  %value = phi i32 [ %left.value, %left.join ],
                   [ %right.add, %right ]
  ret i32 %value
}

define i32 @semantic_main() {
entry:
  %input = alloca i8, align 1
  %read = call i64 @read(i32 0, i8* %input, i64 1)
  %raw = load i8, i8* %input, align 1
  %x = zext i8 %raw to i32
  %y = add i32 %x, 11
  %masked = and i8 %raw, 1
  %condition = icmp eq i8 %masked, 0
  %result = call i32 @two_block_arms(i32 %x, i32 %y, i1 %condition)
  ret i32 %result
}

define i32 @semantic_unequal_main() {
entry:
  %input = alloca i8, align 1
  %read = call i64 @read(i32 0, ptr %input, i64 1)
  %raw = load i8, ptr %input, align 1
  %x = zext i8 %raw to i32
  %y = add i32 %x, 11
  %masked = and i8 %raw, 1
  %condition = icmp eq i8 %masked, 0
  %result = call i32 @unequal_arms(
      i32 %x, i32 %y, i1 %condition)
  ret i32 %result
}

define i32 @semantic_tree_main() {
entry:
  %input = alloca i8, align 1
  %read = call i64 @read(i32 0, ptr %input, i64 1)
  %raw = load i8, ptr %input, align 1
  %x = zext i8 %raw to i32
  %y = add i32 %x, 17
  %outer.mask = and i8 %raw, 1
  %condition = icmp eq i8 %outer.mask, 0
  %inner.mask = and i8 %raw, 2
  %inner = icmp eq i8 %inner.mask, 0
  %result = call i32 @internal_tree(
      i32 %x, i32 %y, i1 %condition, i1 %inner)
  ret i32 %result
}

declare i64 @read(i32, i8*, i64)

; LOWERED-LABEL: define i32 @two_block_arms
; LOWERED-LABEL: entry:
; LOWERED: %hydra.operand = select i1 %condition
; LOWERED: %hydra.merged = add i32
; LOWERED: %hydra.operand{{[0-9]+}} = select i1 %condition
; LOWERED: %hydra.merged{{[0-9]+}} = mul i32
; LOWERED: br label %merge{{.*}}!symcc.hydra_region ![[REGION:[0-9]+]]
; LOWERED-NOT: left.first:
; LOWERED-NOT: left.second:
; LOWERED-NOT: right.first:
; LOWERED-NOT: right.second:
; LOWERED-LABEL: merge:
; LOWERED-NOT: phi
; LOWERED: ret i32 %hydra.merged
; LOWERED: ![[REGION]] = !{!"bounded-multiblock-linear-hydra-v1", i64 201, i32 2, i32 2, i32 2}

; IFSS-LABEL: define i32 @two_block_arms
; IFSS-NOT: left.first:
; IFSS-NOT: right.first:
; IFSS: call ptr @_sym_build_ite
; IFSS: !"bounded-multiblock-linear-hydra-v1"

; UNEQUAL-LABEL: define i32 @unequal_arms
; UNEQUAL-LABEL: entry:
; UNEQUAL: %hydra.merged = add i32
; UNEQUAL: %hydra.extra = mul i32
; UNEQUAL: %hydra.output = select i1 %condition
; UNEQUAL: br label %merge{{.*}}!symcc.hydra_region ![[UNEQUAL_REGION:[0-9]+]]
; UNEQUAL-NOT: left.first:
; UNEQUAL-NOT: left.second:
; UNEQUAL-NOT: right:
; UNEQUAL-LABEL: merge:
; UNEQUAL-NOT: phi
; UNEQUAL: ret i32 %hydra.output
; UNEQUAL: ![[UNEQUAL_REGION]] = !{!"bounded-unequal-linear-hydra-v2", i64 202, i32 2, i32 1, i32 2, i32 1}

; UNEQUAL-IFSS-LABEL: define i32 @unequal_arms
; UNEQUAL-IFSS-NOT: left.first:
; UNEQUAL-IFSS-NOT: right:
; UNEQUAL-IFSS: call ptr @_sym_build_ite
; UNEQUAL-IFSS: !"bounded-unequal-linear-hydra-v2"

; TREE-LABEL: define i32 @internal_tree
; TREE-LABEL: entry:
; TREE: %hydra.outer.not = xor i1 %condition, true
; TREE: %hydra.path = and i1 %condition, %inner
; TREE: %hydra.tree.merged = add i32
; TREE: %hydra.path.not = xor i1 %inner, true
; TREE: %hydra.tree.extra = sub i32
; TREE: %hydra.tree.output
; TREE: br label %merge{{.*}}!symcc.hydra_region ![[TREE_REGION:[0-9]+]]
; TREE-NOT: left.first:
; TREE-NOT: left.a:
; TREE-NOT: left.b:
; TREE-NOT: right:
; TREE-LABEL: merge:
; TREE-NOT: phi
; TREE: ret i32 %hydra.tree.output
; TREE: ![[TREE_REGION]] = !{!"bounded-internal-tree-hydra-v3", i64 203, i32 3, i32 1, i32 2, i32 1, i32 1, i32 0, i32 2, i32 1}

; TREE-IFSS-LABEL: define i32 @internal_tree
; TREE-IFSS-NOT: left.first:
; TREE-IFSS-NOT: right:
; TREE-IFSS: call ptr @_sym_build_ite
; TREE-IFSS: !"bounded-internal-tree-hydra-v3"

; DAG-LABEL: define i32 @internal_reconvergence
; DAG-LABEL: entry:
; DAG: %hydra.dag.phi
; DAG: br label %merge
; DAG-NOT: left.first:
; DAG-NOT: left.join:
; DAG-NOT: right:
; DAG-LABEL: merge:
; DAG-NOT: phi
; DAG: ret i32
; DAG: !"bounded-acyclic-sese-dag-hydra-v4"

!0 = !{i64 201}
!1 = !{i64 202}
!2 = !{i64 203}
!3 = !{i64 204}
