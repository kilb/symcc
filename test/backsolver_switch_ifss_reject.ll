; RUN: env SYMCC_IFSS_SWITCH_STATE=1 %opt -load-pass-plugin=%passlib -passes=ifss-switch-lowering -S %s -o %t.ll
; RUN: %opt -passes=verify -disable-output %t.ll
; RUN: %filecheck %s --input-file=%t.ll --check-prefix=REJECT

define i8 @single_destination(i8 %selector) {
entry:
  switch i8 %selector, label %shared [
    i8 1, label %shared
    i8 2, label %shared
  ]
shared:
  ret i8 10
}

define i8 @external_predecessor(i8 %selector, i1 %external) {
entry:
  br i1 %external, label %outside, label %controller
controller:
  switch i8 %selector, label %default [
    i8 1, label %case
  ]
outside:
  br label %case
case:
  br label %merge
default:
  br label %merge
merge:
  %state = phi i8 [ 10, %case ], [ 20, %default ]
  ret i8 %state
}

define i8 @conditional_arm(i8 %selector, i1 %condition) {
entry:
  switch i8 %selector, label %default [
    i8 1, label %case
  ]
case:
  br i1 %condition, label %merge, label %other
default:
  br label %merge
other:
  br label %merge
merge:
  %state = phi i8 [ 10, %case ], [ 20, %default ], [ 30, %other ]
  ret i8 %state
}

define i8 @nine_arms(i8 %selector) {
entry:
  switch i8 %selector, label %r8 [
    i8 0, label %r0
    i8 1, label %r1
    i8 2, label %r2
    i8 3, label %r3
    i8 4, label %r4
    i8 5, label %r5
    i8 6, label %r6
    i8 7, label %r7
  ]
r0:
  ret i8 0
r1:
  ret i8 1
r2:
  ret i8 2
r3:
  ret i8 3
r4:
  ret i8 4
r5:
  ret i8 5
r6:
  ret i8 6
r7:
  ret i8 7
r8:
  ret i8 8
}

define i8 @mixed_exits(i8 %selector) {
entry:
  switch i8 %selector, label %return [
    i8 1, label %branch
  ]
branch:
  br label %return
return:
  ret i8 0
}

; REJECT-NOT: bounded-switch-chain-v1
; REJECT-LABEL: define i8 @single_destination
; REJECT: switch i8
; REJECT-LABEL: define i8 @external_predecessor
; REJECT: switch i8
; REJECT-LABEL: define i8 @conditional_arm
; REJECT: switch i8
; REJECT-LABEL: define i8 @nine_arms
; REJECT: switch i8
; REJECT-LABEL: define i8 @mixed_exits
; REJECT: switch i8
