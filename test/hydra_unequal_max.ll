; REQUIRES: qsym
; RUN: rm -f %t.manifest
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=301 SYMCC_HYDRA_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=LOWERED
; RUN: %python %S/../util/verify_hydra_transform_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); a=r['alignment']; assert r['left_arm_blocks']==1 and r['right_arm_blocks']==4 and r['left_instruction_count']==4 and r['right_instruction_count']==4 and r['edit_distance']==0 and [s['right_block_ordinal'] for s in a]==[0,1,2,3] and all(s['left_block_ordinal']==0 for s in a)"
; RUN: env SYMCC_HYDRA=0 %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.original.ll
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,b: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([b]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.original.ll',b)==run(r'%t.lowered.ll',b) for b in range(256))"
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=301 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=302 %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.rejected.ll
; RUN: %filecheck %s --input-file=%t.rejected.ll --check-prefix=REJECT

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i32 @one_by_four(i32 %x, i32 %y, i1 %condition) {
entry:
  br i1 %condition, label %left, label %right.0, !symcc.site_id !0

left:
  %left.add = add i32 %x, 7
  %left.mul = mul i32 %left.add, 3
  %left.xor = xor i32 %left.mul, 9
  %left.sub = sub i32 %left.xor, 5
  br label %merge

right.0:
  %right.add = add i32 %y, 11
  br label %right.1

right.1:
  %right.mul = mul i32 %right.add, 13
  br label %right.2

right.2:
  %right.xor = xor i32 %right.mul, 17
  br label %right.3

right.3:
  %right.sub = sub i32 %right.xor, 19
  br label %merge

merge:
  %value = phi i32 [ %left.sub, %left ],
                   [ %right.sub, %right.3 ]
  ret i32 %value
}

define i32 @one_by_five(i32 %x, i32 %y, i1 %condition) {
entry:
  br i1 %condition, label %left, label %right.0, !symcc.site_id !1
left:
  %left.add = add i32 %x, 1
  br label %merge
right.0:
  %right.add = add i32 %y, 1
  br label %right.1
right.1:
  %right.mul = mul i32 %right.add, 2
  br label %right.2
right.2:
  %right.xor = xor i32 %right.mul, 3
  br label %right.3
right.3:
  %right.sub = sub i32 %right.xor, 4
  br label %right.4
right.4:
  %right.or = or i32 %right.sub, 5
  br label %merge
merge:
  %value = phi i32 [ %left.add, %left ],
                   [ %right.or, %right.4 ]
  ret i32 %value
}

define i32 @semantic_main() {
entry:
  %input = alloca i8, align 1
  %read = call i64 @read(i32 0, ptr %input, i64 1)
  %raw = load i8, ptr %input, align 1
  %x = zext i8 %raw to i32
  %y = add i32 %x, 23
  %bit = and i8 %raw, 1
  %condition = icmp eq i8 %bit, 0
  %result = call i32 @one_by_four(
      i32 %x, i32 %y, i1 %condition)
  ret i32 %result
}

declare i64 @read(i32, ptr, i64)

; LOWERED-LABEL: define i32 @one_by_four
; LOWERED-LABEL: entry:
; LOWERED: %hydra.merged = add i32
; LOWERED: %hydra.merged{{[0-9]+}} = mul i32
; LOWERED: %hydra.merged{{[0-9]+}} = xor i32
; LOWERED: %hydra.merged{{[0-9]+}} = sub i32
; LOWERED: br label %merge
; LOWERED-NOT: left:
; LOWERED-NOT: right.0:
; LOWERED-LABEL: merge:
; LOWERED-NOT: phi
; LOWERED: ret i32 %hydra.merged
; LOWERED: !"bounded-unequal-linear-hydra-v2"

; IFSS-LABEL: define i32 @one_by_four
; IFSS-NOT: left:
; IFSS-NOT: right.0:
; IFSS: call ptr @_sym_build_ite
; IFSS: !"bounded-unequal-linear-hydra-v2"

; REJECT-LABEL: define i32 @one_by_five
; REJECT: br i1 %condition, label %left, label %right.0
; REJECT-LABEL: right.4:
; REJECT-LABEL: merge:
; REJECT: phi i32

!0 = !{i64 301}
!1 = !{i64 302}
