; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.ll
; RUN: %opt -passes=verify -disable-output %t.ll
; RUN: %filecheck %s --input-file=%t.ll --check-prefix=REJECT

define i16 @depth_three(
    i8 %selector, i8 %key, ptr %slot) {
entry:
  %other = alloca i8, align 1
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %deep
left:
  store i16 4386, ptr %slot, align 2
  %high = getelementptr inbounds i8, ptr %slot, i64 1
  %g0 = icmp eq i8 %key, 0
  %g1 = icmp eq i8 %key, 1
  %g2 = icmp eq i8 %key, 2
  %level2 = select i1 %g2, ptr %high, ptr %other
  %level1 = select i1 %g1, ptr %level2, ptr %other
  %root = select i1 %g0, ptr %level1, ptr %other
  store i8 51, ptr %root, align 1
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

define i16 @unknown_leaf(
    i8 %selector, i8 %key, ptr %slot, ptr %unknown) {
entry:
  %other = alloca i8, align 1
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %deep
left:
  store i16 4386, ptr %slot, align 2
  %high = getelementptr inbounds i8, ptr %slot, i64 1
  %g0 = icmp eq i8 %key, 0
  %g1 = icmp eq i8 %key, 1
  %inner = select i1 %g1, ptr %high, ptr %unknown
  %root = select i1 %g0, ptr %inner, ptr %other
  store i8 51, ptr %root, align 1
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

define i16 @shared_select_dag(
    i8 %selector, i8 %key, ptr %slot) {
entry:
  %other = alloca i8, align 1
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %deep
left:
  store i16 4386, ptr %slot, align 2
  %high = getelementptr inbounds i8, ptr %slot, i64 1
  %g0 = icmp eq i8 %key, 0
  %g1 = icmp eq i8 %key, 1
  %shared = select i1 %g1, ptr %high, ptr %other
  %root = select i1 %g0, ptr %shared, ptr %shared
  store i8 51, ptr %root, align 1
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

define i16 @two_partition_stores(
    i8 %selector, i8 %key, ptr %slot) {
entry:
  %other0 = alloca i8, align 1
  %other1 = alloca i8, align 1
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %deep
left:
  store i16 4386, ptr %slot, align 2
  %high = getelementptr inbounds i8, ptr %slot, i64 1
  %g0 = icmp eq i8 %key, 0
  %g1 = icmp eq i8 %key, 1
  %inner0 = select i1 %g1, ptr %high, ptr %other0
  %root0 = select i1 %g0, ptr %inner0, ptr %other1
  store i8 51, ptr %root0, align 1
  %inner1 = select i1 %g1, ptr %high, ptr %other1
  %root1 = select i1 %g0, ptr %inner1, ptr %other0
  store i8 52, ptr %root1, align 1
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

; REJECT-LABEL: define i16 @depth_three
; REJECT-NOT: symcc.ifss_continuation_memory
; REJECT: %state = load i16, ptr %slot
; REJECT-LABEL: define i16 @unknown_leaf
; REJECT-NOT: symcc.ifss_continuation_memory
; REJECT: %state = load i16, ptr %slot
; REJECT-LABEL: define i16 @shared_select_dag
; REJECT-NOT: symcc.ifss_continuation_memory
; REJECT: %state = load i16, ptr %slot
; REJECT-LABEL: define i16 @two_partition_stores
; REJECT: %ifss.cont.byte.partition
; REJECT: %ifss.cont.memory = phi i16
; REJECT: ordered-writer-graph-continuation-memory-tuple-v15
