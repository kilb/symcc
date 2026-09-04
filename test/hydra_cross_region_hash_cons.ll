; REQUIRES: qsym
; RUN: rm -f %t.manifest %t.seal %t.tampered.seal
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITES=7001,7002 SYMCC_HYDRA_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.lowered.ll
; RUN: %opt -passes=verify -disable-output %t.lowered.ll
; RUN: %python %S/../util/verify_hydra_transform_manifest.py %t.manifest
; RUN: %filecheck %s --input-file=%t.lowered.ll --check-prefix=LOWERED
; RUN: %python -c "import json; r=[json.loads(x) for x in open(r'%t.manifest')]; assert len(r)==2 and [x['site'] for x in r]==[7001,7002] and [x['transaction_ordinal'] for x in r]==[0,1] and all(x['region_schema']=='bounded-cross-region-shared-predicate-sese-dag-hydra-v6' and x['transaction_sites']==['7001','7002'] and x['multi_site_transaction'] and not x['single_site_build'] for x in r) and r[0]['cross_region_reused_predicate_negations']==0 and r[0]['cross_region_source_sites']==[] and r[1]['cross_region_reused_predicate_negations']>=3 and r[1]['cross_region_source_sites']==['7001'] and r[0]['transaction_fingerprint']==r[1]['transaction_fingerprint']"
; RUN: %python -c "p=open(r'%t.lowered.ll').read(); assert p.count(' = xor i1 ')==5"
; RUN: %python -c "import json; r=[json.loads(x) for x in open(r'%t.manifest')]; r[1]['transaction_ordinal']=0; open(r'%t.ordinal-tamper','w').write(''.join(json.dumps(x)+'\n' for x in r))"
; RUN: not %python %S/../util/verify_hydra_transform_manifest.py %t.ordinal-tamper
; RUN: %python -c "import json; r=[json.loads(x) for x in open(r'%t.manifest')]; r[1]['cross_region_source_sites']=['7002']; open(r'%t.source-tamper','w').write(''.join(json.dumps(x)+'\n' for x in r))"
; RUN: not %python %S/../util/verify_hydra_transform_manifest.py %t.source-tamper
; RUN: %python -c "r=open(r'%t.manifest').readlines(); open(r'%t.order-tamper','w').writelines(reversed(r))"
; RUN: not %python %S/../util/verify_hydra_transform_manifest.py %t.order-tamper
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline hydra --manifest hydra=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python %S/../util/replay_transform_artifact.py --pipeline hydra --manifest hydra=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.seal
; RUN: %python -c "p=open(r'%t.lowered.ll').read(); old=' = mul i32 '; assert old in p; open(r'%t.tampered.ll','w').write(p.replace(old,' = add i32 ',1))"
; RUN: %opt -passes=verify -disable-output %t.tampered.ll
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline hydra --manifest hydra=%t.manifest --input-ir %s --lowered-ir %t.tampered.ll --compiler %passlib --llvm-tool %opt --output %t.tampered.seal
; RUN: not %python %S/../util/replay_transform_artifact.py --pipeline hydra --manifest hydra=%t.manifest --input-ir %s --lowered-ir %t.tampered.ll --compiler %passlib --llvm-tool %opt --seal %t.tampered.seal
; RUN: env SYMCC_HYDRA=0 %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.original.ll
; RUN: %python -c "import pathlib,subprocess; lli=str(pathlib.Path(r'%opt').with_name('lli')); run=lambda p,b: subprocess.run([lli,'--entry-function=semantic_main',p],input=bytes([b]),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode; assert all(run(r'%t.original.ll',b)==run(r'%t.lowered.ll',b) for b in range(256))"
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITES=7001,7002 %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: rm -f %t.reject.manifest
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITES=7002,7001 SYMCC_HYDRA_MANIFEST_OUT=%t.reject.manifest %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.reverse.ll
; RUN: %filecheck %s --input-file=%t.reverse.ll --check-prefix=REJECT
; RUN: %python -c "import pathlib; assert not pathlib.Path(r'%t.reject.manifest').exists()"
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITES=7001,7001 %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.duplicate.ll
; RUN: %filecheck %s --input-file=%t.duplicate.ll --check-prefix=REJECT
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITES=7001,7002,7999 %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.missing.ll
; RUN: %filecheck %s --input-file=%t.missing.ll --check-prefix=REJECT
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITES=7001,7003 %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.no-shared.ll
; RUN: %filecheck %s --input-file=%t.no-shared.ll --check-prefix=REJECT
; RUN: %python %S/../util/cross_llvm_transform_replay.py verify --certificate %S/../benchmark/evidence/hydra_f295_cross_llvm_certificate.json

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i32 @cross_region(
    i32 %x, i1 %outer0, i1 %outer1, i1 %shared, i1 %middle, i1 %last,
    i1 %other.shared, i1 %other.middle, i1 %other.last) {
entry:
  br i1 %outer0, label %r0.left.0, label %r0.right, !symcc.site_id !0

r0.left.0:
  br i1 %shared, label %r0.left.0a, label %r0.left.0b
r0.left.0a:
  br label %r0.left.1
r0.left.0b:
  br label %r0.left.1
r0.left.1:
  br i1 %middle, label %r0.left.1a, label %r0.left.1b
r0.left.1a:
  br label %r0.left.2
r0.left.1b:
  br label %r0.left.2
r0.left.2:
  br i1 %shared, label %r0.left.2a, label %r0.left.2b
r0.left.2a:
  br label %r0.left.3
r0.left.2b:
  br label %r0.left.3
r0.left.3:
  br i1 %last, label %r0.left.3a, label %r0.left.3b
r0.left.3a:
  br label %r0.left.out
r0.left.3b:
  br label %r0.left.out
r0.left.out:
  %r0.left.value = add i32 %x, 31
  br label %r0.merge
r0.right:
  %r0.right.value = sub i32 %x, 7
  br label %r0.merge
r0.merge:
  %r0.value = phi i32 [ %r0.left.value, %r0.left.out ],
                      [ %r0.right.value, %r0.right ]
  br i1 %outer1, label %r1.left.0, label %r1.right, !symcc.site_id !1

r1.left.0:
  br i1 %shared, label %r1.left.0a, label %r1.left.0b
r1.left.0a:
  br label %r1.left.1
r1.left.0b:
  br label %r1.left.1
r1.left.1:
  br i1 %middle, label %r1.left.1a, label %r1.left.1b
r1.left.1a:
  br label %r1.left.2
r1.left.1b:
  br label %r1.left.2
r1.left.2:
  br i1 %shared, label %r1.left.2a, label %r1.left.2b
r1.left.2a:
  br label %r1.left.3
r1.left.2b:
  br label %r1.left.3
r1.left.3:
  br i1 %last, label %r1.left.3a, label %r1.left.3b
r1.left.3a:
  br label %r1.left.out
r1.left.3b:
  br label %r1.left.out
r1.left.out:
  %r1.left.value = mul i32 %r0.value, 3
  br label %r1.merge
r1.right:
  %r1.right.value = xor i32 %r0.value, 85
  br label %r1.merge
r1.merge:
  %r1.value = phi i32 [ %r1.left.value, %r1.left.out ],
                      [ %r1.right.value, %r1.right ]
  br i1 %outer0, label %r2.left.0, label %r2.right, !symcc.site_id !2

r2.left.0:
  br i1 %other.shared, label %r2.left.0a, label %r2.left.0b
r2.left.0a:
  br label %r2.left.1
r2.left.0b:
  br label %r2.left.1
r2.left.1:
  br i1 %other.middle, label %r2.left.1a, label %r2.left.1b
r2.left.1a:
  br label %r2.left.2
r2.left.1b:
  br label %r2.left.2
r2.left.2:
  br i1 %other.shared, label %r2.left.2a, label %r2.left.2b
r2.left.2a:
  br label %r2.left.3
r2.left.2b:
  br label %r2.left.3
r2.left.3:
  br i1 %other.last, label %r2.left.3a, label %r2.left.3b
r2.left.3a:
  br label %r2.left.out
r2.left.3b:
  br label %r2.left.out
r2.left.out:
  %r2.left.value = add i32 %r1.value, 101
  br label %r2.merge
r2.right:
  %r2.right.value = sub i32 %r1.value, 19
  br label %r2.merge
r2.merge:
  %r2.value = phi i32 [ %r2.left.value, %r2.left.out ],
                      [ %r2.right.value, %r2.right ]
  ret i32 %r2.value
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
  %b4v = and i8 %raw, 16
  %b4 = icmp ne i8 %b4v, 0
  %b5v = and i8 %raw, 32
  %b5 = icmp ne i8 %b5v, 0
  %b6v = and i8 %raw, 64
  %b6 = icmp ne i8 %b6v, 0
  %b7v = and i8 %raw, -128
  %b7 = icmp ne i8 %b7v, 0
  %result = call i32 @cross_region(
      i32 %x, i1 %b0, i1 %b1, i1 %b2, i1 %b3, i1 %b4,
      i1 %b5, i1 %b6, i1 %b7)
  ret i32 %result
}

declare i64 @read(i32, ptr, i64)

; LOWERED-LABEL: define i32 @cross_region
; LOWERED-NOT: r0.left.0:
; LOWERED-NOT: r1.left.0:
; LOWERED-COUNT-2: !"bounded-cross-region-shared-predicate-sese-dag-hydra-v6"

; IFSS-LABEL: define i32 @cross_region
; IFSS-NOT: r0.left.0:
; IFSS-NOT: r1.left.0:
; IFSS: call ptr @_sym_build_ite

; REJECT-LABEL: define i32 @cross_region
; REJECT: r0.left.0:
; REJECT: r1.left.0:
; REJECT-NOT: !"bounded-cross-region-shared-predicate-sese-dag-hydra-v6"

!0 = !{i64 7001}
!1 = !{i64 7002}
!2 = !{i64 7003}
