; RUN: rm -f %t.manifest
; RUN: env SYMCC_IFSS_SWITCH_STATE=1 SYMCC_IFSS_SWITCH_MODE=profile SYMCC_IFSS_SWITCH_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=ifss-switch-lowering -S %s -o %t.ll
; RUN: %opt -passes=verify -disable-output %t.ll
; RUN: %filecheck %s --input-file=%t.ll --check-prefix=CHECK
; RUN: %python -c "import json; rows=[json.loads(x) for x in open(r'%t.manifest')]; r=next(x for x in rows if x['site']=='259259'); assert not r['profile_valid'] and r['effective_mode']=='balanced' and r['fallback_reason']=='zero-branch-weight'; assert r['nodes'][0]['value']=='0'; wide=next(x for x in rows if x['site']=='259260'); assert wide['cases'][0]['value']=='1267650600228229401496703205376'"
; RUN: %python %S/../util/verify_ifss_switch_manifest.py %t.manifest

define i8 @zero_weight(i8 %selector) {
entry:
  switch i8 %selector, label %default [
    i8 0, label %case0
    i8 1, label %case1
    i8 2, label %case2
  ], !symcc.site_id !0, !prof !1
case0:
  ret i8 10
case1:
  ret i8 20
case2:
  ret i8 30
default:
  ret i8 40
}

define i8 @wide_case(i128 %selector) {
entry:
  switch i128 %selector, label %default [
    i128 1267650600228229401496703205376, label %case0
    i128 1267650600228229401496703205377, label %case1
  ], !symcc.site_id !2
case0:
  ret i8 10
case1:
  ret i8 20
default:
  ret i8 30
}

; CHECK-LABEL: define i8 @zero_weight
; CHECK: %ifss.switch.range = icmp ule i8 %selector, 0
; CHECK: !"bounded-switch-tree-v1"
; CHECK: !"profile-switch-fallback-v1", i64 259259, !"zero-branch-weight"
; CHECK-NOT: !"profile-weighted-switch-v1"

!0 = !{i64 259259}
!1 = !{!"branch_weights", i32 1, i32 0, i32 1000, i32 1}
!2 = !{i64 259260}
