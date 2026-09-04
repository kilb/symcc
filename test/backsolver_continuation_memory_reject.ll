; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.ll
; RUN: %opt -passes=verify -disable-output %t.ll
; RUN: %filecheck %s --input-file=%t.ll --check-prefix=REJECT

define i8 @may_alias_interference(
    i1 %condition, i8* %slot, i8* %maybe_alias) {
entry:
  br i1 %condition, label %left, label %right
left:
  store i8 10, i8* %slot, align 1
  store i8 1, i8* %maybe_alias, align 1
  br label %continuation_a
right:
  store i8 20, i8* %slot, align 1
  br label %continuation_b
continuation_a:
  %a_tag = phi i8 [ 1, %left ]
  %a = load i8, i8* %slot, align 1
  ret i8 %a
continuation_b:
  %b_tag = phi i8 [ 2, %right ]
  ret i8 %b_tag
}

define i8 @missing_store(i1 %condition, i8* %slot) {
entry:
  br i1 %condition, label %left, label %right
left:
  br label %continuation_a
right:
  store i8 20, i8* %slot, align 1
  br label %continuation_b
continuation_a:
  %a_tag = phi i8 [ 1, %left ]
  %a = load i8, i8* %slot, align 1
  ret i8 %a
continuation_b:
  %b_tag = phi i8 [ 2, %right ]
  ret i8 %b_tag
}

define i8 @volatile_load(i1 %condition, i8* %slot) {
entry:
  br i1 %condition, label %left, label %right
left:
  store i8 10, i8* %slot, align 1
  br label %continuation_a
right:
  store i8 20, i8* %slot, align 1
  br label %continuation_b
continuation_a:
  %a_tag = phi i8 [ 1, %left ]
  %a = load volatile i8, i8* %slot, align 1
  ret i8 %a
continuation_b:
  %b_tag = phi i8 [ 2, %right ]
  ret i8 %b_tag
}

define i8 @external_predecessor(
    i1 %condition, i8 %external, i8* %slot) {
entry:
  switch i8 %external, label %controller [
    i8 1, label %outside
  ]
controller:
  br i1 %condition, label %left, label %right
left:
  store i8 10, i8* %slot, align 1
  br label %continuation_a
right:
  store i8 20, i8* %slot, align 1
  br label %continuation_b
outside:
  br label %continuation_a
continuation_a:
  %a_tag = phi i8 [ 1, %left ], [ 3, %outside ]
  %a = load i8, i8* %slot, align 1
  ret i8 %a
continuation_b:
  %b_tag = phi i8 [ 2, %right ]
  ret i8 %b_tag
}

define i8 @destination_prefix_write(
    i1 %condition, i8* %slot, i8* %noise) {
entry:
  br i1 %condition, label %left, label %right
left:
  store i8 10, i8* %slot, align 1
  br label %continuation_a
right:
  store i8 20, i8* %slot, align 1
  br label %continuation_b
continuation_a:
  %a_tag = phi i8 [ 1, %left ]
  store i8 7, i8* %noise, align 1
  %a = load i8, i8* %slot, align 1
  ret i8 %a
continuation_b:
  %b_tag = phi i8 [ 2, %right ]
  ret i8 %b_tag
}

define i8 @memory_slot_budget(i1 %condition, i8* %slot) {
entry:
  br i1 %condition, label %left, label %right
left:
  store i8 10, i8* %slot, align 1
  br label %continuation_a
right:
  store i8 20, i8* %slot, align 1
  br label %continuation_b
continuation_a:
  %a_tag = phi i8 [ 1, %left ]
  %a0 = load i8, i8* %slot, align 1
  %a1 = load i8, i8* %slot, align 1
  %a2 = load i8, i8* %slot, align 1
  %a3 = load i8, i8* %slot, align 1
  %a4 = load i8, i8* %slot, align 1
  %sum0 = add i8 %a0, %a1
  %sum1 = add i8 %sum0, %a2
  %sum2 = add i8 %sum1, %a3
  %sum3 = add i8 %sum2, %a4
  ret i8 %sum3
continuation_b:
  %b_tag = phi i8 [ 2, %right ]
  ret i8 %b_tag
}

