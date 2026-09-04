; RUN: rm -f %t.safe.json %t.aggressive.json %t.v2.json %t.invalid.json
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_MANIFEST_OUT=%t.safe.json %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.safe.ll
; RUN: %opt -passes=verify -disable-output %t.safe.ll
; RUN: %filecheck %s --input-file=%t.safe.ll --check-prefix=SAFE
; RUN: %python -c "import json; r=json.loads(open(r'%t.safe.json').read()); assert r['schema']=='symcc-hydra-transform-v1'; assert r['mode']=='safe-alu'; assert r['aligned_pairs'] >= 1; assert r['extra_alu'] >= 1; assert r['selects'] >= 1; assert r['single_site_build'] and not r['requires_original_replay']"
; RUN: %python -c "open(r'%t.profile','w').write('# symcc-hydra-profile-v1\n117 99.0 12 4 8000\n')"
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=aggressive SYMCC_HYDRA_PROFILE=%t.profile SYMCC_HYDRA_MANIFEST_OUT=%t.aggressive.json %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.aggressive.ll
; RUN: %opt -passes=verify -disable-output %t.aggressive.ll
; RUN: %filecheck %s --input-file=%t.aggressive.ll --check-prefix=MEMORY
; RUN: %python -c "import json; r=json.loads(open(r'%t.aggressive.json').read()); assert r['site']==117; assert r['mode']=='aggressive-memory'; assert r['linearized_loads'] >= 1; assert r['readback_stores'] >= 1; assert r['requires_original_replay']"
; RUN: %python -c "open(r'%t.v2.profile','w').write('# symcc-hydra-profile-v2\\n# profile_sha256 '+'a'*64+'\\n# profiled_executable_sha256 '+'b'*64+'\\n# profiled_command_sha256 '+'c'*64+'\\n# site score observations interesting solver_time_us\\n117 99.000000000 12 4 8000\\n')"
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=aggressive SYMCC_HYDRA_PROFILE=%t.v2.profile SYMCC_HYDRA_MANIFEST_OUT=%t.v2.json %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.v2.ll
; RUN: %python -c "import json; r=json.loads(open(r'%t.v2.json').read()); assert r['selection_source']=='profile-v2'; assert r['profile_schema']=='symcc-hydra-profile-v2'; assert r['profile_sha256']=='a'*64; assert r['profiled_executable_sha256']=='b'*64; assert r['profiled_command_sha256']=='c'*64"
; RUN: %python %S/../util/verify_hydra_transform_manifest.py %t.v2.json
; RUN: %python -c "open(r'%t.invalid.profile','w').write('# symcc-hydra-profile-v2\\n117 99.000000000 12 4 8000\\n')"
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_PROFILE=%t.invalid.profile SYMCC_HYDRA_MANIFEST_OUT=%t.invalid.json %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.invalid.ll
; RUN: test ! -e %t.invalid.json
; RUN: %filecheck %s --input-file=%t.invalid.ll --check-prefix=BLOCKED
; RUN: %python -c "open(r'%t.denylist','w').write('# symcc-hydra-denylist-v1\n117\n')"
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=aggressive SYMCC_HYDRA_PROFILE=%t.profile SYMCC_HYDRA_DENYLIST=%t.denylist %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.blocked.ll
; RUN: %filecheck %s --input-file=%t.blocked.ll --check-prefix=BLOCKED

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i32 @safe_diamond(i32 %x, i32 %y, i1 %condition) {
entry:
  br i1 %condition, label %left, label %right, !symcc.site_id !0

left:
  %left.add = add i32 %x, 7
  %left.mul = mul i32 %left.add, 3
  br label %merge

right:
  %right.add = add i32 %y, 9
  br label %merge

merge:
  %value = phi i32 [ %left.mul, %left ], [ %right.add, %right ]
  ret i32 %value
}

; SAFE-LABEL: define i32 @safe_diamond
; SAFE: hydra.operand = select i1 %condition{{.*}}!symcc.hydra_select
; SAFE: hydra.merged = add i32
; SAFE: hydra.extra.operand = select i1 %condition{{.*}}!symcc.hydra_select
; SAFE: hydra.extra = mul i32
; SAFE: br label %merge
; SAFE-NOT: left:
; SAFE-LABEL: merge:
; SAFE-NOT: phi
; SAFE: ret i32 %hydra.output

define i8 @memory_diamond(ptr %pointer, i8 %value, i1 %condition) {
entry:
  br i1 %condition, label %write, label %skip, !symcc.site_id !1

write:
  %loaded = load i8, ptr %pointer, align 1
  store i8 %value, ptr %pointer, align 1
  br label %merge

skip:
  br label %merge

merge:
  %result = phi i8 [ %loaded, %write ], [ 0, %skip ]
  ret i8 %result
}

; MEMORY-LABEL: define i8 @memory_diamond
; MEMORY: hydra.extra.load = load i8, ptr %pointer
; MEMORY: hydra.store.old = load i8, ptr %pointer
; MEMORY: hydra.store.value = select i1 %condition, i8 %value, i8 %hydra.store.old
; MEMORY: store i8 %hydra.store.value, ptr %pointer
; MEMORY: br label %merge
; MEMORY-NOT: write:
; MEMORY-LABEL: merge:
; MEMORY-NOT: phi

; BLOCKED-LABEL: define i8 @memory_diamond
; BLOCKED: br i1 %condition, label %write, label %skip
; BLOCKED-LABEL: write:
; BLOCKED: load i8, ptr %pointer
; BLOCKED-LABEL: skip:
; BLOCKED-LABEL: merge:
; BLOCKED: phi i8

!0 = !{i64 101}
!1 = !{i64 117}
