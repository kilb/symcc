; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.ll
; RUN: %opt -passes=verify -disable-output %t.ll
; RUN: %filecheck %s --input-file=%t.ll --check-prefix=REJECT

define i16 @newer_guard_after_partition(
    i8 %selector, i8 %key0, i8 %key1, i8 %new_key, ptr %slot) {
entry:
  %other0 = alloca i8, align 1
  %other1 = alloca i8, align 1
  %other.new = alloca i8, align 1
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %deep

left:
  store i16 4386, ptr %slot, align 2
  %high = getelementptr inbounds i8, ptr %slot, i64 1
  %guard0 = icmp eq i8 %key0, 0
  %guard1 = icmp eq i8 %key1, 0
  %true.pointer = select i1 %guard1, ptr %high, ptr %other0
  %false.pointer = select i1 %guard1, ptr %other1, ptr %high
  %partition.pointer = select i1 %guard0, ptr %true.pointer, ptr %false.pointer
  store i8 51, ptr %partition.pointer, align 1
  %new.guard = icmp eq i8 %new_key, 0
  %new.pointer = select i1 %new.guard, ptr %high, ptr %other.new
  store i8 68, ptr %new.pointer, align 1
  br label %continuation_a

deep:
  store i16 30600, ptr %slot, align 2
  br label %continuation_b

continuation_a:
  %tag_a = phi i16 [ 1, %left ]
  %state = load i16, ptr %slot, align 2
  ret i16 %state

continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

define i16 @two_older_guards_before_partition(
    i8 %selector, i8 %key0, i8 %key1, i8 %old0, i8 %old1,
    ptr %slot) {
entry:
  %other0 = alloca i8, align 1
  %other1 = alloca i8, align 1
  %other.old0 = alloca i8, align 1
  %other.old1 = alloca i8, align 1
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %deep

left:
  store i16 4386, ptr %slot, align 2
  %high = getelementptr inbounds i8, ptr %slot, i64 1
  %old0.guard = icmp eq i8 %old0, 0
  %old0.pointer = select i1 %old0.guard, ptr %high, ptr %other.old0
  store i8 34, ptr %old0.pointer, align 1
  %old1.guard = icmp eq i8 %old1, 0
  %old1.pointer = select i1 %old1.guard, ptr %high, ptr %other.old1
  store i8 35, ptr %old1.pointer, align 1
  %guard0 = icmp eq i8 %key0, 0
  %guard1 = icmp eq i8 %key1, 0
  %true.pointer = select i1 %guard1, ptr %high, ptr %other0
  %false.pointer = select i1 %guard1, ptr %other1, ptr %high
  %partition.pointer = select i1 %guard0, ptr %true.pointer, ptr %false.pointer
  store i8 51, ptr %partition.pointer, align 1
  br label %continuation_a

deep:
  store i16 30600, ptr %slot, align 2
  br label %continuation_b

continuation_a:
  %tag_a = phi i16 [ 1, %left ]
  %state = load i16, ptr %slot, align 2
  ret i16 %state

continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

define i16 @mixed_exit_priority_schemas(
    i8 %selector, i8 %key0, i8 %key1, i8 %old0, i8 %old1,
    ptr %slot) {
entry:
  %other0 = alloca i8, align 1
  %other1 = alloca i8, align 1
  %other.old0 = alloca i8, align 1
  %other.old1 = alloca i8, align 1
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %rest

left:
  store i16 4386, ptr %slot, align 2
  %high.left = getelementptr inbounds i8, ptr %slot, i64 1
  %old.left.guard = icmp eq i8 %old0, 0
  %old.left.pointer = select i1 %old.left.guard, ptr %high.left, ptr %other.old0
  store i8 34, ptr %old.left.pointer, align 1
  %guard0 = icmp eq i8 %key0, 0
  %guard1 = icmp eq i8 %key1, 0
  %true.pointer = select i1 %guard1, ptr %high.left, ptr %other0
  %false.pointer = select i1 %guard1, ptr %other1, ptr %high.left
  %partition.pointer = select i1 %guard0, ptr %true.pointer, ptr %false.pointer
  store i8 51, ptr %partition.pointer, align 1
  br label %continuation_a

rest:
  %is_b = icmp eq i8 %selector, 66
  br i1 %is_b, label %middle, label %deep

middle:
  store i16 17493, ptr %slot, align 2
  %high.middle = getelementptr inbounds i8, ptr %slot, i64 1
  %old.middle.guard = icmp eq i8 %old0, 1
  %old.middle.pointer = select i1 %old.middle.guard, ptr %high.middle, ptr %other.old0
  store i8 35, ptr %old.middle.pointer, align 1
  %new.middle.guard = icmp eq i8 %old1, 1
  %new.middle.pointer = select i1 %new.middle.guard, ptr %high.middle, ptr %other.old1
  store i8 36, ptr %new.middle.pointer, align 1
  br label %continuation_a

deep:
  store i16 30600, ptr %slot, align 2
  br label %continuation_b

continuation_a:
  %tag_a = phi i16 [ 1, %left ], [ 2, %middle ]
  %state = load i16, ptr %slot, align 2
  ret i16 %state

continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

; REJECT-LABEL: define i16 @newer_guard_after_partition
; REJECT: %ifss.cont.byte.partition
; REJECT: %ifss.cont.byte.writer
; REJECT: %ifss.cont.memory = phi i16
; REJECT-LABEL: define i16 @two_older_guards_before_partition
; REJECT: %ifss.cont.byte.writer
; REJECT: %ifss.cont.byte.partition
; REJECT: %ifss.cont.memory = phi i16
; REJECT-LABEL: define i16 @mixed_exit_priority_schemas
; REJECT-NOT: symcc.ifss_continuation_memory
; REJECT: %state = load i16, ptr %slot
; REJECT: ordered-writer-graph-continuation-memory-tuple-v15