define i8 @partial_initial_may_alias(
    i8 %selector, ptr %slot, ptr %maybe_alias) {
entry:
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %rest
left:
  store i8 10, ptr %slot, align 1
  br label %continuation_a
rest:
  %is_b = icmp eq i8 %selector, 66
  br i1 %is_b, label %middle, label %deep
middle:
  store i8 1, ptr %maybe_alias, align 1
  br label %continuation_a
deep:
  store i8 30, ptr %slot, align 1
  br label %continuation_b
continuation_a:
  %tag_a = phi i8 [ 1, %left ], [ 2, %middle ]
  %state = load i8, ptr %slot, align 1
  ret i8 %state
continuation_b:
  %tag_b = phi i8 [ 3, %deep ]
  ret i8 %tag_b
}

define i8 @nested_memoryphi_may_alias(
    i8 %selector, i1 %inner, ptr %slot, ptr %maybe_alias) {
entry:
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %nested.entry, label %rest
nested.entry:
  br i1 %inner, label %nested.left, label %nested.right
nested.left:
  store i8 10, ptr %slot, align 1
  br label %nested.merge
nested.right:
  store i8 20, ptr %slot, align 1
  store i8 1, ptr %maybe_alias, align 1
  br label %nested.merge
nested.merge:
  br label %continuation_a
rest:
  %is_b = icmp eq i8 %selector, 66
  br i1 %is_b, label %middle, label %deep
middle:
  store i8 30, ptr %slot, align 1
  br label %continuation_a
deep:
  store i8 40, ptr %slot, align 1
  br label %continuation_b
continuation_a:
  %tag_a = phi i8 [ 1, %nested.merge ], [ 2, %middle ]
  %state = load i8, ptr %slot, align 1
  ret i8 %state
continuation_b:
  %tag_b = phi i8 [ 3, %deep ]
  ret i8 %tag_b
}

define i8 @nested_memoryphi_arity_budget(
    i8 %selector, i1 %c0, i1 %c1, i1 %c2, i1 %c3, ptr %slot) {
entry:
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %nested.entry, label %rest
nested.entry:
  br i1 %c0, label %nested.0, label %nested.next1
nested.next1:
  br i1 %c1, label %nested.1, label %nested.next2
nested.next2:
  br i1 %c2, label %nested.2, label %nested.next3
nested.next3:
  br i1 %c3, label %nested.3, label %nested.4
nested.0:
  store i8 10, ptr %slot, align 1
  br label %nested.merge
nested.1:
  store i8 11, ptr %slot, align 1
  br label %nested.merge
nested.2:
  store i8 12, ptr %slot, align 1
  br label %nested.merge
nested.3:
  store i8 13, ptr %slot, align 1
  br label %nested.merge
nested.4:
  store i8 14, ptr %slot, align 1
  br label %nested.merge
nested.merge:
  br label %continuation_a
rest:
  %is_b = icmp eq i8 %selector, 66
  br i1 %is_b, label %middle, label %deep
middle:
  store i8 30, ptr %slot, align 1
  br label %continuation_a
deep:
  store i8 40, ptr %slot, align 1
  br label %continuation_b
continuation_a:
  %tag_a = phi i8 [ 1, %nested.merge ], [ 2, %middle ]
  %state = load i8, ptr %slot, align 1
  ret i8 %state
continuation_b:
  %tag_b = phi i8 [ 3, %deep ]
  ret i8 %tag_b
}

