; RUN: env SYMCC_IFSS_CONTINUATION_STATE=1 SYMCC_IFSS_CONTINUATION_MEMORY=1 %opt -load-pass-plugin=%passlib -passes=ifss-continuation-lowering,ifss-continuation-memory -S %s -o %t.ll
; RUN: %opt -passes=verify -disable-output %t.ll
; RUN: %filecheck %s --input-file=%t.ll --check-prefix=REJECT

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i16 @unknown_heap_extent(
    i8 %selector, i64 %extent, i64 %index) {
entry:
  %heap = call ptr @malloc(i64 %extent)
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %deep

left:
  store i16 4386, ptr %heap, align 2
  %dynamic = getelementptr inbounds i8, ptr %heap, i64 %index
  store i8 51, ptr %dynamic, align 1
  br label %continuation_a

deep:
  store i16 30600, ptr %heap, align 2
  br label %continuation_b

continuation_a:
  %tag_a = phi i16 [ 1, %left ]
  %state = load i16, ptr %heap, align 2
  ret i16 %state

continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

define i16 @external_heap_identity(
    i8 %selector, i64 %index, ptr %heap) {
entry:
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %deep

left:
  store i16 4386, ptr %heap, align 2
  %dynamic = getelementptr inbounds i8, ptr %heap, i64 %index
  store i8 51, ptr %dynamic, align 1
  br label %continuation_a

deep:
  store i16 30600, ptr %heap, align 2
  br label %continuation_b

continuation_a:
  %tag_a = phi i16 [ 1, %left ]
  %state = load i16, ptr %heap, align 2
  ret i16 %state

continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

define i16 @mismatched_calloc_prototype(
    i8 %selector, i64 %index) {
entry:
  %heap = call ptr @calloc(i32 2, i32 4)
  %is_a = icmp eq i8 %selector, 65
  br i1 %is_a, label %left, label %deep

left:
  store i16 4386, ptr %heap, align 2
  %dynamic = getelementptr inbounds i8, ptr %heap, i64 %index
  store i8 51, ptr %dynamic, align 1
  br label %continuation_a

deep:
  store i16 30600, ptr %heap, align 2
  br label %continuation_b

continuation_a:
  %tag_a = phi i16 [ 1, %left ]
  %state = load i16, ptr %heap, align 2
  ret i16 %state

continuation_b:
  %tag_b = phi i16 [ 3, %deep ]
  ret i16 %tag_b
}

declare noalias ptr @malloc(i64)
declare noalias ptr @calloc(i32, i32)

; REJECT-LABEL: define i16 @unknown_heap_extent
; REJECT-NOT: symcc.ifss_continuation_memory
; REJECT: %state = load i16, ptr %heap
; REJECT-LABEL: define i16 @external_heap_identity
; REJECT-NOT: symcc.ifss_continuation_memory
; REJECT: %state = load i16, ptr %heap
; REJECT-LABEL: define i16 @mismatched_calloc_prototype
; REJECT-NOT: symcc.ifss_continuation_memory
; REJECT: %state = load i16, ptr %heap
