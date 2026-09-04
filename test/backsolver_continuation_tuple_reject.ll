; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering -S %s -o %t.ll
; RUN: %opt -passes=verify -disable-output %t.ll
; RUN: %filecheck %s --input-file=%t.ll --check-prefix=REJECT

define i8 @single_destination(i1 %condition) {
entry:
  br i1 %condition, label %left, label %right
left:
  br label %merge
right:
  br label %merge
merge:
  %value = phi i8 [ 1, %left ], [ 2, %right ]
  ret i8 %value
}

define <2 x i8> @vector_liveout(i1 %condition) {
entry:
  br i1 %condition, label %left, label %right
left:
  br label %continuation_a
right:
  br label %continuation_b
continuation_a:
  %a = phi <2 x i8> [ <i8 1, i8 2>, %left ]
  ret <2 x i8> %a
continuation_b:
  %b = phi <2 x i8> [ <i8 3, i8 4>, %right ]
  ret <2 x i8> %b
}

define i8 @poison_liveout(i1 %condition) {
entry:
  br i1 %condition, label %left, label %right
left:
  br label %continuation_a
right:
  br label %continuation_b
continuation_a:
  %a = phi i8 [ poison, %left ]
  ret i8 %a
continuation_b:
  %b = phi i8 [ 0, %right ]
  ret i8 %b
}

define i8 @cyclic_region(i1 %condition, i1 %leave) {
entry:
  br i1 %condition, label %loop, label %right
loop:
  br i1 %leave, label %continuation_a, label %loop
right:
  br label %continuation_b
continuation_a:
  %a = phi i8 [ 1, %loop ]
  ret i8 %a
continuation_b:
  %b = phi i8 [ 2, %right ]
  ret i8 %b
}

; REJECT-NOT: bounded-continuation-tuple-v1
; REJECT-NOT: ifss.cont.
; REJECT-LABEL: define i8 @single_destination
; REJECT: %value = phi i8 [ 1, %left ], [ 2, %right ]
; REJECT-LABEL: define <2 x i8> @vector_liveout
; REJECT: %a = phi <2 x i8>
; REJECT: %b = phi <2 x i8>
; REJECT-LABEL: define i8 @poison_liveout
; REJECT: %a = phi i8 [ poison, %left ]
; REJECT-LABEL: define i8 @cyclic_region
; REJECT: br i1 %leave, label %continuation_a, label %loop

