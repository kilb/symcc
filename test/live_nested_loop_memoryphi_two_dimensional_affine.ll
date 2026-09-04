; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.json --entry nested_two_dimensional_affine_i16
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition --expect-nested-loop-memoryphi-last-write-value-summary --expect-nested-loop-memoryphi-two-dimensional-affine-summary
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --input-hex 000101 --expect-values 4778,4778,4778,4778,4778,4778 --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition --expect-nested-loop-memoryphi-last-write-value-summary --expect-nested-loop-memoryphi-two-dimensional-affine-summary
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --input-hex 060203 --expect-values 0,0,0,0,4778,4778 --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition --expect-nested-loop-memoryphi-last-write-value-summary --expect-nested-loop-memoryphi-two-dimensional-affine-summary
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.json --input-hex 010303 --expect-values 0,0,0,43538,43538,43538 --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition --expect-nested-loop-memoryphi-last-write-value-summary --expect-nested-loop-memoryphi-two-dimensional-affine-summary
; RUN: %python %S/../benchmark/generate_nested_loop_memoryphi_two_dimensional_affine_fixture.py --output %t.boundary.ll
; RUN: env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.boundary.ll --output %t.boundary.json --entry generated_nested_loop_memoryphi_two_dimensional_affine
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.boundary.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition --expect-nested-loop-memoryphi-last-write-value-summary --expect-nested-loop-memoryphi-two-dimensional-affine-summary
; RUN: %python %S/../benchmark/generate_nested_loop_memoryphi_two_dimensional_affine_fixture.py --object-bytes 3 --outer-bound-bits 2 --inner-bound-bits 1 --inner-step 1 --affine-outer-scale 1 --affine-inner-scale 2 --writer-widths 1 --writer-values 17 --load-bytes 1 --output %t.affine-stride.ll
; RUN: %python %S/../util/llvm_to_continuation.py %t.affine-stride.ll --output %t.affine-stride.json --entry generated_nested_loop_memoryphi_two_dimensional_affine
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.affine-stride.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-nested-loop-memoryphi-summary-composition --expect-nested-loop-memoryphi-last-write-value-summary --expect-nested-loop-memoryphi-two-dimensional-affine-summary

define i16 @nested_two_dimensional_affine_i16(
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
