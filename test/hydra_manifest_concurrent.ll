; RUN: rm -f %t.jsonl %t.bound.jsonl %t.campaign.json %t.rejected.json
; RUN: %python %S/hydra_manifest_concurrency.py.inc %opt %passlib %s %t.jsonl 9001
; RUN: %python -c "import json; open(r'%t.telemetry','w').write(json.dumps({'solver_time_us':0,'branch_trace':[[1,2,3,9001,0,0]]}))"
; RUN: %python %S/../util/hydra_transform.py profile %t.telemetry --profiled-command-json '["true"]' --profile-output %t.profile --artifact-output %t.profile.json
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_PROFILE=%t.profile SYMCC_HYDRA_MANIFEST_OUT=%t.bound.jsonl %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.bound.ll
; RUN: %python -c "open(r'%t.input','wb').write(b'P')"
; RUN: %python %S/../util/hydra_transform.py campaign --original-command-json '["true"]' --transformed-command-json '["true"]' --input %t.input --site 9001 --manifest %t.bound.jsonl --profile-artifact %t.profile.json --output %t.campaign.json
; RUN: %python %S/../util/hydra_transform.py verify %t.campaign.json
; RUN: not %python %S/../util/hydra_transform.py campaign --original-command-json '["false"]' --transformed-command-json '["true"]' --input %t.input --site 9001 --manifest %t.bound.jsonl --profile-artifact %t.profile.json --output %t.rejected.json

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i32 @concurrent_manifest(i32 %x, i1 %condition) {
entry:
  br i1 %condition, label %left, label %right, !symcc.site_id !0

left:
  %left.value = add i32 %x, 7
  br label %merge

right:
  %right.value = sub i32 %x, 9
  br label %merge

merge:
  %value = phi i32 [ %left.value, %left ], [ %right.value, %right ]
  ret i32 %value
}

!0 = !{i64 9001}
