; RUN: env SYMCC_IFSS_LOOP_SUMMARY=1 %opt -load-pass-plugin=%passlib -passes=ifss-loop-summary -S %s -o %t.ll
; RUN: %opt -passes=verify -disable-output %t.ll
; RUN: %filecheck %s --input-file=%t.ll --check-prefix=REJECT

define i8 @four_break_budget(
    i8 %raw_trip, i8 %b0, i8 %b1, i8 %b2, i8 %b3) {
entry:
  %trip = and i8 %raw_trip, 7
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ %index_next, %check3 ]
  %state = phi i8 [ 0, %entry ], [ %state_next, %check3 ]
  %continue = icmp ult i8 %index, %trip
  br i1 %continue, label %update, label %normal
update:
  %state_next = add i8 %state, 1
  %index_next = add i8 %index, 1
  br label %check0
check0:
  %break0 = icmp eq i8 %index, %b0
  br i1 %break0, label %exit0, label %check1
check1:
  %break1 = icmp eq i8 %index, %b1
  br i1 %break1, label %exit1, label %check2
check2:
  %break2 = icmp eq i8 %index, %b2
  br i1 %break2, label %exit2, label %check3
check3:
  %break3 = icmp eq i8 %index, %b3
  br i1 %break3, label %exit3, label %loop
normal:
  %normal_state = phi i8 [ %state, %loop ]
  ret i8 %normal_state
exit0:
  %state0 = phi i8 [ %state_next, %check0 ]
  ret i8 %state0
exit1:
  %state1 = phi i8 [ %state_next, %check1 ]
  ret i8 %state1
exit2:
  %state2 = phi i8 [ %state_next, %check2 ]
  ret i8 %state2
exit3:
  %state3 = phi i8 [ %state_next, %check3 ]
  ret i8 %state3
}

define i8 @second_predicate_ne(
    i8 %raw_trip, i8 %b0, i8 %b1) {
entry:
  %trip = and i8 %raw_trip, 7
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ %index_next, %check1 ]
  %state = phi i8 [ 0, %entry ], [ %state_next, %check1 ]
  %continue = icmp ult i8 %index, %trip
  br i1 %continue, label %update, label %normal
update:
  %state_next = add i8 %state, 1
  %index_next = add i8 %index, 1
  br label %check0
check0:
  %break0 = icmp eq i8 %index, %b0
  br i1 %break0, label %exit0, label %check1
check1:
  %break1 = icmp ne i8 %index, %b1
  br i1 %break1, label %exit1, label %loop
normal:
  %normal_state = phi i8 [ %state, %loop ]
  ret i8 %normal_state
exit0:
  %state0 = phi i8 [ %state_next, %check0 ]
  ret i8 %state0
exit1:
  %state1 = phi i8 [ %state_next, %check1 ]
  ret i8 %state1
}

define i8 @reversed_first_priority_edge(
    i8 %raw_trip, i8 %b0, i8 %b1) {
entry:
  %trip = and i8 %raw_trip, 7
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ %index_next, %check1 ]
  %state = phi i8 [ 0, %entry ], [ %state_next, %check1 ]
  %continue = icmp ult i8 %index, %trip
  br i1 %continue, label %update, label %normal
update:
  %state_next = add i8 %state, 1
  %index_next = add i8 %index, 1
  br label %check0
check0:
  %break0 = icmp eq i8 %index, %b0
  br i1 %break0, label %check1, label %exit0
check1:
  %break1 = icmp eq i8 %index, %b1
  br i1 %break1, label %exit1, label %loop
normal:
  %normal_state = phi i8 [ %state, %loop ]
  ret i8 %normal_state
exit0:
  %state0 = phi i8 [ %state_next, %check0 ]
  ret i8 %state0
exit1:
  %state1 = phi i8 [ %state_next, %check1 ]
  ret i8 %state1
}

; REJECT-NOT: bounded-multi-break-loop-exit-v2
; REJECT-LABEL: define i8 @four_break_budget
; REJECT: loop:
; REJECT: check3:
; REJECT-LABEL: define i8 @second_predicate_ne
; REJECT: loop:
; REJECT: %break1 = icmp ne
; REJECT-LABEL: define i8 @reversed_first_priority_edge
; REJECT: loop:
; REJECT: br i1 %break0, label %check1, label %exit0
