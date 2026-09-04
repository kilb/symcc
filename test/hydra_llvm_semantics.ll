; RUN: rm -f %t.paired.json %t.extra.json %t.div.json %t.eh.json
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=701 SYMCC_HYDRA_MANIFEST_OUT=%t.paired.json %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.paired.ll
; RUN: %opt -passes=verify -disable-output %t.paired.ll
; RUN: %filecheck %s --input-file=%t.paired.ll --check-prefix=PAIRED
; RUN: %python %S/../util/verify_hydra_transform_manifest.py %t.paired.json
; RUN: %python -c "import json; r=json.loads(open(r'%t.paired.json').read()); assert r['llvm_ir_semantics']=='llvm-poison-undef-freeze-refinement-v1' and r['llvm_major']>=8; assert r['left_freeze_instructions']==r['right_freeze_instructions']==r['aligned_freeze_pairs']==1; assert r['extra_freeze_instructions']==0; assert r['exception_policy']=='reject-non-branch-terminators-and-eh-pads-v1'"
; RUN: %python -c "import json; p=r'%t.paired.json'; r=json.loads(open(p).read()); r['aligned_freeze_pairs']=0; open(r'%t.tampered.json','w').write(json.dumps(r)+'\\n')"
; RUN: not %python %S/../util/verify_hydra_transform_manifest.py %t.tampered.json
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=702 SYMCC_HYDRA_MANIFEST_OUT=%t.extra.json %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.extra.ll
; RUN: %opt -passes=verify -disable-output %t.extra.ll
; RUN: %filecheck %s --input-file=%t.extra.ll --check-prefix=EXTRA
; RUN: %python %S/../util/verify_hydra_transform_manifest.py %t.extra.json
; RUN: %python -c "import json; r=json.loads(open(r'%t.extra.json').read()); assert r['left_freeze_instructions']==1 and r['right_freeze_instructions']==0 and r['aligned_freeze_pairs']==0 and r['extra_freeze_instructions']==1"
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=703 SYMCC_HYDRA_MANIFEST_OUT=%t.div.json %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.div.ll
; RUN: %opt -passes=verify -disable-output %t.div.ll
; RUN: %filecheck %s --input-file=%t.div.ll --check-prefix=DIV
; RUN: %python %S/../util/verify_hydra_transform_manifest.py %t.div.json
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=704 SYMCC_HYDRA_MANIFEST_OUT=%t.eh.json %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.eh.ll
; RUN: test ! -e %t.eh.json
; RUN: %filecheck %s --input-file=%t.eh.ll --check-prefix=EH

define i32 @paired_freeze(i1 %condition) {
entry:
  br i1 %condition, label %left, label %right, !symcc.site_id !0

left:
  %left.freeze = freeze i32 poison
  %left.add = add i32 %left.freeze, 1
  br label %merge

right:
  %right.freeze = freeze i32 undef
  %right.add = add i32 %right.freeze, 2
  br label %merge

merge:
  %result = phi i32 [ %left.add, %left ], [ %right.add, %right ]
  ret i32 %result
}

; PAIRED-LABEL: define i32 @paired_freeze
; PAIRED: %hydra.operand = select i1 %condition, i32 poison, i32 undef
; PAIRED-NEXT: %hydra.merged = freeze i32 %hydra.operand
; PAIRED: %hydra.operand{{.*}} = select i1 %condition, i32 1, i32 2
; PAIRED: %hydra.merged{{.*}} = add i32 %hydra.merged, %hydra.operand
; PAIRED-NOT: freeze i32
; PAIRED: ret i32 %hydra.merged

define i32 @extra_freeze(i32 %value, i1 %condition) {
entry:
  br i1 %condition, label %left, label %right, !symcc.site_id !1

left:
  %left.freeze = freeze i32 %value
  %left.add = add i32 %left.freeze, 3
  br label %merge

right:
  %right.add = add i32 %value, 4
  br label %merge

merge:
  %result = phi i32 [ %left.add, %left ], [ %right.add, %right ]
  ret i32 %result
}

; EXTRA-LABEL: define i32 @extra_freeze
; EXTRA: %hydra.extra.operand = select i1 %condition, i32 %value, i32 0
; EXTRA-NEXT: %hydra.extra = freeze i32 %hydra.extra.operand
; EXTRA: %hydra.operand = select i1 %condition, i32 %hydra.extra, i32 %value
; EXTRA: ret i32 %hydra.merged

define i32 @guarded_divisor(i32 %numerator, i32 %divisor,
                            i1 %condition) {
entry:
  br i1 %condition, label %left, label %right, !symcc.site_id !2

left:
  %quotient = udiv i32 %numerator, %divisor
  br label %merge

right:
  %fallback = add i32 %numerator, 9
  br label %merge

merge:
  %result = phi i32 [ %quotient, %left ], [ %fallback, %right ]
  ret i32 %result
}

; DIV-LABEL: define i32 @guarded_divisor
; DIV: %hydra.extra.operand = select i1 %condition, i32 %numerator, i32 0
; DIV-NEXT: %hydra.extra.operand{{.*}} = select i1 %condition, i32 %divisor, i32 1
; DIV-NEXT: %hydra.extra = udiv i32 %hydra.extra.operand, %hydra.extra.operand
; DIV: ret i32 %hydra.output

declare i32 @may_throw(i32)
declare i32 @__gxx_personality_v0(...)

define i32 @exception_region(i32 %value, i1 %condition)
    personality ptr @__gxx_personality_v0 {
entry:
  br i1 %condition, label %left, label %right, !symcc.site_id !3

left:
  %left.value = add i32 %value, 1
  %called = invoke i32 @may_throw(i32 %left.value)
      to label %merge unwind label %unwind

right:
  %right.value = add i32 %value, 2
  br label %merge

merge:
  %result = phi i32 [ %called, %left ], [ %right.value, %right ]
  ret i32 %result

unwind:
  %landing = landingpad { ptr, i32 }
      cleanup
  ret i32 -1
}

; EH-LABEL: define i32 @exception_region
; EH: br i1 %condition, label %left, label %right
; EH-LABEL: left:
; EH: invoke i32 @may_throw
; EH-LABEL: unwind:
; EH: landingpad

!0 = !{i64 701}
!1 = !{i64 702}
!2 = !{i64 703}
!3 = !{i64 704}
