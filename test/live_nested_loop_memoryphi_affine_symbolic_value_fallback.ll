; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.wrap.json --entry nested_affine_symbolic_value_wrap_flag_fallback
; RUN: %python -c "import json; data=json.load(open(r'%t.wrap.json')); assert data['status'] == 'rejected'; assert any('lacks a dominating full-width initializing store' in item for item in data['diagnostics'])"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.sub.json --entry nested_affine_symbolic_value_sub_fallback
; RUN: %python -c "import json; data=json.load(open(r'%t.sub.json')); assert data['status'] == 'rejected'; assert any('lacks a dominating full-width initializing store' in item for item in data['diagnostics'])"
; RUN: not %python %S/../util/llvm_to_continuation.py %s --output %t.multi.json --entry nested_affine_symbolic_value_multiple_input_fallback
; RUN: %python -c "import json; data=json.load(open(r'%t.multi.json')); assert data['status'] == 'rejected'; assert any('lacks a dominating full-width initializing store' in item for item in data['diagnostics'])"

define i16 @nested_affine_symbolic_value_wrap_flag_fallback(
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
  %stored = add nuw i16 %payload, 1
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

define i16 @nested_affine_symbolic_value_sub_fallback(
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
  %stored = sub i16 %payload, %inner_iv
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

define i16 @nested_affine_symbolic_value_multiple_input_fallback(
    i4 %index, i2 %outer_count, i2 %inner_count, i16 %left, i16 %right) {
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
  %stored = add i16 %left, %right
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
