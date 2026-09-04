; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.carry.json --entry multilatch_writer_or_carry_i8
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.carry.json --input-hex 000101 --expect-values 65,65,65,65,65,65,65 --expect-infeasible --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-multilatch-loop-memoryphi-fixed-point
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.two.json --entry multilatch_two_writers_i8
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.two.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-multilatch-loop-memoryphi-fixed-point
; RUN: %python %S/../util/llvm_to_continuation.py %s --output %t.three.json --entry multilatch_three_way_i8
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.three.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-multilatch-loop-memoryphi-fixed-point
; RUN: env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %s --output %t.four.json --entry multilatch_four_way_strided_i16
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.four.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-multilatch-loop-memoryphi-fixed-point
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.step.json --entry bad_multilatch_different_steps
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.step.json --expect-rejected "stack load lacks a dominating"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.carry-def.json --entry bad_multilatch_carry_definition
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.carry-def.json --expect-rejected "stack load lacks a dominating"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.nondet-guard.json --entry bad_multilatch_nondeterministic_guard
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.nondet-guard.json --expect-rejected "stack load lacks a dominating"
; RUN: %python %S/../benchmark/generate_multilatch_loop_memoryphi_fixture.py --object-bytes 64 --stride 8 --writer-bytes 8 --load-bytes 8 --output %t.boundary64.ll
; RUN: env SYMCC_LIVE_ALIAS_LIMIT=64 %python %S/../util/llvm_to_continuation.py %t.boundary64.ll --output %t.boundary64.json --entry generated_multilatch_loop_memoryphi_fixed_point
; RUN: %python %S/../util/check_live_continuation_lowering.py %t.boundary64.json --validate-only --expect-stack --expect-alias --expect-loop-memoryphi-byte-lane-induction --expect-strided-loop-memoryphi-byte-lane-induction --expect-multilatch-loop-memoryphi-fixed-point

define i8 @multilatch_writer_or_carry_i8(
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
  store i8 65, ptr %write_address, align 1
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

define i8 @multilatch_two_writers_i8(
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
  %choose_a = icmp ult i64 %iv, %choose64
  br i1 %choose_a, label %latch_a, label %latch_b

latch_a:
  %address_a = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %iv
  store i8 66, ptr %address_a, align 1
  %next_a = add nuw i64 %iv, 1
  br label %header

latch_b:
  %address_b = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %iv
  store i8 67, ptr %address_b, align 1
  %next_b = add nuw i64 %iv, 1
  br label %header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %index64
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}

define i8 @multilatch_three_way_i8(
    i8 %index, i8 %count, i8 %first_limit, i8 %second_limit) {
entry:
  %object = alloca [4 x i8], align 4
  %count64 = zext i8 %count to i64
  %first64 = zext i8 %first_limit to i64
  %second64 = zext i8 %second_limit to i64
  br label %header

header:
  %iv = phi i64 [ 0, %entry ], [ %next_a, %latch_a ], [ %next_b, %latch_b ], [ %next_c, %latch_c ]
  %continue = icmp ult i64 %iv, %count64
  br i1 %continue, label %decision_a, label %exit

decision_a:
  %choose_a = icmp ult i64 %iv, %first64
  br i1 %choose_a, label %latch_a, label %decision_b

decision_b:
  %choose_b = icmp ult i64 %iv, %second64
  br i1 %choose_b, label %latch_b, label %latch_c

latch_a:
  %address_a = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %iv
  store i8 68, ptr %address_a, align 1
  %next_a = add nuw i64 %iv, 1
  br label %header

latch_b:
  %address_b = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %iv
  store i8 69, ptr %address_b, align 1
  %next_b = add nuw i64 %iv, 1
  br label %header

latch_c:
  %next_c = add nuw i64 %iv, 1
  br label %header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %index64
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}

define i16 @multilatch_four_way_strided_i16(
    i8 %index, i8 %count, i8 %left_limit, i8 %leaf_limit) {
entry:
  %object = alloca [8 x i8], align 8
  %count64 = zext i8 %count to i64
  %left64 = zext i8 %left_limit to i64
  %leaf64 = zext i8 %leaf_limit to i64
  br label %header

header:
  %iv = phi i64 [ 0, %entry ], [ %next_a, %latch_a ], [ %next_b, %latch_b ], [ %next_c, %latch_c ], [ %next_d, %latch_d ]
  %continue = icmp ult i64 %iv, %count64
  br i1 %continue, label %root, label %exit

root:
  %go_left = icmp ult i64 %iv, %left64
  br i1 %go_left, label %left, label %right

left:
  %left_writer = icmp ult i64 %iv, %leaf64
  br i1 %left_writer, label %latch_a, label %latch_b

right:
  %right_writer = icmp uge i64 %iv, %leaf64
  br i1 %right_writer, label %latch_c, label %latch_d

latch_a:
  %address_a = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %iv
  store i16 17989, ptr %address_a, align 1
  %next_a = add nuw i64 %iv, 2
  br label %header

latch_b:
  %next_b = add nuw i64 %iv, 2
  br label %header

latch_c:
  %address_c = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %iv
  store i16 18503, ptr %address_c, align 1
  %next_c = add nuw i64 %iv, 2
  br label %header

latch_d:
  %next_d = add nuw i64 %iv, 2
  br label %header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [8 x i8], ptr %object, i64 0, i64 %index64
  %value = load i16, ptr %read_address, align 1
  ret i16 %value
}

define i8 @bad_multilatch_different_steps(
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
  %choose_a = icmp ult i64 %iv, %choose64
  br i1 %choose_a, label %latch_a, label %latch_b

latch_a:
  %address_a = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %iv
  store i8 70, ptr %address_a, align 1
  %next_a = add nuw i64 %iv, 1
  br label %header

latch_b:
  %next_b = add nuw i64 %iv, 2
  br label %header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %index64
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}

define i8 @bad_multilatch_carry_definition(
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
  %choose_a = icmp ult i64 %iv, %choose64
  br i1 %choose_a, label %latch_a, label %latch_b

latch_a:
  %address_a = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %iv
  store i8 71, ptr %address_a, align 1
  %next_a = add nuw i64 %iv, 1
  br label %header

latch_b:
  store i8 0, ptr %object, align 1
  %next_b = add nuw i64 %iv, 1
  br label %header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %index64
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}

define i8 @bad_multilatch_nondeterministic_guard(
    i8 %index, i8 %count) {
entry:
  %object = alloca [4 x i8], align 4
  %count64 = zext i8 %count to i64
  %choice = freeze i64 poison
  br label %header

header:
  %iv = phi i64 [ 0, %entry ], [ %next_a, %latch_a ], [ %next_b, %latch_b ]
  %continue = icmp ult i64 %iv, %count64
  br i1 %continue, label %decision, label %exit

decision:
  %choose_a = icmp ult i64 %iv, %choice
  br i1 %choose_a, label %latch_a, label %latch_b

latch_a:
  %address_a = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %iv
  store i8 72, ptr %address_a, align 1
  %next_a = add nuw i64 %iv, 1
  br label %header

latch_b:
  %next_b = add nuw i64 %iv, 1
  br label %header

exit:
  %index64 = zext i8 %index to i64
  %read_address = getelementptr inbounds [4 x i8], ptr %object, i64 0, i64 %index64
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}
