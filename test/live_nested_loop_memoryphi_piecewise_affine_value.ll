; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.json --entry nested_piecewise_affine_value_i16
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition --expect-nested-loop-memoryphi-last-write-value-summary --expect-nested-loop-memoryphi-two-dimensional-affine-summary --expect-nested-loop-memoryphi-piecewise-affine-value-summary
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --input-hex 0001033412 --expect-values 13994,13994,13994,13994,13994,13994 --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-piecewise-affine-value-summary
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --input-hex 0201033412 --expect-values 0,0,0,15018,15018,15018 --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-piecewise-affine-value-summary
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.outer.json --entry nested_piecewise_outer_reversed_i16
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.outer.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-piecewise-affine-value-summary
; RUN: %python %S/../benchmark/generate_nested_loop_memoryphi_piecewise_affine_value_fixture.py --guard-predicate eq --output %t.eq.ll && env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.eq.ll --output %t.eq.json --entry generated_nested_loop_memoryphi_piecewise_affine_value && %python %S/../util/check_live_continuation_lowering.py %t.eq.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-piecewise-affine-value-summary
; RUN: %python %S/../benchmark/generate_nested_loop_memoryphi_piecewise_affine_value_fixture.py --guard-predicate ne --output %t.ne.ll && env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.ne.ll --output %t.ne.json --entry generated_nested_loop_memoryphi_piecewise_affine_value && %python %S/../util/check_live_continuation_lowering.py %t.ne.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-piecewise-affine-value-summary
; RUN: %python %S/../benchmark/generate_nested_loop_memoryphi_piecewise_affine_value_fixture.py --guard-predicate ugt --output %t.ugt.ll && env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.ugt.ll --output %t.ugt.json --entry generated_nested_loop_memoryphi_piecewise_affine_value && %python %S/../util/check_live_continuation_lowering.py %t.ugt.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-piecewise-affine-value-summary
; RUN: %python %S/../benchmark/generate_nested_loop_memoryphi_piecewise_affine_value_fixture.py --guard-predicate uge --output %t.uge.ll && env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.uge.ll --output %t.uge.json --entry generated_nested_loop_memoryphi_piecewise_affine_value && %python %S/../util/check_live_continuation_lowering.py %t.uge.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-piecewise-affine-value-summary
; RUN: %python %S/../benchmark/generate_nested_loop_memoryphi_piecewise_affine_value_fixture.py --guard-predicate ult --output %t.ult.ll && env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.ult.ll --output %t.ult.json --entry generated_nested_loop_memoryphi_piecewise_affine_value && %python %S/../util/check_live_continuation_lowering.py %t.ult.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-piecewise-affine-value-summary
; RUN: %python %S/../benchmark/generate_nested_loop_memoryphi_piecewise_affine_value_fixture.py --guard-predicate ule --output %t.ule.ll && env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.ule.ll --output %t.ule.json --entry generated_nested_loop_memoryphi_piecewise_affine_value && %python %S/../util/check_live_continuation_lowering.py %t.ule.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-piecewise-affine-value-summary
; RUN: %python %S/../benchmark/generate_nested_loop_memoryphi_piecewise_affine_value_fixture.py --object-bytes 24 --outer-bound-bits 1 --inner-bound-bits 2 --address-outer-scale 16 --address-inner-scale 8 --inner-step 1 --value-bits 64 --load-bytes 8 --overlay-byte none --output %t.i64.ll
; RUN: env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.i64.ll --output %t.i64.json --entry generated_nested_loop_memoryphi_piecewise_affine_value
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.i64.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-piecewise-affine-value-summary

