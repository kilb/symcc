; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.single.json --entry nested_loop_memoryphi_summary_i8
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.single.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.single.json --input-hex 000104 --expect-values 91,91,91,91,91,91 --expect-infeasible --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.ordered.json --entry nested_loop_ordered_writer_summary_i16
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.ordered.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.variant-bound.json --entry bad_nested_outer_variant_inner_bound
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.variant-bound.json --expect-rejected "stack load lacks a dominating"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.extra-memory.json --entry bad_nested_extra_memory_use
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.extra-memory.json --expect-rejected "stack load lacks a dominating"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.five-writers.json --entry bad_nested_five_writer_summary
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.five-writers.json --expect-rejected "stack load lacks a dominating"
; RUN: %python %S/../benchmark/generate_nested_loop_memoryphi_summary_fixture.py --object-bytes 64 --outer-step 1 --inner-step 8 --writer-widths 1,2,4,8 --load-bytes 8 --output %t.boundary64.ll
; RUN: env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.boundary64.ll --output %t.boundary64.json --entry generated_nested_loop_memoryphi_summary
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.boundary64.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition

@nested_initialized = internal global i8 1, align 1

define i8 @nested_loop_memoryphi_summary_i8(
    i8 %index, i8 %outer_count, i8 %inner_count) {
entry:
  %object = alloca [4 x i8], align 4
  %outer_count64 = zext i8 %outer_count to i64
  %inner_count64 = zext i8 %inner_count to i64
  br label %outer_header

outer_header:
  %outer_iv = phi i64 [ 0, %entry ], [ %outer_next, %outer_latch ]
  %outer_continue = icmp ult i64 %outer_iv, %outer_count64
  br i1 %outer_continue, label %inner_preheader, label %exit

inner_preheader:
  br label %inner_header

inner_header:
  %inner_iv = phi i64 [ 0, %inner_preheader ], [ %inner_next, %inner_body ]
  %inner_continue = icmp ult i64 %inner_iv, %inner_count64
  br i1 %inner_continue, label %inner_body, label %outer_latch

inner_body:
  %write_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %inner_iv
  store i8 91, ptr %write_address, align 1
  %inner_next = add nuw i64 %inner_iv, 1
  br label %inner_header

outer_latch:
  %outer_next = add nuw i64 %outer_iv, 1
  br label %outer_header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %index64
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}

define i16 @nested_loop_ordered_writer_summary_i16(
    i8 %index, i8 %outer_count, i8 %inner_count) {
entry:
  %object = alloca [8 x i8], align 8
  %outer_count64 = zext i8 %outer_count to i64
  %inner_count64 = zext i8 %inner_count to i64
  br label %outer_header

outer_header:
  %outer_iv = phi i64 [ 0, %entry ], [ %outer_next, %outer_latch ]
  %outer_continue = icmp ult i64 %outer_iv, %outer_count64
  br i1 %outer_continue, label %inner_preheader, label %exit

inner_preheader:
  br label %inner_header

inner_header:
  %inner_iv = phi i64 [ 0, %inner_preheader ], [ %inner_next, %inner_body ]
  %inner_continue = icmp ult i64 %inner_iv, %inner_count64
  br i1 %inner_continue, label %inner_body, label %outer_latch

inner_body:
  %write_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %inner_iv
  store i8 92, ptr %write_address, align 1
  store i16 23900, ptr %write_address, align 1
  %inner_next = add nuw i64 %inner_iv, 2
  br label %inner_header

outer_latch:
  %outer_next = add nuw i64 %outer_iv, 1
  br label %outer_header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %index64
  %value = load i16, ptr %read_address, align 1
  ret i16 %value
}

define i8 @bad_nested_outer_variant_inner_bound(
    i8 %index, i8 %outer_count) {
entry:
  %object = alloca [4 x i8], align 4
  %outer_count64 = zext i8 %outer_count to i64
  br label %outer_header

outer_header:
  %outer_iv = phi i64 [ 0, %entry ], [ %outer_next, %outer_latch ]
  %outer_continue = icmp ult i64 %outer_iv, %outer_count64
  br i1 %outer_continue, label %inner_preheader, label %exit

inner_preheader:
  %inner_bound = add nuw i64 %outer_iv, 1
  br label %inner_header

inner_header:
  %inner_iv = phi i64 [ 0, %inner_preheader ], [ %inner_next, %inner_body ]
  %inner_continue = icmp ult i64 %inner_iv, %inner_bound
  br i1 %inner_continue, label %inner_body, label %outer_latch

inner_body:
  %write_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %inner_iv
  store i8 93, ptr %write_address, align 1
  %inner_next = add nuw i64 %inner_iv, 1
  br label %inner_header

outer_latch:
  %outer_next = add nuw i64 %outer_iv, 1
  br label %outer_header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %index64
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}

define i8 @bad_nested_extra_memory_use(
    i8 %index, i8 %outer_count, i8 %inner_count) {
entry:
  %object = alloca [4 x i8], align 4
  %outer_count64 = zext i8 %outer_count to i64
  %inner_count64 = zext i8 %inner_count to i64
  br label %outer_header

outer_header:
  %outer_iv = phi i64 [ 0, %entry ], [ %outer_next, %outer_latch ]
  %outer_continue = icmp ult i64 %outer_iv, %outer_count64
  br i1 %outer_continue, label %inner_preheader, label %exit

inner_preheader:
  br label %inner_header

inner_header:
  %inner_iv = phi i64 [ 0, %inner_preheader ], [ %inner_next, %inner_body ]
  %inner_continue = icmp ult i64 %inner_iv, %inner_count64
  br i1 %inner_continue, label %inner_body, label %outer_latch

inner_body:
  %side = load volatile i8, ptr @nested_initialized, align 1
  %write_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %inner_iv
  store i8 %side, ptr %write_address, align 1
  %inner_next = add nuw i64 %inner_iv, 1
  br label %inner_header

outer_latch:
  %outer_next = add nuw i64 %outer_iv, 1
  br label %outer_header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %index64
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}

define i8 @bad_nested_five_writer_summary(
    i8 %index, i8 %outer_count, i8 %inner_count) {
entry:
  %object = alloca [4 x i8], align 4
  %outer_count64 = zext i8 %outer_count to i64
  %inner_count64 = zext i8 %inner_count to i64
  br label %outer_header

outer_header:
  %outer_iv = phi i64 [ 0, %entry ], [ %outer_next, %outer_latch ]
  %outer_continue = icmp ult i64 %outer_iv, %outer_count64
  br i1 %outer_continue, label %inner_preheader, label %exit

inner_preheader:
  br label %inner_header

inner_header:
  %inner_iv = phi i64 [ 0, %inner_preheader ], [ %inner_next, %inner_body ]
  %inner_continue = icmp ult i64 %inner_iv, %inner_count64
  br i1 %inner_continue, label %inner_body, label %outer_latch

inner_body:
  %write_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %inner_iv
  store i8 94, ptr %write_address, align 1
  store i8 95, ptr %write_address, align 1
  store i8 96, ptr %write_address, align 1
  store i8 97, ptr %write_address, align 1
  store i8 98, ptr %write_address, align 1
  %inner_next = add nuw i64 %inner_iv, 1
  br label %inner_header

outer_latch:
  %outer_next = add nuw i64 %outer_iv, 1
  br label %outer_header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %index64
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}
