; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.signed.json --entry piecewise_signed_guard_fallback
; RUN: %python -c "import json; data=json.load(open(r'%t.signed.json')); assert data['status'] == 'rejected'; assert any('lacks a dominating full-width initializing store' in item for item in data['diagnostics'])"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.input.json --entry piecewise_input_guard_fallback
; RUN: %python -c "import json; data=json.load(open(r'%t.input.json')); assert data['status'] == 'rejected'; assert any('lacks a dominating full-width initializing store' in item for item in data['diagnostics'])"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.inputs.json --entry piecewise_distinct_inputs_fallback
; RUN: %python -c "import json; data=json.load(open(r'%t.inputs.json')); assert data['status'] == 'rejected'; assert any('lacks a dominating full-width initializing store' in item for item in data['diagnostics'])"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.identical.json --entry piecewise_identical_arms_fallback
; RUN: %python -c "import json; data=json.load(open(r'%t.identical.json')); assert data['status'] == 'rejected'; assert any('lacks a dominating full-width initializing store' in item for item in data['diagnostics'])"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.modulo.json --entry piecewise_modulo_identical_arms_fallback
; RUN: %python -c "import json; data=json.load(open(r'%t.modulo.json')); assert data['status'] == 'rejected'; assert any('lacks a dominating full-width initializing store' in item for item in data['diagnostics'])"

define i16 @piecewise_signed_guard_fallback(
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
  %address = getelementptr inbounds [12 x i8], ptr %object, i16 0, i16 %flat
  %true_value = add i16 %payload, %outer_iv
  %false_value = add i16 %payload, %inner_iv
  %guard = icmp slt i16 %inner_iv, 2
  %stored = select i1 %guard, i16 %true_value, i16 %false_value
  store i16 %stored, ptr %address, align 1
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

define i16 @piecewise_input_guard_fallback(
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
  %address = getelementptr inbounds [12 x i8], ptr %object, i16 0, i16 %flat
  %true_value = add i16 %payload, %outer_iv
  %false_value = add i16 %payload, %inner_iv
  %guard = icmp ult i16 %payload, 2
  %stored = select i1 %guard, i16 %true_value, i16 %false_value
  store i16 %stored, ptr %address, align 1
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

define i16 @piecewise_distinct_inputs_fallback(
    i4 %index, i2 %outer_count, i2 %inner_count,
    i16 %payload_a, i16 %payload_b) {
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
  %address = getelementptr inbounds [12 x i8], ptr %object, i16 0, i16 %flat
  %true_value = add i16 %payload_a, %outer_iv
  %false_value = add i16 %payload_b, %inner_iv
  %guard = icmp ult i16 %inner_iv, 2
  %stored = select i1 %guard, i16 %true_value, i16 %false_value
  store i16 %stored, ptr %address, align 1
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

define i16 @piecewise_identical_arms_fallback(
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
  %address = getelementptr inbounds [12 x i8], ptr %object, i16 0, i16 %flat
  %arm = add i16 %payload, %inner_iv
  %guard = icmp ult i16 %inner_iv, 2
  %stored = select i1 %guard, i16 %arm, i16 %arm
  store i16 %stored, ptr %address, align 1
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

define i8 @piecewise_modulo_identical_arms_fallback(
    i1 %index, i1 %outer_count, i1 %inner_count, i8 %payload) {
entry:
  %object = alloca [1 x i8], align 1
  %outer_bound = zext i1 %outer_count to i8
  %inner_bound = zext i1 %inner_count to i8
  br label %outer_header
outer_header:
  %outer_iv = phi i8 [ 0, %entry ], [ %outer_next, %outer_latch ]
  %outer_continue = icmp ult i8 %outer_iv, %outer_bound
  br i1 %outer_continue, label %inner_preheader, label %exit
inner_preheader:
  br label %inner_header
inner_header:
  %inner_iv = phi i8 [ 0, %inner_preheader ], [ %inner_next, %inner_body ]
  %inner_continue = icmp ult i8 %inner_iv, %inner_bound
  br i1 %inner_continue, label %inner_body, label %outer_latch
inner_body:
  %flat = add nuw i8 %outer_iv, %inner_iv
  %address = getelementptr inbounds [1 x i8], ptr %object, i8 0, i8 %flat
  %scaled_a = mul i8 %payload, 127
  %half_a = add i8 %scaled_a, %payload
  %scaled_b = mul i8 %payload, 127
  %half_b = add i8 %scaled_b, %payload
  %modulo_zero = add i8 %half_a, %half_b
  %guard = icmp ult i8 %inner_iv, 1
  %stored = select i1 %guard, i8 %modulo_zero, i8 0
  store i8 %stored, ptr %address, align 1
  %inner_next = add nuw i8 %inner_iv, 1
  br label %inner_header
outer_latch:
  %outer_next = add nuw i8 %outer_iv, 1
  br label %outer_header
exit:
  %index8 = zext i1 %index to i8
  %read_address = getelementptr inbounds [1 x i8], ptr %object, i8 0, i8 %index8
  %value = load i8, ptr %read_address, align 1
  ret i8 %value
}
