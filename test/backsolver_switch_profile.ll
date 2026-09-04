; REQUIRES: qsym
; RUN: %python -c "open(r'%t.profile','w').write('257257 0 1000\\n257257 1 1\\n257257 2 1\\n257257 3 1\\n257257 4 1\\n257257 5 1\\n257257 6 1\\n257257 default 1\\n')"
; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_SWITCH_STATE=1 SYMCC_IFSS_SWITCH_MODE=profile SYMCC_IFSS_SWITCH_PROFILE=%t.profile SYMCC_IFSS_SWITCH_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-switch-lowering -S %s -o %t.profile.ll
; RUN: %opt -passes=verify -disable-output %t.profile.ll
; RUN: %filecheck %s --input-file=%t.profile.ll --check-prefix=PROFILE
; RUN: %python -c "text=open(r'%t.profile.ll').read(); assert text.count('icmp ule i8') == 6; assert text.count('icmp eq i8') == 8; assert 'i8 0, i64 1000' in text"
; RUN: %python -c "import json; rows=[json.loads(x) for x in open(r'%t.manifest')]; assert len(rows)==1; r=rows[0]; assert r['schema']=='symcc-ifss-switch-tree-v1' and r['site']=='257257'; assert r['requested_mode']==r['effective_mode']=='profile' and r['profile_source']=='external-stable-site'; assert r['logical_edges']==8 and r['lowered_edges']==14 and r['nodes'][0]['value']=='0'; assert r['cases'][0]['weight']=='1000' and r['profile_fingerprint'] and r['tree_fingerprint']"
; RUN: %python %S/../util/verify_ifss_switch_manifest.py %t.manifest
; RUN: %python -c "import json; r=json.loads(open(r'%t.manifest').read()); r['nodes'][0]['value']='1'; open(r'%t.tampered','w').write(json.dumps(r)+'\\n')"
; RUN: not %python %S/../util/verify_ifss_switch_manifest.py %t.tampered
; RUN: rm -f %t.branch.manifest
; RUN: env SYMCC_IFSS_SWITCH_STATE=1 SYMCC_IFSS_SWITCH_MODE=profile SYMCC_IFSS_SWITCH_MANIFEST_OUT=%t.branch.manifest %opt -load-pass-plugin=%passlib -passes=ifss-switch-lowering -S %s -o %t.branch.ll
; RUN: %opt -passes=verify -disable-output %t.branch.ll
; RUN: %filecheck %s --input-file=%t.branch.ll --check-prefix=BRANCH
; RUN: %python -c "import json; r=json.loads(open(r'%t.branch.manifest').read()); assert r['profile_source']=='llvm-branch-weights' and r['effective_mode']=='profile'; assert r['nodes'][0]['value']=='0' and r['default_weight']=='1' and r['objective_cost']=='2028'"
; RUN: %python %S/../util/verify_ifss_switch_manifest.py %t.branch.manifest
; RUN: %python -c "open(r'%t.incomplete','w').write('257257 0 1000\\n257257 default 1\\n')"
; RUN: rm -f %t.fallback.manifest
; RUN: env SYMCC_IFSS_SWITCH_STATE=1 SYMCC_IFSS_SWITCH_MODE=profile SYMCC_IFSS_SWITCH_PROFILE=%t.incomplete SYMCC_IFSS_SWITCH_MANIFEST_OUT=%t.fallback.manifest %opt -load-pass-plugin=%passlib -passes=ifss-switch-lowering -S %s -o %t.fallback.ll
; RUN: %opt -passes=verify -disable-output %t.fallback.ll
; RUN: %filecheck %s --input-file=%t.fallback.ll --check-prefix=FALLBACK
; RUN: %python -c "import json; r=json.loads(open(r'%t.fallback.manifest').read()); assert not r['profile_valid'] and r['effective_mode']=='balanced' and r['fallback_reason']=='incomplete-profile'; assert r['nodes'][0]['value']=='2'"
; RUN: %python %S/../util/verify_ifss_switch_manifest.py %t.fallback.manifest
; RUN: env SYMCC_IFSS_SWITCH_STATE=1 SYMCC_IFSS_SWITCH_MODE=profile SYMCC_IFSS_SWITCH_PROFILE=%t.absent %opt -load-pass-plugin=%passlib -passes=ifss-switch-lowering -S %s -o %t.unreadable.ll
; RUN: %filecheck %s --input-file=%t.unreadable.ll --check-prefix=UNREADABLE
; RUN: %python -c "open(r'%t.duplicate','w').write(open(r'%t.profile').read() + '257257 0 3\\n')"
; RUN: env SYMCC_IFSS_SWITCH_STATE=1 SYMCC_IFSS_SWITCH_MODE=profile SYMCC_IFSS_SWITCH_PROFILE=%t.duplicate %opt -load-pass-plugin=%passlib -passes=ifss-switch-lowering -S %s -o %t.duplicate.ll
; RUN: %filecheck %s --input-file=%t.duplicate.ll --check-prefix=DUPLICATE
; RUN: env SYMCC_IFSS_SWITCH_STATE=1 SYMCC_IFSS_SWITCH_MODE=profile SYMCC_IFSS_SWITCH_PROFILE=%t.profile %symcc -O0 -S -emit-llvm %s -o %t.symbolized.ll
; RUN: %opt -passes=verify -disable-output %t.symbolized.ll
; RUN: %filecheck %s --input-file=%t.symbolized.ll --check-prefix=IFSS
; RUN: env SYMCC_IFSS_SWITCH_STATE=1 SYMCC_IFSS_SWITCH_MODE=profile SYMCC_IFSS_SWITCH_PROFILE=%t.profile %symcc -O0 %s -o %t
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
  ], !symcc.site_id !0, !prof !1

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

; PROFILE-LABEL: define i32 @main
; PROFILE: %ifss.switch.range = icmp ule i8 %selector, 0
; PROFILE: !"bounded-switch-profile-tree-v1"
; PROFILE: !"profile-weighted-switch-v1", i64 257257, !"external-stable-site"
; PROFILE-NOT: !"profile-switch-fallback-v1"

; BRANCH-LABEL: define i32 @main
; BRANCH: %ifss.switch.range = icmp ule i8 %selector, 0
; BRANCH: !"profile-weighted-switch-v1", i64 257257, !"llvm-branch-weights"

; FALLBACK-LABEL: define i32 @main
; FALLBACK: %ifss.switch.range = icmp ule i8 %selector, 2
; FALLBACK: !"bounded-switch-tree-v1"
; FALLBACK: !"profile-switch-fallback-v1", i64 257257, !"incomplete-profile"

; UNREADABLE-LABEL: define i32 @main
; UNREADABLE: %ifss.switch.range = icmp ule i8 %selector, 2
; UNREADABLE: !"profile-switch-fallback-v1", i64 257257, !"unreadable-profile"

; DUPLICATE-LABEL: define i32 @main
; DUPLICATE: %ifss.switch.range = icmp ule i8 %selector, 2
; DUPLICATE: !"profile-switch-fallback-v1", i64 257257, !"invalid-site-profile"

; IFSS-LABEL: define i32 @main
; IFSS-COUNT-7: call ptr @_sym_build_ite

!0 = !{i64 257257}
!1 = !{!"branch_weights", i32 1, i32 1000, i32 1, i32 1, i32 1, i32 1, i32 1, i32 1}
