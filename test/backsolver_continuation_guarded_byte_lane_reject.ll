; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.ll
; RUN: %opt -passes=verify -disable-output %t.ll
; RUN: %filecheck %s --input-file=%t.ll --check-prefix=REJECT

define i16 @unknown_alias_arm(
    i8 %selector, i8 %key, ptr %slot, ptr %maybe_alias) {
entry:
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %rest
left:
  store i16 4386, ptr %slot, align 2
  %high = getelementptr inbounds i8, ptr %slot, i64 1
  %guard = icmp eq i8 %key, 1
  %alias = select i1 %guard, ptr %high, ptr %maybe_alias
  store i8 51, ptr %alias, align 1
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

define i16 @three_guarded_stores(i8 %selector, i8 %key, ptr %slot) {
entry:
  %other0 = alloca i8, align 1
  %other1 = alloca i8, align 1
  %other2 = alloca i8, align 1
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %rest
left:
  store i16 4386, ptr %slot, align 2
  %high = getelementptr inbounds i8, ptr %slot, i64 1
  %guard0 = icmp eq i8 %key, 1
  %alias0 = select i1 %guard0, ptr %high, ptr %other0
  store i8 51, ptr %alias0, align 1
  %guard1 = icmp eq i8 %key, 2
  %alias1 = select i1 %guard1, ptr %slot, ptr %other1
  store i8 68, ptr %alias1, align 1
  %guard2 = icmp eq i8 %key, 3
  %alias2 = select i1 %guard2, ptr %high, ptr %other2
  store i8 85, ptr %alias2, align 1
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

define i16 @argument_guard(
    i8 %selector, i1 %guard, ptr %slot) {
entry:
  %other = alloca i8, align 1
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %rest
left:
  store i16 4386, ptr %slot, align 2
  %high = getelementptr inbounds i8, ptr %slot, i64 1
  %alias = select i1 %guard, ptr %high, ptr %other
  store i8 51, ptr %alias, align 1
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

; REJECT-LABEL: define i16 @unknown_alias_arm
; REJECT: %state = load i16, ptr %slot
; REJECT-LABEL: define i16 @three_guarded_stores
; REJECT: %state = load i16, ptr %slot
; REJECT-LABEL: define i16 @argument_guard
; REJECT: %state = load i16, ptr %slot
; REJECT-NOT: guarded-byte-lane-continuation-memory-tuple-v5
; REJECT-NOT: guarded-write-priority-continuation-memory-tuple-v8
