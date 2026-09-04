; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.json --entry nested_two_dimensional_affine_big_endian_i16
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --input-hex 000101 --expect-values 43572,43572,43572,43572,43572,43572 --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition --expect-nested-loop-memoryphi-last-write-value-summary --expect-nested-loop-memoryphi-two-dimensional-affine-summary

target datalayout = "E-p:64:64"

define i16 @nested_two_dimensional_affine_big_endian_i16(
    i4 %index, i2 %outer_count, i2 %inner_count) {
entry:
  %object = alloca [12 x i8], align 4
  %outer_count64 = zext i2 %outer_count to i64
  %inner_count64 = zext i2 %inner_count to i64
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
  %row = mul nuw i64 %outer_iv, 4
  %flat = add nuw i64 %row, %inner_iv
  %write_address = getelementptr inbounds [12 x i8], ptr %object, i64 0, i64 %flat
  store i16 4660, ptr %write_address, align 1
  store i8 -86, ptr %write_address, align 1
  %inner_next = add nuw i64 %inner_iv, 2
  br label %inner_header

outer_latch:
  %outer_next = add nuw i64 %outer_iv, 1
  br label %outer_header

exit:
  %index64 = zext i4 %index to i64
  %read_address = getelementptr inbounds [12 x i8], ptr %object, i64 0, i64 %index64
  %value = load i16, ptr %read_address, align 1
  ret i16 %value
}
