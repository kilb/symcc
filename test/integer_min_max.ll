; REQUIRES: qsym
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: %symcc -O0 %s -o %t 2> %t.compile.log
; RUN: not grep -E "unhandled LLVM intrinsic llvm\\.(smin|smax|umin|umax)" %t.compile.log
; RUN: %symcc -O0 -S -emit-llvm %s -o %t.instrumented.ll
; RUN: %filecheck %s --input-file=%t.instrumented.ll --check-prefix=INSTRUMENT
; RUN: echo -ne "\x00\x00" | env SYMCC_OUTPUT_DIR=%t-out %t
; RUN: %python -c "from pathlib import Path; assert bytes.fromhex('2efb') in [p.read_bytes() for p in Path(r'%t-out').iterdir() if p.is_file()]"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

@static_limit = internal constant i16 500, align 2

define i32 @main() {
entry:
  %input = alloca i16, align 2
  %bytes = bitcast i16* %input to i8*
  %read = call i64 @read(i32 0, i8* %bytes, i64 2)
  %value = load i16, i16* %input, align 2

  %signed_min = call i16 @llvm.smin.i16(i16 %value, i16 100)
  %signed_max = call i16 @llvm.smax.i16(i16 %value, i16 -2000)
  %unsigned_min = call i16 @llvm.umin.i16(i16 %value, i16 65000)
  %unsigned_max = call i16 @llvm.umax.i16(i16 %value, i16 42)
  %signed_min_ok = icmp eq i16 %signed_min, -1234
  %signed_max_ok = icmp eq i16 %signed_max, -1234
  %unsigned_min_ok = icmp eq i16 %unsigned_min, 64302
  %unsigned_max_ok = icmp eq i16 %unsigned_max, 64302
  %signed_ok = and i1 %signed_min_ok, %signed_max_ok
  %unsigned_ok = and i1 %unsigned_min_ok, %unsigned_max_ok
  %target = and i1 %signed_ok, %unsigned_ok
  br i1 %target, label %yes, label %no

yes:
  ret i32 0

no:
  ret i32 0
}

define i1 @static_data_origin(i16 %value) {
entry:
  %limit = load i16, i16* @static_limit, align 2
  %minimum = call i16 @llvm.umin.i16(i16 %value, i16 %limit)
  %matches = icmp eq i16 %minimum, 123
  ret i1 %matches
}

; INSTRUMENT-LABEL: define i32 @main()
; INSTRUMENT-DAG: call {{.*}} @_sym_build_signed_min(
; INSTRUMENT-DAG: call {{.*}} @_sym_build_signed_max(
; INSTRUMENT-DAG: call {{.*}} @_sym_build_unsigned_min(
; INSTRUMENT-DAG: call {{.*}} @_sym_build_unsigned_max(
; INSTRUMENT-LABEL: define i1 @static_data_origin(
; INSTRUMENT: call void @_sym_notify_data_cmp_ext(

declare i64 @read(i32, i8*, i64)
declare i16 @llvm.smin.i16(i16, i16)
declare i16 @llvm.smax.i16(i16, i16)
declare i16 @llvm.umin.i16(i16, i16)
declare i16 @llvm.umax.i16(i16, i16)
