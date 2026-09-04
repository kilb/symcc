; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.switch.json --entry memoryssa_switch_heap_initialization
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.switch.json --input-hex 00 --expect-values 17,34,51 --expect-heap --expect-pointer-union --expect-shared-phi-edge-discriminator --expect-collective-heap-union-initialization --expect-memoryssa-aa-heap-initialization
; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.single.json --entry memoryssa_single_object_initialization
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.single.json --input-hex 00 --expect-values 68,85 --expect-heap --expect-pointer-union --expect-memoryssa-aa-heap-initialization
; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.nested.json --entry memoryssa_nested_phi_initialization
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.nested.json --input-hex 00 --expect-values 102,119,136 --expect-heap --expect-pointer-union --expect-memoryssa-aa-heap-initialization
; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.modref.json --entry memoryssa_no_modref_call_initialization
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.modref.json --input-hex 00 --expect-values 153,170 --expect-heap --expect-pointer-union --expect-memoryssa-aa-heap-initialization --expect-memoryssa-aa-no-modref
; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.disjoint.json --entry memoryssa_same_object_disjoint_initialization
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.disjoint.json --input-hex 00 --expect-values 187,204 --expect-heap --expect-pointer-union --expect-memoryssa-aa-heap-initialization
; RUN: not env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.missing.json --entry bad_memoryssa_missing_initialization
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.missing.json --expect-rejected "heap load lacks a dominating"
; RUN: not env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.partial.json --entry bad_memoryssa_partial_initialization
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.partial.json --expect-rejected "heap load lacks a dominating"
; RUN: %python %S/../benchmark/generate_memoryssa_aa_heap_initialization_fixture.py --paths 64 --output %t.fan64.ll
; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.fan64.ll --output %t.fan64.json --entry generated_memoryssa_aa_heap_initialization
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.fan64.json --expect-values 1 --expect-heap --expect-pointer-union --expect-shared-phi-edge-discriminator --expect-collective-heap-union-initialization --expect-memoryssa-aa-heap-initialization

declare ptr @malloc(i64)

define internal void @touch_inaccessible_memory() #0 {
entry:
  ret void
}

define i8 @memoryssa_switch_heap_initialization(i8 %selector) {
entry:
  %first = call ptr @malloc(i64 1)
  %second = call ptr @malloc(i64 1)
  %third = call ptr @malloc(i64 1)
  %scratch = call ptr @malloc(i64 1)
  switch i8 %selector, label %initialize_third [
    i8 0, label %initialize_first
    i8 1, label %initialize_second
  ]

initialize_first:
  store i8 17, ptr %first, align 1
  store i8 1, ptr %scratch, align 1
  br label %merge

initialize_second:
  store i8 34, ptr %second, align 1
  store i8 2, ptr %scratch, align 1
  br label %merge

initialize_third:
  store i8 51, ptr %third, align 1
  store i8 3, ptr %scratch, align 1
  br label %merge

merge:
  %selected = phi ptr [ %first, %initialize_first ],
                      [ %second, %initialize_second ],
                      [ %third, %initialize_third ]
  %value = load i8, ptr %selected, align 1
  %is_first = icmp eq i8 %value, 17
  br i1 %is_first, label %first_value, label %check_second

check_second:
  %is_second = icmp eq i8 %value, 34
  br i1 %is_second, label %second_value, label %third_value

first_value:
  ret i8 17

second_value:
  ret i8 34

third_value:
  ret i8 51
}

define i8 @memoryssa_single_object_initialization(i1 %choose) {
entry:
  %object = call ptr @malloc(i64 1)
  %scratch = call ptr @malloc(i64 1)
  br i1 %choose, label %initialize_left, label %initialize_right

initialize_left:
  store i8 68, ptr %object, align 1
  store i8 1, ptr %scratch, align 1
  br label %merge

initialize_right:
  store i8 85, ptr %object, align 1
  store i8 2, ptr %scratch, align 1
  br label %merge

merge:
  %value = load i8, ptr %object, align 1
  %is_left = icmp eq i8 %value, 68
  br i1 %is_left, label %left_value, label %right_value

left_value:
  ret i8 68

right_value:
  ret i8 85
}

define i8 @memoryssa_nested_phi_initialization(i8 %selector) {
entry:
  %object = call ptr @malloc(i64 1)
  %scratch = call ptr @malloc(i64 1)
  %go_left = icmp ult i8 %selector, 2
  %left_first = icmp eq i8 %selector, 0
  br i1 %go_left, label %left_dispatch, label %initialize_right

left_dispatch:
  br i1 %left_first, label %initialize_first, label %initialize_second

initialize_first:
  store i8 102, ptr %object, align 1
  store i8 1, ptr %scratch, align 1
  br label %left_join

initialize_second:
  store i8 119, ptr %object, align 1
  store i8 2, ptr %scratch, align 1
  br label %left_join

left_join:
  br label %merge

initialize_right:
  store i8 136, ptr %object, align 1
  store i8 3, ptr %scratch, align 1
  br label %merge

merge:
  %value = load i8, ptr %object, align 1
  %is_first = icmp eq i8 %value, 102
  br i1 %is_first, label %first_value, label %check_second

check_second:
  %is_second = icmp eq i8 %value, 119
  br i1 %is_second, label %second_value, label %third_value

first_value:
  ret i8 102

second_value:
  ret i8 119

third_value:
  ret i8 136
}

define i8 @memoryssa_no_modref_call_initialization(i1 %choose) {
entry:
  %object = call ptr @malloc(i64 1)
  br i1 %choose, label %initialize_left, label %initialize_right

initialize_left:
  store i8 153, ptr %object, align 1
  call void @touch_inaccessible_memory()
  br label %merge

initialize_right:
  store i8 170, ptr %object, align 1
  call void @touch_inaccessible_memory()
  br label %merge

merge:
  %value = load i8, ptr %object, align 1
  %is_left = icmp eq i8 %value, 153
  br i1 %is_left, label %left_value, label %right_value

left_value:
  ret i8 153

right_value:
  ret i8 170
}

define i8 @memoryssa_same_object_disjoint_initialization(i1 %choose) {
entry:
  %object = call ptr @malloc(i64 2)
  %scratch = getelementptr i8, ptr %object, i64 1
  br i1 %choose, label %initialize_left, label %initialize_right

initialize_left:
  store i8 187, ptr %object, align 1
  store i8 1, ptr %scratch, align 1
  br label %merge

initialize_right:
  store i8 204, ptr %object, align 1
  store i8 2, ptr %scratch, align 1
  br label %merge

merge:
  %value = load i8, ptr %object, align 1
  %is_left = icmp eq i8 %value, 187
  br i1 %is_left, label %left_value, label %right_value

left_value:
  ret i8 187

right_value:
  ret i8 204
}

define i8 @bad_memoryssa_missing_initialization(i1 %choose) {
entry:
  %object = call ptr @malloc(i64 1)
  br i1 %choose, label %initialize, label %skip

initialize:
  store i8 1, ptr %object, align 1
  br label %merge

skip:
  br label %merge

merge:
  %value = load i8, ptr %object, align 1
  ret i8 %value
}

define i16 @bad_memoryssa_partial_initialization(i1 %choose) {
entry:
  %object = call ptr @malloc(i64 2)
  br i1 %choose, label %initialize_left, label %initialize_right

initialize_left:
  store i8 1, ptr %object, align 1
  br label %merge

initialize_right:
  store i8 2, ptr %object, align 1
  br label %merge

merge:
  %value = load i16, ptr %object, align 1
  ret i16 %value
}

attributes #0 = { inaccessiblememonly nounwind }
