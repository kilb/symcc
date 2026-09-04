; RUN: env SYMCC_IFSS_LOOP_SUMMARY=1 %opt -load-pass-plugin=%passlib -passes=ifss-loop-summary -S %s -o %t.ll
; RUN: %opt -passes=verify -disable-output %t.ll
; RUN: %python -c "t=open(r'%t.ll').read(); assert 'bounded-break-loop-exit-v1' not in t; assert t.count('%'+'state = phi i8')==4; assert t.count('%'+'should_break = icmp')==4"

define i8 @not_equal(i8 %trip, i8 %break_at) {
entry:
  %limit = and i8 %trip, 7
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ %index_next, %latch ]
  %state = phi i8 [ 0, %entry ], [ %state_next, %latch ]
  %continue = icmp ult i8 %index, %limit
  br i1 %continue, label %latch, label %normal
latch:
  %state_next = add i8 %state, 1
  %index_next = add i8 %index, 1
  %should_break = icmp ne i8 %index, %break_at
  br i1 %should_break, label %break, label %loop
normal:
  %normal_state = phi i8 [ %state, %loop ]
  ret i8 %normal_state
break:
  %break_state = phi i8 [ %state_next, %latch ]
  ret i8 %break_state
}

define i8 @updated_induction(i8 %trip, i8 %break_at) {
entry:
  %limit = and i8 %trip, 7
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ %index_next, %latch ]
  %state = phi i8 [ 0, %entry ], [ %state_next, %latch ]
  %continue = icmp ult i8 %index, %limit
  br i1 %continue, label %latch, label %normal
latch:
  %state_next = add i8 %state, 1
  %index_next = add i8 %index, 1
  %should_break = icmp eq i8 %index_next, %break_at
  br i1 %should_break, label %break, label %loop
normal:
  %normal_state = phi i8 [ %state, %loop ]
  ret i8 %normal_state
break:
  %break_state = phi i8 [ %state_next, %latch ]
  ret i8 %break_state
}

define i8 @reversed_successors(i8 %trip, i8 %break_at) {
entry:
  %limit = and i8 %trip, 7
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ %index_next, %latch ]
  %state = phi i8 [ 0, %entry ], [ %state_next, %latch ]
  %continue = icmp ult i8 %index, %limit
  br i1 %continue, label %latch, label %normal
latch:
  %state_next = add i8 %state, 1
  %index_next = add i8 %index, 1
  %should_break = icmp eq i8 %index, %break_at
  br i1 %should_break, label %loop, label %break
normal:
  %normal_state = phi i8 [ %state, %loop ]
  ret i8 %normal_state
break:
  %break_state = phi i8 [ %state_next, %latch ]
  ret i8 %break_state
}

define i8 @direct_break_use(i8 %trip, i8 %break_at) {
entry:
  %limit = and i8 %trip, 7
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ %index_next, %latch ]
  %state = phi i8 [ 0, %entry ], [ %state_next, %latch ]
  %continue = icmp ult i8 %index, %limit
  br i1 %continue, label %latch, label %normal
latch:
  %state_next = add i8 %state, 1
  %index_next = add i8 %index, 1
  %should_break = icmp eq i8 %index, %break_at
  br i1 %should_break, label %break, label %loop
normal:
  %normal_state = phi i8 [ %state, %loop ]
  ret i8 %normal_state
break:
  ret i8 %state_next
}
