; RUN: %opt -load-pass-plugin=%passlib -passes='default<O0>' -S %s -o %t.instrumented.ll
; RUN: %filecheck %s --input-file=%t.instrumented.ll

target datalayout = "e-m:e-p0:64:64:64:32-p1:64:64:64:64-p2:64:64:64:64-ni:1-i64:64-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

%large = type { [4294967296 x i8], i8 }

define i8* @narrow_negative_index(i8* %base, i8 %index) {
entry:
  %result = getelementptr i8, i8* %base, i8 %index
  ret i8* %result
}

; ANY-LABEL: define {{.*}} @narrow_negative_index(
; ANY: call {{.*}} @_sym_build_sext({{.*}}, i8 24)
; ANY: call {{.*}} @_sym_build_mul(
; ANY: call {{.*}} @_sym_build_trunc({{.*}}, i8 32)
; ANY: call {{.*}} @_sym_build_and(
; ANY: call {{.*}} @_sym_build_zext({{.*}}, i8 32)
; ANY: call {{.*}} @_sym_build_or(

define i8 addrspace(2)* @large_struct_member_offset(
    %large addrspace(2)* %base, i8 %object_index) {
entry:
  %result = getelementptr %large, %large addrspace(2)* %base,
      i8 %object_index, i32 1
  ret i8 addrspace(2)* %result
}

; ANY-LABEL: define {{.*}} @large_struct_member_offset(
; ANY: call {{.*}} @_sym_build_integer(i64 4294967296, i8 64)
; ANY: call {{.*}} @_sym_build_add(

define i8 addrspace(1)* @non_integral_pointer(
    i8 addrspace(1)* %base, i32 %index) {
entry:
  %result = getelementptr i8, i8 addrspace(1)* %base, i32 %index
  ret i8 addrspace(1)* %result
}

; ANY-LABEL: define {{.*}} @non_integral_pointer(
; ANY-NOT: call {{.*}} @_sym_build_
; ANY: ret ptr addrspace(1) %result

define i8* @wide_index(i8* %base, i128 %index) {
entry:
  %result = getelementptr i8, i8* %base, i128 %index
  ret i8* %result
}

; ANY-LABEL: define {{.*}} @wide_index(
; ANY: call {{.*}} @_sym_build_trunc({{.*}}, i8 32)
; ANY: call {{.*}} @_sym_build_mul(
; ANY: call {{.*}} @_sym_build_or(

define <vscale x 4 x i32>* @scalable_element_size(
    <vscale x 4 x i32>* %base, i32 %index) {
entry:
  %result = getelementptr <vscale x 4 x i32>,
      <vscale x 4 x i32>* %base, i32 %index
  ret <vscale x 4 x i32>* %result
}

; ANY-LABEL: define {{.*}} @scalable_element_size(
; ANY: call i32 @llvm.vscale.i32()
; ANY: call {{.*}} @_sym_build_mul(
; ANY: call {{.*}} @_sym_build_or(
