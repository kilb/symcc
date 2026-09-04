; REQUIRES: qsym
; RUN: rm -f %t.manifest
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=401 SYMCC_HYDRA_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=LOWERED
; RUN: %python %S/../util/verify_hydra_transform_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); assert r['region_schema']=='bounded-internal-tree-hydra-v3' and r['left_arm_blocks']==7 and r['right_arm_blocks']==7 and r['left_internal_branches']==3 and r['right_internal_branches']==3 and r['left_leaf_edges']==4 and r['right_leaf_edges']==4 and r['aligned_pairs']==4 and r['edit_distance']==0 and not r['unequal_arm_blocks']"
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['right_topology'][4]['successors'][1]['ordinal']=5; open(r'%t.edge-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/verify_hydra_transform_manifest.py %t.edge-tamper
; RUN: rm -f %t.unified.seal %t.lowered-tamper.seal
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline hydra --manifest hydra=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.unified.seal
; RUN: %python %S/../util/seal_transform_artifact.py verify --pipeline hydra --manifest hydra=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.unified.seal
; RUN: %python %S/../util/replay_transform_artifact.py --pipeline hydra --manifest hydra=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.unified.seal
; RUN: not %python %S/../util/seal_transform_artifact.py seal --pipeline hydra --manifest hydra=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.unified.seal
; RUN: %python -c "import json; r=json.load(open(r'%t.unified.seal')); r['pipeline']='loop'; open(r'%t.unified-seal-tamper','w').write(json.dumps(r)+'\n')"
; RUN: not %python %S/../util/seal_transform_artifact.py verify --pipeline hydra --manifest hydra=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.unified-seal-tamper
; RUN: %python -c "p=open(r'%t.lowered.ll').read(); old=' = add i32 '; assert p.count(old)>=1; open(r'%t.lowered-tamper.ll','w').write(p.replace(old,' = sub i32 ',1))"
; RUN: %opt -passes=verify -disable-output %t.lowered-tamper.ll
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline hydra --manifest hydra=%t.manifest --input-ir %s --lowered-ir %t.lowered-tamper.ll --compiler %passlib --llvm-tool %opt --output %t.lowered-tamper.seal
; RUN: not %python %S/../util/replay_transform_artifact.py --pipeline hydra --manifest hydra=%t.manifest --input-ir %s --lowered-ir %t.lowered-tamper.ll --compiler %passlib --llvm-tool %opt --seal %t.lowered-tamper.seal
; RUN: env SYMCC_HYDRA=0 %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.original.ll
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,b: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([b]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.original.ll',b)==run(r'%t.lowered.ll',b) for b in range(256))"
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=401 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=402 %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.rejected.ll
; RUN: %filecheck %s --input-file=%t.rejected.ll --check-prefix=REJECT
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=403 %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.empty-rejected.ll
; RUN: %filecheck %s --input-file=%t.empty-rejected.ll --check-prefix=EMPTY-REJECT

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i32 @max_tree(
    i32 %x, i32 %y, i1 %outer,
    i1 %la, i1 %lb, i1 %lc,
    i1 %ra, i1 %rb, i1 %rc) {
entry:
  br i1 %outer, label %left.root, label %right.root, !symcc.site_id !0

left.root:
  br i1 %la, label %left.0, label %left.1
left.0:
  br i1 %lb, label %left.00, label %left.01
left.00:
  %l00 = add i32 %x, 1
  br label %merge
left.01:
  %l01 = sub i32 %x, 2
  br label %merge
left.1:
  br i1 %lc, label %left.10, label %left.11
left.10:
  %l10 = xor i32 %x, 4
  br label %merge
left.11:
  %l11 = or i32 %x, 8
  br label %merge

right.root:
  br i1 %ra, label %right.0, label %right.1
right.0:
  br i1 %rb, label %right.00, label %right.01
right.00:
  %r00 = add i32 %y, 3
  br label %merge
right.01:
  %r01 = sub i32 %y, 5
  br label %merge
right.1:
  br i1 %rc, label %right.10, label %right.11
right.10:
  %r10 = xor i32 %y, 7
  br label %merge
right.11:
  %r11 = or i32 %y, 16
  br label %merge

merge:
  %value = phi i32 [ %l00, %left.00 ],
                   [ %l01, %left.01 ],
                   [ %l10, %left.10 ],
                   [ %l11, %left.11 ],
                   [ %r00, %right.00 ],
                   [ %r01, %right.01 ],
                   [ %r10, %right.10 ],
                   [ %r11, %right.11 ]
  ret i32 %value
}

define i32 @four_internal_branches(
    i32 %x, i32 %y, i1 %outer,
    i1 %a, i1 %b, i1 %c, i1 %d) {
entry:
  br i1 %outer, label %left.0, label %right, !symcc.site_id !1
left.0:
  br i1 %a, label %merge, label %left.1
left.1:
  br i1 %b, label %merge, label %left.2
left.2:
  br i1 %c, label %merge, label %left.3
left.3:
  br i1 %d, label %merge, label %left.leaf
left.leaf:
  %left.value = add i32 %x, 1
  br label %merge
right:
  %right.value = add i32 %y, 1
  br label %merge
merge:
  %value = phi i32 [ %x, %left.0 ],
                   [ %x, %left.1 ],
                   [ %x, %left.2 ],
                   [ %x, %left.3 ],
                   [ %left.value, %left.leaf ],
                   [ %right.value, %right ]
  ret i32 %value
}

define i32 @empty_tree(i1 %outer, i1 %inner) {
entry:
  br i1 %outer, label %left.root, label %right, !symcc.site_id !2
left.root:
  br i1 %inner, label %left.a, label %left.b
left.a:
  br label %merge
left.b:
  br label %merge
right:
  br label %merge
merge:
  %value = phi i32 [ 1, %left.a ], [ 2, %left.b ], [ 3, %right ]
  ret i32 %value
}

define i32 @semantic_main() {
entry:
  %input = alloca i8, align 1
  %read = call i64 @read(i32 0, ptr %input, i64 1)
  %raw = load i8, ptr %input, align 1
  %x = zext i8 %raw to i32
  %y = add i32 %x, 13
  %b0v = and i8 %raw, 1
  %b0 = icmp ne i8 %b0v, 0
  %b1v = and i8 %raw, 2
  %b1 = icmp ne i8 %b1v, 0
  %b2v = and i8 %raw, 4
  %b2 = icmp ne i8 %b2v, 0
  %b3v = and i8 %raw, 8
  %b3 = icmp ne i8 %b3v, 0
  %b4v = and i8 %raw, 16
  %b4 = icmp ne i8 %b4v, 0
  %b5v = and i8 %raw, 32
  %b5 = icmp ne i8 %b5v, 0
  %b6v = and i8 %raw, 64
  %b6 = icmp ne i8 %b6v, 0
  %result = call i32 @max_tree(
      i32 %x, i32 %y, i1 %b0,
      i1 %b1, i1 %b2, i1 %b3,
      i1 %b4, i1 %b5, i1 %b6)
  ret i32 %result
}

declare i64 @read(i32, ptr, i64)

; LOWERED-LABEL: define i32 @max_tree
; LOWERED-LABEL: entry:
; LOWERED: %hydra.outer.not = xor i1 %outer, true
; LOWERED: %hydra.path = and i1 %outer, %la
; LOWERED: %hydra.path{{[0-9]+}} = and i1 %hydra.path, %lb
; LOWERED: %hydra.tree.merged = add i32
; LOWERED: %hydra.tree.merged{{[0-9]+}} = sub i32
; LOWERED: %hydra.tree.merged{{[0-9]+}} = xor i32
; LOWERED: %hydra.tree.merged{{[0-9]+}} = or i32
; LOWERED: br label %merge
; LOWERED-NOT: left.root:
; LOWERED-NOT: right.root:
; LOWERED-LABEL: merge:
; LOWERED-NOT: phi
; LOWERED: ret i32 %hydra.tree.output
; LOWERED: !"bounded-internal-tree-hydra-v3"

; IFSS-LABEL: define i32 @max_tree
; IFSS-NOT: left.root:
; IFSS-NOT: right.root:
; IFSS: call ptr @_sym_build_ite
; IFSS: !"bounded-internal-tree-hydra-v3"

; REJECT-LABEL: define i32 @four_internal_branches
; REJECT: br i1 %outer, label %left.0, label %right
; REJECT-LABEL: left.3:
; REJECT: br i1 %d, label %merge, label %left.leaf
; REJECT-LABEL: merge:
; REJECT: phi i32

; EMPTY-REJECT-LABEL: define i32 @empty_tree
; EMPTY-REJECT: br i1 %outer, label %left.root, label %right
; EMPTY-REJECT-LABEL: left.root:
; EMPTY-REJECT: br i1 %inner, label %left.a, label %left.b
; EMPTY-REJECT-LABEL: merge:
; EMPTY-REJECT: phi i32

!0 = !{i64 401}
!1 = !{i64 402}
!2 = !{i64 403}