define i16 @byte_lane_dynamic_offset(
    i8 %selector, i64 %offset, ptr %slot) {
entry:
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %rest
left:
  store i16 4386, ptr %slot, align 2
  %dynamic = getelementptr inbounds i8, ptr %slot, i64 %offset
  store i8 51, ptr %dynamic, align 1
  br label %continuation_a
rest:
  %is_b = icmp eq i8 %selector, 66
  br i1 %is_b, label %middle, label %deep
middle:
  store i16 17493, ptr %slot, align 2
  br label %continuation_a
deep:
  store i16 30600, ptr %slot, align 2
  br label %continuation_b
continuation_a:
  %tag_a = phi i16 [ 1, %left ], [ 2, %middle ]
  %state = load i16, ptr %slot, align 2
  %result = add i16 %state, %tag_a
  ret i16 %result
continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

define i16 @byte_lane_volatile_store(i8 %selector, ptr %slot) {
entry:
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %rest
left:
  store i16 4386, ptr %slot, align 2
  %high = getelementptr inbounds i8, ptr %slot, i64 1
  store volatile i8 51, ptr %high, align 1
  br label %continuation_a
rest:
  %is_b = icmp eq i8 %selector, 66
  br i1 %is_b, label %middle, label %deep
middle:
  store i16 17493, ptr %slot, align 2
  br label %continuation_a
deep:
  store i16 30600, ptr %slot, align 2
  br label %continuation_b
continuation_a:
  %tag_a = phi i16 [ 1, %left ], [ 2, %middle ]
  %state = load i16, ptr %slot, align 2
  %result = add i16 %state, %tag_a
  ret i16 %result
continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

define i16 @byte_lane_memoryphi(
    i8 %selector, i1 %inner, ptr %slot) {
entry:
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %nested.entry, label %rest
nested.entry:
  br i1 %inner, label %nested.left, label %nested.right
nested.left:
  store i8 17, ptr %slot, align 1
  br label %nested.merge
nested.right:
  store i8 34, ptr %slot, align 1
  br label %nested.merge
nested.merge:
  br label %continuation_a
rest:
  %is_b = icmp eq i8 %selector, 66
  br i1 %is_b, label %middle, label %deep
middle:
  store i16 17493, ptr %slot, align 2
  br label %continuation_a
deep:
  store i16 30600, ptr %slot, align 2
  br label %continuation_b
continuation_a:
  %tag_a = phi i16 [ 1, %nested.merge ], [ 2, %middle ]
  %state = load i16, ptr %slot, align 2
  %result = add i16 %state, %tag_a
  ret i16 %result
continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

; REJECT-NOT: must-alias-continuation-memory-tuple-v1
; REJECT-NOT: must-alias-or-live-on-entry-continuation-memory-tuple-v2
; REJECT-NOT: bounded-acyclic-memoryphi-continuation-memory-tuple-v3
; REJECT-NOT: byte-lane-continuation-memory-tuple-v4
; REJECT-NOT: symcc.ifss_continuation_memory
; REJECT-LABEL: define i8 @may_alias_interference
; REJECT: %a = load i8, ptr %slot
; REJECT-LABEL: define i8 @missing_store
; REJECT: %a = load i8, ptr %slot
; REJECT-LABEL: define i8 @volatile_load
; REJECT: %a = load volatile i8, ptr %slot
; REJECT-LABEL: define i8 @external_predecessor
; REJECT: %a = load i8, ptr %slot
; REJECT-LABEL: define i8 @destination_prefix_write
; REJECT: %a = load i8, ptr %slot
; REJECT-LABEL: define i8 @memory_slot_budget
; REJECT: %a4 = load i8, ptr %slot
; REJECT-LABEL: define i8 @partial_initial_may_alias
; REJECT: %state = load i8, ptr %slot
; REJECT-LABEL: define i8 @nested_memoryphi_may_alias
; REJECT: %state = load i8, ptr %slot
; REJECT-LABEL: define i8 @nested_memoryphi_arity_budget
; REJECT: %state = load i8, ptr %slot
; REJECT-LABEL: define i16 @byte_lane_dynamic_offset
; REJECT: %state = load i16, ptr %slot
; REJECT-LABEL: define i16 @byte_lane_volatile_store
; REJECT: %state = load i16, ptr %slot
; REJECT-LABEL: define i16 @byte_lane_memoryphi
; REJECT: %state = load i16, ptr %slot
