; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.wrapper.json --entry wrapper_initialized_heap
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.wrapper.json --expect-values 42 --expect-heap --expect-cross-function-pointer --expect-interprocedural-heap-effect --expect-interprocedural-allocator-effect
; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=2 %python %S/../util/llvm_to_continuation.py %s --output %t.wrapper-pool.json --entry wrapper_initialized_heap
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.wrapper-pool.json --expect-values 42 --expect-heap-pool --expect-pointer-union --expect-cross-function-pointer --expect-interprocedural-heap-effect --expect-interprocedural-allocator-effect
; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.calloc.json --entry calloc_wrapper_initialized_heap
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.calloc.json --expect-values 0 --expect-heap --expect-cross-function-pointer --expect-interprocedural-heap-effect --expect-interprocedural-allocator-effect
; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.argument.json --entry argument_initialized_heap
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.argument.json --expect-values 77 --expect-heap --expect-cross-function-pointer --expect-interprocedural-heap-effect --expect-interprocedural-argument-effect --expect-interprocedural-noalias-skip
; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.multicall.json --entry multicallsite_argument_initialized_heap
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.multicall.json --expect-values 154 --expect-heap --expect-cross-function-pointer --expect-interprocedural-heap-effect --expect-interprocedural-argument-effect
; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.subobject.json --entry subobject_argument_initialized_heap
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.subobject.json --expect-values 99 --expect-heap --expect-cross-function-pointer --expect-interprocedural-heap-effect --expect-interprocedural-argument-effect
; RUN: %python %S/../benchmark/generate_interprocedural_heap_effect_fixture.py --calls 64 --output %t.calls64.ll
; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.calls64.ll --output %t.calls64.json --entry generated_interprocedural_heap_effect
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.calls64.json --expect-values 64 --expect-heap --expect-cross-function-pointer --expect-interprocedural-heap-effect --expect-interprocedural-argument-effect
; RUN: not env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.conditional.json --entry bad_conditional_initializer
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.conditional.json --expect-rejected "heap load lacks a dominating"
; RUN: not env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.partial.json --entry bad_partial_initializer
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.partial.json --expect-rejected "heap load lacks a dominating"
; RUN: not env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.clobber.json --entry bad_post_initializer_clobber
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.clobber.json --expect-rejected "heap load lacks a dominating"

declare ptr @malloc(i64)
declare ptr @calloc(i64, i64)

define internal ptr @allocate_and_initialize() {
entry:
  %object = call ptr @malloc(i64 1)
  store i8 42, ptr %object, align 1
  ret ptr %object
}

define i8 @wrapper_initialized_heap() {
entry:
  %object = call ptr @allocate_and_initialize()
  %value = load i8, ptr %object, align 1
  ret i8 %value
}

define internal ptr @allocate_zeroed() {
entry:
  %object = call ptr @calloc(i64 1, i64 1)
  ret ptr %object
}

define i8 @calloc_wrapper_initialized_heap() {
entry:
  %object = call ptr @allocate_zeroed()
  %value = load i8, ptr %object, align 1
  ret i8 %value
}

define internal void @initialize_argument(ptr %object) {
entry:
  store i8 77, ptr %object, align 1
  ret void
}

define i8 @argument_initialized_heap() {
entry:
  %object = call ptr @malloc(i64 2)
  call void @initialize_argument(ptr %object)
  %scratch = getelementptr i8, ptr %object, i64 1
  store i8 9, ptr %scratch, align 1
  %value = load i8, ptr %object, align 1
  ret i8 %value
}

define i8 @multicallsite_argument_initialized_heap() {
entry:
  %first = call ptr @malloc(i64 1)
  %second = call ptr @malloc(i64 1)
  call void @initialize_argument(ptr %first)
  %first_value = load i8, ptr %first, align 1
  call void @initialize_argument(ptr %second)
  %second_value = load i8, ptr %second, align 1
  %sum = add i8 %first_value, %second_value
  ret i8 %sum
}

define internal void @initialize_second_byte(ptr %object) {
entry:
  %field = getelementptr i8, ptr %object, i64 1
  store i8 99, ptr %field, align 1
  ret void
}

define i8 @subobject_argument_initialized_heap() {
entry:
  %object = call ptr @malloc(i64 2)
  call void @initialize_second_byte(ptr %object)
  %field = getelementptr i8, ptr %object, i64 1
  %value = load i8, ptr %field, align 1
  ret i8 %value
}

define internal void @conditional_initialize(ptr %object, i1 %choose) {
entry:
  br i1 %choose, label %write, label %done

write:
  store i8 1, ptr %object, align 1
  br label %done

done:
  ret void
}

define i8 @bad_conditional_initializer(i1 %choose) {
entry:
  %object = call ptr @malloc(i64 1)
  call void @conditional_initialize(ptr %object, i1 %choose)
  %value = load i8, ptr %object, align 1
  ret i8 %value
}

define internal void @partial_initialize(ptr %object) {
entry:
  store i8 1, ptr %object, align 1
  ret void
}

define i16 @bad_partial_initializer() {
entry:
  %object = call ptr @malloc(i64 2)
  call void @partial_initialize(ptr %object)
  %value = load i16, ptr %object, align 1
  ret i16 %value
}

define internal void @initialize_wide_argument(ptr %object) {
entry:
  store i16 4660, ptr %object, align 1
  ret void
}

define i16 @bad_post_initializer_clobber() {
entry:
  %object = call ptr @malloc(i64 2)
  call void @initialize_wide_argument(ptr %object)
  store i8 1, ptr %object, align 1
  %value = load i16, ptr %object, align 1
  ret i16 %value
}
