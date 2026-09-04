; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.json --entry nested_piecewise_affine_value_big_endian_i16
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-piecewise-affine-value-summary
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --input-hex 0201033412 --expect-values 0,0,0,43699,43699,43699 --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-piecewise-affine-value-summary

target datalayout = "E-p:64:64"

define i16 @nested_piecewise_affine_value_big_endian_i16(
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
