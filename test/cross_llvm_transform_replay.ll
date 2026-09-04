; REQUIRES: llvm17-cross
; RUN: rm -f %t.manifest %t.lowered.ll %t.seal %t.candidates.json %t.certificate.json
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=801 SYMCC_HYDRA_MANIFEST_OUT=%t.manifest %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.lowered.ll
; RUN: %python %S/../util/seal_transform_artifact.py seal --pipeline hydra --manifest hydra=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --output %t.seal
; RUN: %python -c "import json; json.dump({'schema':'symcc-cross-llvm-candidates-v1','candidates':[{'label':'llvm17','opt':r'%opt17','compiler':r'%passlib17','llvm_diff':r'%llvmdiff17'}]},open(r'%t.candidates.json','w'))"
; RUN: %python %S/../util/cross_llvm_transform_replay.py audit --pipeline hydra --manifest hydra=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.seal --candidates %t.candidates.json --output %t.certificate.json
; RUN: %python %S/../util/cross_llvm_transform_replay.py verify --certificate %t.certificate.json
; RUN: %python -c "import json; r=json.load(open(r'%t.certificate.json')); assert r['verified_llvm_majors']==[17,18] and r['cross_major_verified']; assert r['baseline']['lowered_ir']['sha256']==r['candidates'][0]['lowered_ir']['sha256']; assert r['baseline']['normalized_manifest_sha256']==r['candidates'][0]['normalized_manifest_sha256']"
; RUN: %python -c "import hashlib,json,sys; sys.path.insert(0,r'%S/../util'); from seal_transform_artifact import canonical_bytes; p=r'%t.certificate.json'; r=json.load(open(p)); r['cross_major_verified']=False; r.pop('certificate_sha256'); r['certificate_sha256']=hashlib.sha256(canonical_bytes(r)).hexdigest(); json.dump(r,open(r'%t.tampered.json','w'))"
; RUN: not %python %S/../util/cross_llvm_transform_replay.py verify --certificate %t.tampered.json
; RUN: %python -c "import json; json.dump({'schema':'symcc-cross-llvm-candidates-v1','candidates':[{'label':'llvm18-again','opt':r'%opt','compiler':r'%passlib'}]},open(r'%t.same-major.json','w'))"
; RUN: not %python %S/../util/cross_llvm_transform_replay.py audit --pipeline hydra --manifest hydra=%t.manifest --input-ir %s --lowered-ir %t.lowered.ll --compiler %passlib --llvm-tool %opt --seal %t.seal --candidates %t.same-major.json --output %t.same-major.certificate.json

define i32 @cross_llvm_freeze(i1 %condition) {
entry:
  br i1 %condition, label %left, label %right, !symcc.site_id !0

left:
  %left.freeze = freeze i32 poison
  %left.value = add i32 %left.freeze, 1
  br label %merge

right:
  %right.freeze = freeze i32 undef
  %right.value = add i32 %right.freeze, 2
  br label %merge

merge:
  %result = phi i32 [ %left.value, %left ], [ %right.value, %right ]
  ret i32 %result
}

!0 = !{i64 801}
