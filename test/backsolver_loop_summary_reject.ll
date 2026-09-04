; RUN: env SYMCC_IFSS_LOOP_SUMMARY=1 %opt -load-pass-plugin=%passlib -passes=ifss-loop-summary -S %s -o %t.ll
; RUN: %opt -passes=verify -disable-output %t.ll
; RUN: %python -c "t=open(r'%t.ll').read(); assert 'loop-summary-v1' not in t; assert t.count('%'+'state = phi i8') == 9; assert t.count('br i1 %continue, label %latch, label %exit') == 9"
; RUN: %filecheck %s --input-file=%t.ll --check-prefix=REJECT

define i8 @unbounded(i8 %raw) {
entry:
  %limit = and i8 %raw, 15
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ %index_next, %latch ]
  %state = phi i8 [ 10, %entry ], [ %state_next, %latch ]
  %continue = icmp ult i8 %index, %limit
  br i1 %continue, label %latch, label %exit
latch:
  %state_next = add i8 %state, 3
  %index_next = add i8 %index, 1
  br label %loop
exit:
  ret i8 %state
}

define i8 @memory_effect(i8 %raw, i8* %pointer) {
entry:
  %limit = and i8 %raw, 7
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ %index_next, %latch ]
  %state = phi i8 [ 10, %entry ], [ %state_next, %latch ]
  %continue = icmp ult i8 %index, %limit
  br i1 %continue, label %latch, label %exit
latch:
  store i8 %state, i8* %pointer
  %state_next = add i8 %state, 3
  %index_next = add i8 %index, 1
  br label %loop
exit:
  ret i8 %state
}

define i8 @non_affine(i8 %raw) {
entry:
  %limit = and i8 %raw, 7
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ %index_next, %latch ]
  %state = phi i8 [ 2, %entry ], [ %state_next, %latch ]
  %continue = icmp ult i8 %index, %limit
  br i1 %continue, label %latch, label %exit
latch:
  %state_next = mul i8 %state, 3
  %index_next = add i8 %index, 1
  br label %loop
exit:
  ret i8 %state
}

define i8 @signed_condition(i8 %raw) {
entry:
  %limit = and i8 %raw, 7
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ %index_next, %latch ]
  %state = phi i8 [ 10, %entry ], [ %state_next, %latch ]
  %continue = icmp slt i8 %index, %limit
  br i1 %continue, label %latch, label %exit
latch:
  %state_next = add i8 %state, 3
  %index_next = add i8 %index, 1
  br label %loop
exit:
  ret i8 %state
}

define i8 @poison_flag(i8 %raw) {
entry:
  %limit = and i8 %raw, 7
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ %index_next, %latch ]
  %state = phi i8 [ 10, %entry ], [ %state_next, %latch ]
  %continue = icmp ult i8 %index, %limit
  br i1 %continue, label %latch, label %exit
latch:
  %state_next = add nuw i8 %state, 3
  %index_next = add i8 %index, 1
  br label %loop
exit:
  ret i8 %state
}

define i8 @poison_trip() {
entry:
  br label %loop
loop:
  %index = phi i3 [ 0, %entry ], [ %index_next, %latch ]
  %state = phi i8 [ 10, %entry ], [ %state_next, %latch ]
  %continue = icmp ult i3 %index, poison
  br i1 %continue, label %latch, label %exit
latch:
  %state_next = add i8 %state, 3
  %index_next = add i3 %index, 1
  br label %loop
exit:
  ret i8 %state
}

define i8 @reverse_dependency(i8 %raw) {
entry:
  %limit = and i8 %raw, 7
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ %index_next, %latch ]
  %left = phi i8 [ 1, %entry ], [ %left_next, %latch ]
  %state = phi i8 [ 2, %entry ], [ %state_next, %latch ]
  %continue = icmp ult i8 %index, %limit
  br i1 %continue, label %latch, label %exit
latch:
  %left_next = add i8 %left, 1
  %state_next = add i8 %state, %left
  %index_next = add i8 %index, 1
  br label %loop
exit:
  %result = add i8 %left, %state
  ret i8 %result
}

define i8 @non_unit_diagonal(i8 %raw) {
entry:
  %limit = and i8 %raw, 7
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ %index_next, %latch ]
  %state = phi i8 [ 1, %entry ], [ %state_next, %latch ]
  %right = phi i8 [ 2, %entry ], [ %right_next, %latch ]
  %continue = icmp ult i8 %index, %limit
  br i1 %continue, label %latch, label %exit
latch:
  %double = mul i8 %state, 2
  %state_next = add i8 %double, %right
  %right_next = add i8 %right, 1
  %index_next = add i8 %index, 1
  br label %loop
exit:
  %result = add i8 %state, %right
  ret i8 %result
}

define i16 @cross_width_dependency(i8 %raw) {
entry:
  %limit = and i8 %raw, 7
  br label %loop
loop:
  %index = phi i8 [ 0, %entry ], [ %index_next, %latch ]
  %state = phi i8 [ 1, %entry ], [ %state_next, %latch ]
  %wide = phi i16 [ 2, %entry ], [ %wide_next, %latch ]
  %continue = icmp ult i8 %index, %limit
  br i1 %continue, label %latch, label %exit
latch:
  %wide_narrow = trunc i16 %wide to i8
  %state_next = add i8 %state, %wide_narrow
  %wide_next = add i16 %wide, 1
  %index_next = add i8 %index, 1
  br label %loop
exit:
  %state_wide = zext i8 %state to i16
  %result = add i16 %state_wide, %wide
  ret i16 %result
}

; REJECT-NOT: loop-summary-v1
; REJECT-DAG: store i8 %state, ptr %pointer
; REJECT-DAG: %state_next = mul i8 %state, 3
; REJECT-DAG: %continue = icmp slt i8 %index, %limit
; REJECT-DAG: %state_next = add nuw i8 %state, 3
; REJECT-DAG: %continue = icmp ult i3 %index, poison
; REJECT-DAG: %state_next = add i8 %state, %left
; REJECT-DAG: %state_next = add i8 %double, %right
; REJECT-DAG: %wide_narrow = trunc i16 %wide to i8
