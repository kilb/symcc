; REQUIRES: qsym
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=603 %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.external.ll
; RUN: %filecheck %s --input-file=%t.external.ll --check-prefix=EXTERNAL
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=604 %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.cyclic.ll
; RUN: %filecheck %s --input-file=%t.cyclic.ll --check-prefix=CYCLIC
; RUN: env SYMCC_HYDRA=1 SYMCC_HYDRA_MODE=safe SYMCC_HYDRA_SITE=605 %opt -load-pass-plugin=%passlib -passes=hydra-transform -S %s -o %t.over-budget.ll
; RUN: %filecheck %s --input-file=%t.over-budget.ll --check-prefix=OVER

define i32 @external_predecessor(
    i32 %x, i1 %outer, i1 %left.take, i1 %right.take) {
entry:
  br i1 %outer, label %left.root, label %right.root, !symcc.site_id !0
left.root:
  br i1 %left.take, label %left.a, label %left.b
left.a:
  %a = add i32 %x, 1
  br label %left.join
left.b:
  %b = sub i32 %x, 1
  br label %left.join
right.root:
  br i1 %right.take, label %right.cross, label %right.out
right.cross:
  %cross = xor i32 %x, 3
  br label %left.join
left.join:
  %joined = phi i32 [ %a, %left.a ], [ %b, %left.b ], [ %cross, %right.cross ]
  br label %merge
right.out:
  %out = add i32 %x, 5
  br label %merge
merge:
  %value = phi i32 [ %joined, %left.join ], [ %out, %right.out ]
  ret i32 %value
}

define i32 @cyclic_arm(i32 %x, i1 %outer, i1 %again) {
entry:
  br i1 %outer, label %left.root, label %right, !symcc.site_id !1
left.root:
  %next = add i32 %x, 1
  br i1 %again, label %left.root, label %left.out
left.out:
  br label %merge
right:
  br label %merge
merge:
  %value = phi i32 [ %next, %left.out ], [ %x, %right ]
  ret i32 %value
}

define i32 @five_local_merges(
    i32 %x, i1 %outer, i1 %a, i1 %b, i1 %c, i1 %d, i1 %e) {
entry:
  br i1 %outer, label %left.0, label %right, !symcc.site_id !2
left.0:
  br i1 %a, label %left.0a, label %left.0b
left.0a:
  br label %left.1
left.0b:
  br label %left.1
left.1:
  %p1 = phi i32 [ %x, %left.0a ], [ 1, %left.0b ]
  br i1 %b, label %left.1a, label %left.1b
left.1a:
  br label %left.2
left.1b:
  br label %left.2
left.2:
  %p2 = phi i32 [ %p1, %left.1a ], [ 2, %left.1b ]
  br i1 %c, label %left.2a, label %left.2b
left.2a:
  br label %left.3
left.2b:
  br label %left.3
left.3:
  %p3 = phi i32 [ %p2, %left.2a ], [ 3, %left.2b ]
  br i1 %d, label %left.3a, label %left.3b
left.3a:
  br label %left.out
left.3b:
  br label %left.out
left.out:
  %p4 = phi i32 [ %p3, %left.3a ], [ 4, %left.3b ]
  br i1 %e, label %left.4a, label %left.4b
left.4a:
  br label %left.final
left.4b:
  br label %left.final
left.final:
  %p5 = phi i32 [ %p4, %left.4a ], [ 5, %left.4b ]
  br label %merge
right:
  br label %merge
merge:
  %value = phi i32 [ %p5, %left.final ], [ %x, %right ]
  ret i32 %value
}

; EXTERNAL-LABEL: define i32 @external_predecessor
; EXTERNAL: br i1 %outer, label %left.root, label %right.root
; EXTERNAL-LABEL: left.join:
; EXTERNAL: phi i32
; EXTERNAL-LABEL: merge:
; EXTERNAL: phi i32

; CYCLIC-LABEL: define i32 @cyclic_arm
; CYCLIC-LABEL: left.root:
; CYCLIC: br i1 %again, label %left.root, label %left.out
; CYCLIC-LABEL: merge:
; CYCLIC: phi i32

; OVER-LABEL: define i32 @five_local_merges
; OVER: br i1 %outer, label %left.0, label %right
; OVER-LABEL: left.final:
; OVER: %p5 = phi i32
; OVER-LABEL: merge:
; OVER: phi i32

!0 = !{i64 603}
!1 = !{i64 604}
!2 = !{i64 605}
