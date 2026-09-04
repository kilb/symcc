; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.json --entry nested_affine_symbolic_value_big_endian_i16
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --input-hex 0001013412 --expect-values 43693,43693,43693,43693,43693,43693 --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition --expect-nested-loop-memoryphi-last-write-value-summary --expect-nested-loop-memoryphi-two-dimensional-affine-summary --expect-nested-loop-memoryphi-affine-symbolic-value-summary

target datalayout = "E-p:64:64"

define i16 @nested_affine_symbolic_value_big_endian_i16(
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
