; RUN: env SYMCC_IFSS_EXIT_STATE=1 %opt -load-pass-plugin=%passlib -passes=ifss-exit-lowering -S %s -o %t.ll
; RUN: %opt -passes=verify -disable-output %t.ll
; RUN: %filecheck %s --input-file=%t.ll --check-prefix=REJECT

define i8 @switch_returns(i8 %value) {
entry:
  switch i8 %value, label %default [
    i8 0, label %zero
    i8 1, label %one
  ]
zero:
  ret i8 10
one:
  ret i8 20
default:
  ret i8 30
}

define i8 @loop_returns(i1 %condition, i1 %leave) {
entry:
  br i1 %condition, label %loop, label %other
loop:
  br i1 %leave, label %exit, label %loop
exit:
  ret i8 10
other:
  ret i8 20
}

define {i8, i8} @aggregate_returns(i1 %condition) {
entry:
  br i1 %condition, label %left, label %right
left:
  ret {i8, i8} {i8 1, i8 2}
right:
  ret {i8, i8} {i8 3, i8 4}
}

define i8 @poison_returns(i1 %condition) {
entry:
  br i1 %condition, label %left, label %right
left:
  ret i8 poison
right:
  ret i8 0
}

define i8 @nine_returns(i1 %c0, i1 %c1, i1 %c2, i1 %c3,
                        i1 %c4, i1 %c5, i1 %c6, i1 %c7) {
entry:
  br i1 %c0, label %r0, label %test1
test1:
  br i1 %c1, label %r1, label %test2
test2:
  br i1 %c2, label %r2, label %test3
test3:
  br i1 %c3, label %r3, label %test4
test4:
  br i1 %c4, label %r4, label %test5
test5:
  br i1 %c5, label %r5, label %test6
test6:
  br i1 %c6, label %r6, label %test7
test7:
  br i1 %c7, label %r7, label %r8
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

declare i8 @tail_target(i8)

define i8 @musttail_returns(i8 %value) {
entry:
  %condition = icmp eq i8 %value, 0
  br i1 %condition, label %left, label %right
left:
  %result = musttail call i8 @tail_target(i8 %value)
  ret i8 %result
right:
  ret i8 0
}

; REJECT-NOT: !symcc.ifss_exit
; REJECT-LABEL: define i8 @switch_returns
; REJECT: switch i8
; REJECT-COUNT-3: ret i8
; REJECT-LABEL: define i8 @loop_returns
; REJECT: br i1 %leave, label %exit, label %loop
; REJECT-COUNT-2: ret i8
; REJECT-LABEL: define { i8, i8 } @aggregate_returns
; REJECT-COUNT-2: ret { i8, i8 }
; REJECT-LABEL: define i8 @poison_returns
; REJECT: ret i8 poison
; REJECT: ret i8 0
; REJECT-LABEL: define i8 @nine_returns
; REJECT-COUNT-9: ret i8
; REJECT-LABEL: define i8 @musttail_returns
; REJECT: musttail call i8 @tail_target
; REJECT-COUNT-2: ret i8
