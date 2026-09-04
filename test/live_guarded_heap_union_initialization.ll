; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.select.json --entry guarded_select_initialization
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.select.json --input-hex 00 --expect-values 11,22 --expect-heap --expect-pointer-union --expect-collective-heap-union-initialization --expect-guard-correlated-heap-union-initialization
; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.phi.json --entry guarded_phi_initialization
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.phi.json --input-hex 00 --expect-values 31,42 --expect-heap --expect-pointer-union --expect-shared-phi-edge-discriminator --expect-collective-heap-union-initialization --expect-guard-correlated-heap-union-initialization
; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.nested.json --entry nested_guarded_select_initialization
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.nested.json --input-hex 00 --expect-values 51,62,73,84 --expect-heap --expect-pointer-union --expect-collective-heap-union-initialization --expect-guard-correlated-heap-union-initialization
; RUN: not env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.mismatch.json --entry bad_guard_store_mismatch
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.mismatch.json --expect-rejected "heap load lacks a dominating"
; RUN: not env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.missing.json --entry bad_missing_path_initialization
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.missing.json --expect-rejected "heap load lacks a dominating"
; RUN: not env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.partial.json --entry bad_partial_path_initialization
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.partial.json --expect-rejected "heap load lacks a dominating"
; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.repeated.json --entry bad_conflicting_repeated_guard
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.repeated.json --expect-values 1,4 --expect-heap --expect-pointer-union --expect-shared-phi-edge-discriminator --expect-collective-heap-union-initialization --expect-memoryssa-aa-heap-initialization
; RUN: %python %S/../benchmark/generate_guarded_heap_union_initialization_fixture.py --depth 6 --output %t.depth6.ll
; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.depth6.ll --output %t.depth6.json --entry generated_guarded_heap_union_initialization
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.depth6.json --expect-values 1 --expect-heap --expect-pointer-union --expect-shared-phi-edge-discriminator --expect-collective-heap-union-initialization --expect-guard-correlated-heap-union-initialization

declare ptr @malloc(i64)

define i8 @guarded_select_initialization(i1 %choose) {
entry:
  %left = call ptr @malloc(i64 1)
  %right = call ptr @malloc(i64 1)
  br i1 %choose, label %initialize_left, label %initialize_right

initialize_left:
  store i8 11, ptr %left, align 1
  br label %merge

initialize_right:
  store i8 22, ptr %right, align 1
  br label %merge

merge:
  %selected = select i1 %choose, ptr %left, ptr %right
  %value = load i8, ptr %selected, align 1
  %is_left = icmp eq i8 %value, 11
  br i1 %is_left, label %left_value, label %right_value

left_value:
  ret i8 11

right_value:
  ret i8 22
}

define i8 @guarded_phi_initialization(i1 %choose) {
entry:
  %left = call ptr @malloc(i64 1)
  %right = call ptr @malloc(i64 1)
  br i1 %choose, label %initialize_left, label %initialize_right

initialize_left:
  store i8 31, ptr %left, align 1
  br label %merge

initialize_right:
  store i8 42, ptr %right, align 1
  br label %merge

merge:
  %selected = phi ptr [ %left, %initialize_left ], [ %right, %initialize_right ]
  %value = load i8, ptr %selected, align 1
  %is_left = icmp eq i8 %value, 31
  br i1 %is_left, label %left_value, label %right_value

left_value:
  ret i8 31

right_value:
  ret i8 42
}

