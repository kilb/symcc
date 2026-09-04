; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.ordered.json --entry ordered_two_writer_transfer_i8
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.ordered.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-multilatch-loop-memoryphi-fixed-point --expect-ordered-multilatch-writer-transfer
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.mixed.json --entry ordered_and_singleton_transfers_i8
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.mixed.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-multilatch-loop-memoryphi-fixed-point --expect-ordered-multilatch-writer-transfer
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.too-many.json --entry bad_ordered_five_writer_transfer_i8
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.too-many.json --expect-rejected "stack load lacks a dominating"
; RUN: %python %S/../benchmark/generate_ordered_multilatch_memoryphi_fixture.py --object-bytes 64 --stride 8 --writer-bytes 8 --load-bytes 8 --writers-per-transfer 4 --output %t.boundary64.ll
; RUN: env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.boundary64.ll --output %t.boundary64.json --entry generated_ordered_multilatch_memoryphi_transfer
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.boundary64.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-multilatch-loop-memoryphi-fixed-point --expect-ordered-multilatch-writer-transfer

define i16 @ordered_two_writer_transfer_i8(
    i8 %index, i8 %count, i8 %choose) {
entry:
  %object = alloca [8 x i8], align 8
  %count64 = zext i8 %count to i64
  %choose64 = zext i8 %choose to i64
  br label %header

header:
  %iv = phi i64 [ 0, %entry ], [ %next_writer, %writer_latch ], [ %next_carry, %carry_latch ]
  %continue = icmp ult i64 %iv, %count64
  br i1 %continue, label %decision, label %exit

decision:
  %take_writer = icmp ult i64 %iv, %choose64
  br i1 %take_writer, label %writer_latch, label %carry_latch

writer_latch:
  %write_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %iv
  store i8 73, ptr %write_address, align 1
  store i16 19018, ptr %write_address, align 1
  %next_writer = add nuw i64 %iv, 2
  br label %header

carry_latch:
  %next_carry = add nuw i64 %iv, 2
  br label %header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %index64
  %value = load i16, ptr %read_address, align 1
  ret i16 %value
}

define i8 @ordered_and_singleton_transfers_i8(
    i8 %index, i8 %count, i8 %choose) {
entry:
  %object = alloca [4 x i8], align 4
  %count64 = zext i8 %count to i64
  %choose64 = zext i8 %choose to i64
  br label %header

header:
  %iv = phi i64 [ 0, %entry ], [ %next_a, %latch_a ], [ %next_b, %latch_b ]
  %continue = icmp ult i64 %iv, %count64
  br i1 %continue, label %decision, label %exit

decision:
  %take_a = icmp ult i64 %iv, %choose64
  br i1 %take_a, label %latch_a, label %latch_b

latch_a:
  %address_a = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %iv
  store i8 80, ptr %address_a, align 1
  store i8 81, ptr %address_a, align 1
  %next_a = add nuw i64 %iv, 1
  br label %header

latch_b:
  %address_b = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %iv
  store i8 82, ptr %address_b, align 1
  %next_b = add nuw i64 %iv, 1
  br label %header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %index64
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}

define i8 @bad_ordered_five_writer_transfer_i8(
    i8 %index, i8 %count, i8 %choose) {
entry:
  %object = alloca [4 x i8], align 4
  %count64 = zext i8 %count to i64
  %choose64 = zext i8 %choose to i64
  br label %header

header:
  %iv = phi i64 [ 0, %entry ], [ %next_writer, %writer_latch ], [ %next_carry, %carry_latch ]
  %continue = icmp ult i64 %iv, %count64
  br i1 %continue, label %decision, label %exit

decision:
  %take_writer = icmp ult i64 %iv, %choose64
  br i1 %take_writer, label %writer_latch, label %carry_latch

writer_latch:
  %write_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %iv
  store i8 75, ptr %write_address, align 1
  store i8 76, ptr %write_address, align 1
  store i8 77, ptr %write_address, align 1
  store i8 78, ptr %write_address, align 1
  store i8 79, ptr %write_address, align 1
  %next_writer = add nuw i64 %iv, 1
  br label %header

carry_latch:
  %next_carry = add nuw i64 %iv, 1
  br label %header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %index64
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}
