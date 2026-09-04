; REQUIRES: qsym
; RUN: %symcc -O0 %s -o %t
; RUN: rm -rf %t-out && mkdir %t-out
; RUN: rm -f %t.map %t.cache %t.json
; RUN: printf "\0" | env SYMCC_OUTPUT_DIR=%t-out SYMCC_AFL_COVERAGE_MAP=%t.map SYMCC_POLY_CACHE=%t.cache SYMCC_TELEMETRY_OUT=%t.json %t
; RUN: %python -c "from pathlib import Path; values=[p.read_bytes() for p in Path(r'%t-out').iterdir() if p.is_file()]; assert any(v == b'\xff' for v in values), values"

target datalayout = "e-m:e-p270:32:32-p271:32:32-p272:64:64-i64:64-f80:128-n8:16:32:64-S128"
target triple = "x86_64-pc-linux-gnu"

define i32 @main() {
entry:
  %input = alloca i8, align 1
  %read = call i64 @read(i32 0, i8* %input, i64 1)
  %value = load i8, i8* %input, align 1
  %sum = add i8 %value, 1
  %prefix = icmp ult i8 %sum, 2
  br i1 %prefix, label %inside, label %done

inside:
  %target = icmp eq i8 %value, -1
  br i1 %target, label %hit, label %done

hit:
  ret i32 7

done:
  ret i32 0
}

declare i64 @read(i32, i8*, i64)