define i16 @nested_piecewise_affine_value_i16(
    i4 %index, i2 %outer_count, i2 %inner_count, i16 %payload) {
entry:
  %object = alloca [12 x i8], align 4
  %outer_bound = zext i2 %outer_count to i16
  %inner_bound = zext i2 %inner_count to i16
  br label %outer_header

outer_header:
  %outer_iv = phi i16 [ 0, %entry ], [ %outer_next, %outer_latch ]
  %outer_continue = icmp ult i16 %outer_iv, %outer_bound
  br i1 %outer_continue, label %inner_preheader, label %exit

inner_preheader:
  br label %inner_header

inner_header:
  %inner_iv = phi i16 [ 0, %inner_preheader ], [ %inner_next, %inner_body ]
  %inner_continue = icmp ult i16 %inner_iv, %inner_bound
  br i1 %inner_continue, label %inner_body, label %outer_latch

inner_body:
  %row = mul nuw i16 %outer_iv, 4
  %flat = add nuw i16 %row, %inner_iv
  %write_address = getelementptr inbounds [12 x i8], ptr %object, i16 0, i16 %flat
  %true_input = mul i16 %payload, 3
  %true_outer = mul i16 %outer_iv, 5
  %true_inner = mul i16 %inner_iv, 7
  %true_partial = add i16 %true_input, %true_outer
  %true_sum = add i16 %true_partial, %true_inner
  %true_value = add i16 %true_sum, 17
  %false_input = mul i16 %payload, 3
  %false_outer = mul i16 %outer_iv, 2
  %false_inner = mul i16 %inner_iv, 11
  %false_partial = add i16 %false_input, %false_outer
  %false_sum = add i16 %false_partial, %false_inner
  %false_value = add i16 %false_sum, 1025
  %piecewise_guard = icmp ult i16 %inner_iv, 2
  %stored = select i1 %piecewise_guard, i16 %true_value, i16 %false_value
  store i16 %stored, ptr %write_address, align 1
  store i8 -86, ptr %write_address, align 1
  %inner_next = add nuw i16 %inner_iv, 2
  br label %inner_header

outer_latch:
  %outer_next = add nuw i16 %outer_iv, 1
  br label %outer_header

exit:
  %index16 = zext i4 %index to i16
  %read_address = getelementptr inbounds [12 x i8], ptr %object, i16 0, i16 %index16
  %value = load i16, ptr %read_address, align 1
  ret i16 %value
}

define i16 @nested_piecewise_outer_reversed_i16(
    i4 %index, i2 %outer_count, i2 %inner_count, i16 %payload) {
entry:
  %object = alloca [12 x i8], align 4
  %outer_bound = zext i2 %outer_count to i16
  %inner_bound = zext i2 %inner_count to i16
  br label %outer_header

outer_header:
  %outer_iv = phi i16 [ 0, %entry ], [ %outer_next, %outer_latch ]
  %outer_continue = icmp ult i16 %outer_iv, %outer_bound
  br i1 %outer_continue, label %inner_preheader, label %exit

inner_preheader:
  br label %inner_header

inner_header:
  %inner_iv = phi i16 [ 0, %inner_preheader ], [ %inner_next, %inner_body ]
  %inner_continue = icmp ult i16 %inner_iv, %inner_bound
  br i1 %inner_continue, label %inner_body, label %outer_latch

inner_body:
  %row = mul nuw i16 %outer_iv, 4
  %flat = add nuw i16 %row, %inner_iv
  %write_address = getelementptr inbounds [12 x i8], ptr %object, i16 0, i16 %flat
  %true_input = mul i16 %payload, 5
  %true_outer = mul i16 %outer_iv, 13
  %true_value = add i16 %true_input, %true_outer
  %false_input = mul i16 %payload, 7
  %false_inner = mul i16 %inner_iv, 17
  %false_value = add i16 %false_input, %false_inner
  %piecewise_guard = icmp uge i16 1, %outer_iv
  %stored = select i1 %piecewise_guard, i16 %true_value, i16 %false_value
  store i16 %stored, ptr %write_address, align 1
  %inner_next = add nuw i16 %inner_iv, 2
  br label %inner_header

outer_latch:
  %outer_next = add nuw i16 %outer_iv, 1
  br label %outer_header

exit:
  %index16 = zext i4 %index to i16
  %read_address = getelementptr inbounds [12 x i8], ptr %object, i16 0, i16 %index16
  %value = load i16, ptr %read_address, align 1
  ret i16 %value
}