define i8 @nested_guarded_select_initialization(i8 %selector) {
entry:
  %first = call ptr @malloc(i64 1)
  %second = call ptr @malloc(i64 1)
  %third = call ptr @malloc(i64 1)
  %fourth = call ptr @malloc(i64 1)
  %left_half = icmp ult i8 %selector, 2
  %first_leaf = icmp eq i8 %selector, 0
  %third_leaf = icmp eq i8 %selector, 2
  br i1 %left_half, label %choose_left, label %choose_right

choose_left:
  br i1 %first_leaf, label %initialize_first, label %initialize_second

choose_right:
  br i1 %third_leaf, label %initialize_third, label %initialize_fourth

initialize_first:
  store i8 51, ptr %first, align 1
  br label %merge

initialize_second:
  store i8 62, ptr %second, align 1
  br label %merge

initialize_third:
  store i8 73, ptr %third, align 1
  br label %merge

initialize_fourth:
  store i8 84, ptr %fourth, align 1
  br label %merge

merge:
  %left_selected = select i1 %first_leaf, ptr %first, ptr %second
  %right_selected = select i1 %third_leaf, ptr %third, ptr %fourth
  %selected = select i1 %left_half, ptr %left_selected, ptr %right_selected
  %value = load i8, ptr %selected, align 1
  %is_first = icmp eq i8 %value, 51
  br i1 %is_first, label %first_value, label %check_second

check_second:
  %is_second = icmp eq i8 %value, 62
  br i1 %is_second, label %second_value, label %check_third

check_third:
  %is_third = icmp eq i8 %value, 73
  br i1 %is_third, label %third_value, label %fourth_value

first_value:
  ret i8 51

second_value:
  ret i8 62

third_value:
  ret i8 73

fourth_value:
  ret i8 84
}

define i8 @bad_guard_store_mismatch(i1 %choose) {
entry:
  %left = call ptr @malloc(i64 1)
  %right = call ptr @malloc(i64 1)
  br i1 %choose, label %initialize_left, label %initialize_right

initialize_left:
  store i8 1, ptr %left, align 1
  br label %merge

initialize_right:
  store i8 2, ptr %right, align 1
  br label %merge

merge:
  %selected = select i1 %choose, ptr %right, ptr %left
  %value = load i8, ptr %selected, align 1
  ret i8 %value
}

define i8 @bad_missing_path_initialization(i1 %choose) {
entry:
  %left = call ptr @malloc(i64 1)
  %right = call ptr @malloc(i64 1)
  br i1 %choose, label %initialize_left, label %skip_right

initialize_left:
  store i8 1, ptr %left, align 1
  br label %merge

skip_right:
  br label %merge

merge:
  %selected = select i1 %choose, ptr %left, ptr %right
  %value = load i8, ptr %selected, align 1
  ret i8 %value
}

define i16 @bad_partial_path_initialization(i1 %choose) {
entry:
  %left = call ptr @malloc(i64 2)
  %right = call ptr @malloc(i64 2)
  br i1 %choose, label %initialize_left, label %initialize_right

initialize_left:
  store i8 1, ptr %left, align 1
  br label %merge

initialize_right:
  store i8 2, ptr %right, align 1
  br label %merge

merge:
  %selected = select i1 %choose, ptr %left, ptr %right
  %value = load i16, ptr %selected, align 1
  ret i16 %value
}

define i8 @bad_conflicting_repeated_guard(i1 %choose) {
entry:
  %first = call ptr @malloc(i64 1)
  %second = call ptr @malloc(i64 1)
  %third = call ptr @malloc(i64 1)
  %fourth = call ptr @malloc(i64 1)
  br i1 %choose, label %left, label %right

left:
  br i1 %choose, label %initialize_first, label %initialize_second

right:
  br i1 %choose, label %initialize_third, label %initialize_fourth

initialize_first:
  store i8 1, ptr %first, align 1
  br label %merge

initialize_second:
  store i8 2, ptr %second, align 1
  br label %merge

initialize_third:
  store i8 3, ptr %third, align 1
  br label %merge

initialize_fourth:
  store i8 4, ptr %fourth, align 1
  br label %merge

merge:
  %selected = phi ptr [
    %first, %initialize_first
  ], [
    %second, %initialize_second
  ], [
    %third, %initialize_third
  ], [
    %fourth, %initialize_fourth
  ]
  %value = load i8, ptr %selected, align 1
  ret i8 %value
}
