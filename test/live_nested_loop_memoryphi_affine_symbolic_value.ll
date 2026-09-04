; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.json --entry nested_affine_symbolic_value_i16
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition --expect-nested-loop-memoryphi-last-write-value-summary --expect-nested-loop-memoryphi-two-dimensional-affine-summary --expect-nested-loop-memoryphi-affine-symbolic-value-summary
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --input-hex 0001013412 --expect-values 13994,13994,13994,13994,13994,13994 --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition --expect-nested-loop-memoryphi-last-write-value-summary --expect-nested-loop-memoryphi-two-dimensional-affine-summary --expect-nested-loop-memoryphi-affine-symbolic-value-summary
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --input-hex 0602038012 --expect-values 0,0,0,0,14250,14250 --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition --expect-nested-loop-memoryphi-last-write-value-summary --expect-nested-loop-memoryphi-two-dimensional-affine-summary --expect-nested-loop-memoryphi-affine-symbolic-value-summary
; RUN: %python %S/../benchmark/generate_nested_loop_memoryphi_affine_symbolic_value_fixture.py --output %t.generated.ll
; RUN: env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.generated.ll --output %t.generated.json --entry generated_nested_loop_memoryphi_affine_symbolic_value
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.generated.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition --expect-nested-loop-memoryphi-last-write-value-summary --expect-nested-loop-memoryphi-two-dimensional-affine-summary --expect-nested-loop-memoryphi-affine-symbolic-value-summary
; RUN: %python %S/../benchmark/generate_nested_loop_memoryphi_affine_symbolic_value_fixture.py --object-bytes 12 --outer-bound-bits 1 --inner-bound-bits 2 --address-outer-scale 8 --address-inner-scale 4 --inner-step 1 --value-bits 32 --load-bytes 4 --overlay-byte none --output %t.i32.ll
; RUN: %python %S/../util/llvm_to_continuation.py %t.i32.ll --output %t.i32.json --entry generated_nested_loop_memoryphi_affine_symbolic_value
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.i32.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition --expect-nested-loop-memoryphi-last-write-value-summary --expect-nested-loop-memoryphi-two-dimensional-affine-summary --expect-nested-loop-memoryphi-affine-symbolic-value-summary
; RUN: %python %S/../benchmark/generate_nested_loop_memoryphi_affine_symbolic_value_fixture.py --object-bytes 24 --outer-bound-bits 1 --inner-bound-bits 2 --address-outer-scale 16 --address-inner-scale 8 --inner-step 1 --value-bits 64 --load-bytes 8 --overlay-byte none --output %t.i64.ll
; RUN: env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.i64.ll --output %t.i64.json --entry generated_nested_loop_memoryphi_affine_symbolic_value
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.i64.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition --expect-nested-loop-memoryphi-last-write-value-summary --expect-nested-loop-memoryphi-two-dimensional-affine-summary --expect-nested-loop-memoryphi-affine-symbolic-value-summary
; RUN: %python %S/../benchmark/generate_nested_loop_memoryphi_affine_symbolic_value_fixture.py --object-bytes 14 --outer-bound-bits 2 --inner-bound-bits 2 --address-outer-scale 4 --address-inner-scale 2 --inner-step 1 --value-input-scale 0 --overlay-byte none --output %t.iv-only.ll
; RUN: %python %S/../util/llvm_to_continuation.py %t.iv-only.ll --output %t.iv-only.json --entry generated_nested_loop_memoryphi_affine_symbolic_value
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.iv-only.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition --expect-nested-loop-memoryphi-last-write-value-summary --expect-nested-loop-memoryphi-two-dimensional-affine-summary --expect-nested-loop-memoryphi-affine-symbolic-value-summary

define i16 @nested_affine_symbolic_value_i16(
    i4 %index, i2 %outer_count, i2 %inner_count, i16 %payload) {
entry:
  %object = alloca [12 x i8], align 4
  %outer_count16 = zext i2 %outer_count to i16
  %inner_count16 = zext i2 %inner_count to i16
  br label %outer_header

outer_header:
  %outer_iv = phi i16 [ 0, %entry ], [ %outer_next, %outer_latch ]
  %outer_continue = icmp ult i16 %outer_iv, %outer_count16
  br i1 %outer_continue, label %inner_preheader, label %exit

inner_preheader:
  br label %inner_header

inner_header:
  %inner_iv = phi i16 [ 0, %inner_preheader ], [ %inner_next, %inner_body ]
  %inner_continue = icmp ult i16 %inner_iv, %inner_count16
  br i1 %inner_continue, label %inner_body, label %outer_latch

inner_body:
  %row = mul nuw i16 %outer_iv, 4
  %flat = add nuw i16 %row, %inner_iv
  %write_address = getelementptr inbounds [12 x i8], ptr %object, i16 0, i16 %flat
  %value_input = mul i16 %payload, 3
  %value_outer = mul i16 %outer_iv, 5
  %value_inner = mul i16 %inner_iv, 7
  %value_partial = add i16 %value_input, %value_outer
  %value_sum = add i16 %value_partial, %value_inner
  %stored = add i16 %value_sum, 17
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
