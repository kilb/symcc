; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.stack.json --entry symbolic_memset_dynamic_stack
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.stack.json --input-hex 0204 --expect-values 16705 --expect-stack --expect-alias --expect-symbolic-region-effect --expect-dynamic-byte-lane-cover
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.copy.json --entry symbolic_memcpy_dynamic_stack
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.copy.json --input-hex 0204 --expect-values 17475 --expect-stack --expect-alias --expect-symbolic-region-effect --expect-dynamic-byte-lane-cover --expect-symbolic-region-copy
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.intrinsic.json --entry symbolic_intrinsic_memset_dynamic_stack
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.intrinsic.json --input-hex 0204 --expect-values 17990 --expect-stack --expect-alias --expect-symbolic-region-effect --expect-dynamic-byte-lane-cover
; RUN: env SYMCC_LIVE_HEAP_SITE_CAPACITY=1 %python %S/../util/llvm_to_continuation.py %s --output %t.heap.json --entry symbolic_memset_dynamic_heap
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.heap.json --input-hex 0204 --expect-values 16962 --expect-heap --expect-alias --expect-symbolic-region-effect --expect-dynamic-byte-lane-cover
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.skip.json --entry symbolic_memset_dynamic_noalias_skip
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.skip.json --input-hex 0204 --expect-values 17219 --expect-stack --expect-alias --expect-symbolic-region-effect --expect-dynamic-byte-lane-cover --expect-dynamic-byte-lane-noalias-skip
; RUN: %python %S/../benchmark/generate_symbolic_length_byte_lane_fixture.py --object-bytes 64 --load-bytes 1 --output %t.boundary64.ll
; RUN: env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.boundary64.ll --output %t.boundary64.json --entry generated_symbolic_length_byte_lane
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.boundary64.json --input-hex 3f40 --expect-values 90 --expect-zero-forks --expect-stack --expect-alias --expect-symbolic-region-effect --expect-dynamic-byte-lane-cover
; RUN: %python %S/../benchmark/generate_symbolic_length_byte_lane_fixture.py --object-bytes 64 --load-bytes 8 --output %t.boundary64-wide.ll
; RUN: env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.boundary64-wide.ll --output %t.boundary64-wide.json --entry generated_symbolic_length_byte_lane
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.boundary64-wide.json --input-hex 3840 --expect-values 6510615555426900570 --expect-zero-forks --expect-stack --expect-alias --expect-symbolic-region-effect --expect-dynamic-byte-lane-cover
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.partial.json --entry bad_symbolic_region_partial
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.partial.json --expect-rejected "stack load lacks a dominating"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.clobber.json --entry bad_symbolic_region_clobber
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.clobber.json --expect-rejected "stack load lacks a dominating"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.multi-region.json --entry bad_multiple_symbolic_regions
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.multi-region.json --expect-rejected "stack load lacks a dominating"

@copy_source = private constant [8 x i8] c"ABCDEFGH"
@side_effect = private global i8 0

declare ptr @malloc(i64)
declare void @free(ptr)
declare ptr @memset(ptr, i32, i64)
declare ptr @memcpy(ptr, ptr, i64)
declare void @llvm.memset.p0.i64(ptr nocapture writeonly, i8, i64, i1 immarg)

define i16 @symbolic_memset_dynamic_stack(i8 %index, i8 %count) {
entry:
  %object = alloca [8 x i8], align 8
  %length = zext i8 %count to i64
  call ptr @memset(ptr %object, i32 65, i64 %length)
  %wide_index = zext i8 %index to i64
  %address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %wide_index
  %value = load i16, ptr %address, align 1
  ret i16 %value
}

define i16 @symbolic_memcpy_dynamic_stack(i8 %index, i8 %count) {
entry:
  %object = alloca [8 x i8], align 8
  %length = zext i8 %count to i64
  call ptr @memcpy(ptr %object, ptr @copy_source, i64 %length)
  %wide_index = zext i8 %index to i64
  %address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %wide_index
  %value = load i16, ptr %address, align 1
  ret i16 %value
}

define i16 @symbolic_intrinsic_memset_dynamic_stack(
    i8 %index, i8 %count) {
entry:
  %object = alloca [8 x i8], align 8
  %length = zext i8 %count to i64
  call void @llvm.memset.p0.i64(
      ptr %object, i8 70, i64 %length, i1 false)
  %wide_index = zext i8 %index to i64
  %address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %wide_index
  %value = load i16, ptr %address, align 1
  ret i16 %value
}

define i16 @symbolic_memset_dynamic_heap(i8 %index, i8 %count) {
entry:
  %object = call ptr @malloc(i64 8)
  %length = zext i8 %count to i64
  call ptr @memset(ptr %object, i32 66, i64 %length)
  %wide_index = zext i8 %index to i64
  %address = getelementptr inbounds i8, ptr %object, i64 %wide_index
  %value = load i16, ptr %address, align 1
  call void @free(ptr %object)
  ret i16 %value
}

define i16 @symbolic_memset_dynamic_noalias_skip(i8 %index, i8 %count) {
entry:
  %object = alloca [8 x i8], align 8
  %length = zext i8 %count to i64
  call ptr @memset(ptr %object, i32 67, i64 %length)
  store i8 9, ptr @side_effect, align 1
  %wide_index = zext i8 %index to i64
  %address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %wide_index
  %value = load i16, ptr %address, align 1
  ret i16 %value
}

define i16 @bad_symbolic_region_partial(i8 %index, i8 %count) {
entry:
  %object = alloca [8 x i8], align 8
  %writer = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 1
  %length = zext i8 %count to i64
  call ptr @memset(ptr %writer, i32 68, i64 %length)
  %wide_index = zext i8 %index to i64
  %address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %wide_index
  %value = load i16, ptr %address, align 1
  ret i16 %value
}

define i16 @bad_symbolic_region_clobber(i8 %index, i8 %count) {
entry:
  %object = alloca [8 x i8], align 8
  %length = zext i8 %count to i64
  call ptr @memset(ptr %object, i32 69, i64 %length)
  store i8 1, ptr %object, align 1
  %wide_index = zext i8 %index to i64
  %address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %wide_index
  %value = load i16, ptr %address, align 1
  ret i16 %value
}

define i16 @bad_multiple_symbolic_regions(i8 %index, i8 %count) {
entry:
  %object = alloca [8 x i8], align 8
  %other = alloca [8 x i8], align 8
  %length = zext i8 %count to i64
  call ptr @memset(ptr %other, i32 1, i64 %length)
  call ptr @memset(ptr %object, i32 70, i64 %length)
  %wide_index = zext i8 %index to i64
  %address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %wide_index
  %value = load i16, ptr %address, align 1
  ret i16 %value
}
